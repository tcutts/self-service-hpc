import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ApiGateway } from '../../lib/constructs/api-gateway';
import { ProjectManagement } from '../../lib/constructs/project-management';

describe('ProjectManagement', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    const projectsTable = new dynamodb.Table(stack, 'TestProjectsTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const clustersTable = new dynamodb.Table(stack, 'TestClustersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const platformUsersTable = new dynamodb.Table(stack, 'TestUsersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const budgetNotificationTopic = new sns.Topic(stack, 'TestBudgetTopic');
    const apiGw = new ApiGateway(stack, 'ApiGateway', { userPool });

    new ProjectManagement(stack, 'ProjectManagement', {
      projectsTable,
      clustersTable,
      platformUsersTable,
      userPool,
      api: apiGw.api,
      cognitoAuthorizer: apiGw.cognitoAuthorizer,
      sharedLayer: apiGw.sharedLayer,
      budgetNotificationTopic,
    });

    template = Template.fromStack(stack);
  });

  it('creates exactly 1 Lambda function', () => {
    template.resourceCountIs('AWS::Lambda::Function', 1);
  });

  it('configures the Lambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-management',
      Runtime: 'python3.13',
      Handler: 'handler.handler',
      MemorySize: 256,
      Timeout: 30,
    });
  });

  it('configures the Lambda with correct environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          PROJECTS_TABLE_NAME: Match.anyValue(),
          CLUSTERS_TABLE_NAME: Match.anyValue(),
          USERS_TABLE_NAME: Match.anyValue(),
          USER_POOL_ID: Match.anyValue(),
          BUDGET_SNS_TOPIC_ARN: Match.anyValue(),
        }),
      },
    });
  });

  it('grants DynamoDB read/write access on Projects table via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'dynamodb:BatchGetItem',
              'dynamodb:Query',
              'dynamodb:GetItem',
              'dynamodb:Scan',
              'dynamodb:BatchWriteItem',
              'dynamodb:PutItem',
              'dynamodb:UpdateItem',
              'dynamodb:DeleteItem',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants DynamoDB read access on Clusters and PlatformUsers tables via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'dynamodb:BatchGetItem',
              'dynamodb:Query',
              'dynamodb:GetItem',
              'dynamodb:Scan',
              'dynamodb:ConditionCheckItem',
              'dynamodb:DescribeTable',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants Cognito group management actions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'cognito-idp:AdminAddUserToGroup',
              'cognito-idp:AdminRemoveUserFromGroup',
              'cognito-idp:AdminListGroupsForUser',
              'cognito-idp:CreateGroup',
              'cognito-idp:DeleteGroup',
              'cognito-idp:GetGroup',
              'cognito-idp:ListUsersInGroup',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants AWS Budgets permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'budgets:CreateBudget',
              'budgets:ModifyBudget',
              'budgets:ViewBudget',
              'budgets:CreateNotification',
              'budgets:UpdateNotification',
              'budgets:DeleteNotification',
              'budgets:CreateSubscriber',
              'budgets:DeleteSubscriber',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants SNS publish permission for budget notification topic', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sns:Publish',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants STS GetCallerIdentity via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:GetCallerIdentity',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('creates API Gateway method resources for /projects routes', () => {
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
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'PUT',
      AuthorizationType: 'COGNITO_USER_POOLS',
    });
  });

  it('creates the expected number of API Gateway resources for project routes', () => {
    // /projects, /projects/{projectId}, /projects/{projectId}/members,
    // /projects/{projectId}/members/{userId}, /projects/{projectId}/budget,
    // /projects/{projectId}/deactivate, /projects/{projectId}/reactivate,
    // /projects/{projectId}/deploy, /projects/{projectId}/destroy,
    // /projects/{projectId}/update, /projects/batch, /projects/batch/update,
    // /projects/batch/deploy, /projects/batch/destroy
    // Plus /health from ApiGateway construct = 15 total
    template.resourceCountIs('AWS::ApiGateway::Resource', 15);
  });
});
