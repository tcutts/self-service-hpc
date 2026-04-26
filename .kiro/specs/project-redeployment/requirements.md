# Requirements Document

## Introduction

This document specifies the requirements for adding an update capability to the Self-Service HPC platform's project lifecycle. Currently, once a project is deployed (ACTIVE), there is no way to update its infrastructure when the underlying CDK code changes (e.g. new security group rules, updated VPC configuration, or new resource additions). Administrators need the ability to update a project's infrastructure stack without destroying and recreating it, and critically, without disrupting clusters that are already running inside that project.

Update leverages CloudFormation's update-in-place behaviour via `cdk deploy`, which applies only the delta between the current and desired stack state. Resources with stable logical IDs (VPC, EFS, S3 bucket, security groups) are preserved. The feature introduces a new UPDATING lifecycle state, a new API endpoint, a new Step Functions workflow, and corresponding UI and documentation changes.

## Glossary

- **Platform**: The Self-Service HPC web application and its backend services.
- **Administrator**: A user in the Cognito Administrators group with full platform management access.
- **Project**: The primary organisational unit providing dedicated VPC, EFS, S3, and security groups for HPC clusters.
- **Cluster**: An HPC compute environment (PCS cluster with login and compute nodes) running inside a project's VPC.
- **Update**: The process of updating a project's existing CloudFormation stack by running `cdk deploy` against the already-deployed `HpcProject-{projectId}` stack, applying only infrastructure changes.
- **Project_Infrastructure_Stack**: The CDK stack (`ProjectInfrastructureStack`) that provisions per-project VPC, EFS, S3, security groups, and CloudWatch log groups.
- **Lifecycle_State_Machine**: The set of valid project status transitions enforced by `lifecycle.py`.
- **Update_Workflow**: The Step Functions state machine that orchestrates the update process.
- **CodeBuild_Project**: The shared CodeBuild project used to execute CDK CLI commands for deploy, destroy, and update operations.
- **Infrastructure_Outputs**: The CloudFormation stack outputs (VpcId, EfsFileSystemId, S3BucketName, subnet IDs, security group IDs) recorded in the project's DynamoDB record.

## Requirements

### Requirement 1: Lifecycle State Extension for Update

**User Story:** As an Administrator, I want the project lifecycle to support an UPDATING state, so that the platform can track and enforce update operations safely.

#### Acceptance Criteria

1. THE Lifecycle_State_Machine SHALL include UPDATING as a valid project status.
2. WHEN an ACTIVE project is updated, THE Lifecycle_State_Machine SHALL transition the project from ACTIVE to UPDATING.
3. WHEN an update succeeds, THE Lifecycle_State_Machine SHALL transition the project from UPDATING to ACTIVE.
4. WHEN an update fails, THE Lifecycle_State_Machine SHALL transition the project from UPDATING back to ACTIVE and store the error message.
5. WHILE a project is in UPDATING status, THE Lifecycle_State_Machine SHALL reject transitions to DESTROYING.
6. WHILE a project is in UPDATING status, THE Lifecycle_State_Machine SHALL allow cluster creation and cluster destruction operations to proceed, because the underlying infrastructure (VPC, subnets, security groups, EFS) remains available during a CloudFormation update.
7. THE Lifecycle_State_Machine SHALL enforce all update transitions atomically using DynamoDB ConditionExpressions.

### Requirement 2: Update API Endpoint

**User Story:** As an Administrator, I want an API endpoint to trigger a project update, so that I can update project infrastructure when CDK code changes.

#### Acceptance Criteria

1. THE Platform SHALL expose a `POST /projects/{projectId}/update` endpoint.
2. WHEN the update endpoint is called, THE Platform SHALL verify the caller has the Administrator role.
3. WHEN the update endpoint is called for a project not in ACTIVE status, THE Platform SHALL return a 409 Conflict error with a descriptive message.
4. WHEN the update endpoint is called for a project that does not exist, THE Platform SHALL return a 404 Not Found error.
5. WHEN the update endpoint is called for a valid ACTIVE project, THE Platform SHALL transition the project to UPDATING, start the Update_Workflow, and return 202 Accepted.
6. THE Platform SHALL set initial progress tracking fields (currentStep=0, totalSteps) when starting an update.

### Requirement 3: Update Workflow Orchestration

**User Story:** As an Administrator, I want the update to run as a Step Functions workflow with progress tracking, so that I can monitor the update and the system can handle failures gracefully.

#### Acceptance Criteria

