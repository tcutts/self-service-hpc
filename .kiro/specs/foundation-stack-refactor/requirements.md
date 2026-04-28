# Requirements Document

## Introduction

The `FoundationStack` class in `lib/foundation-stack.ts` is a 2,559-line file with a single monolithic constructor that defines all platform resources inline. This refactoring decomposes the constructor into cohesive CDK Construct classes in separate files, improving readability, maintainability, and testability while preserving the existing CloudFormation output and public API surface.

## Glossary

- **Foundation_Stack**: The top-level CDK Stack class (`FoundationStack`) that provisions all shared control-plane resources for the HPC platform.
- **Construct**: A CDK Construct (L3 or composition construct) that encapsulates a logically related group of AWS resources and is instantiated within the Foundation_Stack.
- **Refactored_Construct**: A new CDK Construct class extracted from the Foundation_Stack constructor into its own file under `lib/constructs/`.
- **Public_Property**: A `public readonly` property on the Foundation_Stack class that is accessed by external consumers (`bin/self-service-hpc.ts`, test files).
- **CloudFormation_Template**: The synthesised CloudFormation JSON/YAML output produced by `cdk synth` for the Foundation_Stack.
- **Existing_Test_Suite**: The test file `test/foundation-stack.test.ts` containing 1,833 lines of `Template.fromStack` assertions against the Foundation_Stack.
- **Cross_Reference**: A runtime dependency between resources in different Refactored_Constructs (e.g., a Lambda environment variable referencing a state machine ARN, or a table grant to a Lambda function).
- **API_Gateway**: The REST API Gateway resource and all route/method definitions attached to it.
- **Shared_Layer**: The Lambda Layer containing shared Python utilities used by all Lambda functions in the stack.

## Requirements

### Requirement 1: Extract Cognito Resources into a Dedicated Construct

**User Story:** As a developer, I want Cognito resources (UserPool, UserPoolClient, Administrators group) grouped in a dedicated construct, so that authentication configuration is isolated and easy to locate.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the UserPool, UserPoolClient, and Administrators CfnUserPoolGroup resources in a single `CognitoAuth` construct class in `lib/constructs/cognito-auth.ts`.
2. THE `CognitoAuth` Construct SHALL expose `userPool` and `userPoolClient` as public readonly properties.
3. WHEN the `CognitoAuth` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `userPool` and `userPoolClient` Public_Properties from the construct's exposed properties.
4. THE CloudFormation_Template produced after refactoring SHALL contain identical Cognito resource definitions as before refactoring.

### Requirement 2: Extract DynamoDB Tables into a Dedicated Construct

**User Story:** As a developer, I want all DynamoDB table definitions and seed data grouped in a dedicated construct, so that data layer configuration is centralised.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define all five DynamoDB tables (PlatformUsers, Projects, ClusterTemplates, Clusters, ClusterNameRegistry), their GSIs, and all seed custom resources in a single `DatabaseTables` construct class in `lib/constructs/database-tables.ts`.
2. THE `DatabaseTables` Construct SHALL expose each table as a public readonly property.
3. WHEN the `DatabaseTables` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own table Public_Properties from the construct's exposed properties.
4. THE CloudFormation_Template produced after refactoring SHALL contain identical DynamoDB table definitions, GSIs, and seed custom resources as before refactoring.

### Requirement 3: Extract API Gateway and Shared Lambda Layer into a Dedicated Construct

**User Story:** As a developer, I want the API Gateway REST API, Cognito authorizer, and shared Lambda layer grouped in a dedicated construct, so that API infrastructure is defined in one place.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the REST API, Cognito authorizer, health endpoint, API access log group, infrastructure log group, and Shared_Layer in a single `ApiGateway` construct class in `lib/constructs/api-gateway.ts`.
2. THE `ApiGateway` Construct SHALL expose `api`, `cognitoAuthorizer`, and `sharedLayer` as public readonly properties.
3. WHEN the `ApiGateway` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `api` and `cognitoAuthorizer` Public_Properties from the construct's exposed properties.
4. THE CloudFormation_Template produced after refactoring SHALL contain identical API Gateway, authorizer, log group, and layer resource definitions as before refactoring.

