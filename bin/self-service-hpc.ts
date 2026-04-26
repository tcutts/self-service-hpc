#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { FoundationStack } from '../lib/foundation-stack';
import { ProjectInfrastructureStack } from '../lib/project-infrastructure-stack';

// ---------------------------------------------------------------------------
// CDK Aspect: enforce cost-allocation tags across ALL stacks in the app
// ---------------------------------------------------------------------------

/**
 * CDK Aspect that applies the `Project` and `ClusterName` cost-allocation tags
 * to every taggable resource in the app. Foundation-level resources receive
 * the placeholder value `hpc-platform`; per-project stacks override `Project`
 * with the actual project identifier via `cdk.Tags.of(stack).add(...)`.
 */
class GlobalCostAllocationTagAspect implements cdk.IAspect {
  public visit(node: Construct): void {
    if (cdk.TagManager.isTaggable(node)) {
      // Apply tags directly to the tag manager to avoid creating new
      // constructs inside the aspect visit (which causes infinite loops).
      node.tags.setTag('Project', 'hpc-platform', 50);
      node.tags.setTag('ClusterName', 'N/A', 50);
    }
  }
}

// ---------------------------------------------------------------------------
// App entry point
// ---------------------------------------------------------------------------

const app = new cdk.App();

// Shared environment — reads account/region from CDK context or falls back to
// the SDK default resolution (profile, env vars, instance metadata).
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

// -----------------------------------------------------------------------
// 1. Foundation Stack — shared control plane
// -----------------------------------------------------------------------
const foundation = new FoundationStack(app, 'HpcFoundationStack', {
  description: 'Self-Service HPC Platform — Foundation (Cognito, DynamoDB, API Gateway)',
  env,
});

// -----------------------------------------------------------------------
// Platform-wide configuration from CDK context
//
//   Set via cdk.json context, -c flag, or environment:
//     cdk deploy -c trustedCidrRanges="10.0.0.0/8,172.16.0.0/12"
//
//   Defaults to 10.0.0.0/8 if not specified.
// -----------------------------------------------------------------------
const trustedCidrRangesRaw: string = app.node.tryGetContext('trustedCidrRanges') ?? '10.0.0.0/8';
const trustedCidrRanges: string[] = trustedCidrRangesRaw
  .split(',')
  .map((s: string) => s.trim())
  .filter((s: string) => s.length > 0);

// -----------------------------------------------------------------------
// 2. Sample Project Infrastructure Stack (parameterised)
//
//    In production the platform Lambda creates project stacks dynamically.
//    This sample shows how to wire a project stack with cross-stack
//    references from the Foundation stack.
// -----------------------------------------------------------------------
const sampleProject = new ProjectInfrastructureStack(app, 'HpcProject-sample-project', {
  description: 'Self-Service HPC Platform — Project Infrastructure (sample-project)',
  env,
  projectId: 'sample-project',
  projectName: 'Sample Project',
  costAllocationTag: 'sample-project',
  trustedCidrRanges,
});

// Explicit dependency so the Foundation stack deploys first
sampleProject.addDependency(foundation);

// -----------------------------------------------------------------------
// 2b. Dynamic Project Infrastructure Stacks
//
//    When CodeBuild runs `npx cdk deploy HpcProject-<projectId>`, the
//    PROJECT_ID environment variable tells us which stack to synthesise.
//    This block creates the stack on-the-fly so CDK can find it.
// -----------------------------------------------------------------------
const dynamicProjectId = process.env.PROJECT_ID;
if (dynamicProjectId && dynamicProjectId !== 'sample-project') {
  const dynamicProject = new ProjectInfrastructureStack(
    app,
    `HpcProject-${dynamicProjectId}`,
    {
      description: `Self-Service HPC Platform — Project Infrastructure (${dynamicProjectId})`,
      env,
      projectId: dynamicProjectId,
      projectName: dynamicProjectId,
      trustedCidrRanges,
    },
  );
  dynamicProject.addDependency(foundation);
}

// -----------------------------------------------------------------------
// 3. Cross-stack references via SSM parameters
//
//    The Foundation stack already exports CfnOutputs (ApiUrl, UserPoolId,
//    table names, etc.). For runtime consumption by Lambda functions in
//    project stacks we also write key values to SSM Parameter Store so
//    they can be read without CloudFormation import/export coupling.
// -----------------------------------------------------------------------
new cdk.CfnOutput(foundation, 'SsmApiUrl', {
  value: foundation.api.url,
  description: 'API Gateway URL (also available via SSM /hpc-platform/api-url)',
  exportName: 'HpcPlatform-ApiUrl',
});

new cdk.CfnOutput(foundation, 'SsmUserPoolId', {
  value: foundation.userPool.userPoolId,
  description: 'Cognito User Pool ID (also available via SSM /hpc-platform/user-pool-id)',
  exportName: 'HpcPlatform-UserPoolId',
});

new cdk.CfnOutput(foundation, 'SsmPlatformUsersTableName', {
  value: foundation.platformUsersTable.tableName,
  description: 'PlatformUsers table name (also available via SSM)',
  exportName: 'HpcPlatform-PlatformUsersTableName',
});

new cdk.CfnOutput(foundation, 'SsmProjectsTableName', {
  value: foundation.projectsTable.tableName,
  description: 'Projects table name (also available via SSM)',
  exportName: 'HpcPlatform-ProjectsTableName',
});

new cdk.CfnOutput(foundation, 'SsmClustersTableName', {
  value: foundation.clustersTable.tableName,
  description: 'Clusters table name (also available via SSM)',
  exportName: 'HpcPlatform-ClustersTableName',
});

new cdk.CfnOutput(foundation, 'SsmClusterTemplatesTableName', {
  value: foundation.clusterTemplatesTable.tableName,
  description: 'ClusterTemplates table name (also available via SSM)',
  exportName: 'HpcPlatform-ClusterTemplatesTableName',
});

// -----------------------------------------------------------------------
// 4. Apply cost-allocation tags globally via CDK Aspects
// -----------------------------------------------------------------------
cdk.Aspects.of(app).add(new GlobalCostAllocationTagAspect());
