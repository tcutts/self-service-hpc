import * as cdk from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as path from 'path';
import { ProjectLifecycle } from '../../lib/constructs/project-lifecycle';

describe('ProjectLifecycle', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const projectsTable = new dynamodb.Table(stack, 'TestProjectsTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });
    const clustersTable = new dynamodb.Table(stack, 'TestClustersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });

    // Create a minimal CodeBuild project as a dependency
    const cdkDeployProject = new codebuild.Project(stack, 'TestCodeBuildProject', {
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: { build: { commands: ['echo test'] } },
      }),
    });

    // Create a minimal shared layer as a dependency
    const sharedLayer = new lambda.LayerVersion(stack, 'TestSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
    });

    new ProjectLifecycle(stack, 'ProjectLifecycle', {
      projectsTable,
      clustersTable,
      cdkDeployProject,
      sharedLayer,
    });

    template = Template.fromStack(stack);
  });

  it('creates 3 Lambda functions for project lifecycle (deploy + destroy + update steps)', () => {
    template.resourceCountIs('AWS::Lambda::Function', 3);
  });

  it('creates 3 state machines (deploy + destroy + update)', () => {
    template.resourceCountIs('AWS::StepFunctions::StateMachine', 3);
  });

  it('configures the ProjectDeployStepLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-deploy-steps',
      Runtime: 'python3.13',
      Handler: 'project_deploy.step_handler',
      MemorySize: 512,
      Timeout: 300,
    });
  });

  it('configures the ProjectDestroyStepLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-destroy-steps',
      Runtime: 'python3.13',
      Handler: 'project_destroy.step_handler',
      MemorySize: 512,
      Timeout: 300,
    });
  });

  it('configures the ProjectUpdateStepLambda with correct runtime, handler, and memory', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-update-steps',
      Runtime: 'python3.13',
      Handler: 'project_update.step_handler',
      MemorySize: 512,
      Timeout: 300,
    });
  });

  it('creates the project deploy state machine with correct name', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'hpc-project-deploy',
    });
  });

  it('creates the project destroy state machine with correct name', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'hpc-project-destroy',
    });
  });

  it('creates the project update state machine with correct name', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'hpc-project-update',
    });
  });

  it('grants CodeBuild permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'codebuild:StartBuild',
              'codebuild:BatchGetBuilds',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants CloudFormation DescribeStacks permissions via IAM policy', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'cloudformation:DescribeStacks',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('grants DynamoDB read/write permissions via IAM policy', () => {
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
              'dynamodb:DescribeTable',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('configures the deploy step Lambda with correct environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-deploy-steps',
      Environment: {
        Variables: Match.objectLike({
          PROJECTS_TABLE_NAME: Match.anyValue(),
          CODEBUILD_PROJECT_NAME: Match.anyValue(),
        }),
      },
    });
  });

  it('configures the destroy step Lambda with correct environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-destroy-steps',
      Environment: {
        Variables: Match.objectLike({
          PROJECTS_TABLE_NAME: Match.anyValue(),
          CLUSTERS_TABLE_NAME: Match.anyValue(),
          CODEBUILD_PROJECT_NAME: Match.anyValue(),
        }),
      },
    });
  });

  it('configures the update step Lambda with correct environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'hpc-project-update-steps',
      Environment: {
        Variables: Match.objectLike({
          PROJECTS_TABLE_NAME: Match.anyValue(),
          CODEBUILD_PROJECT_NAME: Match.anyValue(),
        }),
      },
    });
  });
});
