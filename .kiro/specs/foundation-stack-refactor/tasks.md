# Tasks

## Task 1: Create CognitoAuth construct and test

- [x] 1.1 Create `lib/constructs/cognito-auth.ts` with `CognitoAuth` construct class that defines the UserPool, UserPoolClient, and Administrators CfnUserPoolGroup, exposing `userPool` and `userPoolClient` as public readonly properties
- [x] 1.2 Create `test/constructs/cognito-auth.test.ts` verifying: 1 UserPool, 1 UserPoolClient, 1 CfnUserPoolGroup, correct password policy, email sign-in, and RETAIN removal policy
- [x] 1.3 Run `npx jest test/constructs/cognito-auth.test.ts` and confirm all tests pass

## Task 2: Create DatabaseTables construct and test

- [x] 2.1 Create `lib/constructs/database-tables.ts` with `DatabaseTables` construct class that defines all 5 DynamoDB tables (PlatformUsers, Projects, ClusterTemplates, Clusters, ClusterNameRegistry), their GSIs, and all seed custom resources, exposing each table as a public readonly property
- [x] 2.2 Create `test/constructs/database-tables.test.ts` verifying: 5 DynamoDB tables, correct key schemas, GSIs (StatusIndex, UserProjectsIndex), PAY_PER_REQUEST billing, PITR enabled, and seed custom resources
- [x] 2.3 Run `npx jest test/constructs/database-tables.test.ts` and confirm all tests pass

## Task 3: Create ApiGateway construct and test

- [x] 3.1 Create `lib/constructs/api-gateway.ts` with `ApiGateway` construct class that defines the REST API, Cognito authorizer, health endpoint, API access log group, infrastructure log group, and SharedUtilsLayer, accepting `userPool` as a prop and exposing `api`, `cognitoAuthorizer`, and `sharedLayer` as public readonly properties
- [x] 3.2 Create `test/constructs/api-gateway.test.ts` verifying: 1 RestApi, 1 Authorizer, 2 LogGroups, 1 LayerVersion, health endpoint, CORS configuration, and correct log retention
- [x] 3.3 Run `npx jest test/constructs/api-gateway.test.ts` and confirm all tests pass

## Task 4: Create NotificationTopics construct and test

- [x] 4.1 Create `lib/constructs/notification-topics.ts` with `NotificationTopics` construct class that defines the budget notification topic and cluster lifecycle notification topic, exposing both as public readonly properties
- [x] 4.2 Create `test/constructs/notification-topics.test.ts` verifying: 2 SNS topics with correct names and display names
- [x] 4.3 Run `npx jest test/constructs/notification-topics.test.ts` and confirm all tests pass

## Task 5: Create UserManagement construct and test

- [x] 5.1 Create `lib/constructs/user-management.ts` with `UserManagement` construct class that defines the user management Lambda, IAM policies, and all `/users` API routes, accepting tables/userPool/api/authorizer/sharedLayer as props and exposing `lambda` as a public readonly property
- [x] 5.2 Create `test/constructs/user-management.test.ts` verifying: 1 Lambda function with correct runtime/handler/environment, DynamoDB and Cognito IAM policies, and API Gateway method resources for /users routes
- [x] 5.3 Run `npx jest test/constructs/user-management.test.ts` and confirm all tests pass

## Task 6: Create ProjectManagement construct and test

- [x] 6.1 Create `lib/constructs/project-management.ts` with `ProjectManagement` construct class that defines the project management Lambda, IAM policies, and all `/projects` API routes (including batch operations, deploy, destroy, update sub-resources), accepting required props and exposing `lambda` and `projectIdResource` as public readonly properties
- [x] 6.2 Create `test/constructs/project-management.test.ts` verifying: 1 Lambda function with correct runtime/handler/environment, DynamoDB/Cognito/Budgets/SNS/STS IAM policies, and API Gateway method resources for /projects routes
- [x] 6.3 Run `npx jest test/constructs/project-management.test.ts` and confirm all tests pass

## Task 7: Create TemplateManagement construct and test

- [x] 7.1 Create `lib/constructs/template-management.ts` with `TemplateManagement` construct class that defines the template management Lambda, IAM policies, and all `/templates` API routes (including batch delete), accepting required props and exposing `lambda` as a public readonly property
- [x] 7.2 Create `test/constructs/template-management.test.ts` verifying: 1 Lambda function with correct runtime/handler/environment, DynamoDB and EC2 IAM policies, and API Gateway method resources for /templates routes
- [x] 7.3 Run `npx jest test/constructs/template-management.test.ts` and confirm all tests pass