### Requirement 4: Extract User Management Resources into a Dedicated Construct

**User Story:** As a developer, I want the user management Lambda, IAM policies, and API routes grouped in a dedicated construct, so that user management concerns are encapsulated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the user management Lambda function, its IAM policies, and all `/users` API Gateway routes in a single `UserManagement` construct class in `lib/constructs/user-management.ts`.
2. THE `UserManagement` Construct SHALL accept the PlatformUsers table, UserPool, API root resource, Cognito authorizer, and Shared_Layer as constructor props.
3. THE `UserManagement` Construct SHALL expose `lambda` (the user management Lambda function) as a public readonly property.
4. WHEN the `UserManagement` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `userManagementLambda` Public_Property from the construct's exposed property.
5. THE CloudFormation_Template produced after refactoring SHALL contain identical user management resource definitions as before refactoring.

### Requirement 5: Extract SNS Topics into a Dedicated Construct

**User Story:** As a developer, I want SNS topic definitions grouped in a dedicated construct, so that notification infrastructure is centralised.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the budget notification topic and cluster lifecycle notification topic in a single `NotificationTopics` construct class in `lib/constructs/notification-topics.ts`.
2. THE `NotificationTopics` Construct SHALL expose `budgetNotificationTopic` and `clusterLifecycleNotificationTopic` as public readonly properties.
3. WHEN the `NotificationTopics` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own SNS topic Public_Properties from the construct's exposed properties.
4. THE CloudFormation_Template produced after refactoring SHALL contain identical SNS topic definitions as before refactoring.

### Requirement 6: Extract Project Management Resources into a Dedicated Construct

**User Story:** As a developer, I want the project management Lambda, IAM policies, and API routes grouped in a dedicated construct, so that project management concerns are encapsulated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the project management Lambda function, its IAM policies, and all `/projects` API Gateway routes (including batch operations, deploy, destroy, update sub-resources) in a single `ProjectManagement` construct class in `lib/constructs/project-management.ts`.
2. THE `ProjectManagement` Construct SHALL accept the required tables, UserPool, API root resource, Cognito authorizer, Shared_Layer, and budget notification topic as constructor props.
3. THE `ProjectManagement` Construct SHALL expose `lambda` (the project management Lambda function) and the `projectIdResource` API Gateway resource as public readonly properties, so that downstream constructs can attach child routes (e.g., `/projects/{projectId}/clusters`).
4. WHEN the `ProjectManagement` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `projectManagementLambda` Public_Property from the construct's exposed property.
5. THE CloudFormation_Template produced after refactoring SHALL contain identical project management resource definitions as before refactoring.

### Requirement 7: Extract Template Management Resources into a Dedicated Construct

**User Story:** As a developer, I want the template management Lambda, IAM policies, and API routes grouped in a dedicated construct, so that template management concerns are encapsulated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the template management Lambda function, its IAM policies, and all `/templates` API Gateway routes (including batch delete) in a single `TemplateManagement` construct class in `lib/constructs/template-management.ts`.
2. THE `TemplateManagement` Construct SHALL accept the ClusterTemplates table, API root resource, Cognito authorizer, and Shared_Layer as constructor props.
3. THE `TemplateManagement` Construct SHALL expose `lambda` (the template management Lambda function) as a public readonly property.
4. WHEN the `TemplateManagement` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `templateManagementLambda` Public_Property from the construct's exposed property.
5. THE CloudFormation_Template produced after refactoring SHALL contain identical template management resource definitions as before refactoring.

### Requirement 8: Extract Cluster Operations and State Machines into a Dedicated Construct

