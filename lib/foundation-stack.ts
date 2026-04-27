import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cloudfrontOrigins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as path from 'path';
import { Construct } from 'constructs';

/** Retention periods for CloudWatch log groups. */
const INFRASTRUCTURE_LOG_RETENTION_DAYS = logs.RetentionDays.THREE_MONTHS; // 90 days
const USER_ACCESS_LOG_RETENTION_DAYS = logs.RetentionDays.ONE_YEAR; // 365 days

/**
 * Platform Foundation stack — provisions the shared control-plane resources
 * used by every other service in the Self-Service HPC platform:
 *
 *  • Cognito User Pool + Client + Administrators group
 *  • DynamoDB tables (PlatformUsers, Projects, ClusterTemplates, Clusters, ClusterNameRegistry)
 *  • API Gateway REST API with Cognito authoriser
 *  • CloudWatch log groups with appropriate retention
 *  • Cost-allocation tag enforcement via CDK Aspects
 */
export class FoundationStack extends cdk.Stack {
  /** Cognito User Pool for platform authentication. */
  public readonly userPool: cognito.UserPool;
  /** Cognito User Pool Client for the web portal. */
  public readonly userPoolClient: cognito.UserPoolClient;
  /** Cognito authoriser for the API Gateway. */
  public readonly cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  /** API Gateway REST API for the platform. */
  public readonly api: apigateway.RestApi;
  /** DynamoDB table: PlatformUsers. */
  public readonly platformUsersTable: dynamodb.Table;
  /** DynamoDB table: Projects. */
  public readonly projectsTable: dynamodb.Table;
  /** DynamoDB table: ClusterTemplates. */
  public readonly clusterTemplatesTable: dynamodb.Table;
  /** DynamoDB table: Clusters. */
  public readonly clustersTable: dynamodb.Table;
  /** DynamoDB table: ClusterNameRegistry. */
  public readonly clusterNameRegistryTable: dynamodb.Table;
  /** Lambda function: User Management. */
  public readonly userManagementLambda: lambda.Function;
  /** Lambda function: Project Management. */
  public readonly projectManagementLambda: lambda.Function;
  /** Lambda function: Template Management. */
  public readonly templateManagementLambda: lambda.Function;
  /** SNS topic for budget notifications. */
  public readonly budgetNotificationTopic: sns.Topic;
  /** SNS topic for cluster lifecycle notifications. */
  public readonly clusterLifecycleNotificationTopic: sns.Topic;
  /** Lambda function: Cluster Operations. */
  public readonly clusterOperationsLambda: lambda.Function;
  /** Step Functions state machine: Cluster Creation workflow. */
  public readonly clusterCreationStateMachine: sfn.StateMachine;
  /** Step Functions state machine: Cluster Destruction workflow. */
  public readonly clusterDestructionStateMachine: sfn.StateMachine;
  /** CodeBuild project for CDK deploy/destroy operations. */
  public readonly cdkDeployProject: codebuild.Project;
  /** Step Functions state machine: Project Deploy workflow. */
  public readonly projectDeployStateMachine: sfn.StateMachine;
  /** Step Functions state machine: Project Destroy workflow. */
  public readonly projectDestroyStateMachine: sfn.StateMachine;
  /** Step Functions state machine: Project Update workflow. */
  public readonly projectUpdateStateMachine: sfn.StateMachine;
  /** Lambda function: Accounting Query. */
  public readonly accountingQueryLambda: lambda.Function;
  /** Lambda function: Budget Notification handler. */
  public readonly budgetNotificationLambda: lambda.Function;
  /** Lambda function: FSx Cleanup. */
  public readonly fsxCleanupLambda: lambda.Function;
  /** EventBridge rule: FSx Cleanup schedule. */
  public readonly fsxCleanupScheduleRule: events.Rule;
  /** S3 bucket for static web portal assets. */
  public readonly webPortalBucket: s3.Bucket;
  /** CloudFront distribution for the web portal. */
  public readonly webPortalDistribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ---------------------------------------------------------------
    // Cognito User Pool
    // ---------------------------------------------------------------
    this.userPool = new cognito.UserPool(this, 'HpcUserPool', {
      userPoolName: 'hpc-platform-users',
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
        givenName: { required: false, mutable: true },
        familyName: { required: false, mutable: true },
      },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Administrators group
    new cognito.CfnUserPoolGroup(this, 'AdministratorsGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'Administrators',
      description: 'Platform administrators with full management access',
    });

