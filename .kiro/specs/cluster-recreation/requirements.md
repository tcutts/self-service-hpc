# Requirements Document

## Introduction

This document defines the requirements for adding a cluster recreation capability to the self-service HPC platform. Currently, when a cluster is destroyed, its DynamoDB record transitions to DESTROYED status and the underlying AWS resources (PCS cluster, FSx filesystem, node groups) are deleted. The cluster name remains registered in the ClusterNameRegistry for the same project, and the project's persistent storage (EFS home directories, S3 bucket) is preserved. However, there is no way to re-create a destroyed cluster — users must create a brand-new cluster with a different name or manually reuse the name via the standard creation flow without any awareness of the previous cluster's configuration.

This feature introduces a dedicated "recreate" action for clusters in DESTROYED status. Recreation reuses the original cluster name and template configuration, runs the same Step Functions creation workflow, and produces a fresh set of AWS resources. The destroyed cluster record is replaced by the new CREATING record, preserving a link to the original configuration while making the process seamless for users who want to spin up the same environment again.

## Glossary

- **Cluster_Operations_API**: The Lambda-backed API that handles cluster creation, destruction, listing, and detail retrieval, routed through API Gateway.
- **Cluster_Record**: The DynamoDB item in the Clusters table representing a cluster, keyed by PROJECT#{projectId} / CLUSTER#{clusterName}, containing resource IDs, status, template reference, and timestamps.
- **ClusterNameRegistry**: A DynamoDB table that enforces global uniqueness of cluster names across projects, allowing same-project reuse.
- **Cluster_Status**: The lifecycle state of a cluster, one of: CREATING, ACTIVE, FAILED, DESTROYING, or DESTROYED.
- **Creation_Workflow**: The Step Functions state machine that orchestrates multi-step cluster provisioning (name registration, budget check, FSx creation, PCS cluster creation, node groups, queue, tagging, and DynamoDB recording).
- **Destruction_Workflow**: The Step Functions state machine that orchestrates cluster teardown (FSx export, PCS resource deletion, FSx deletion, DynamoDB status update).
- **Project_User**: A user authorised to create, destroy, and access clusters within a project (includes Project Administrators and platform Administrators).
- **Project_Administrator**: A user who owns a project, manages membership and budgets, and has all Project_User rights.
- **Administrator**: A platform-level user who manages users, projects, and cluster templates.
- **Web_Portal**: The web-based interface for the HPC platform, implemented as a vanilla JS single-page application with Cognito authentication.
- **Budget_Breach**: A state where the project's spending has exceeded its configured budget limit, blocking cluster creation and access.
- **Template**: A predefined cluster configuration specifying instance types, node counts, and software configuration, stored in the Templates DynamoDB table.

## Requirements

### Requirement 1: Recreate Cluster API Endpoint

**User Story:** As a Project_User, I want to recreate a previously destroyed cluster, so that I can quickly restore the same environment without manually re-entering the cluster name and template configuration.

#### Acceptance Criteria

1. WHEN a Project_User sends a POST request to /projects/{projectId}/clusters/{clusterName}/recreate, THE Cluster_Operations_API SHALL initiate recreation of the specified cluster.
2. THE Cluster_Operations_API SHALL accept an optional templateId field in the request body to allow overriding the original template.
3. IF the request body does not include a templateId, THEN THE Cluster_Operations_API SHALL use the templateId stored in the destroyed Cluster_Record.
4. WHEN recreation is initiated successfully, THE Cluster_Operations_API SHALL return HTTP 202 Accepted with the projectId, clusterName, and templateId in the response body.
5. THE Cluster_Operations_API SHALL start the same Creation_Workflow Step Functions execution used for new cluster creation, passing the projectId, clusterName, templateId, and the caller identity as createdBy.

### Requirement 2: Cluster Status Validation for Recreation

**User Story:** As a Project_User, I want the platform to prevent recreation of clusters that are not in DESTROYED status, so that I do not accidentally interfere with active or in-progress clusters.

#### Acceptance Criteria

1. WHEN a recreation request is received, THE Cluster_Operations_API SHALL retrieve the existing Cluster_Record for the specified projectId and clusterName.
2. IF the Cluster_Record does not exist, THEN THE Cluster_Operations_API SHALL return HTTP 404 Not Found with error code NOT_FOUND.
3. IF the Cluster_Record status is not DESTROYED, THEN THE Cluster_Operations_API SHALL return HTTP 409 Conflict with error code CONFLICT and a message indicating the current status and that only DESTROYED clusters can be recreated.
4. IF the Cluster_Record status is DESTROYED, THEN THE Cluster_Operations_API SHALL proceed with the recreation workflow.