## Task 8: Create ClusterOperations construct and test

- [x] 8.1 Create `lib/constructs/cluster-operations.ts` with `ClusterOperations` construct class that defines the cluster operations Lambda, cluster creation/destruction step Lambdas, both state machines (with all step definitions, parallel branches, wait loops, error handling, MarkClusterFailed SDK integration), all IAM policies, and the cluster API routes, accepting required props and exposing `clusterOperationsLambda`, `clusterCreationStateMachine`, and `clusterDestructionStateMachine` as public readonly properties
- [x] 8.2 Create `test/constructs/cluster-operations.test.ts` verifying: 3 Lambda functions, 2 StateMachines, correct IAM policies for PCS/FSx/EC2/S3/IAM/SecretsManager, and API Gateway method resources for /clusters routes
- [x] 8.3 Run `npx jest test/constructs/cluster-operations.test.ts` and confirm all tests pass

## Task 9: Create CdkDeployProject construct and test

- [x] 9.1 Create `lib/constructs/cdk-deploy-project.ts` with `CdkDeployProject` construct class that defines the CDK source asset, CodeBuild project, and all associated IAM policies, exposing `project` as a public readonly property
- [x] 9.2 Create `test/constructs/cdk-deploy-project.test.ts` verifying: 1 CodeBuild project with correct environment/buildspec, and IAM policies for CloudFormation/EC2/EFS/S3/Logs/SSM/STS
- [x] 9.3 Run `npx jest test/constructs/cdk-deploy-project.test.ts` and confirm all tests pass

## Task 10: Create ProjectLifecycle construct and test

- [x] 10.1 Create `lib/constructs/project-lifecycle.ts` with `ProjectLifecycle` construct class that defines the project deploy/destroy/update step Lambdas and all three project lifecycle state machines, accepting required props and exposing `projectDeployStateMachine`, `projectDestroyStateMachine`, and `projectUpdateStateMachine` as public readonly properties. The construct SHALL NOT wire state machine ARNs into the project management Lambda.
- [x] 10.2 Create `test/constructs/project-lifecycle.test.ts` verifying: 3 Lambda functions, 3 StateMachines, correct IAM policies for CodeBuild/CloudFormation/DynamoDB
- [x] 10.3 Run `npx jest test/constructs/project-lifecycle.test.ts` and confirm all tests pass

## Task 11: Create PlatformOperations construct and test

- [x] 11.1 Create `lib/constructs/platform-operations.ts` with `PlatformOperations` construct class that defines the accounting query Lambda, budget notification Lambda, FSx cleanup Lambda, FSx cleanup EventBridge schedule, cluster creation failure handler Lambda, and cluster creation failure EventBridge rule, accepting required props and exposing `accountingQueryLambda`, `budgetNotificationLambda`, `fsxCleanupLambda`, and `fsxCleanupScheduleRule` as public readonly properties
- [x] 11.2 Create `test/constructs/platform-operations.test.ts` verifying: 4 Lambda functions, 2 EventBridge rules, 1 SNS subscription, correct IAM policies, and API Gateway method resources for /accounting routes
- [x] 11.3 Run `npx jest test/constructs/platform-operations.test.ts` and confirm all tests pass

## Task 12: Create WebPortal construct and test

- [x] 12.1 Create `lib/constructs/web-portal.ts` with `WebPortal` construct class that defines the S3 bucket, CloudFront distribution, frontend S3 deployment (including generated config.js), and documentation S3 deployment, accepting userPool/userPoolClient/api as props and exposing `bucket` and `distribution` as public readonly properties
- [x] 12.2 Create `test/constructs/web-portal.test.ts` verifying: 1 S3 bucket with BlockPublicAccess, 1 CloudFront distribution with HTTPS redirect, and 2 BucketDeployments
- [x] 12.3 Run `npx jest test/constructs/web-portal.test.ts` and confirm all tests pass

## Task 13: Refactor FoundationStack to orchestrate constructs

- [x] 13.1 Rewrite `lib/foundation-stack.ts` to import and instantiate all 11 constructs in dependency order, assign public properties from construct outputs, perform cross-reference wiring (state machine ARN injection via `addEnvironment`, `grantStartExecution` calls, Cost Explorer permissions), create the foundation timestamp custom resource, and emit all CfnOutputs. The constructor must contain no inline resource definitions other than cross-reference wiring, the foundation timestamp, and CfnOutputs.
- [x] 13.2 Run `npx jest test/foundation-stack.test.ts` and confirm the existing 1,833-line integration test suite passes without modification
- [x] 13.3 Run `npx jest` to confirm all tests (existing integration + new construct-level) pass together
