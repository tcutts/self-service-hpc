import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as events from 'aws-cdk-lib/aws-events';
import { Construct } from 'constructs';

import { CognitoAuth } from './constructs/cognito-auth';
import { DatabaseTables } from './constructs/database-tables';
import { ApiGateway } from './constructs/api-gateway';
import { NotificationTopics } from './constructs/notification-topics';
import { UserManagement } from './constructs/user-management';
import { ProjectManagement } from './constructs/project-management';
import { TemplateManagement } from './constructs/template-management';
import { ClusterOperations } from './constructs/cluster-operations';
import { CdkDeployProject } from './constructs/cdk-deploy-project';
import { ProjectLifecycle } from './constructs/project-lifecycle';
import { PlatformOperations } from './constructs/platform-operations';
import { WebPortal } from './constructs/web-portal';

/**
 * Platform Foundation stack — provisions the shared control-plane resources
 * used by every other service in the Self-Service HPC platform.
 *
 * This stack is a thin orchestrator that instantiates focused constructs,
 * wires cross-references between them, and emits CfnOutputs.
 */
export class FoundationStack extends cdk.Stack {
  /** Cognito User Pool for platform authentication. */
  public readonly userPool: cognito.UserPool;
  /** Cognito User Pool Client for the web portal. */
  public readonly userPoolClient: cognito.UserPoolClient;
  /** Cognito authoriser for the API Gateway. */
  public readonly cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  /** API Gateway REST API for the platform. */
  public readonly api: apigateway.RestApi;
  /** DynamoDB table: PlatformUsers. */
  public readonly platformUsersTable: dynamodb.Table;
  /** DynamoDB table: Projects. */
  public readonly projectsTable: dynamodb.Table;
  /** DynamoDB table: ClusterTemplates. */
  public readonly clusterTemplatesTable: dynamodb.Table;
  /** DynamoDB table: Clusters. */
  public readonly clustersTable: dynamodb.Table;
  /** DynamoDB table: ClusterNameRegistry. */
  public readonly clusterNameRegistryTable: dynamodb.Table;
  /** Lambda function: User Management. */
  public readonly userManagementLambda: lambda.Function;
  /** Lambda function: Project Management. */
  public readonly projectManagementLambda: lambda.Function;
  /** Lambda function: Template Management. */
  public readonly templateManagementLambda: lambda.Function;
  /** SNS topic for budget notifications. */
  public readonly budgetNotificationTopic: sns.Topic;
  /** SNS topic for cluster lifecycle notifications. */
  public readonly clusterLifecycleNotificationTopic: sns.Topic;
  /** Lambda function: Cluster Operations. */
  public readonly clusterOperationsLambda: lambda.Function;
  /** Step Functions state machine: Cluster Creation workflow. */
  public readonly clusterCreationStateMachine: sfn.StateMachine;
  /** Step Functions state machine: Cluster Destruction workflow. */
  public readonly clusterDestructionStateMachine: sfn.StateMachine;
  /** CodeBuild project for CDK deploy/destroy operations. */
  public readonly cdkDeployProject: codebuild.Project;
  /** Step Functions state machine: Project Deploy workflow. */
  public readonly projectDeployStateMachine: sfn.StateMachine;
  /** Step Functions state machine: Project Destroy workflow. */
  public readonly projectDestroyStateMachine: sfn.StateMachine;
  /** Step Functions state machine: Project Update workflow. */
  public readonly projectUpdateStateMachine: sfn.StateMachine;
  /** Lambda function: Accounting Query. */
  public readonly accountingQueryLambda: lambda.Function;
  /** Lambda function: Budget Notification handler. */
  public readonly budgetNotificationLambda: lambda.Function;
  /** Lambda function: FSx Cleanup. */
  public readonly fsxCleanupLambda: lambda.Function;
  /** EventBridge rule: FSx Cleanup schedule. */
  public readonly fsxCleanupScheduleRule: events.Rule;
  /** S3 bucket for static web portal assets. */
  public readonly webPortalBucket: s3.Bucket;
  /** CloudFront distribution for the web portal. */
  public readonly webPortalDistribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // =================================================================
    // 1. Instantiate constructs in dependency order
    // =================================================================

