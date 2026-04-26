import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { Construct } from 'constructs';
import { FoundationStack } from '../lib/foundation-stack';
import { ProjectInfrastructureStack } from '../lib/project-infrastructure-stack';

// ---------------------------------------------------------------------------
// Replicate the GlobalCostAllocationTagAspect from the entry point so we can
// test its behaviour without importing the bin/ file (which calls app.synth).
// ---------------------------------------------------------------------------
class GlobalCostAllocationTagAspect implements cdk.IAspect {
  public visit(node: Construct): void {
    if (cdk.TagManager.isTaggable(node)) {
      cdk.Tags.of(node).add('Project', 'hpc-platform', { priority: 50 });
      cdk.Tags.of(node).add('ClusterName', 'N/A', { priority: 50 });
    }
  }
}

describe('CDK App Entry Point', () => {
  let app: cdk.App;
  let foundation: FoundationStack;
  let projectStack: ProjectInfrastructureStack;
  let foundationTemplate: Template;
  let projectTemplate: Template;

  beforeAll(() => {
    app = new cdk.App();

    foundation = new FoundationStack(app, 'TestFoundationStack', {
      description: 'Self-Service HPC Platform — Foundation (Cognito, DynamoDB, API Gateway)',
    });

    projectStack = new ProjectInfrastructureStack(app, 'TestProjectStack', {
      description: 'Self-Service HPC Platform — Project Infrastructure (test-project)',
      projectId: 'test-project',
      projectName: 'Test Project',
      costAllocationTag: 'test-project',
      trustedCidrRanges: ['10.0.0.0/8'],
    });

    projectStack.addDependency(foundation);

    // Add cross-stack export outputs (mirrors the entry point)
    new cdk.CfnOutput(foundation, 'SsmApiUrl', {
      value: foundation.api.url,
      exportName: 'HpcPlatform-ApiUrl',
    });

    new cdk.CfnOutput(foundation, 'SsmUserPoolId', {
      value: foundation.userPool.userPoolId,
      exportName: 'HpcPlatform-UserPoolId',
    });

    new cdk.CfnOutput(foundation, 'SsmPlatformUsersTableName', {
      value: foundation.platformUsersTable.tableName,
      exportName: 'HpcPlatform-PlatformUsersTableName',
    });

    new cdk.CfnOutput(foundation, 'SsmProjectsTableName', {
      value: foundation.projectsTable.tableName,
      exportName: 'HpcPlatform-ProjectsTableName',
    });

    new cdk.CfnOutput(foundation, 'SsmClustersTableName', {
      value: foundation.clustersTable.tableName,
      exportName: 'HpcPlatform-ClustersTableName',
    });

    new cdk.CfnOutput(foundation, 'SsmClusterTemplatesTableName', {
      value: foundation.clusterTemplatesTable.tableName,
      exportName: 'HpcPlatform-ClusterTemplatesTableName',
    });

    // Apply the global cost-allocation tag aspect
    cdk.Aspects.of(app).add(new GlobalCostAllocationTagAspect());

    foundationTemplate = Template.fromStack(foundation);
    projectTemplate = Template.fromStack(projectStack);
  });

  // -------------------------------------------------------------------------
  // Foundation stack synthesises correctly
  // -------------------------------------------------------------------------
  describe('Foundation Stack', () => {
    it('synthesises without errors', () => {
      expect(foundationTemplate.toJSON()).toBeDefined();
    });

    it('has cross-stack export outputs', () => {
      const outputs = foundationTemplate.findOutputs('*');
      const outputKeys = Object.keys(outputs);

      // Verify key exports exist
      expect(outputKeys.some(k => outputs[k].Export?.Name === 'HpcPlatform-ApiUrl')).toBe(true);
      expect(outputKeys.some(k => outputs[k].Export?.Name === 'HpcPlatform-UserPoolId')).toBe(true);
      expect(outputKeys.some(k => outputs[k].Export?.Name === 'HpcPlatform-PlatformUsersTableName')).toBe(true);
      expect(outputKeys.some(k => outputs[k].Export?.Name === 'HpcPlatform-ProjectsTableName')).toBe(true);
      expect(outputKeys.some(k => outputs[k].Export?.Name === 'HpcPlatform-ClustersTableName')).toBe(true);
      expect(outputKeys.some(k => outputs[k].Export?.Name === 'HpcPlatform-ClusterTemplatesTableName')).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // Project Infrastructure stack synthesises correctly
  // -------------------------------------------------------------------------
  describe('Project Infrastructure Stack', () => {
    it('synthesises without errors', () => {
      expect(projectTemplate.toJSON()).toBeDefined();
    });

    it('creates a VPC for the project', () => {
      projectTemplate.resourceCountIs('AWS::EC2::VPC', 1);
    });

    it('creates an EFS filesystem', () => {
      projectTemplate.resourceCountIs('AWS::EFS::FileSystem', 1);
    });

    it('creates an S3 bucket for project storage', () => {
      projectTemplate.hasResourceProperties('AWS::S3::Bucket', {
        VersioningConfiguration: { Status: 'Enabled' },
      });
    });
  });

  // -------------------------------------------------------------------------
  // Global cost-allocation tags via CDK Aspects
  // Validates: Requirements 14.1, 14.4
  // -------------------------------------------------------------------------
  describe('Global Cost Allocation Tags', () => {
    it('applies Project tag to Foundation stack resources', () => {
      // DynamoDB tables should have the Project tag
      const tables = foundationTemplate.findResources('AWS::DynamoDB::Table');
      for (const [, resource] of Object.entries(tables)) {
        const tags = (resource as any).Properties?.Tags ?? [];
        const hasProjectTag = tags.some(
          (t: any) => t.Key === 'Project',
        );
        expect(hasProjectTag).toBe(true);
      }
    });

    it('applies ClusterName tag to Foundation stack resources', () => {
      const tables = foundationTemplate.findResources('AWS::DynamoDB::Table');
      for (const [, resource] of Object.entries(tables)) {
        const tags = (resource as any).Properties?.Tags ?? [];
        const hasClusterNameTag = tags.some(
          (t: any) => t.Key === 'ClusterName',
        );
        expect(hasClusterNameTag).toBe(true);
      }
    });

    it('applies Project tag to Project Infrastructure stack VPC', () => {
      projectTemplate.hasResourceProperties('AWS::EC2::VPC', {
        Tags: Match.arrayWith([
          Match.objectLike({ Key: 'Project', Value: 'test-project' }),
        ]),
      });
    });

    it('applies ClusterName tag to Project Infrastructure stack VPC', () => {
      projectTemplate.hasResourceProperties('AWS::EC2::VPC', {
        Tags: Match.arrayWith([
          Match.objectLike({ Key: 'ClusterName' }),
        ]),
      });
    });

    it('applies Project tag to Project Infrastructure stack security groups', () => {
      const sgs = projectTemplate.findResources('AWS::EC2::SecurityGroup');
      for (const [, resource] of Object.entries(sgs)) {
        const tags = (resource as any).Properties?.Tags ?? [];
        const hasProjectTag = tags.some(
          (t: any) => t.Key === 'Project' && t.Value === 'test-project',
        );
        expect(hasProjectTag).toBe(true);
      }
    });

    it('applies Project tag to Project Infrastructure stack EFS', () => {
      projectTemplate.hasResourceProperties('AWS::EFS::FileSystem', {
        FileSystemTags: Match.arrayWith([
          Match.objectLike({ Key: 'Project', Value: 'test-project' }),
        ]),
      });
    });
  });

  // -------------------------------------------------------------------------
  // Cross-stack dependency
  // -------------------------------------------------------------------------
  describe('Cross-stack Dependencies', () => {
    it('project stack depends on foundation stack', () => {
      const dependencies = projectStack.dependencies;
      expect(dependencies).toContain(foundation);
    });
  });
});
