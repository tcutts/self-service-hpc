import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { Construct } from 'constructs';

/** Retention periods for CloudWatch log groups. */
const INFRASTRUCTURE_LOG_RETENTION_DAYS = logs.RetentionDays.THREE_MONTHS; // 90 days
const USER_ACCESS_LOG_RETENTION_DAYS = logs.RetentionDays.ONE_YEAR; // 365 days

export interface ApiGatewayProps {
  userPool: cognito.UserPool;
}

/**
 * Encapsulates the API Gateway REST API, Cognito authorizer, shared Lambda
 * layer, and associated CloudWatch log groups for the HPC platform.
 */
export class ApiGateway extends Construct {
  public readonly api: apigateway.RestApi;
  public readonly cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  public readonly sharedLayer: lambda.LayerVersion;

  constructor(scope: Construct, id: string, props: ApiGatewayProps) {
    super(scope, id);

    // Access log group for API Gateway (user access → 365 days)
    const apiAccessLogGroup = new logs.LogGroup(this, 'ApiAccessLogGroup', {
      logGroupName: '/hpc-platform/api-gateway/access-logs',
      retention: USER_ACCESS_LOG_RETENTION_DAYS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.api = new apigateway.RestApi(this, 'HpcPlatformApi', {
      restApiName: 'hpc-platform-api',
      description: 'Self-Service HPC Platform API',
      deployOptions: {
        stageName: 'prod',
        accessLogDestination: new apigateway.LogGroupLogDestination(apiAccessLogGroup),
        accessLogFormat: apigateway.AccessLogFormat.jsonWithStandardFields({
          caller: true,
          httpMethod: true,
          ip: true,
          protocol: true,
          requestTime: true,
          resourcePath: true,
          responseLength: true,
          status: true,
          user: true,
        }),
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: false,
        metricsEnabled: true,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: [
          'Content-Type',
          'Authorization',
          'X-Amz-Date',
          'X-Api-Key',
          'X-Amz-Security-Token',
        ],
      },
    });

    // Cognito authoriser for the API — exposed as a public property so
    // downstream stacks/constructs can attach it to their methods.
    this.cognitoAuthorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'CognitoAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'hpc-cognito-authorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // Attach the authoriser to a placeholder health-check endpoint so CDK
    // can validate the authoriser is bound to the REST API during synthesis.
    const healthResource = this.api.root.addResource('health');
    healthResource.addMethod('GET', new apigateway.MockIntegration({
      integrationResponses: [{ statusCode: '200' }],
      requestTemplates: { 'application/json': '{"statusCode": 200}' },
    }), {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: this.cognitoAuthorizer,
      methodResponses: [{ statusCode: '200' }],
    });

    // Infrastructure log group for Lambda functions (90 days)
    new logs.LogGroup(this, 'LambdaInfraLogGroup', {
      logGroupName: '/hpc-platform/lambda/infrastructure',
      retention: INFRASTRUCTURE_LOG_RETENTION_DAYS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Shared Lambda Layer (api_logging and common utilities)
    // Lambda Layers for Python require code under a python/ prefix.
    // We point the asset at lambda/shared and use local bundling to
    // copy the files into the required python/ directory structure.
    this.sharedLayer = new lambda.LayerVersion(this, 'SharedUtilsLayer', {
      layerVersionName: 'hpc-shared-utils',
      description: 'Shared utilities (api_logging, authorization) for HPC platform Lambda functions',
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'shared'), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_13.bundlingImage,
          command: [
            'bash', '-c',
            'mkdir -p /asset-output/python && cp -r /asset-input/*.py /asset-output/python/',
          ],
          local: {
            tryBundle(outputDir: string): boolean {
              const fs = require('fs');
              const sharedDir = path.join(__dirname, '..', '..', 'lambda', 'shared');
              const pythonDir = path.join(outputDir, 'python');
              fs.mkdirSync(pythonDir, { recursive: true });
              for (const file of fs.readdirSync(sharedDir)) {
                if (file.endsWith('.py')) {
                  fs.copyFileSync(path.join(sharedDir, file), path.join(pythonDir, file));
                }
              }
              return true;
            },
          },
        },
      }),
    });
  }
}