    // 1. CognitoAuth — no dependencies
    const cognitoAuth = new CognitoAuth(this, 'CognitoAuth');

    // 2. DatabaseTables — no dependencies
    const databaseTables = new DatabaseTables(this, 'DatabaseTables');

    // 3. ApiGateway — depends on CognitoAuth
    const apiGateway = new ApiGateway(this, 'ApiGateway', {
      userPool: cognitoAuth.userPool,
    });

    // 4. NotificationTopics — no dependencies
    const notificationTopics = new NotificationTopics(this, 'NotificationTopics');

    // 5. UserManagement — depends on DatabaseTables, CognitoAuth, ApiGateway
    const userManagement = new UserManagement(this, 'UserManagement', {
      platformUsersTable: databaseTables.platformUsersTable,
      userPool: cognitoAuth.userPool,
      api: apiGateway.api,
      cognitoAuthorizer: apiGateway.cognitoAuthorizer,
      sharedLayer: apiGateway.sharedLayer,
    });

    // 6. ProjectManagement — depends on DatabaseTables, CognitoAuth, ApiGateway, NotificationTopics
    const projectManagement = new ProjectManagement(this, 'ProjectManagement', {
      projectsTable: databaseTables.projectsTable,
      clustersTable: databaseTables.clustersTable,
      platformUsersTable: databaseTables.platformUsersTable,
      userPool: cognitoAuth.userPool,
      api: apiGateway.api,
      cognitoAuthorizer: apiGateway.cognitoAuthorizer,
      sharedLayer: apiGateway.sharedLayer,
      budgetNotificationTopic: notificationTopics.budgetNotificationTopic,
    });

    // 7. TemplateManagement — depends on DatabaseTables, ApiGateway
    const templateManagement = new TemplateManagement(this, 'TemplateManagement', {
      clusterTemplatesTable: databaseTables.clusterTemplatesTable,
      api: apiGateway.api,
      cognitoAuthorizer: apiGateway.cognitoAuthorizer,
      sharedLayer: apiGateway.sharedLayer,
    });

    // 8. ClusterOperations — depends on DatabaseTables, CognitoAuth, ApiGateway, NotificationTopics, ProjectManagement
    const clusterOperations = new ClusterOperations(this, 'ClusterOperations', {
      clustersTable: databaseTables.clustersTable,
      projectsTable: databaseTables.projectsTable,
      clusterNameRegistryTable: databaseTables.clusterNameRegistryTable,
      platformUsersTable: databaseTables.platformUsersTable,
      clusterTemplatesTable: databaseTables.clusterTemplatesTable,
      userPool: cognitoAuth.userPool,
      cognitoAuthorizer: apiGateway.cognitoAuthorizer,
      sharedLayer: apiGateway.sharedLayer,
      clusterLifecycleNotificationTopic: notificationTopics.clusterLifecycleNotificationTopic,
      projectIdResource: projectManagement.projectIdResource,
    });

    // 9. CdkDeployProject — no dependencies
    const cdkDeploy = new CdkDeployProject(this, 'CdkDeployProject');

    // 10. ProjectLifecycle — depends on DatabaseTables, CdkDeployProject
    const projectLifecycle = new ProjectLifecycle(this, 'ProjectLifecycle', {
      projectsTable: databaseTables.projectsTable,
      clustersTable: databaseTables.clustersTable,
      cdkDeployProject: cdkDeploy.project,
      sharedLayer: apiGateway.sharedLayer,
    });

    // 11. PlatformOperations — depends on DatabaseTables, CognitoAuth, ApiGateway, NotificationTopics, ClusterOperations
    const platformOperations = new PlatformOperations(this, 'PlatformOperations', {
      clustersTable: databaseTables.clustersTable,
      projectsTable: databaseTables.projectsTable,
      platformUsersTable: databaseTables.platformUsersTable,
      userPool: cognitoAuth.userPool,
      api: apiGateway.api,
      cognitoAuthorizer: apiGateway.cognitoAuthorizer,
      sharedLayer: apiGateway.sharedLayer,
      budgetNotificationTopic: notificationTopics.budgetNotificationTopic,
      clusterLifecycleNotificationTopic: notificationTopics.clusterLifecycleNotificationTopic,
      clusterCreationStateMachine: clusterOperations.clusterCreationStateMachine,
      posixReconciliationLambda: clusterOperations.posixReconciliationLambda,
    });

