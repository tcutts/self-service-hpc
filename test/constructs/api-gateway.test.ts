import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ApiGateway } from '../../lib/constructs/api-gateway';

describe('ApiGateway', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    new ApiGateway(stack, 'ApiGateway', { userPool });
    template = Template.fromStack(stack);
  });

  it('creates exactly 1 RestApi', () => {
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
  });

  it('creates exactly 1 Authorizer', () => {
    template.resourceCountIs('AWS::ApiGateway::Authorizer', 1);
  });

  it('creates exactly 2 LogGroups', () => {
    template.resourceCountIs('AWS::Logs::LogGroup', 2);
  });

  it('creates exactly 1 LayerVersion', () => {
    template.resourceCountIs('AWS::Lambda::LayerVersion', 1);
  });

  it('configures the REST API with correct name', () => {
    template.hasResourceProperties('AWS::ApiGateway::RestApi', {
      Name: 'hpc-platform-api',
      Description: 'Self-Service HPC Platform API',
    });
  });

  it('configures the health endpoint with mock integration', () => {
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'GET',
      AuthorizationType: 'COGNITO_USER_POOLS',
      Integration: Match.objectLike({
        Type: 'MOCK',
      }),
    });
  });

  it('configures CORS with OPTIONS methods', () => {
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'OPTIONS',
    });
  });

  it('creates API access log group with 365-day retention', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/hpc-platform/api-gateway/access-logs',
      RetentionInDays: 365,
    });
  });

  it('creates infrastructure log group with 90-day retention', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/hpc-platform/lambda/infrastructure',
      RetentionInDays: 90,
    });
  });

  it('sets RETAIN removal policy on log groups', () => {
    const logGroups = template.findResources('AWS::Logs::LogGroup');
    for (const [, resource] of Object.entries(logGroups)) {
      expect((resource as any).DeletionPolicy).toBe('Retain');
      expect((resource as any).UpdateReplacePolicy).toBe('Retain');
    }
  });

  it('configures the Cognito authorizer with correct name', () => {
    template.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Name: 'hpc-cognito-authorizer',
      Type: 'COGNITO_USER_POOLS',
      IdentitySource: 'method.request.header.Authorization',
    });
  });

  it('configures the shared layer for Python 3.13', () => {
    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      LayerName: 'hpc-shared-utils',
      CompatibleRuntimes: ['python3.13'],
    });
  });
});
