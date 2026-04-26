# Requirements Document

## Introduction

This document defines the requirements for enhancing the self-service HPC platform with a full project lifecycle management system. Currently, project creation writes a DynamoDB record with status "ACTIVE" and empty infrastructure fields, but no infrastructure is deployed at creation time. This feature introduces explicit lifecycle states (CREATED, DEPLOYING, ACTIVE, DESTROYING, ARCHIVED), context-sensitive UI actions tied to those states, persistent project selection context across pages, constrained project editing (budget-only), immediate budget enforcement on change, and selectable budget types (monthly or total project lifetime).

## Glossary

- **Web_Portal**: The web-based administration and access interface for the HPC platform, implemented as a vanilla JS single-page application with Cognito authentication.
- **Administrator**: A platform-level user who manages users, projects, and cluster templates.
- **Project_Administrator**: A user who owns a project, manages project membership, sets budget limits, and has all Project_User rights.
- **Project_User**: A user authorised to create and destroy clusters within a project, and to log into and use those clusters.
- **Project**: A logical grouping of clusters, storage, and users aligned with a business need, stored as a DynamoDB record with infrastructure references and lifecycle state.
- **Project_Status**: The current lifecycle state of a Project, one of: CREATED, DEPLOYING, ACTIVE, DESTROYING, or ARCHIVED.
- **Project_Infrastructure**: The set of AWS resources provisioned for a project via the ProjectInfrastructureStack CDK stack, including VPC, EFS, S3 bucket, security groups, and CloudWatch log groups.
- **Budget_Type**: The time scope of a project budget, either MONTHLY (resets each calendar month) or TOTAL (covers the entire project lifetime).
- **Budget_Alert**: An AWS Budgets alert associated with a project cost allocation tag that notifies when spending approaches or exceeds the project budget limit.
- **Cost_Allocation_Tag**: An AWS tag applied to all resources within a project, used for cost tracking and budget enforcement.
- **Project_Context**: The currently selected project, persisted in the browser across page navigations so that users do not need to re-enter the project identifier on each page.
- **Confirmation_Input**: A UI pattern requiring the user to type a specific value (such as the project identifier) to confirm a destructive action.

## Requirements

### Requirement 1: Project Lifecycle States

**User Story:** As an Administrator, I want projects to have explicit lifecycle states reflecting their infrastructure provisioning status, so that I can see whether a project is ready to use, still deploying, or has been torn down.

#### Acceptance Criteria

1. WHEN an Administrator creates a new project, THE Web_Portal SHALL store the project record with Project_Status set to CREATED, empty infrastructure fields (vpcId, efsFileSystemId, s3BucketName, cdkStackName), and a default budgetLimit of $50 with Budget_Type MONTHLY.
2. THE Web_Portal SHALL support the following Project_Status values: CREATED, DEPLOYING, ACTIVE, DESTROYING, and ARCHIVED.
3. WHEN a project is in CREATED status, THE Web_Portal SHALL display the project in the project list with a status badge indicating that infrastructure has not been deployed.
4. WHEN a project transitions from one Project_Status to another, THE Web_Portal SHALL update the project record with the new status and a timestamp of the transition.
5. IF an API consumer requests a status transition that is not valid for the current Project_Status, THEN THE Web_Portal SHALL reject the request and return a descriptive error message listing the valid transitions from the current state.
6. THE Web_Portal SHALL permit the following state transitions: CREATED to DEPLOYING, DEPLOYING to ACTIVE, DEPLOYING to CREATED (on deployment failure), ACTIVE to DESTROYING, DESTROYING to ARCHIVED, and DESTROYING to ACTIVE (on destruction failure).

### Requirement 2: Project Infrastructure Deployment

**User Story:** As an Administrator, I want to deploy project infrastructure on demand after creating the project record, so that I can review and configure the project before committing cloud resources.

#### Acceptance Criteria