    // 12. WebPortal — depends on CognitoAuth, ApiGateway
    const webPortal = new WebPortal(this, 'WebPortal', {
      userPool: cognitoAuth.userPool,
      userPoolClient: cognitoAuth.userPoolClient,
      api: apiGateway.api,
    });

    // =================================================================
    // 2. Assign public properties from construct outputs
    // =================================================================
    this.userPool = cognitoAuth.userPool;
    this.userPoolClient = cognitoAuth.userPoolClient;
    this.cognitoAuthorizer = apiGateway.cognitoAuthorizer;
    this.api = apiGateway.api;
    this.platformUsersTable = databaseTables.platformUsersTable;
    this.projectsTable = databaseTables.projectsTable;
    this.clusterTemplatesTable = databaseTables.clusterTemplatesTable;
    this.clustersTable = databaseTables.clustersTable;
    this.clusterNameRegistryTable = databaseTables.clusterNameRegistryTable;
    this.userManagementLambda = userManagement.lambda;
    this.projectManagementLambda = projectManagement.lambda;
    this.templateManagementLambda = templateManagement.lambda;
    this.budgetNotificationTopic = notificationTopics.budgetNotificationTopic;
    this.clusterLifecycleNotificationTopic = notificationTopics.clusterLifecycleNotificationTopic;
    this.clusterOperationsLambda = clusterOperations.clusterOperationsLambda;
    this.clusterCreationStateMachine = clusterOperations.clusterCreationStateMachine;
    this.clusterDestructionStateMachine = clusterOperations.clusterDestructionStateMachine;
    this.cdkDeployProject = cdkDeploy.project;
    this.projectDeployStateMachine = projectLifecycle.projectDeployStateMachine;
    this.projectDestroyStateMachine = projectLifecycle.projectDestroyStateMachine;
    this.projectUpdateStateMachine = projectLifecycle.projectUpdateStateMachine;
    this.accountingQueryLambda = platformOperations.accountingQueryLambda;
    this.budgetNotificationLambda = platformOperations.budgetNotificationLambda;
    this.fsxCleanupLambda = platformOperations.fsxCleanupLambda;
    this.fsxCleanupScheduleRule = platformOperations.fsxCleanupScheduleRule;
    this.webPortalBucket = webPortal.bucket;
    this.webPortalDistribution = webPortal.distribution;

    // =================================================================
    // 3. Cross-reference wiring
    // =================================================================

    // --- Cluster state machine ARNs → Cluster Operations Lambda ---
    this.clusterOperationsLambda.addEnvironment(
      'CREATION_STATE_MACHINE_ARN',
      this.clusterCreationStateMachine.stateMachineArn,
    );
    this.clusterOperationsLambda.addEnvironment(
      'DESTRUCTION_STATE_MACHINE_ARN',
      this.clusterDestructionStateMachine.stateMachineArn,
    );
    this.clusterCreationStateMachine.grantStartExecution(this.clusterOperationsLambda);
    this.clusterDestructionStateMachine.grantStartExecution(this.clusterOperationsLambda);

    // --- Project lifecycle state machine ARNs → Project Management Lambda ---
    this.projectManagementLambda.addEnvironment(
      'PROJECT_DEPLOY_STATE_MACHINE_ARN',
      this.projectDeployStateMachine.stateMachineArn,
    );
    this.projectManagementLambda.addEnvironment(
      'PROJECT_DESTROY_STATE_MACHINE_ARN',
      this.projectDestroyStateMachine.stateMachineArn,
    );
    this.projectManagementLambda.addEnvironment(
      'PROJECT_UPDATE_STATE_MACHINE_ARN',
      this.projectUpdateStateMachine.stateMachineArn,
    );
    this.projectDeployStateMachine.grantStartExecution(this.projectManagementLambda);
    this.projectDestroyStateMachine.grantStartExecution(this.projectManagementLambda);
    this.projectUpdateStateMachine.grantStartExecution(this.projectManagementLambda);