**User Story:** As a developer, I want the cluster operations Lambda, cluster creation/destruction step Lambdas, both state machines, all associated IAM policies, and the cluster API routes grouped in a dedicated construct, so that the most complex section of the stack is self-contained.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the cluster operations Lambda, cluster creation step Lambda, cluster destruction step Lambda, the cluster creation state machine (including all step definitions, parallel branches, wait loops, error handling, and the MarkClusterFailed SDK integration), the cluster destruction state machine, all associated IAM policies, and the `/projects/{projectId}/clusters` API routes in a single `ClusterOperations` construct class in `lib/constructs/cluster-operations.ts`.
2. THE `ClusterOperations` Construct SHALL accept the required tables, UserPool, Cognito authorizer, Shared_Layer, cluster lifecycle notification topic, templates table, and the `projectIdResource` API Gateway resource as constructor props.
3. THE `ClusterOperations` Construct SHALL expose `clusterOperationsLambda`, `clusterCreationStateMachine`, and `clusterDestructionStateMachine` as public readonly properties.
4. WHEN the `ClusterOperations` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own cluster-related Public_Properties from the construct's exposed properties.
5. THE `ClusterOperations` Construct SHALL wire the state machine ARNs into the cluster operations Lambda environment variables and grant `startExecution` permissions internally.
6. THE CloudFormation_Template produced after refactoring SHALL contain identical cluster operations resource definitions as before refactoring.

### Requirement 9: Extract CodeBuild Project into a Dedicated Construct

**User Story:** As a developer, I want the CodeBuild project and its IAM policies grouped in a dedicated construct, so that CI/CD infrastructure is isolated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the CDK source asset, CodeBuild project, and all associated IAM policies in a single `CdkDeployProject` construct class in `lib/constructs/cdk-deploy-project.ts`.
2. THE `CdkDeployProject` Construct SHALL expose `project` (the CodeBuild project) as a public readonly property.
3. WHEN the `CdkDeployProject` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `cdkDeployProject` Public_Property from the construct's exposed property.
4. THE CloudFormation_Template produced after refactoring SHALL contain identical CodeBuild resource definitions as before refactoring.

### Requirement 10: Extract Project Lifecycle State Machines into a Dedicated Construct

**User Story:** As a developer, I want the project deploy, destroy, and update state machines grouped in a dedicated construct, so that project lifecycle orchestration is encapsulated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the project deploy step Lambda, project destroy step Lambda, project update step Lambda, and the three project lifecycle state machines (deploy, destroy, update) with all step definitions, wait loops, and error handling in a single `ProjectLifecycle` construct class in `lib/constructs/project-lifecycle.ts`.
2. THE `ProjectLifecycle` Construct SHALL accept the Projects table, Clusters table, CodeBuild project, and Shared_Layer as constructor props.
3. THE `ProjectLifecycle` Construct SHALL expose `projectDeployStateMachine`, `projectDestroyStateMachine`, and `projectUpdateStateMachine` as public readonly properties.
4. WHEN the `ProjectLifecycle` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own state machine Public_Properties from the construct's exposed properties.
5. THE `ProjectLifecycle` Construct SHALL NOT wire state machine ARNs into the project management Lambda; THE Foundation_Stack SHALL perform that Cross_Reference wiring after both constructs are instantiated.
6. THE CloudFormation_Template produced after refactoring SHALL contain identical project lifecycle resource definitions as before refactoring.

### Requirement 11: Extract Accounting, Budget Notification, FSx Cleanup, and Failure Handler into a Dedicated Construct

**User Story:** As a developer, I want the supporting Lambda functions (accounting, budget notification, FSx cleanup, cluster creation failure handler) and their triggers grouped in a dedicated construct, so that auxiliary operations are encapsulated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the accounting query Lambda, budget notification Lambda, FSx cleanup Lambda, FSx cleanup EventBridge schedule rule, cluster creation failure handler Lambda, and cluster creation failure EventBridge rule in a single `PlatformOperations` construct class in `lib/constructs/platform-operations.ts`.
2. THE `PlatformOperations` Construct SHALL accept the required tables, UserPool, API root resource, Cognito authorizer, Shared_Layer, SNS topics, and cluster creation state machine as constructor props.
3. THE `PlatformOperations` Construct SHALL expose `accountingQueryLambda`, `budgetNotificationLambda`, `fsxCleanupLambda`, and `fsxCleanupScheduleRule` as public readonly properties.
4. WHEN the `PlatformOperations` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own operations-related Public_Properties from the construct's exposed properties.
5. THE CloudFormation_Template produced after refactoring SHALL contain identical operations resource definitions as before refactoring.