1. WHEN an Administrator triggers infrastructure deployment for a project in CREATED status, THE Web_Portal SHALL transition the Project_Status to DEPLOYING and initiate provisioning of the Project_Infrastructure via the ProjectInfrastructureStack.
2. WHEN the ProjectInfrastructureStack deployment completes successfully, THE Web_Portal SHALL update the project record with the provisioned resource identifiers (vpcId, efsFileSystemId, s3BucketName, cdkStackName) and transition the Project_Status to ACTIVE.
3. IF the ProjectInfrastructureStack deployment fails, THEN THE Web_Portal SHALL transition the Project_Status back to CREATED and store the failure reason in the project record.
4. IF an Administrator attempts to deploy infrastructure for a project that is not in CREATED status, THEN THE Web_Portal SHALL reject the request and return a descriptive error message.
5. WHILE a project is in DEPLOYING status, THE Web_Portal SHALL display the current deployment step, a description of what is being provisioned, and the progress as "step N of M" so that the user can see that deployment is progressing.
6. THE Web_Portal SHALL update the project record with the current step number, total steps, and step description as each deployment step begins, so that the UI can poll for and display progress.
7. THE Web_Portal SHALL allow the user to navigate away from the project list page and return later to see the current deployment progress.

### Requirement 3: Project Infrastructure Destruction

**User Story:** As an Administrator, I want to destroy a project's infrastructure when it is no longer needed, so that cloud resources are released and costs are minimised.

#### Acceptance Criteria

1. WHEN an Administrator triggers infrastructure destruction for a project in ACTIVE status, THE Web_Portal SHALL verify that no active clusters exist in the project before proceeding.
2. IF active clusters exist in the project, THEN THE Web_Portal SHALL reject the destruction request and list the active clusters that must be destroyed first.
3. WHEN destruction is confirmed and no active clusters exist, THE Web_Portal SHALL transition the Project_Status to DESTROYING and initiate teardown of the Project_Infrastructure.
4. WHEN the infrastructure teardown completes successfully, THE Web_Portal SHALL transition the Project_Status to ARCHIVED and clear the infrastructure resource identifiers from the project record.
5. IF the infrastructure teardown fails, THEN THE Web_Portal SHALL transition the Project_Status back to ACTIVE and store the failure reason in the project record.
6. IF an Administrator attempts to destroy infrastructure for a project that is not in ACTIVE status, THEN THE Web_Portal SHALL reject the request and return a descriptive error message.
7. WHILE a project is in DESTROYING status, THE Web_Portal SHALL display the current teardown step, a description of what is being removed, and the progress as "step N of M" so that the user can see that destruction is progressing.
8. THE Web_Portal SHALL update the project record with the current step number, total steps, and step description as each teardown step begins, so that the UI can poll for and display progress.

### Requirement 4: Context-Sensitive Project Actions in UI

**User Story:** As an Administrator, I want the project list to show context-sensitive actions based on each project's lifecycle state, so that I can only perform operations that are valid for the project's current state.

#### Acceptance Criteria

1. THE Web_Portal SHALL display the current Project_Status as a status badge for each project in the project list.
2. WHEN a project is in CREATED status, THE Web_Portal SHALL display a "Deploy" action button for that project.
3. WHEN a project is in ACTIVE status, THE Web_Portal SHALL display "Edit" and "Destroy" action buttons for that project.
4. WHEN a project is in DEPLOYING or DESTROYING status, THE Web_Portal SHALL disable all action buttons for that project and display the current step progress (step N of M with a description of the current operation).
5. WHEN a project is in ARCHIVED status, THE Web_Portal SHALL NOT display any action buttons for that project.
6. WHEN an Administrator clicks the "Destroy" action for a project, THE Web_Portal SHALL display a Confirmation_Input dialog requiring the Administrator to type the project identifier before the destruction proceeds.
7. IF the Administrator types a value that does not match the project identifier in the Confirmation_Input dialog, THEN THE Web_Portal SHALL keep the confirmation button disabled.

### Requirement 5: Persistent Project Selection Context

**User Story:** As a Project_User, I want the currently selected project to persist across page navigations, so that I do not have to re-enter the project identifier each time I visit the cluster operations page.

#### Acceptance Criteria

