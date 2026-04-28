import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { CdkDeployProject } from '../../lib/constructs/cdk-deploy-project';

describe('CdkDeployProject', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    new CdkDeployProject(stack, 'CdkDeployProject');
    template = Template.fromStack(stack);
  });

  it('creates exactly 1 CodeBuild project', () => {
    template.resourceCountIs('AWS::CodeBuild::Project', 1);
  });

  it('creates the CodeBuild project with correct name and description', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Name: 'hpc-cdk-deploy',
      Description: 'Runs CDK deploy/destroy for project infrastructure stacks',
    });
  });

  it('configures the CodeBuild project with STANDARD_7_0 image and SMALL compute', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Environment: {
        Image: 'aws/codebuild/standard:7.0',
        ComputeType: 'BUILD_GENERAL1_SMALL',
      },
    });
  });

  it('sets CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION environment variables', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Environment: {
        EnvironmentVariables: Match.arrayWith([
          Match.objectLike({ Name: 'CDK_DEFAULT_ACCOUNT' }),
          Match.objectLike({ Name: 'CDK_DEFAULT_REGION' }),
        ]),
      },
    });
  });

  it('uses S3 source type', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Source: {
        Type: 'S3',
      },
    });
  });

  it('sets a 60-minute timeout', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      TimeoutInMinutes: 60,
    });
  });

  it('grants CloudFormation permissions scoped to HpcProject and CDKToolkit stacks', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'cloudformation:CreateStack',
              'cloudformation:UpdateStack',
              'cloudformation:DeleteStack',
              'cloudformation:DescribeStacks',
            ]),
          }),
        ]),
      },
    });
  });

  it('grants EC2/VPC permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'ec2:CreateVpc',
              'ec2:DeleteVpc',
              'ec2:CreateSubnet',
              'ec2:DeleteSubnet',
              'ec2:CreateSecurityGroup',
              'ec2:DeleteSecurityGroup',
            ]),
          }),
        ]),
      },
    });
  });

  it('grants EFS permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'elasticfilesystem:CreateFileSystem',
              'elasticfilesystem:DeleteFileSystem',
              'elasticfilesystem:CreateMountTarget',
              'elasticfilesystem:DeleteMountTarget',
            ]),
          }),
        ]),
      },
    });
  });

  it('grants S3 bucket management permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              's3:CreateBucket',
              's3:DeleteBucket',
              's3:PutBucketPolicy',
            ]),
          }),
        ]),
      },
    });
  });

  it('grants CloudWatch Logs permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'logs:CreateLogGroup',
              'logs:DeleteLogGroup',
              'logs:PutRetentionPolicy',
            ]),
          }),
        ]),
      },
    });
  });

  it('grants SSM parameter permissions scoped to cdk-bootstrap', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'ssm:GetParameter',
              'ssm:PutParameter',
            ]),
          }),
        ]),
      },
    });
  });

  it('grants STS AssumeRole for CDK roles', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
          }),
        ]),
      },
    });
  });
});