### Requirement 3: Budget Enforcement for Recreation

**User Story:** As a Project_Administrator, I want cluster recreation to respect budget limits, so that destroyed clusters cannot be recreated when the project budget has been exceeded.

#### Acceptance Criteria

1. WHEN a recreation request is received for a cluster in DESTROYED status, THE Cluster_Operations_API SHALL check the project budget breach status using a consistent DynamoDB read.
2. IF the project budget is breached, THEN THE Cluster_Operations_API SHALL return HTTP 403 Forbidden with error code BUDGET_EXCEEDED and a message indicating that recreation is blocked until the budget is resolved.
3. IF the project budget is not breached, THEN THE Cluster_Operations_API SHALL proceed with the recreation workflow.

### Requirement 4: Authorisation for Recreation

**User Story:** As a platform operator, I want cluster recreation to enforce the same authorisation rules as cluster creation, so that only authorised project members can recreate clusters.

#### Acceptance Criteria

1. WHEN a recreation request is received, THE Cluster_Operations_API SHALL verify that the caller is a Project_User, Project_Administrator, or Administrator for the specified project.
2. IF the caller is not authorised for the project, THEN THE Cluster_Operations_API SHALL return HTTP 403 Forbidden with error code AUTHORISATION_ERROR.

### Requirement 5: Cluster Record Transition on Recreation

**User Story:** As a Project_User, I want the recreated cluster to replace the destroyed record seamlessly, so that I can monitor creation progress and access the new cluster using the same cluster name.

#### Acceptance Criteria

1. WHEN the Creation_Workflow begins for a recreated cluster, THE Creation_Workflow SHALL overwrite the existing DESTROYED Cluster_Record with a new record in CREATING status.
2. THE new Cluster_Record SHALL include the clusterName, projectId, templateId, createdBy (the user who initiated recreation), createdAt timestamp, and progress tracking fields (currentStep, totalSteps, stepDescription).
3. WHEN the Creation_Workflow completes successfully, THE Creation_Workflow SHALL update the Cluster_Record to ACTIVE status with all resource identifiers (pcsClusterId, fsxFilesystemId, loginNodeGroupId, computeNodeGroupId, queueId, loginNodeIp).
4. IF the Creation_Workflow fails, THEN THE Creation_Workflow SHALL update the Cluster_Record to FAILED status with the error message, following the same rollback procedure as new cluster creation.

### Requirement 6: Cluster Name Registry Compatibility

**User Story:** As a Project_User, I want cluster recreation to work with the existing name registry, so that the same cluster name remains reserved for my project without conflicts.

#### Acceptance Criteria

1. WHEN the Creation_Workflow runs the name registration step for a recreated cluster, THE ClusterNameRegistry SHALL accept the registration because the name is already registered to the same project.
2. THE Cluster_Operations_API SHALL NOT require the cluster name to be deregistered before recreation.

### Requirement 7: Web Portal Recreate Action

**User Story:** As a Project_User, I want to see a "Recreate" button for destroyed clusters in the web portal, so that I can trigger recreation without using the API directly.

#### Acceptance Criteria

1. WHEN the Web_Portal displays a cluster list for a project, THE Web_Portal SHALL show a "Recreate" action button for each cluster in DESTROYED status.
2. WHEN a Project_User clicks the "Recreate" button, THE Web_Portal SHALL send a POST request to /projects/{projectId}/clusters/{clusterName}/recreate using the stored templateId from the destroyed Cluster_Record.
3. WHEN the recreation request returns HTTP 202, THE Web_Portal SHALL update the cluster status display to CREATING and begin polling for progress updates.
4. IF the recreation request returns an error, THEN THE Web_Portal SHALL display the error message to the user.
5. WHILE a cluster is in DESTROYED status, THE Web_Portal SHALL NOT display the "Recreate" button if the project budget is breached.

### Requirement 8: API Documentation Update

**User Story:** As a developer integrating with the platform, I want the API reference and cluster management documentation to describe the recreate endpoint, so that I can use it correctly.

#### Acceptance Criteria

1. THE cluster management documentation in docs/project-admin/cluster-management.md SHALL include a section describing the recreate endpoint, its request format, response format, and error cases.
2. THE API reference documentation in docs/api/reference.md SHALL list the POST /projects/{projectId}/clusters/{clusterName}/recreate endpoint with request and response schemas.
3. THE cluster status lifecycle diagram in the documentation SHALL be updated to show the DESTROYED to CREATING transition via recreation.