1. THE Web_Portal SHALL display the currently selected Project_Context in a visible location that is accessible from all pages after authentication.
2. WHEN a user selects a project from the project list or sets a project on the cluster operations page, THE Web_Portal SHALL store the selection as the active Project_Context.
3. WHEN a user navigates to the cluster operations page, THE Web_Portal SHALL pre-populate the project identifier field with the active Project_Context value.
4. THE Web_Portal SHALL persist the Project_Context across page navigations within the same browser session.
5. WHEN a user changes the Project_Context, THE Web_Portal SHALL update the displayed context indicator and use the new project for subsequent operations.
6. IF no Project_Context has been selected, THEN THE Web_Portal SHALL display a prompt indicating that no project is selected.

### Requirement 6: Project Editing Constraints

**User Story:** As a Project_Administrator, I want to edit my project's budget while seeing other project properties as read-only, so that I can adjust spending limits without accidentally modifying immutable project attributes.

#### Acceptance Criteria

1. WHEN a Project_Administrator opens the project edit view for an ACTIVE project, THE Web_Portal SHALL display all project properties including projectId, projectName, costAllocationTag, and budgetLimit.
2. THE Web_Portal SHALL render the projectId, projectName, and costAllocationTag fields as disabled (greyed out) inputs to indicate they are not editable.
3. THE Web_Portal SHALL render the budgetLimit field and Budget_Type selector as editable inputs.
4. WHEN a Project_Administrator submits the edit form, THE Web_Portal SHALL update only the budgetLimit and Budget_Type values in the project record.
5. IF a Project_Administrator attempts to edit a project that is not in ACTIVE status, THEN THE Web_Portal SHALL reject the request and return a descriptive error message.
6. IF a user who is not a Project_Administrator for the target project attempts to edit the project, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 7: Immediate Budget Application

**User Story:** As a Project_Administrator, I want budget changes to take effect immediately, so that users whose access was blocked by a budget breach can resume work as soon as the budget is increased.

#### Acceptance Criteria

1. WHEN a Project_Administrator increases the budgetLimit to a value above the current spending, THE Web_Portal SHALL clear the budgetBreached flag on the project record immediately within the same API request.
2. WHEN the budgetBreached flag is cleared, THE Web_Portal SHALL restore cluster creation and cluster access for the project without waiting for the AWS Budgets asynchronous evaluation cycle.
3. THE Web_Portal SHALL compare the new budgetLimit against the current actual spending reported by AWS Cost Explorer or the stored budget state to determine whether to clear the budgetBreached flag.
4. IF the new budgetLimit is still below or equal to the current spending, THEN THE Web_Portal SHALL retain the budgetBreached flag and inform the Project_Administrator that the budget remains exceeded.
5. WHEN the budgetBreached flag is cleared by a budget increase, THE Web_Portal SHALL log the event including the project identifier, previous budget limit, new budget limit, and the user who made the change.

### Requirement 8: Budget Type Selection

**User Story:** As a Project_Administrator, I want to choose between a monthly budget and a total project budget, so that I can align cost controls with the project's funding model.

#### Acceptance Criteria

1. WHEN a Project_Administrator sets or updates a budget, THE Web_Portal SHALL accept a Budget_Type parameter with a value of MONTHLY or TOTAL.
7. THE Web_Portal SHALL reject any budgetLimit value of zero or less and return a descriptive error message indicating that a positive budget is required.
2. WHEN Budget_Type is MONTHLY, THE Web_Portal SHALL create or update the AWS Budget with TimeUnit set to MONTHLY so that the budget resets each calendar month.
3. WHEN Budget_Type is TOTAL, THE Web_Portal SHALL create or update the AWS Budget with TimeUnit set to ANNUALLY and a TimePeriod spanning from the project creation date to a date far in the future, so that the budget covers the entire project lifetime without resetting.
4. THE Web_Portal SHALL store the selected Budget_Type in the project DynamoDB record.
5. WHEN a Project_Administrator changes the Budget_Type for an existing budget, THE Web_Portal SHALL recreate the AWS Budget with the new time configuration.
6. IF a Project_Administrator does not specify a Budget_Type, THEN THE Web_Portal SHALL default to MONTHLY to maintain backward compatibility with existing projects.
