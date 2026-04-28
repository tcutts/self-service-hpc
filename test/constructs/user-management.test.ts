import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ApiGateway } from '../../lib/constructs/api-gateway';
import { UserManagement } from '../../lib/constructs/user-management';

describe('UserManagement', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    const platformUsersTable = new dynamodb.Table(stack, 'TestUsersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const apiGw = new ApiGateway(stack, 'ApiGateway', { userPool });

    new UserManagement(stack, 'UserManagement', {
      platformUsersTable,
      userPool,
      api: apiGw.api,
      cognitoAuthorizer: apiGw.cognitoAuthorizer,
      sharedLayer: apiGw.sharedLayer,
    });

    template = Template.fromStack(stack);
  });

  it('creates exactly 1 Lambda function', () => {
    template.resourceCountIs('AWS::Lambda::Function', 1);
  });

  it('configures the Lambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-user-management',
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
          USERS_TABLE_NAME: Match.anyValue(),
          USER_POOL_ID: Match.anyValue(),
        }),
      },
    });
  });

  it('grants DynamoDB read/write access via IAM policy', () => {
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

  it('grants Cognito admin actions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'cognito-idp:AdminCreateUser',
              'cognito-idp:AdminDeleteUser',
              'cognito-idp:AdminDisableUser',
              'cognito-idp:AdminEnableUser',
              'cognito-idp:AdminGetUser',
              'cognito-idp:AdminListGroupsForUser',
              'cognito-idp:AdminAddUserToGroup',
              'cognito-idp:AdminRemoveUserFromGroup',
              'cognito-idp:AdminUserGlobalSignOut',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('creates API Gateway method resources for /users routes', () => {
    // GET and POST on /users, GET and DELETE on /users/{userId},
    // POST on /users/{userId}/reactivate,
    // POST on /users/batch/deactivate, POST on /users/batch/reactivate
    // Plus OPTIONS methods for CORS on each resource
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

  it('creates the expected number of API Gateway resources for user routes', () => {
    // /users, /users/{userId}, /users/{userId}/reactivate,
    // /users/batch, /users/batch/deactivate, /users/batch/reactivate
    // Plus /health from ApiGateway construct = 7 total
    template.resourceCountIs('AWS::ApiGateway::Resource', 7);
  });
});
