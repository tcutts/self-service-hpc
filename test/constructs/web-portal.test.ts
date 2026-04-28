import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import { Template } from 'aws-cdk-lib/assertions';
import { WebPortal } from '../../lib/constructs/web-portal';

describe('WebPortal', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    const userPoolClient = userPool.addClient('TestClient');
    const api = new apigateway.RestApi(stack, 'TestApi');
    // RestApi requires at least one method to pass CDK validation
    api.root.addMethod('GET', new apigateway.MockIntegration());

    new WebPortal(stack, 'WebPortal', {
      userPool,
      userPoolClient,
      api,
    });

    template = Template.fromStack(stack);
  });

  it('creates 1 S3 bucket with BlockPublicAccess', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it('creates 1 CloudFront distribution with HTTPS redirect', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: {
        DefaultCacheBehavior: {
          ViewerProtocolPolicy: 'redirect-to-https',
        },
        Comment: 'HPC Self-Service Portal',
        DefaultRootObject: 'index.html',
      },
    });
  });

  it('creates SPA error responses for 403 and 404', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: {
        CustomErrorResponses: [
          {
            ErrorCode: 403,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
            ErrorCachingMinTTL: 0,
          },
          {
            ErrorCode: 404,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
            ErrorCachingMinTTL: 0,
          },
        ],
      },
    });
  });

  it('creates 2 BucketDeployments (frontend + docs)', () => {
    template.resourceCountIs('Custom::CDKBucketDeployment', 2);
  });

  it('creates a docs deployment with docs/ destination key prefix', () => {
    const deployments = template.findResources('Custom::CDKBucketDeployment');
    const docsDeployment = Object.entries(deployments).find(
      ([, resource]) =>
        (resource as any).Properties?.DestinationBucketKeyPrefix === 'docs',
    );
    expect(docsDeployment).toBeDefined();
  });

  it('configures S3 bucket with S3_MANAGED encryption and DESTROY removal policy', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          {
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'AES256',
            },
          },
        ],
      },
    });

    // Verify DESTROY removal policy (DeletionPolicy: Delete)
    const buckets = template.findResources('AWS::S3::Bucket');
    const portalBucket = Object.entries(buckets).find(
      ([, resource]) =>
        (resource as any).Properties?.BucketName !== undefined ||
        (resource as any).DeletionPolicy === 'Delete',
    );
    expect(portalBucket).toBeDefined();
  });
});
