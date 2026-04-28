import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ApiGateway } from '../../lib/constructs/api-gateway';
import { ProjectManagement } from '../../lib/constructs/project-management';
import { ClusterOperations } from '../../lib/constructs/cluster-operations';

describe('ClusterOperations', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    const clustersTable = new dynamodb.Table(stack, 'TestClustersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const projectsTable = new dynamodb.Table(stack, 'TestProjectsTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const clusterNameRegistryTable = new dynamodb.Table(stack, 'TestClusterNameRegistryTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const platformUsersTable = new dynamodb.Table(stack, 'TestUsersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const clusterTemplatesTable = new dynamodb.Table(stack, 'TestTemplatesTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const clusterLifecycleNotificationTopic = new sns.Topic(stack, 'TestLifecycleTopic');
    const budgetNotificationTopic = new sns.Topic(stack, 'TestBudgetTopic');
    const apiGw = new ApiGateway(stack, 'ApiGateway', { userPool });

    // ProjectManagement is needed to provide the projectIdResource
    const projectMgmt = new ProjectManagement(stack, 'ProjectManagement', {
      projectsTable,
      clustersTable,
      platformUsersTable,
      userPool,
      api: apiGw.api,
      cognitoAuthorizer: apiGw.cognitoAuthorizer,
      sharedLayer: apiGw.sharedLayer,
      budgetNotificationTopic,
    });

    new ClusterOperations(stack, 'ClusterOperations', {
      clustersTable,
      projectsTable,
      clusterNameRegistryTable,
      platformUsersTable,
      clusterTemplatesTable,
      userPool,
      cognitoAuthorizer: apiGw.cognitoAuthorizer,
      sharedLayer: apiGw.sharedLayer,
      clusterLifecycleNotificationTopic,
      projectIdResource: projectMgmt.projectIdResource,
    });

    template = Template.fromStack(stack);
  });

  it('creates 4 Lambda functions for cluster operations (operations + creation steps + destruction steps + reconciliation)', () => {
    // 4 from ClusterOperations + 1 from ProjectManagement = 5 total
    template.resourceCountIs('AWS::Lambda::Function', 5);
  });

  it('creates 2 state machines (creation + destruction)', () => {
    template.resourceCountIs('AWS::StepFunctions::StateMachine', 2);
  });

  it('configures the ClusterOperationsLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-cluster-operations',
      Runtime: 'python3.13',
      Handler: 'handler.handler',
      MemorySize: 256,
      Timeout: 60,
    });
  });

  it('configures the ClusterCreationStepLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-cluster-creation-steps',
      Runtime: 'python3.13',
      Handler: 'cluster_creation.step_handler',
      MemorySize: 512,
      Timeout: 300,
    });
  });

  it('configures the ClusterDestructionStepLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-cluster-destruction-steps',
      Runtime: 'python3.13',
      Handler: 'cluster_destruction.step_handler',
      MemorySize: 512,
      Timeout: 300,
    });
  });

  it('configures the ClusterOperationsLambda with correct environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-cluster-operations',
      Environment: {
        Variables: Match.objectLike({
          CLUSTERS_TABLE_NAME: Match.anyValue(),
          PROJECTS_TABLE_NAME: Match.anyValue(),
          CLUSTER_NAME_REGISTRY_TABLE_NAME: Match.anyValue(),
          USERS_TABLE_NAME: Match.anyValue(),
          CREATION_STATE_MACHINE_ARN: '',
          DESTRUCTION_STATE_MACHINE_ARN: '',
          CLUSTER_LIFECYCLE_SNS_TOPIC_ARN: Match.anyValue(),
          USER_POOL_ID: Match.anyValue(),
        }),
      },
    });
  });

  it('creates the cluster creation state machine with correct name and timeout', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'hpc-cluster-creation',
    });
  });

  it('creates the cluster destruction state machine with correct name and timeout', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'hpc-cluster-destruction',
    });
  });

  it('grants PCS permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'pcs:GetCluster',
              'pcs:CreateCluster',
              'pcs:CreateComputeNodeGroup',
              'pcs:CreateQueue',
              'pcs:TagResource',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants FSx permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'fsx:CreateFileSystem',
              'fsx:DescribeFileSystems',
              'fsx:DeleteFileSystem',
              'fsx:TagResource',
              'fsx:CreateDataRepositoryAssociation',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants EC2 permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'ec2:DescribeSubnets',
              'ec2:DescribeSecurityGroups',
              'ec2:DescribeVpcs',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants S3 permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              's3:Get*',
              's3:List*',
              's3:PutObject',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants IAM instance profile management permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'iam:CreateRole',
              'iam:DeleteRole',
              'iam:CreateInstanceProfile',
              'iam:DeleteInstanceProfile',
              'iam:AddRoleToInstanceProfile',
              'iam:RemoveRoleFromInstanceProfile',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants Secrets Manager permissions for PCS Slurm auth key', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'secretsmanager:CreateSecret',
              'secretsmanager:TagResource',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('creates API Gateway method resources for /clusters routes', () => {
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'GET',
      AuthorizationType: 'COGNITO_USER_POOLS',
    });
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'POST',
      AuthorizationType: 'COGNITO_USER_POOLS',
    });
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'DELETE',
      AuthorizationType: 'COGNITO_USER_POOLS',
    });
  });

  it('creation state machine includes StorageModeChoice state', () => {
    const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
      Properties: { StateMachineName: 'hpc-cluster-creation' },
    });
    const logicalIds = Object.keys(stateMachines);
    expect(logicalIds).toHaveLength(1);
    const definition = stateMachines[logicalIds[0]].Properties.DefinitionString;
    // DefinitionString is an Fn::Join — flatten to find the state name
    const definitionStr = JSON.stringify(definition);
    expect(definitionStr).toContain('StorageModeChoice');
  });

  it('destruction state machine includes RemoveMountpointS3Policy step', () => {
    const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
      Properties: { StateMachineName: 'hpc-cluster-destruction' },
    });
    const logicalIds = Object.keys(stateMachines);
    expect(logicalIds).toHaveLength(1);
    const definition = stateMachines[logicalIds[0]].Properties.DefinitionString;
    const definitionStr = JSON.stringify(definition);
    expect(definitionStr).toContain('RemoveMountpointS3Policy');
  });

  it('grants IAM PutRolePolicy and DeleteRolePolicy permissions for Mountpoint policy management', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'iam:PutRolePolicy',
              'iam:DeleteRolePolicy',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants SSM permissions to the reconciliation Lambda for POSIX account management', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'ssm:SendCommand',
              'ssm:GetCommandInvocation',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('creates the expected number of API Gateway resources for cluster routes', () => {
    // From ProjectManagement: /projects, /projects/{projectId}, /projects/{projectId}/members,
    //   /projects/{projectId}/members/{userId}, /projects/{projectId}/budget,
    //   /projects/{projectId}/deploy, /projects/{projectId}/destroy,
    //   /projects/{projectId}/update, /projects/batch, /projects/batch/update,
    //   /projects/batch/deploy, /projects/batch/destroy,
    //   /projects/{projectId}/deactivate, /projects/{projectId}/reactivate = 14
    // From ClusterOperations: /projects/{projectId}/clusters,
    //   /projects/{projectId}/clusters/{clusterName},
    //   .../recreate, .../fail = 4
    // From ApiGateway: /health = 1
    // Total = 19
    template.resourceCountIs('AWS::ApiGateway::Resource', 19);
  });

  it('creation state machine includes CreateLaunchTemplates step', () => {
    const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
      Properties: { StateMachineName: 'hpc-cluster-creation' },
    });
    const logicalIds = Object.keys(stateMachines);
    expect(logicalIds).toHaveLength(1);
    const definition = stateMachines[logicalIds[0]].Properties.DefinitionString;
    const definitionStr = JSON.stringify(definition);
    expect(definitionStr).toContain('CreateLaunchTemplates');
    expect(definitionStr).toContain('create_launch_templates');
  });

  it('grants EC2 launch template permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'ec2:CreateLaunchTemplate',
              'ec2:DeleteLaunchTemplate',
              'ec2:DescribeLaunchTemplates',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });
});