### Requirement 12: Extract Web Portal into a Dedicated Construct

**User Story:** As a developer, I want the S3 bucket, CloudFront distribution, and S3 deployments grouped in a dedicated construct, so that web portal infrastructure is isolated.

#### Acceptance Criteria

1. THE Refactored_Construct SHALL define the web portal S3 bucket, CloudFront distribution, frontend S3 deployment (including generated config.js), and documentation S3 deployment in a single `WebPortal` construct class in `lib/constructs/web-portal.ts`.
2. THE `WebPortal` Construct SHALL accept the UserPool, UserPoolClient, and API as constructor props to generate the config.js content.
3. THE `WebPortal` Construct SHALL expose `bucket` and `distribution` as public readonly properties.
4. WHEN the `WebPortal` Construct is instantiated within the Foundation_Stack, THE Foundation_Stack SHALL assign its own `webPortalBucket` and `webPortalDistribution` Public_Properties from the construct's exposed properties.
5. THE CloudFormation_Template produced after refactoring SHALL contain identical web portal resource definitions as before refactoring.

### Requirement 13: Extract Stack Outputs and Foundation Timestamp into the Foundation Stack Orchestration

**User Story:** As a developer, I want the Foundation_Stack constructor to be a concise orchestrator that instantiates constructs, wires Cross_References, and emits CfnOutputs, so that the top-level file is readable at a glance.

#### Acceptance Criteria

1. THE Foundation_Stack constructor SHALL instantiate all Refactored_Constructs, perform Cross_Reference wiring (state machine ARN injection into Lambda environment variables, `grantStartExecution` calls, Cost Explorer permissions), define the foundation timestamp custom resource, and emit all CfnOutputs.
2. THE Foundation_Stack constructor SHALL contain no inline resource definitions other than Cross_Reference wiring, the foundation timestamp custom resource, and CfnOutputs.
3. THE Foundation_Stack SHALL preserve all existing Public_Properties with identical types and semantics.
4. THE CloudFormation_Template produced after refactoring SHALL be functionally equivalent to the template produced before refactoring.

### Requirement 14: Preserve Backward Compatibility with External Consumers

**User Story:** As a developer, I want the refactoring to be transparent to all external consumers, so that no downstream code changes are required.

#### Acceptance Criteria

1. THE Foundation_Stack SHALL export the same class name (`FoundationStack`) from the same module path (`lib/foundation-stack`).
2. THE Foundation_Stack SHALL expose all existing Public_Properties with identical names and types.
3. WHEN `bin/self-service-hpc.ts` instantiates the Foundation_Stack and accesses `api`, `userPool`, `userPoolClient`, `platformUsersTable`, `projectsTable`, `clustersTable`, and `clusterTemplatesTable`, THE Foundation_Stack SHALL return valid CDK construct references.
4. THE Existing_Test_Suite SHALL pass without modification after refactoring.

### Requirement 15: Comprehensive Unit Tests for Refactored Constructs

**User Story:** As a developer, I want each refactored construct to have its own unit test file, so that construct-level behaviour can be verified independently.

#### Acceptance Criteria

1. WHEN a Refactored_Construct is instantiated in an isolated test stack, THE test SHALL verify that the construct creates the expected resource types and counts.
2. WHEN a Refactored_Construct is instantiated in an isolated test stack, THE test SHALL verify key resource properties (names, configurations, IAM policy actions).
3. THE test files SHALL be located in `test/constructs/` with names matching the construct file (e.g., `test/constructs/cognito-auth.test.ts`).
4. THE Existing_Test_Suite in `test/foundation-stack.test.ts` SHALL continue to pass, validating the full integrated stack.