1. THE Update_Workflow SHALL validate that the project exists and its status is UPDATING before proceeding.
2. THE Update_Workflow SHALL execute `cdk deploy` against the existing `HpcProject-{projectId}` CloudFormation stack via the CodeBuild_Project.
3. THE Update_Workflow SHALL pass the `--exclusively --require-approval never` flags to the CDK deploy command.
4. THE Update_Workflow SHALL poll the CodeBuild build status with a wait loop until the build completes, fails, or times out.
5. WHEN the CDK deploy succeeds, THE Update_Workflow SHALL extract the updated CloudFormation stack outputs.
6. WHEN the CDK deploy succeeds, THE Update_Workflow SHALL update the project's DynamoDB record with the latest Infrastructure_Outputs (VpcId, EfsFileSystemId, S3BucketName, subnet IDs, security group IDs).
7. WHEN the CDK deploy succeeds, THE Update_Workflow SHALL transition the project to ACTIVE.
8. WHEN the CDK deploy fails, THE Update_Workflow SHALL transition the project back to ACTIVE and store the error message from the build.
9. THE Update_Workflow SHALL update progress tracking fields (currentStep, totalSteps, stepDescription) in DynamoDB at each step so the UI can display progress.

### Requirement 4: Cluster Safety During Update

**User Story:** As a cluster user, I want my running clusters to remain operational during a project update, so that my workloads are not interrupted by infrastructure updates.

#### Acceptance Criteria

1. THE Project_Infrastructure_Stack SHALL use stable CDK construct IDs for VPC, EFS, S3 bucket, and security group resources so that CloudFormation preserves these resources during stack updates.
2. WHILE a project is in UPDATING status, THE Platform SHALL allow cluster listing, cluster detail retrieval, cluster creation, and cluster destruction operations for that project.
3. WHEN an update completes, THE Platform SHALL verify that the Infrastructure_Outputs recorded in DynamoDB match the current CloudFormation stack outputs.
4. IF the update changes a security group ID, subnet ID, VPC ID, or EFS filesystem ID, THEN THE Update_Workflow SHALL log a warning identifying the changed resource, because existing clusters reference the previous resource IDs.

### Requirement 5: Update Progress and UI

**User Story:** As an Administrator, I want to see update progress in the web portal, so that I can monitor the update without checking the AWS console.

#### Acceptance Criteria

1. WHILE a project is in UPDATING status, THE Platform SHALL include a progress object (currentStep, totalSteps, stepDescription) in the `GET /projects/{projectId}` response.
2. WHILE a project is in UPDATING status, THE Platform SHALL display a progress bar in the project list showing the current step and description.
3. WHILE any project is in UPDATING status, THE Platform SHALL poll the project list every 5 seconds.
4. WHEN a project transitions from UPDATING to ACTIVE, THE Platform SHALL display a toast notification indicating the update succeeded.
5. WHEN a project transitions from UPDATING to ACTIVE with an error message, THE Platform SHALL display a toast notification indicating the update failed and include the error message.
6. WHILE a project is in ACTIVE status, THE Platform SHALL display an "Update" button in the project list actions.

### Requirement 6: CDK Infrastructure for Update

**User Story:** As a platform operator, I want the update workflow provisioned as CDK infrastructure, so that it is managed alongside the existing deploy and destroy workflows.

#### Acceptance Criteria

1. THE Foundation_Stack SHALL include a Step Functions state machine for the Update_Workflow.
2. THE Update_Workflow state machine SHALL reuse the existing CodeBuild_Project for executing CDK commands.
3. THE Foundation_Stack SHALL include a Lambda function for the update step handlers.
4. THE Foundation_Stack SHALL grant the update Lambda function read/write access to the Projects DynamoDB table, CodeBuild start/describe permissions, and CloudFormation describe permissions.
5. THE Foundation_Stack SHALL add the `POST /projects/{projectId}/update` route to the API Gateway with Cognito authorisation.
6. THE Foundation_Stack SHALL pass the update state machine ARN as an environment variable to the Project Management Lambda.
7. THE Foundation_Stack SHALL grant the Project Management Lambda `states:StartExecution` permission on the update state machine.

### Requirement 7: Documentation Updates

**User Story:** As an Administrator, I want the documentation to cover the update feature, so that I understand how and when to use it.

#### Acceptance Criteria

1. WHEN the update feature is implemented, THE Platform SHALL update `docs/admin/project-management.md` to document the UPDATING lifecycle state and its transitions.
2. WHEN the update feature is implemented, THE Platform SHALL update `docs/admin/project-management.md` to document the update endpoint, its prerequisites, progress tracking, and error handling.
3. WHEN the update feature is implemented, THE Platform SHALL update `docs/api/reference.md` to document the `POST /projects/{projectId}/update` endpoint including request, response, and error codes.
4. WHEN the update feature is implemented, THE Platform SHALL document that updates do not disrupt running clusters because CloudFormation updates preserve resources with stable logical IDs.
