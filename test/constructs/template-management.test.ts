import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ApiGateway } from '../../lib/constructs/api-gateway';
import { TemplateManagement } from '../../lib/constructs/template-management';

describe('TemplateManagement', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    const clusterTemplatesTable = new dynamodb.Table(stack, 'TestTemplatesTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const apiGw = new ApiGateway(stack, 'ApiGateway', { userPool });

    new TemplateManagement(stack, 'TemplateManagement', {
      clusterTemplatesTable,
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
      FunctionName: 'hpc-template-management',
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
          TEMPLATES_TABLE_NAME: Match.anyValue(),
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

  it('grants EC2 DescribeImages via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'ec2:DescribeImages',
            Effect: 'Allow',
            Resource: '*',
          }),
        ]),
      },
    });
  });

  it('creates API Gateway method resources for /templates routes', () => {
    // POST and GET on /templates, GET on /templates/default-ami,
    // GET, DELETE, PUT on /templates/{templateId},
    // POST on /templates/batch/delete
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

  it('creates the expected number of API Gateway resources for template routes', () => {
    // /templates, /templates/default-ami, /templates/{templateId},
    // /templates/batch, /templates/batch/delete
    // Plus /health from ApiGateway construct = 6 total
    template.resourceCountIs('AWS::ApiGateway::Resource', 6);
  });
});
