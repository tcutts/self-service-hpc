import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ApiGateway } from '../../lib/constructs/api-gateway';
import { PlatformOperations } from '../../lib/constructs/platform-operations';

describe('PlatformOperations', () => {
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
    const platformUsersTable = new dynamodb.Table(stack, 'TestUsersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const budgetNotificationTopic = new sns.Topic(stack, 'TestBudgetTopic');
    const clusterLifecycleNotificationTopic = new sns.Topic(stack, 'TestLifecycleTopic');
    const apiGw = new ApiGateway(stack, 'ApiGateway', { userPool });

    // Create a minimal state machine to serve as the cluster creation state machine dependency
    const clusterCreationStateMachine = new sfn.StateMachine(stack, 'TestStateMachine', {
      definitionBody: sfn.DefinitionBody.fromChainable(new sfn.Pass(stack, 'StartState')),
      stateMachineName: 'test-cluster-creation',
    });

    // Create a minimal Lambda to serve as the POSIX reconciliation Lambda dependency
    const posixReconciliationLambda = new lambda.Function(stack, 'TestReconciliationLambda', {
      functionName: 'hpc-posix-reconciliation',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'posix_reconciliation.handler',
      code: lambda.Code.fromInline('def handler(event, context): pass'),
    });

    new PlatformOperations(stack, 'PlatformOperations', {
      clustersTable,
      projectsTable,
      platformUsersTable,
      userPool,
      api: apiGw.api,
      cognitoAuthorizer: apiGw.cognitoAuthorizer,
      sharedLayer: apiGw.sharedLayer,
      budgetNotificationTopic,
      clusterLifecycleNotificationTopic,
      clusterCreationStateMachine,
      posixReconciliationLambda,
    });

    template = Template.fromStack(stack);
  });

  it('creates 5 Lambda functions (accounting, budget notification, fsx cleanup, failure handler, reconciliation)', () => {
    template.resourceCountIs('AWS::Lambda::Function', 5);
  });

  it('creates 3 EventBridge rules (fsx cleanup schedule, cluster creation failure, posix reconciliation schedule)', () => {
    template.resourceCountIs('AWS::Events::Rule', 3);
  });

  it('creates 1 SNS subscription for budget notification Lambda', () => {
    template.resourceCountIs('AWS::SNS::Subscription', 1);
  });

  it('configures the AccountingQueryLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-accounting-query',
      Runtime: 'python3.13',
      Handler: 'handler.handler',
      MemorySize: 256,
      Timeout: 60,
    });
  });

  it('configures the AccountingQueryLambda with correct environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-accounting-query',
      Environment: {
        Variables: Match.objectLike({
          CLUSTERS_TABLE_NAME: Match.anyValue(),
          PROJECTS_TABLE_NAME: Match.anyValue(),
          USER_POOL_ID: Match.anyValue(),
        }),
      },
    });
  });

  it('configures the BudgetNotificationLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-budget-notification',
      Runtime: 'python3.13',
      Handler: 'handler.handler',
      MemorySize: 256,
      Timeout: 30,
    });
  });

  it('configures the FsxCleanupLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-fsx-cleanup',
      Runtime: 'python3.13',
      Handler: 'handler.handler',
      MemorySize: 256,
      Timeout: 300,
    });
  });

  it('configures the ClusterCreationFailureHandler with correct runtime and handler', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-cluster-creation-failure-handler',
      Runtime: 'python3.13',
      Handler: 'cluster_creation.mark_cluster_failed_from_event',
      MemorySize: 256,
      Timeout: 30,
    });
  });

  it('grants SSM permissions for accounting query Lambda', () => {
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

  it('grants Cognito read permissions for accounting query Lambda', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'cognito-idp:AdminListGroupsForUser',
              'cognito-idp:AdminGetUser',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants FSx permissions for fsx cleanup Lambda', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'fsx:DescribeFileSystems',
              'fsx:DescribeDataRepositoryAssociations',
              'fsx:DeleteDataRepositoryAssociation',
              'fsx:DeleteFileSystem',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants states:DescribeExecution for cluster creation failure handler', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'states:DescribeExecution',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('creates the FSx cleanup schedule rule with 6-hour rate', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'hpc-fsx-cleanup-schedule',
      ScheduleExpression: 'rate(6 hours)',
    });
  });

  it('creates the POSIX reconciliation schedule rule with daily cron at 2 AM UTC', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'hpc-posix-reconciliation-schedule',
      ScheduleExpression: 'cron(0 2 * * ? *)',
    });
  });

  it('creates the cluster creation failure EventBridge rule with correct event pattern', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'hpc-cluster-creation-failure',
      EventPattern: Match.objectLike({
        source: ['aws.states'],
        'detail-type': ['Step Functions Execution Status Change'],
        detail: {
          status: ['TIMED_OUT', 'FAILED', 'ABORTED'],
        },
      }),
    });
  });

  it('creates API Gateway method resources for /accounting routes', () => {
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'GET',
      AuthorizationType: 'COGNITO_USER_POOLS',
    });
  });

  it('creates the expected number of API Gateway resources for accounting routes', () => {
    // /accounting, /accounting/jobs from PlatformOperations
    // /health from ApiGateway construct
    // Total = 3
    template.resourceCountIs('AWS::ApiGateway::Resource', 3);
  });
});
