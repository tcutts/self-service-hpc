import * as cdk from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as path from 'path';
import { Construct } from 'constructs';

/**
 * CdkDeployProject — CodeBuild project for CDK deploy/destroy operations.
 *
 * Packages the CDK project source as an S3 asset, creates a CodeBuild project
 * that runs `npm ci` followed by `$CDK_COMMAND`, and attaches IAM policies for
 * CloudFormation, EC2/VPC, EFS, S3, CloudWatch Logs, SSM, and STS AssumeRole.
 */
export class CdkDeployProject extends Construct {
  /** CodeBuild project for CDK deploy/destroy operations. */
  public readonly project: codebuild.Project;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // S3 asset containing the CDK project source code.  CodeBuild
    // pulls this as its source so that `npm ci` and `npx cdk deploy`
    // have access to package.json, package-lock.json, and the CDK app.
    const cdkSourceAsset = new s3assets.Asset(this, 'CdkSourceAsset', {
      path: path.join(__dirname, '..', '..'),
      exclude: [
        'cdk.out',
        '.cdk.staging',
        'node_modules',
        '.venv',
        'venv',
        '.git',
        '.hypothesis',
        '.pytest_cache',
        '__pycache__',
        '*.pyc',
      ],
    });

    this.project = new codebuild.Project(this, 'CdkDeployProject', {
      projectName: 'hpc-cdk-deploy',
      description: 'Runs CDK deploy/destroy for project infrastructure stacks',
      source: codebuild.Source.s3({
        bucket: cdkSourceAsset.bucket,
        path: cdkSourceAsset.s3ObjectKey,
      }),
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.SMALL,
        environmentVariables: {
          CDK_DEFAULT_ACCOUNT: {
            value: cdk.Aws.ACCOUNT_ID,
          },
          CDK_DEFAULT_REGION: {
            value: cdk.Aws.REGION,
          },
        },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          install: {
            'runtime-versions': {
              nodejs: '20',
            },
            commands: [
              'npm ci',
            ],
          },
          build: {
            commands: [
              // cdk.json may contain a "profile" key for local development.
              // In CodeBuild the service role credentials are used directly,
              // so we strip the profile to avoid "no credentials" errors.
              'node -e "const f=\'cdk.json\';const c=JSON.parse(require(\'fs\').readFileSync(f));delete c.profile;require(\'fs\').writeFileSync(f,JSON.stringify(c,null,2))"',
              '$CDK_COMMAND',
            ],
          },
        },
      }),
      timeout: cdk.Duration.minutes(60),
    });

    // Grant CodeBuild permissions to deploy/destroy CloudFormation stacks
    // and manage project infrastructure resources
    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudformation:CreateStack',
        'cloudformation:UpdateStack',
        'cloudformation:DeleteStack',
        'cloudformation:DescribeStacks',
        'cloudformation:DescribeStackEvents',
        'cloudformation:GetTemplate',
        'cloudformation:CreateChangeSet',
        'cloudformation:DescribeChangeSet',
        'cloudformation:ExecuteChangeSet',
        'cloudformation:DeleteChangeSet',
        'cloudformation:GetTemplateSummary',
      ],
      resources: [
        `arn:aws:cloudformation:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:stack/HpcProject-*/*`,
        `arn:aws:cloudformation:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:stack/CDKToolkit/*`,
      ],
    }));

    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:CreateVpc',
        'ec2:DeleteVpc',
        'ec2:DescribeVpcs',
        'ec2:ModifyVpcAttribute',
        'ec2:CreateSubnet',
        'ec2:DeleteSubnet',
        'ec2:DescribeSubnets',
        'ec2:CreateRouteTable',
        'ec2:DeleteRouteTable',
        'ec2:AssociateRouteTable',
        'ec2:DisassociateRouteTable',
        'ec2:CreateRoute',
        'ec2:DeleteRoute',
        'ec2:DescribeRouteTables',
        'ec2:CreateInternetGateway',
        'ec2:DeleteInternetGateway',
        'ec2:AttachInternetGateway',
        'ec2:DetachInternetGateway',
        'ec2:DescribeInternetGateways',
        'ec2:AllocateAddress',
        'ec2:ReleaseAddress',
        'ec2:DescribeAddresses',
        'ec2:CreateNatGateway',
        'ec2:DeleteNatGateway',
        'ec2:DescribeNatGateways',
        'ec2:CreateSecurityGroup',
        'ec2:DeleteSecurityGroup',
        'ec2:DescribeSecurityGroups',
        'ec2:AuthorizeSecurityGroupIngress',
        'ec2:RevokeSecurityGroupIngress',
        'ec2:AuthorizeSecurityGroupEgress',
        'ec2:RevokeSecurityGroupEgress',
        'ec2:CreateTags',
        'ec2:DeleteTags',
        'ec2:DescribeAvailabilityZones',
      ],
      resources: ['*'],
    }));

    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'elasticfilesystem:CreateFileSystem',
        'elasticfilesystem:DeleteFileSystem',
        'elasticfilesystem:DescribeFileSystems',
        'elasticfilesystem:CreateMountTarget',
        'elasticfilesystem:DeleteMountTarget',
        'elasticfilesystem:DescribeMountTargets',
        'elasticfilesystem:TagResource',
        'elasticfilesystem:UntagResource',
      ],
      resources: ['*'],
    }));

    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        's3:CreateBucket',
        's3:DeleteBucket',
        's3:PutBucketPolicy',
        's3:DeleteBucketPolicy',
        's3:GetBucketPolicy',
        's3:PutBucketVersioning',
        's3:PutBucketPublicAccessBlock',
        's3:PutEncryptionConfiguration',
        's3:PutBucketTagging',
      ],
      resources: ['*'],
    }));

    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'logs:CreateLogGroup',
        'logs:DeleteLogGroup',
        'logs:PutRetentionPolicy',
        'logs:TagResource',
        'logs:UntagResource',
        'logs:DescribeLogGroups',
      ],
      resources: ['*'],
    }));

    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ssm:GetParameter',
        'ssm:PutParameter',
      ],
      resources: [`arn:aws:ssm:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:parameter/cdk-bootstrap/*`],
    }));

    this.project.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: [`arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:role/cdk-*`],
    }));
  }
}