    // User Pool Client for the web portal
    this.userPoolClient = this.userPool.addClient('WebPortalClient', {
      userPoolClientName: 'hpc-web-portal',
      authFlows: {
        userPassword: true,
        userSrp: true,
      },
      preventUserExistenceErrors: true,
      idTokenValidity: cdk.Duration.hours(1),
      accessTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // ---------------------------------------------------------------
    // DynamoDB Tables
    // ---------------------------------------------------------------

    // PlatformUsers table
    this.platformUsersTable = new dynamodb.Table(this, 'PlatformUsersTable', {
      tableName: 'PlatformUsers',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: StatusIndex — status (PK), userId (SK)
    this.platformUsersTable.addGlobalSecondaryIndex({
      indexName: 'StatusIndex',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Seed the POSIX UID atomic counter item (PK=COUNTER, SK=POSIX_UID, currentValue=10000)
    new cr.AwsCustomResource(this, 'PosixUidCounterSeed', {
      onCreate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.platformUsersTable.tableName,
          Item: {
            PK: { S: 'COUNTER' },
            SK: { S: 'POSIX_UID' },
            currentValue: { N: '10000' },
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        },
        physicalResourceId: cr.PhysicalResourceId.of('PosixUidCounterSeed'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.platformUsersTable.tableArn],
      }),
    });

    // Projects table
    this.projectsTable = new dynamodb.Table(this, 'ProjectsTable', {
      tableName: 'Projects',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: UserProjectsIndex — userId (PK), projectId (SK)
    this.projectsTable.addGlobalSecondaryIndex({
      indexName: 'UserProjectsIndex',
      partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'projectId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ClusterTemplates table
    this.clusterTemplatesTable = new dynamodb.Table(this, 'ClusterTemplatesTable', {
      tableName: 'ClusterTemplates',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Seed default cluster template: cpu-general
    const seedTimestamp = new Date().toISOString();
    new cr.AwsCustomResource(this, 'DefaultTemplateCpuGeneralSeed', {
      onCreate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.clusterTemplatesTable.tableName,
          Item: {
            PK: { S: 'TEMPLATE#cpu-general' },
            SK: { S: 'METADATA' },
            templateId: { S: 'cpu-general' },
            templateName: { S: 'General CPU Workloads' },
            description: { S: 'Cost-effective CPU cluster template suitable for general HPC workloads. Uses Graviton-based c7g.medium instances.' },
            instanceTypes: { L: [{ S: 'c7g.medium' }] },
            loginInstanceType: { S: 'c7g.medium' },
            minNodes: { N: '1' },
            maxNodes: { N: '10' },
            amiId: { S: 'ami-placeholder-cpu' },
            softwareStack: { M: { scheduler: { S: 'slurm' }, schedulerVersion: { S: '24.11' } } },
            createdAt: { S: seedTimestamp },
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        },
        physicalResourceId: cr.PhysicalResourceId.of('DefaultTemplateCpuGeneralSeed'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.clusterTemplatesTable.tableArn],
      }),
    });

    // Seed default cluster template: gpu-basic
    new cr.AwsCustomResource(this, 'DefaultTemplateGpuBasicSeed', {
      onCreate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.clusterTemplatesTable.tableName,
          Item: {
            PK: { S: 'TEMPLATE#gpu-basic' },
            SK: { S: 'METADATA' },
            templateId: { S: 'gpu-basic' },
            templateName: { S: 'Basic GPU Workloads' },
            description: { S: 'Basic GPU cluster template suitable for introductory GPU workloads. Uses NVIDIA T4-based g4dn.xlarge instances.' },
            instanceTypes: { L: [{ S: 'g4dn.xlarge' }] },
            loginInstanceType: { S: 'g4dn.xlarge' },
            minNodes: { N: '1' },
            maxNodes: { N: '4' },
            amiId: { S: 'ami-placeholder-gpu' },
            softwareStack: { M: { scheduler: { S: 'slurm' }, schedulerVersion: { S: '24.11' }, cudaVersion: { S: '12.4' } } },
            createdAt: { S: seedTimestamp },
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        },
        physicalResourceId: cr.PhysicalResourceId.of('DefaultTemplateGpuBasicSeed'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.clusterTemplatesTable.tableArn],
      }),
    });

    // Clusters table
    this.clustersTable = new dynamodb.Table(this, 'ClustersTable', {
      tableName: 'Clusters',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ClusterNameRegistry table
    this.clusterNameRegistryTable = new dynamodb.Table(this, 'ClusterNameRegistryTable', {
      tableName: 'ClusterNameRegistry',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------
    // API Gateway REST API with Cognito Authoriser
    // ---------------------------------------------------------------

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
      cognitoUserPools: [this.userPool],
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

    // ---------------------------------------------------------------
    // Infrastructure log group for Lambda functions (90 days)
    // ---------------------------------------------------------------
    new logs.LogGroup(this, 'LambdaInfraLogGroup', {
      logGroupName: '/hpc-platform/lambda/infrastructure',
      retention: INFRASTRUCTURE_LOG_RETENTION_DAYS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------
    // Shared Lambda Layer (api_logging and common utilities)
    // ---------------------------------------------------------------
    // Lambda Layers for Python require code under a python/ prefix.
    // We point the asset at lambda/shared and use local bundling to
    // copy the files into the required python/ directory structure.
    const sharedLayer = new lambda.LayerVersion(this, 'SharedUtilsLayer', {
      layerVersionName: 'hpc-shared-utils',
      description: 'Shared utilities (api_logging) for HPC platform Lambda functions',
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'shared'), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_13.bundlingImage,
          command: [
            'bash', '-c',
            'mkdir -p /asset-output/python && cp -r /asset-input/*.py /asset-output/python/',
          ],
          local: {
            tryBundle(outputDir: string): boolean {
              const fs = require('fs');
              const sharedDir = path.join(__dirname, '..', 'lambda', 'shared');
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

    // ---------------------------------------------------------------
    // User Management Lambda Function
    // ---------------------------------------------------------------
    this.userManagementLambda = new lambda.Function(this, 'UserManagementLambda', {
      functionName: 'hpc-user-management',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'user_management')),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        USERS_TABLE_NAME: this.platformUsersTable.tableName,
        USER_POOL_ID: this.userPool.userPoolId,
      },
      description: 'Handles user CRUD operations including POSIX UID/GID assignment',
    });

    // Grant DynamoDB read/write access to PlatformUsers table
    this.platformUsersTable.grantReadWriteData(this.userManagementLambda);

    // Grant Cognito admin actions on the User Pool
    this.userManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminCreateUser',
        'cognito-idp:AdminDeleteUser',
        'cognito-idp:AdminDisableUser',
        'cognito-idp:AdminEnableUser',
        'cognito-idp:AdminGetUser',
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:AdminAddUserToGroup',
        'cognito-idp:AdminRemoveUserFromGroup',
        'cognito-idp:AdminUserGlobalSignOut',
      ],
      resources: [this.userPool.userPoolArn],
    }));

    // ---------------------------------------------------------------
    // API Gateway — User Management Resources
    // ---------------------------------------------------------------
    const usersResource = this.api.root.addResource('users');
    const userIdResource = usersResource.addResource('{userId}');

    const userManagementIntegration = new apigateway.LambdaIntegration(this.userManagementLambda);

    const cognitoMethodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: this.cognitoAuthorizer,
    };

    // GET /users — list users
    usersResource.addMethod('GET', userManagementIntegration, cognitoMethodOptions);
    // POST /users — create user
    usersResource.addMethod('POST', userManagementIntegration, cognitoMethodOptions);
    // GET /users/{userId} — get user details
    userIdResource.addMethod('GET', userManagementIntegration, cognitoMethodOptions);
    // DELETE /users/{userId} — deactivate user
    userIdResource.addMethod('DELETE', userManagementIntegration, cognitoMethodOptions);
    // POST /users/{userId}/reactivate — reactivate user
    const reactivateResource = userIdResource.addResource('reactivate');
    reactivateResource.addMethod('POST', userManagementIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // SNS Topic for Budget Notifications
    // ---------------------------------------------------------------
    this.budgetNotificationTopic = new sns.Topic(this, 'BudgetNotificationTopic', {
      topicName: 'hpc-budget-notifications',
      displayName: 'HPC Platform Budget Notifications',
    });

    // ---------------------------------------------------------------
    // SNS Topic for Cluster Lifecycle Notifications
    // ---------------------------------------------------------------
    this.clusterLifecycleNotificationTopic = new sns.Topic(this, 'ClusterLifecycleNotificationTopic', {
      topicName: 'hpc-cluster-lifecycle-notifications',
      displayName: 'HPC Cluster Lifecycle Notifications',
    });

    // ---------------------------------------------------------------
    // Project Management Lambda Function
    // ---------------------------------------------------------------
    this.projectManagementLambda = new lambda.Function(this, 'ProjectManagementLambda', {
      functionName: 'hpc-project-management',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'project_management')),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
        USERS_TABLE_NAME: this.platformUsersTable.tableName,
        USER_POOL_ID: this.userPool.userPoolId,
        BUDGET_SNS_TOPIC_ARN: this.budgetNotificationTopic.topicArn,
      },
      description: 'Handles project CRUD, membership management, and budget configuration',
    });

    // Grant DynamoDB read/write on Projects table
    this.projectsTable.grantReadWriteData(this.projectManagementLambda);
    // Grant DynamoDB read on Clusters table (for checking active clusters on deletion)
    this.clustersTable.grantReadData(this.projectManagementLambda);
    // Grant DynamoDB read on PlatformUsers table (for validating user existence on membership add)
    this.platformUsersTable.grantReadData(this.projectManagementLambda);

    // Grant Cognito admin actions for group management
    this.projectManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminAddUserToGroup',
        'cognito-idp:AdminRemoveUserFromGroup',
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:CreateGroup',
        'cognito-idp:DeleteGroup',
        'cognito-idp:GetGroup',
      ],
      resources: [this.userPool.userPoolArn],
    }));

    // Grant AWS Budgets permissions for creating/updating project budgets
    this.projectManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'budgets:CreateBudget',
        'budgets:ModifyBudget',
        'budgets:ViewBudget',
        'budgets:CreateNotification',
        'budgets:UpdateNotification',
        'budgets:DeleteNotification',
        'budgets:CreateSubscriber',
        'budgets:DeleteSubscriber',
      ],
      resources: ['*'],
    }));

    // Grant SNS publish for budget notification topic
    this.budgetNotificationTopic.grantPublish(this.projectManagementLambda);

    // Grant STS get caller identity (required by the Budgets API for account ID)
    this.projectManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sts:GetCallerIdentity'],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Project Management Resources
    // ---------------------------------------------------------------
    const projectsResource = this.api.root.addResource('projects');
    const projectIdResource = projectsResource.addResource('{projectId}');
    const membersResource = projectIdResource.addResource('members');
    const memberUserIdResource = membersResource.addResource('{userId}');
    const budgetResource = projectIdResource.addResource('budget');

    const projectManagementIntegration = new apigateway.LambdaIntegration(this.projectManagementLambda);

    // POST /projects — create project
    projectsResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // GET /projects — list projects
    projectsResource.addMethod('GET', projectManagementIntegration, cognitoMethodOptions);
    // GET /projects/{projectId} — get project details
    projectIdResource.addMethod('GET', projectManagementIntegration, cognitoMethodOptions);
    // DELETE /projects/{projectId} — delete project
    projectIdResource.addMethod('DELETE', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/members — add member
    membersResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // DELETE /projects/{projectId}/members/{userId} — remove member
    memberUserIdResource.addMethod('DELETE', projectManagementIntegration, cognitoMethodOptions);
    // PUT /projects/{projectId}/budget — set budget
    budgetResource.addMethod('PUT', projectManagementIntegration, cognitoMethodOptions);
    // PUT /projects/{projectId} — edit project (budget only)
    projectIdResource.addMethod('PUT', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/deploy — deploy project infrastructure
    const deployResource = projectIdResource.addResource('deploy');
    deployResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/destroy — destroy project infrastructure
    const destroyResource = projectIdResource.addResource('destroy');
    destroyResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/update — update project infrastructure
    const updateResource = projectIdResource.addResource('update');
    updateResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // Template Management Lambda Function
    // ---------------------------------------------------------------
    this.templateManagementLambda = new lambda.Function(this, 'TemplateManagementLambda', {
      functionName: 'hpc-template-management',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'template_management')),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        TEMPLATES_TABLE_NAME: this.clusterTemplatesTable.tableName,
      },
      description: 'Handles cluster template CRUD operations',
    });

    // Grant DynamoDB read/write access to ClusterTemplates table
    this.clusterTemplatesTable.grantReadWriteData(this.templateManagementLambda);

    // Grant EC2 DescribeImages for PCS sample AMI lookup
    this.templateManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeImages'],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Template Management Resources
    // ---------------------------------------------------------------
    const templatesResource = this.api.root.addResource('templates');
    const defaultAmiResource = templatesResource.addResource('default-ami');
    const templateIdResource = templatesResource.addResource('{templateId}');

    const templateManagementIntegration = new apigateway.LambdaIntegration(this.templateManagementLambda);

    // POST /templates — create template (admin only in handler)
    templatesResource.addMethod('POST', templateManagementIntegration, cognitoMethodOptions);
    // GET /templates — list templates (any authenticated user in handler)
    templatesResource.addMethod('GET', templateManagementIntegration, cognitoMethodOptions);
    // GET /templates/default-ami — look up latest PCS sample AMI (any authenticated user)
    defaultAmiResource.addMethod('GET', templateManagementIntegration, cognitoMethodOptions);
    // GET /templates/{templateId} — get template details (any authenticated user in handler)
    templateIdResource.addMethod('GET', templateManagementIntegration, cognitoMethodOptions);
    // DELETE /templates/{templateId} — delete template (admin only in handler)
    templateIdResource.addMethod('DELETE', templateManagementIntegration, cognitoMethodOptions);
    // PUT /templates/{templateId} — update template (admin only in handler)
    templateIdResource.addMethod('PUT', templateManagementIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // Cluster Operations Lambda Function
    // ---------------------------------------------------------------
    this.clusterOperationsLambda = new lambda.Function(this, 'ClusterOperationsLambda', {
      functionName: 'hpc-cluster-operations',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'cluster_operations')),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        CLUSTER_NAME_REGISTRY_TABLE_NAME: this.clusterNameRegistryTable.tableName,
        USERS_TABLE_NAME: this.platformUsersTable.tableName,
        CREATION_STATE_MACHINE_ARN: '', // set after state machine creation
        DESTRUCTION_STATE_MACHINE_ARN: '', // set after state machine creation
        CLUSTER_LIFECYCLE_SNS_TOPIC_ARN: this.clusterLifecycleNotificationTopic.topicArn,
        USER_POOL_ID: this.userPool.userPoolId,
      },
      description: 'Handles cluster CRUD operations and orchestrates creation/destruction via Step Functions',
    });

    // Grant DynamoDB read/write on Clusters, Projects, ClusterNameRegistry, and PlatformUsers tables
    this.clustersTable.grantReadWriteData(this.clusterOperationsLambda);
    this.projectsTable.grantReadData(this.clusterOperationsLambda);
    this.clusterNameRegistryTable.grantReadWriteData(this.clusterOperationsLambda);
    this.platformUsersTable.grantReadData(this.clusterOperationsLambda);

    // Grant SNS publish for cluster lifecycle notifications
    this.clusterLifecycleNotificationTopic.grantPublish(this.clusterOperationsLambda);

    // Grant Cognito read for authorisation checks
    this.clusterOperationsLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:AdminGetUser',
      ],
      resources: [this.userPool.userPoolArn],
    }));

    // ---------------------------------------------------------------
    // Step Functions — Cluster Creation State Machine
    // ---------------------------------------------------------------

    // Lambda function for cluster creation workflow steps (reuses the same code bundle)
    const clusterCreationStepLambda = new lambda.Function(this, 'ClusterCreationStepLambda', {
      functionName: 'hpc-cluster-creation-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'cluster_creation.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'cluster_operations')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        CLUSTER_NAME_REGISTRY_TABLE_NAME: this.clusterNameRegistryTable.tableName,
        USERS_TABLE_NAME: this.platformUsersTable.tableName,
        CLUSTER_LIFECYCLE_SNS_TOPIC_ARN: this.clusterLifecycleNotificationTopic.topicArn,
        TEMPLATES_TABLE_NAME: this.clusterTemplatesTable.tableName,
      },
      description: 'Executes individual steps of the cluster creation workflow',
    });

    // Grant creation step Lambda broad permissions for PCS, FSx, DynamoDB, SNS, tagging
    this.clustersTable.grantReadWriteData(clusterCreationStepLambda);
    this.projectsTable.grantReadData(clusterCreationStepLambda);
    this.clusterNameRegistryTable.grantReadWriteData(clusterCreationStepLambda);
    this.platformUsersTable.grantReadData(clusterCreationStepLambda);
    this.clusterTemplatesTable.grantReadData(clusterCreationStepLambda);
    this.clusterLifecycleNotificationTopic.grantPublish(clusterCreationStepLambda);

    // SNS subscribe permission for lifecycle notifications
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sns:Subscribe'],
      resources: [this.clusterLifecycleNotificationTopic.topicArn],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'pcs:CreateCluster',
        'pcs:CreateComputeNodeGroup',
        'pcs:CreateQueue',
        'pcs:DescribeCluster',
        'pcs:DescribeComputeNodeGroup',
        'pcs:DescribeQueue',
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:TagResource',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateFileSystem',
        'fsx:DescribeFileSystems',
        'fsx:DeleteFileSystem',
        'fsx:TagResource',
        'fsx:CreateDataRepositoryAssociation',
        'fsx:DescribeDataRepositoryAssociations',
      ],
      resources: ['*'],
    }));

    // S3 permissions required by FSx — the FSx CreateFileSystem API
    // validates that the calling principal has s3:Get*, s3:List*, and
    // s3:PutObject on the linked S3 bucket before proceeding.
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        's3:Get*',
        's3:List*',
        's3:PutObject',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:DescribeSubnets',
        'ec2:DescribeSecurityGroups',
        'ec2:GetSecurityGroupsForVpc',
        'ec2:DescribeVpcs',
        'ec2:CreateNetworkInterface',
        'ec2:DescribeNetworkInterfaces',
        'ec2:CreateTags',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'tag:TagResources',
        'tag:UntagResources',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'iam:PassRole',
        'iam:GetRole',
      ],
      resources: ['*'],
    }));

    // FSx for Lustre requires TWO service-linked roles:
    //  1. AWSServiceRoleForAmazonFSx (fsx.amazonaws.com) — general FSx operations
    //  2. AWSServiceRoleForFSxS3Access_fsx (s3.data-source.lustre.fsx.amazonaws.com)
    //     — required for data repository associations that link FSx to S3
    //
    // If either role does not already exist in the account, CreateFileSystem
    // will attempt to create it automatically — but only if the caller has
    // iam:CreateServiceLinkedRole (and iam:AttachRolePolicy / iam:PutRolePolicy
    // for the S3 data-source SLR).
    //
    // See: https://docs.aws.amazon.com/fsx/latest/LustreGuide/setting-up.html#fsx-adding-permissions-s3
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:CreateServiceLinkedRole'],
      resources: ['arn:aws:iam::*:role/aws-service-role/fsx.amazonaws.com/*'],
      conditions: {
        'StringLike': {
          'iam:AWSServiceName': 'fsx.amazonaws.com',
        },
      },
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'iam:CreateServiceLinkedRole',
        'iam:AttachRolePolicy',
        'iam:PutRolePolicy',
      ],
      resources: ['arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*'],
      conditions: {
        'StringLike': {
          'iam:AWSServiceName': 's3.data-source.lustre.fsx.amazonaws.com',
        },
      },
    }));

    // Lambda function for cluster destruction workflow steps
    const clusterDestructionStepLambda = new lambda.Function(this, 'ClusterDestructionStepLambda', {
      functionName: 'hpc-cluster-destruction-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'cluster_destruction.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'cluster_operations')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
      },
      description: 'Executes individual steps of the cluster destruction workflow',
    });

    // Grant destruction step Lambda permissions
    this.clustersTable.grantReadWriteData(clusterDestructionStepLambda);

    clusterDestructionStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:DescribeCluster',
      ],
      resources: ['*'],
    }));

    clusterDestructionStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateDataRepositoryTask',
        'fsx:DescribeDataRepositoryTasks',
        'fsx:DeleteFileSystem',
        'fsx:DescribeFileSystems',
      ],
      resources: ['*'],
    }));

    // --- Cluster Creation State Machine Definition ---

    // Step 1: Validate and register cluster name
    const validateAndRegisterName = new tasks.LambdaInvoke(this, 'ValidateAndRegisterName', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'validate_and_register_name',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Check budget breach
    const checkBudgetBreach = new tasks.LambdaInvoke(this, 'CheckBudgetBreach', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_budget_breach',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2b: Resolve template fields from ClusterTemplates table
    const resolveTemplate = new tasks.LambdaInvoke(this, 'ResolveTemplate', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'resolve_template',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 3: Create FSx filesystem
    const createFsxFilesystem = new tasks.LambdaInvoke(this, 'CreateFsxFilesystem', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_fsx_filesystem',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 4: Check FSx status (with wait loop)
    const checkFsxStatus = new tasks.LambdaInvoke(this, 'CheckFsxStatus', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_fsx_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForFsx = new sfn.Wait(this, 'WaitForFsx', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 4b: Create Data Repository Association (after FSx is available)
    const createFsxDra = new tasks.LambdaInvoke(this, 'CreateFsxDra', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_fsx_dra',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Create PCS cluster
    const createPcsCluster = new tasks.LambdaInvoke(this, 'CreatePcsCluster', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_pcs_cluster',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 6: Create login node group
    const createLoginNodeGroup = new tasks.LambdaInvoke(this, 'CreateLoginNodeGroup', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_login_node_group',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 7: Create compute node group
    const createComputeNodeGroup = new tasks.LambdaInvoke(this, 'CreateComputeNodeGroup', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_compute_node_group',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 8: Create PCS queue
    const createPcsQueue = new tasks.LambdaInvoke(this, 'CreatePcsQueue', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_pcs_queue',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 9: Tag resources
    const tagResources = new tasks.LambdaInvoke(this, 'TagResources', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'tag_resources',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 10: Record cluster in DynamoDB
    const recordCluster = new tasks.LambdaInvoke(this, 'RecordCluster', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_cluster',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Rollback handler on failure
    const handleCreationFailure = new tasks.LambdaInvoke(this, 'HandleCreationFailure', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_creation_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const creationFailed = new sfn.Fail(this, 'CreationFailed', {
      cause: 'Cluster creation failed',
      error: 'ClusterCreationError',
    });

    const creationSuccess = new sfn.Succeed(this, 'CreationSucceeded');

    // Add catch to all steps for rollback
    const catchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const failureChain = handleCreationFailure.next(creationFailed);

    // If the rollback handler itself fails, go directly to the Fail
    // state.  The initial CREATING record (written by the API handler)
    // will remain, but the Step Functions execution will be in FAILED
    // status so operators can investigate.
    handleCreationFailure.addCatch(creationFailed, { resultPath: '$.error' });

    validateAndRegisterName.addCatch(failureChain, catchConfig);
    checkBudgetBreach.addCatch(failureChain, catchConfig);
    resolveTemplate.addCatch(failureChain, catchConfig);
    // createFsxFilesystem, checkFsxStatus, createFsxDra, and createPcsCluster
    // are inside the ParallelFsxAndPcs state which has its own addCatch below.
    createLoginNodeGroup.addCatch(failureChain, catchConfig);
    createComputeNodeGroup.addCatch(failureChain, catchConfig);
    createPcsQueue.addCatch(failureChain, catchConfig);
    tagResources.addCatch(failureChain, catchConfig);
    recordCluster.addCatch(failureChain, catchConfig);

    // FSx wait loop: check status → if not available, wait → check again
    const fsxWaitLoop = waitForFsx.next(checkFsxStatus);
    const isFsxAvailable = new sfn.Choice(this, 'IsFsxAvailable')
      .when(sfn.Condition.booleanEquals('$.fsxAvailable', true), createFsxDra)
      .otherwise(fsxWaitLoop);

    // --- Parallel execution: FSx branch and PCS branch run concurrently ---

    // FSx branch: create filesystem → wait for available → create DRA
    const fsxBranch = createFsxFilesystem
      .next(checkFsxStatus)
      .next(isFsxAvailable);

    // PCS branch: create cluster (runs independently of FSx)
    const pcsBranch = createPcsCluster;

    // Parallel state runs both branches concurrently.
    // Each branch receives the full state as input.
    // Output is an array: [fsxBranchResult, pcsBranchResult].
    // Errors from either branch are caught at the Parallel level and
    // routed to the rollback handler.
    const parallelFsxAndPcs = new sfn.Parallel(this, 'ParallelFsxAndPcs', {
      comment: 'Create FSx filesystem and PCS cluster in parallel',
      resultSelector: {
        // Merge the two branch outputs into a single flat object.
        // Branch 0 (FSx) has fsxFilesystemId, fsxDnsName, fsxMountName, fsxDraId.
        // Branch 1 (PCS) has pcsClusterId, pcsClusterArn.
        // Both branches carry the original event fields (including
        // template-driven fields injected by ResolveTemplate).
        'projectId.$': '$[0].projectId',
        'clusterName.$': '$[0].clusterName',
        'templateId.$': '$[0].templateId',
        'createdBy.$': '$[0].createdBy',
        'vpcId.$': '$[0].vpcId',
        'efsFileSystemId.$': '$[0].efsFileSystemId',
        's3BucketName.$': '$[0].s3BucketName',
        'publicSubnetIds.$': '$[0].publicSubnetIds',
        'privateSubnetIds.$': '$[0].privateSubnetIds',
        'securityGroupIds.$': '$[0].securityGroupIds',
        'fsxFilesystemId.$': '$[0].fsxFilesystemId',
        'fsxDnsName.$': '$[0].fsxDnsName',
        'fsxMountName.$': '$[0].fsxMountName',
        'fsxDraId.$': '$[0].fsxDraId',
        'pcsClusterId.$': '$[1].pcsClusterId',
        'pcsClusterArn.$': '$[1].pcsClusterArn',
        // Template-driven fields — present on both branches via the
        // shared input from ResolveTemplate; read from branch 0.
        'loginInstanceType.$': '$[0].loginInstanceType',
        'instanceTypes.$': '$[0].instanceTypes',
        'maxNodes.$': '$[0].maxNodes',
        'minNodes.$': '$[0].minNodes',
        'purchaseOption.$': '$[0].purchaseOption',
      },
      resultPath: '$',
    });

    parallelFsxAndPcs.branch(fsxBranch, pcsBranch);
    parallelFsxAndPcs.addCatch(failureChain, catchConfig);

    // Chain: validate → budget → resolve template → parallel(FSx, PCS) → login nodes → compute → queue → tag → record → success
    const creationDefinition = validateAndRegisterName
      .next(checkBudgetBreach)
      .next(resolveTemplate)
      .next(parallelFsxAndPcs)
      .next(createLoginNodeGroup)
      .next(createComputeNodeGroup)
      .next(createPcsQueue)
      .next(tagResources)
      .next(recordCluster)
      .next(creationSuccess);

    this.clusterCreationStateMachine = new sfn.StateMachine(this, 'ClusterCreationStateMachine', {
      stateMachineName: 'hpc-cluster-creation',
      definitionBody: sfn.DefinitionBody.fromChainable(creationDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // --- Cluster Destruction State Machine Definition ---

    // Step 1: Create FSx export task
    const createFsxExportTask = new tasks.LambdaInvoke(this, 'CreateFsxExportTask', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_fsx_export_task',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Check FSx export status (with wait loop)
    const checkFsxExportStatus = new tasks.LambdaInvoke(this, 'CheckFsxExportStatus', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_fsx_export_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForExport = new sfn.Wait(this, 'WaitForExport', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(60)),
    });

    // Step 3: Delete PCS resources
    const deletePcsResources = new tasks.LambdaInvoke(this, 'DeletePcsResources', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_pcs_resources',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 4: Delete FSx filesystem
    const deleteFsxFilesystem = new tasks.LambdaInvoke(this, 'DeleteFsxFilesystem', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_fsx_filesystem',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Record cluster as destroyed
    const recordClusterDestroyed = new tasks.LambdaInvoke(this, 'RecordClusterDestroyed', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_cluster_destroyed',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const destructionSuccess = new sfn.Succeed(this, 'DestructionSucceeded');

    // Export wait loop: check status → if not complete, wait → check again
    const exportWaitLoop = waitForExport.next(checkFsxExportStatus);
    const isExportComplete = new sfn.Choice(this, 'IsExportComplete')
      .when(sfn.Condition.booleanEquals('$.exportComplete', true), deletePcsResources)
      .otherwise(exportWaitLoop);

    // Chain: steps 1-2 → export wait loop → steps 3-5 → success
    const destructionDefinition = createFsxExportTask
      .next(checkFsxExportStatus)
      .next(isExportComplete);

    // Post-export chain (connected via the Choice "when complete" branch)
    deletePcsResources
      .next(deleteFsxFilesystem)
      .next(recordClusterDestroyed)
      .next(destructionSuccess);

    this.clusterDestructionStateMachine = new sfn.StateMachine(this, 'ClusterDestructionStateMachine', {
      stateMachineName: 'hpc-cluster-destruction',
      definitionBody: sfn.DefinitionBody.fromChainable(destructionDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // --- Update Cluster Operations Lambda with state machine ARNs ---
    // Use addEnvironment to set the ARNs after state machine creation
    this.clusterOperationsLambda.addEnvironment(
      'CREATION_STATE_MACHINE_ARN',
      this.clusterCreationStateMachine.stateMachineArn,
    );
    this.clusterOperationsLambda.addEnvironment(
      'DESTRUCTION_STATE_MACHINE_ARN',
      this.clusterDestructionStateMachine.stateMachineArn,
    );

    // Scope down Step Functions permissions to the specific state machines
    this.clusterCreationStateMachine.grantStartExecution(this.clusterOperationsLambda);
    this.clusterDestructionStateMachine.grantStartExecution(this.clusterOperationsLambda);

    // Grant Step Functions execution roles permissions for PCS, FSx, EC2, tagging, DynamoDB
    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'pcs:CreateCluster',
        'pcs:CreateComputeNodeGroup',
        'pcs:CreateQueue',
        'pcs:DescribeCluster',
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:TagResource',
      ],
      resources: ['*'],
    }));

    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateFileSystem',
        'fsx:DescribeFileSystems',
        'fsx:DeleteFileSystem',
        'fsx:TagResource',
        'fsx:CreateDataRepositoryAssociation',
        'fsx:DescribeDataRepositoryAssociations',
      ],
      resources: ['*'],
    }));

    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'ec2:DescribeSubnets',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeVpcs',
        'ec2:CreateTags',
      ],
      resources: ['*'],
    }));

    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'tag:TagResources',
        'tag:UntagResources',
      ],
      resources: ['*'],
    }));

    this.clusterDestructionStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:DescribeCluster',
      ],
      resources: ['*'],
    }));

    this.clusterDestructionStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateDataRepositoryTask',
        'fsx:DescribeDataRepositoryTasks',
        'fsx:DeleteFileSystem',
        'fsx:DescribeFileSystems',
      ],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // CodeBuild Project for CDK Deploy/Destroy
    // ---------------------------------------------------------------

    // S3 asset containing the CDK project source code.  CodeBuild
    // pulls this as its source so that `npm ci` and `npx cdk deploy`
    // have access to package.json, package-lock.json, and the CDK app.
    const cdkSourceAsset = new s3assets.Asset(this, 'CdkSourceAsset', {
      path: path.join(__dirname, '..'),
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

    this.cdkDeployProject = new codebuild.Project(this, 'CdkDeployProject', {
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
    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
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

    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
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

    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
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

    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
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

    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
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

    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ssm:GetParameter',
        'ssm:PutParameter',
      ],
      resources: [`arn:aws:ssm:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:parameter/cdk-bootstrap/*`],
    }));

    this.cdkDeployProject.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: [`arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:role/cdk-*`],
    }));

    // ---------------------------------------------------------------
    // Step Functions — Project Deploy State Machine
    // ---------------------------------------------------------------

    // Lambda function for project deploy workflow steps
    const projectDeployStepLambda = new lambda.Function(this, 'ProjectDeployStepLambda', {
      functionName: 'hpc-project-deploy-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'project_deploy.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'project_management')),
      layers: [sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        CODEBUILD_PROJECT_NAME: this.cdkDeployProject.projectName,
      },
      description: 'Executes individual steps of the project deploy workflow',
    });

    // Grant deploy step Lambda permissions
    this.projectsTable.grantReadWriteData(projectDeployStepLambda);

    projectDeployStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'codebuild:StartBuild',
        'codebuild:BatchGetBuilds',
      ],
      resources: [this.cdkDeployProject.projectArn],
    }));

    projectDeployStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudformation:DescribeStacks',
      ],
      resources: [
        `arn:aws:cloudformation:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:stack/HpcProject-*/*`,
      ],
    }));

    // --- Project Deploy State Machine Definition ---

    // Step 1: Validate project state
    const validateProjectState = new tasks.LambdaInvoke(this, 'ValidateProjectState', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'validate_project_state',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Start CDK deploy
    const startCdkDeploy = new tasks.LambdaInvoke(this, 'StartCdkDeploy', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'start_cdk_deploy',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 3: Check deploy status (with wait loop)
    const checkDeployStatus = new tasks.LambdaInvoke(this, 'CheckDeployStatus', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_deploy_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForDeploy = new sfn.Wait(this, 'WaitForDeploy', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 4: Extract stack outputs
    const extractStackOutputs = new tasks.LambdaInvoke(this, 'ExtractStackOutputs', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'extract_stack_outputs',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Record infrastructure
    const recordInfrastructure = new tasks.LambdaInvoke(this, 'RecordInfrastructure', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_infrastructure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler
    const handleDeployFailure = new tasks.LambdaInvoke(this, 'HandleDeployFailure', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_deploy_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const deployFailed = new sfn.Fail(this, 'DeployFailed', {
      cause: 'Project deployment failed',
      error: 'ProjectDeployError',
    });

    const deploySuccess = new sfn.Succeed(this, 'DeploySucceeded');

    // Add catch to all deploy steps for failure handling
    const deployCatchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const deployFailureChain = handleDeployFailure.next(deployFailed);

    validateProjectState.addCatch(deployFailureChain, deployCatchConfig);
    startCdkDeploy.addCatch(deployFailureChain, deployCatchConfig);
    checkDeployStatus.addCatch(deployFailureChain, deployCatchConfig);
    extractStackOutputs.addCatch(deployFailureChain, deployCatchConfig);
    recordInfrastructure.addCatch(deployFailureChain, deployCatchConfig);

    // Deploy wait loop: check status → if not complete, wait → check again
    const deployWaitLoop = waitForDeploy.next(checkDeployStatus);
    const isDeployComplete = new sfn.Choice(this, 'IsDeployComplete')
      .when(sfn.Condition.booleanEquals('$.deployComplete', true), extractStackOutputs)
      .otherwise(deployWaitLoop);

    // Chain: Validate → Start CDK Deploy → Check Status → wait loop → Extract Outputs → Record Infrastructure → Success
    const deployDefinition = validateProjectState
      .next(startCdkDeploy)
      .next(checkDeployStatus)
      .next(isDeployComplete);

    // Post-deploy chain (connected via the Choice "when complete" branch)
    extractStackOutputs
      .next(recordInfrastructure)
      .next(deploySuccess);

    this.projectDeployStateMachine = new sfn.StateMachine(this, 'ProjectDeployStateMachine', {
      stateMachineName: 'hpc-project-deploy',
      definitionBody: sfn.DefinitionBody.fromChainable(deployDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // ---------------------------------------------------------------
    // Step Functions — Project Destroy State Machine
    // ---------------------------------------------------------------

    // Lambda function for project destroy workflow steps
    const projectDestroyStepLambda = new lambda.Function(this, 'ProjectDestroyStepLambda', {
      functionName: 'hpc-project-destroy-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'project_destroy.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'project_management')),
      layers: [sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
        CODEBUILD_PROJECT_NAME: this.cdkDeployProject.projectName,
      },
      description: 'Executes individual steps of the project destroy workflow',
    });

    // Grant destroy step Lambda permissions
    this.projectsTable.grantReadWriteData(projectDestroyStepLambda);
    this.clustersTable.grantReadData(projectDestroyStepLambda);

    projectDestroyStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'codebuild:StartBuild',
        'codebuild:BatchGetBuilds',
      ],
      resources: [this.cdkDeployProject.projectArn],
    }));

    // --- Project Destroy State Machine Definition ---

    // Step 1: Validate project state and check clusters
    const validateAndCheckClusters = new tasks.LambdaInvoke(this, 'ValidateAndCheckClusters', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'validate_and_check_clusters',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Start CDK destroy
    const startCdkDestroy = new tasks.LambdaInvoke(this, 'StartCdkDestroy', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'start_cdk_destroy',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 3: Check destroy status (with wait loop)
    const checkDestroyStatus = new tasks.LambdaInvoke(this, 'CheckDestroyStatus', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_destroy_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForDestroy = new sfn.Wait(this, 'WaitForDestroy', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 4: Clear infrastructure
    const clearInfrastructure = new tasks.LambdaInvoke(this, 'ClearInfrastructure', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'clear_infrastructure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Archive project
    const archiveProject = new tasks.LambdaInvoke(this, 'ArchiveProject', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'archive_project',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler
    const handleDestroyFailure = new tasks.LambdaInvoke(this, 'HandleDestroyFailure', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_destroy_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const projectDestroyFailed = new sfn.Fail(this, 'ProjectDestroyFailed', {
      cause: 'Project destruction failed',
      error: 'ProjectDestroyError',
    });

    const projectDestroySuccess = new sfn.Succeed(this, 'ProjectDestroySucceeded');

    // Add catch to all destroy steps for failure handling
    const destroyCatchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const destroyFailureChain = handleDestroyFailure.next(projectDestroyFailed);

    validateAndCheckClusters.addCatch(destroyFailureChain, destroyCatchConfig);
    startCdkDestroy.addCatch(destroyFailureChain, destroyCatchConfig);
    checkDestroyStatus.addCatch(destroyFailureChain, destroyCatchConfig);
    clearInfrastructure.addCatch(destroyFailureChain, destroyCatchConfig);
    archiveProject.addCatch(destroyFailureChain, destroyCatchConfig);

    // Destroy wait loop: check status → if not complete, wait → check again
    const destroyWaitLoop = waitForDestroy.next(checkDestroyStatus);
    const isDestroyComplete = new sfn.Choice(this, 'IsDestroyComplete')
      .when(sfn.Condition.booleanEquals('$.destroyComplete', true), clearInfrastructure)
      .otherwise(destroyWaitLoop);

    // Chain: Validate & Check Clusters → Start CDK Destroy → Check Status → wait loop → Clear Infrastructure → Archive → Success
    const destroyDefinition = validateAndCheckClusters
      .next(startCdkDestroy)
      .next(checkDestroyStatus)
      .next(isDestroyComplete);

    // Post-destroy chain (connected via the Choice "when complete" branch)
    clearInfrastructure
      .next(archiveProject)
      .next(projectDestroySuccess);

    this.projectDestroyStateMachine = new sfn.StateMachine(this, 'ProjectDestroyStateMachine', {
      stateMachineName: 'hpc-project-destroy',
      definitionBody: sfn.DefinitionBody.fromChainable(destroyDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // --- Update Project Management Lambda with state machine ARNs ---
    this.projectManagementLambda.addEnvironment(
      'PROJECT_DEPLOY_STATE_MACHINE_ARN',
      this.projectDeployStateMachine.stateMachineArn,
    );
    this.projectManagementLambda.addEnvironment(
      'PROJECT_DESTROY_STATE_MACHINE_ARN',
      this.projectDestroyStateMachine.stateMachineArn,
    );

    // Grant project management Lambda permission to start executions on deploy/destroy state machines
    this.projectDeployStateMachine.grantStartExecution(this.projectManagementLambda);
    this.projectDestroyStateMachine.grantStartExecution(this.projectManagementLambda);

    // ---------------------------------------------------------------
    // Step Functions — Project Update State Machine
    // ---------------------------------------------------------------

    // Lambda function for project update workflow steps
    const projectUpdateStepLambda = new lambda.Function(this, 'ProjectUpdateStepLambda', {
      functionName: 'hpc-project-update-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'project_update.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'project_management')),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      environment: {
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        CODEBUILD_PROJECT_NAME: this.cdkDeployProject.projectName,
      },
      description: 'Executes individual steps of the project update workflow',
    });

    // Grant update step Lambda permissions
    this.projectsTable.grantReadWriteData(projectUpdateStepLambda);

    projectUpdateStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'codebuild:StartBuild',
        'codebuild:BatchGetBuilds',
      ],
      resources: [this.cdkDeployProject.projectArn],
    }));

    projectUpdateStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudformation:DescribeStacks',
      ],
      resources: [
        `arn:aws:cloudformation:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:stack/HpcProject-*/*`,
      ],
    }));

    // --- Project Update State Machine Definition ---

    // Step 1: Validate update state
    const validateUpdateState = new tasks.LambdaInvoke(this, 'ValidateUpdateState', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'validate_update_state',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Start CDK update
    const startCdkUpdate = new tasks.LambdaInvoke(this, 'StartCdkUpdate', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'start_cdk_update',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 3: Check update status (with wait loop)
    const checkUpdateStatus = new tasks.LambdaInvoke(this, 'CheckUpdateStatus', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_update_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForUpdate = new sfn.Wait(this, 'WaitForUpdate', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 4: Extract stack outputs
    const extractUpdateStackOutputs = new tasks.LambdaInvoke(this, 'ExtractUpdateStackOutputs', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'extract_stack_outputs',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Record updated infrastructure
    const recordUpdatedInfrastructure = new tasks.LambdaInvoke(this, 'RecordUpdatedInfrastructure', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_updated_infrastructure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler
    const handleUpdateFailure = new tasks.LambdaInvoke(this, 'HandleUpdateFailure', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_update_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const updateFailed = new sfn.Fail(this, 'UpdateFailed', {
      cause: 'Project update failed',
      error: 'ProjectUpdateError',
    });

    const updateSuccess = new sfn.Succeed(this, 'UpdateSucceeded');

    // Add catch to all update steps for failure handling
    const updateCatchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const updateFailureChain = handleUpdateFailure.next(updateFailed);

    validateUpdateState.addCatch(updateFailureChain, updateCatchConfig);
    startCdkUpdate.addCatch(updateFailureChain, updateCatchConfig);
    checkUpdateStatus.addCatch(updateFailureChain, updateCatchConfig);
    extractUpdateStackOutputs.addCatch(updateFailureChain, updateCatchConfig);
    recordUpdatedInfrastructure.addCatch(updateFailureChain, updateCatchConfig);

    // Update wait loop: check status → if not complete, wait → check again
    const updateWaitLoop = waitForUpdate.next(checkUpdateStatus);
    const isUpdateComplete = new sfn.Choice(this, 'IsUpdateComplete')
      .when(sfn.Condition.booleanEquals('$.updateComplete', true), extractUpdateStackOutputs)
      .otherwise(updateWaitLoop);

    // Chain: Validate → Start CDK Update → Check Status → wait loop → Extract Outputs → Record Infrastructure → Success
    const updateDefinition = validateUpdateState
      .next(startCdkUpdate)
      .next(checkUpdateStatus)
      .next(isUpdateComplete);

    // Post-update chain (connected via the Choice "when complete" branch)
    extractUpdateStackOutputs
      .next(recordUpdatedInfrastructure)
      .next(updateSuccess);

    this.projectUpdateStateMachine = new sfn.StateMachine(this, 'ProjectUpdateStateMachine', {
      stateMachineName: 'hpc-project-update',
      definitionBody: sfn.DefinitionBody.fromChainable(updateDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // --- Update Project Management Lambda with update state machine ARN ---
    this.projectManagementLambda.addEnvironment(
      'PROJECT_UPDATE_STATE_MACHINE_ARN',
      this.projectUpdateStateMachine.stateMachineArn,
    );

    // Grant project management Lambda permission to start executions on the update state machine
    this.projectUpdateStateMachine.grantStartExecution(this.projectManagementLambda);

    // Grant project management Lambda permission to query Cost Explorer for budget breach clearing
    this.projectManagementLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ce:GetCostAndUsage'],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Cluster Operations Resources
    // ---------------------------------------------------------------
    const clustersResource = projectIdResource.addResource('clusters');
    const clusterNameResource = clustersResource.addResource('{clusterName}');

    const clusterOperationsIntegration = new apigateway.LambdaIntegration(this.clusterOperationsLambda);

    // POST /projects/{projectId}/clusters — create cluster
    clustersResource.addMethod('POST', clusterOperationsIntegration, cognitoMethodOptions);
    // GET /projects/{projectId}/clusters — list clusters
    clustersResource.addMethod('GET', clusterOperationsIntegration, cognitoMethodOptions);
    // GET /projects/{projectId}/clusters/{clusterName} — get cluster details
    clusterNameResource.addMethod('GET', clusterOperationsIntegration, cognitoMethodOptions);
    // DELETE /projects/{projectId}/clusters/{clusterName} — destroy cluster
    clusterNameResource.addMethod('DELETE', clusterOperationsIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/clusters/{clusterName}/recreate — recreate destroyed cluster
    const recreateResource = clusterNameResource.addResource('recreate');
    recreateResource.addMethod('POST', clusterOperationsIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // Accounting Query Lambda Function
    // ---------------------------------------------------------------
    this.accountingQueryLambda = new lambda.Function(this, 'AccountingQueryLambda', {
      functionName: 'hpc-accounting-query',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'accounting')),
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        USER_POOL_ID: this.userPool.userPoolId,
      },
      description: 'Queries cross-cluster Slurm job accounting data via SSM Run Command on login nodes',
    });

    // Grant DynamoDB read access on Clusters and Projects tables
    this.clustersTable.grantReadData(this.accountingQueryLambda);
    this.projectsTable.grantReadData(this.accountingQueryLambda);

    // Grant SSM permissions for querying sacct on cluster login nodes
    this.accountingQueryLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ssm:SendCommand',
        'ssm:GetCommandInvocation',
      ],
      resources: ['*'],
    }));

    // Grant Cognito read for authorisation checks
    this.accountingQueryLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:AdminGetUser',
      ],
      resources: [this.userPool.userPoolArn],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Accounting Query Resources
    // ---------------------------------------------------------------
    const accountingResource = this.api.root.addResource('accounting');
    const accountingJobsResource = accountingResource.addResource('jobs');

    const accountingQueryIntegration = new apigateway.LambdaIntegration(this.accountingQueryLambda);

    // GET /accounting/jobs — query job records across clusters
    accountingJobsResource.addMethod('GET', accountingQueryIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // Budget Notification Lambda Function
    // ---------------------------------------------------------------
    this.budgetNotificationLambda = new lambda.Function(this, 'BudgetNotificationLambda', {
      functionName: 'hpc-budget-notification',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'budget_notification')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        PROJECTS_TABLE_NAME: this.projectsTable.tableName,
        USERS_TABLE_NAME: this.platformUsersTable.tableName,
      },
      description: 'Processes AWS Budgets SNS notifications and updates project budget breach status',
    });

    // Grant DynamoDB read/write on Projects table (to set budgetBreached flag)
    this.projectsTable.grantReadWriteData(this.budgetNotificationLambda);
    // Grant DynamoDB read on PlatformUsers table (to look up admin emails)
    this.platformUsersTable.grantReadData(this.budgetNotificationLambda);

    // Subscribe the Lambda to the budget notification SNS topic
    this.budgetNotificationTopic.addSubscription(
      new snsSubscriptions.LambdaSubscription(this.budgetNotificationLambda),
    );

    // ---------------------------------------------------------------
    // FSx Cleanup Lambda Function (Scheduled)
    // ---------------------------------------------------------------
    this.fsxCleanupLambda = new lambda.Function(this, 'FsxCleanupLambda', {
      functionName: 'hpc-fsx-cleanup',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'fsx_cleanup')),
      layers: [sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: this.clustersTable.tableName,
        SNS_TOPIC_ARN: this.clusterLifecycleNotificationTopic.topicArn,
      },
      description: 'Detects and deletes orphaned FSx for Lustre filesystems on a schedule',
    });

    // Grant DynamoDB read-only access on Clusters table (no write permissions)
    this.clustersTable.grantReadData(this.fsxCleanupLambda);

    // Grant FSx permissions for describing and deleting filesystems and DRAs
    this.fsxCleanupLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'fsx:DescribeFileSystems',
        'fsx:DescribeDataRepositoryAssociations',
        'fsx:DeleteDataRepositoryAssociation',
        'fsx:DeleteFileSystem',
      ],
      resources: ['*'],
    }));

    // Grant SNS publish on the cluster lifecycle notification topic
    this.clusterLifecycleNotificationTopic.grantPublish(this.fsxCleanupLambda);

    // EventBridge rule to trigger FSx cleanup every 6 hours
    this.fsxCleanupScheduleRule = new events.Rule(this, 'FsxCleanupScheduleRule', {
      ruleName: 'hpc-fsx-cleanup-schedule',
      description: 'Triggers the FSx cleanup Lambda every 6 hours to remove orphaned filesystems',
      schedule: events.Schedule.rate(cdk.Duration.hours(6)),
    });

    this.fsxCleanupScheduleRule.addTarget(
      new eventsTargets.LambdaFunction(this.fsxCleanupLambda),
    );

    // ---------------------------------------------------------------
    // Web Portal — S3 Bucket + CloudFront Distribution
    // ---------------------------------------------------------------

    this.webPortalBucket = new s3.Bucket(this, 'WebPortalBucket', {
      bucketName: `hpc-portal-${cdk.Aws.ACCOUNT_ID}-${cdk.Aws.REGION}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    this.webPortalDistribution = new cloudfront.Distribution(this, 'WebPortalDistribution', {
      comment: 'HPC Self-Service Portal',
      defaultBehavior: {
        origin: cloudfrontOrigins.S3BucketOrigin.withOriginAccessControl(this.webPortalBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      defaultRootObject: 'index.html',
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
      ],
    });

    // Deploy frontend assets to S3 and invalidate CloudFront cache.
    // The static files (HTML, CSS, app JS) are bundled from the frontend/
    // directory. A generated config.js with real deployment values is added
    // as a second source so the portal works immediately without manual
    // configuration. Later sources overwrite earlier ones, so the generated
    // config replaces the placeholder shipped in the frontend/ directory.
    new s3deploy.BucketDeployment(this, 'WebPortalDeployment', {
      sources: [
        s3deploy.Source.asset(path.join(__dirname, '..', 'frontend')),
        s3deploy.Source.data(
          'js/config.js',
          [
            '/**',
            ' * Configuration for the HPC Self-Service Portal.',
            ' * Auto-generated by CDK during deployment — do not edit manually.',
            ' */',
            'const CONFIG = {',
            `  cognitoUserPoolId: '${this.userPool.userPoolId}',`,
            `  cognitoClientId: '${this.userPoolClient.userPoolClientId}',`,
            `  cognitoRegion: '${cdk.Aws.REGION}',`,
            `  apiBaseUrl: '${this.api.url}',`,
            '  clusterPollIntervalMs: 5000,',
            '};',
          ].join('\n'),
        ),
      ],
      destinationBucket: this.webPortalBucket,
      distribution: this.webPortalDistribution,
      distributionPaths: ['/*'],
    });

    // Deploy documentation to S3 under the docs/ prefix
    new s3deploy.BucketDeployment(this, 'DocsDeployment', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '..', 'docs'))],
      destinationBucket: this.webPortalBucket,
      destinationKeyPrefix: 'docs',
      distribution: this.webPortalDistribution,
      distributionPaths: ['/docs/*'],
    });

    // ---------------------------------------------------------------
    // Stack Outputs
    // ---------------------------------------------------------------
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: 'Cognito User Pool ID',
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'Cognito User Pool Client ID',
    });

    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      description: 'API Gateway URL',
    });

    new cdk.CfnOutput(this, 'PlatformUsersTableName', {
      value: this.platformUsersTable.tableName,
      description: 'PlatformUsers DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ProjectsTableName', {
      value: this.projectsTable.tableName,
      description: 'Projects DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ClusterTemplatesTableName', {
      value: this.clusterTemplatesTable.tableName,
      description: 'ClusterTemplates DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ClustersTableName', {
      value: this.clustersTable.tableName,
      description: 'Clusters DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'ClusterNameRegistryTableName', {
      value: this.clusterNameRegistryTable.tableName,
      description: 'ClusterNameRegistry DynamoDB table name',
    });

    new cdk.CfnOutput(this, 'BudgetNotificationTopicArn', {
      value: this.budgetNotificationTopic.topicArn,
      description: 'SNS topic ARN for budget notifications',
    });

    new cdk.CfnOutput(this, 'ClusterLifecycleNotificationTopicArn', {
      value: this.clusterLifecycleNotificationTopic.topicArn,
      description: 'SNS topic ARN for cluster lifecycle notifications',
    });

    new cdk.CfnOutput(this, 'ClusterOperationsLambdaArn', {
      value: this.clusterOperationsLambda.functionArn,
      description: 'Cluster Operations Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'ClusterCreationStateMachineArn', {
      value: this.clusterCreationStateMachine.stateMachineArn,
      description: 'Cluster Creation Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'ClusterDestructionStateMachineArn', {
      value: this.clusterDestructionStateMachine.stateMachineArn,
      description: 'Cluster Destruction Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'CdkDeployProjectName', {
      value: this.cdkDeployProject.projectName,
      description: 'CodeBuild project name for CDK deploy/destroy',
    });

    new cdk.CfnOutput(this, 'ProjectDeployStateMachineArn', {
      value: this.projectDeployStateMachine.stateMachineArn,
      description: 'Project Deploy Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'ProjectDestroyStateMachineArn', {
      value: this.projectDestroyStateMachine.stateMachineArn,
      description: 'Project Destroy Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'ProjectUpdateStateMachineArn', {
      value: this.projectUpdateStateMachine.stateMachineArn,
      description: 'Project Update Step Functions state machine ARN',
    });

    new cdk.CfnOutput(this, 'AccountingQueryLambdaArn', {
      value: this.accountingQueryLambda.functionArn,
      description: 'Accounting Query Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'BudgetNotificationLambdaArn', {
      value: this.budgetNotificationLambda.functionArn,
      description: 'Budget Notification Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'WebPortalUrl', {
      value: `https://${this.webPortalDistribution.distributionDomainName}`,
      description: 'Web Portal CloudFront URL',
    });

    new cdk.CfnOutput(this, 'WebPortalBucketName', {
      value: this.webPortalBucket.bucketName,
      description: 'Web Portal S3 bucket name',
    });

    // ---------------------------------------------------------------
    // Apply cost allocation tags via CDK Aspects
    // ---------------------------------------------------------------
    // Cost allocation tags are applied globally via the CDK Aspect in bin/self-service-hpc.ts
  }
}