    // --- Cost Explorer permissions for Project Management Lambda ---
    this.projectManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ce:GetCostAndUsage'],
      resources: ['*'],
    }));

    // =================================================================
    // 4. Foundation Stack Timestamp (staleness detection)
    // =================================================================
    // Writes a timestamp to the Projects table on every deploy/update.
    // The portal compares each project's statusChangedAt against this
    // timestamp to determine whether the project stack is up to date.
    new cr.AwsCustomResource(this, 'FoundationStackTimestamp', {
      onUpdate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.projectsTable.tableName,
          Item: {
            PK: { S: 'PLATFORM' },
            SK: { S: 'FOUNDATION_TIMESTAMP' },
            timestamp: { S: new Date().toISOString() },
          },
        },
        physicalResourceId: cr.PhysicalResourceId.of('FoundationStackTimestamp-' + Date.now()),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.projectsTable.tableArn],
      }),
    });

    // =================================================================
    // 5. Stack Outputs
    // =================================================================
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: 'Cognito User Pool ID',
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'Cognito User Pool Client ID',
    });

    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      description: 'API Gateway URL',
    });

    new cdk.CfnOutput(this, 'PlatformUsersTableName', {
      value: this.platformUsersTable.tableName,
      description: 'PlatformUsers DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ProjectsTableName', {
      value: this.projectsTable.tableName,
      description: 'Projects DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ClusterTemplatesTableName', {
      value: this.clusterTemplatesTable.tableName,
      description: 'ClusterTemplates DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ClustersTableName', {
      value: this.clustersTable.tableName,
      description: 'Clusters DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ClusterNameRegistryTableName', {
      value: this.clusterNameRegistryTable.tableName,
      description: 'ClusterNameRegistry DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'BudgetNotificationTopicArn', {
      value: this.budgetNotificationTopic.topicArn,
      description: 'SNS topic ARN for budget notifications',
    });

    new cdk.CfnOutput(this, 'ClusterLifecycleNotificationTopicArn', {
      value: this.clusterLifecycleNotificationTopic.topicArn,
      description: 'SNS topic ARN for cluster lifecycle notifications',
    });

    new cdk.CfnOutput(this, 'ClusterOperationsLambdaArn', {
      value: this.clusterOperationsLambda.functionArn,
      description: 'Cluster Operations Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'ClusterCreationStateMachineArn', {
      value: this.clusterCreationStateMachine.stateMachineArn,
      description: 'Cluster Creation Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'ClusterDestructionStateMachineArn', {
      value: this.clusterDestructionStateMachine.stateMachineArn,
      description: 'Cluster Destruction Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'CdkDeployProjectName', {
      value: this.cdkDeployProject.projectName,
      description: 'CodeBuild project name for CDK deploy/destroy',
    });

    new cdk.CfnOutput(this, 'ProjectDeployStateMachineArn', {
      value: this.projectDeployStateMachine.stateMachineArn,
      description: 'Project Deploy Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'ProjectDestroyStateMachineArn', {
      value: this.projectDestroyStateMachine.stateMachineArn,
      description: 'Project Destroy Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'ProjectUpdateStateMachineArn', {
      value: this.projectUpdateStateMachine.stateMachineArn,
      description: 'Project Update Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'AccountingQueryLambdaArn', {
      value: this.accountingQueryLambda.functionArn,
      description: 'Accounting Query Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'BudgetNotificationLambdaArn', {
      value: this.budgetNotificationLambda.functionArn,
      description: 'Budget Notification Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'WebPortalUrl', {
      value: `https://${this.webPortalDistribution.distributionDomainName}`,
      description: 'Web Portal CloudFront URL',
    });

    new cdk.CfnOutput(this, 'WebPortalBucketName', {
      value: this.webPortalBucket.bucketName,
      description: 'Web Portal S3 bucket name',
    });
  }
}
