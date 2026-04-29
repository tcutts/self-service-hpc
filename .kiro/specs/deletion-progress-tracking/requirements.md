# Requirements Document

## Introduction

Cluster and project deletion workflows currently run as Step Functions state machines but provide no progress feedback to the user. Creation and deployment workflows already track progress via `currentStep`, `totalSteps`, and `stepDescription` fields in DynamoDB, with the frontend rendering a progress bar during transitional states. This feature extends the same progress tracking pattern to the DESTROYING status for both clusters and projects, giving users visibility into deletion progress.

## Glossary

- **Cluster_Destruction_Workflow**: The Step Functions state machine that executes the multi-step cluster destruction process (FSx export, PCS resource deletion, PCS cluster deletion, FSx filesystem deletion, IAM cleanup, launch template cleanup, name deregistration, record destroyed).
- **Project_Destroy_Workflow**: The Step Functions state machine that executes the multi-step project destruction process (validate state, start CDK destroy, check destroy status, clear infrastructure, archive project).
- **Progress_Fields**: The set of DynamoDB attributes (`currentStep`, `totalSteps`, `stepDescription`) used to track step-by-step progress of a workflow.
- **Cluster_API**: The Lambda handler that serves cluster CRUD operations via API Gateway, including GET endpoints that return cluster records.
- **Project_API**: The Lambda handler that serves project CRUD operations via API Gateway, including GET endpoints that return project records.
- **Frontend**: The single-page JavaScript application (app.js) that renders cluster and project lists, detail pages, and progress bars.
- **Progress_Bar**: The UI component that displays a labelled bar showing current step, total steps, step description, and percentage complete.
- **Polling**: The periodic GET requests the Frontend makes to refresh data during transitional states.

## Requirements

### Requirement 1: Cluster Destruction Progress Tracking in Backend

**User Story:** As a platform user, I want the cluster destruction workflow to report step-by-step progress, so that I can see how far along the deletion process is.

#### Acceptance Criteria

1. WHEN the Cluster_API receives a DELETE request for a cluster, THE Cluster_API SHALL set the cluster record Progress_Fields to `currentStep: 0`, `totalSteps: N` (where N is the total number of destruction steps), and `stepDescription: "Starting cluster destruction"` before starting the Cluster_Destruction_Workflow.
2. WHEN each step of the Cluster_Destruction_Workflow begins execution, THE Cluster_Destruction_Workflow SHALL update the cluster record Progress_Fields with the current step number and a human-readable step description.
3. THE Cluster_Destruction_Workflow SHALL increment `currentStep` monotonically from 1 through `totalSteps` as each step begins.
4. WHEN the Cluster_Destruction_Workflow completes all steps, THE Cluster_Destruction_Workflow SHALL set the cluster status to DESTROYED and clear the Progress_Fields.
5. IF a step of the Cluster_Destruction_Workflow fails, THEN THE Cluster_Destruction_Workflow SHALL preserve the Progress_Fields at the last successfully started step so the Frontend can display where the failure occurred.

### Requirement 2: Cluster Destruction Progress in API Response

**User Story:** As a frontend developer, I want the cluster GET endpoint to include progress fields when a cluster is in DESTROYING status, so that the UI can render a progress bar.

#### Acceptance Criteria

1. WHEN the Cluster_API returns a cluster record with status DESTROYING, THE Cluster_API SHALL include a `progress` object containing `currentStep`, `totalSteps`, and `stepDescription`.
2. THE Cluster_API SHALL return `currentStep` and `totalSteps` as integer values in the `progress` object.
3. WHEN the Cluster_API returns a cluster record with status other than CREATING or DESTROYING, THE Cluster_API SHALL omit the `progress` object (unless the status is another transitional state).

### Requirement 3: Cluster Destruction Progress Bar in Cluster List

**User Story:** As a platform user, I want to see a progress bar in the cluster list table when a cluster is being destroyed, so that I can monitor deletion progress without navigating to the detail page.

#### Acceptance Criteria

1. WHEN a cluster has status DESTROYING, THE Frontend SHALL render a Progress_Bar in the cluster list table progress column showing the current step, total steps, and step description.
2. WHEN a cluster has status DESTROYING, THE Frontend SHALL display the percentage complete calculated as `Math.round((currentStep / totalSteps) * 100)`.
3. WHEN at least one cluster in the list has status DESTROYING, THE Frontend SHALL activate Polling to refresh the cluster list at the configured poll interval.
4. WHEN no clusters in the list have a transitional status (CREATING or DESTROYING), THE Frontend SHALL stop Polling for the cluster list.

### Requirement 4: Cluster Destruction Progress Bar in Cluster Detail Page

**User Story:** As a platform user, I want to see a progress bar on the cluster detail page when the cluster is being destroyed, so that I can monitor the deletion in detail.

#### Acceptance Criteria

1. WHEN the cluster detail page is loaded for a cluster with status DESTROYING, THE Frontend SHALL render a Progress_Bar showing "Step X of Y: description".
2. WHEN the cluster detail page displays a cluster with status DESTROYING, THE Frontend SHALL activate Polling to refresh the cluster detail at the configured poll interval.
3. WHEN the cluster status transitions from DESTROYING to DESTROYED, THE Frontend SHALL display a toast notification indicating the cluster has been destroyed.
4. WHEN the cluster status transitions from DESTROYING to DESTROYED, THE Frontend SHALL stop Polling for the cluster detail.

### Requirement 5: Project Destruction Progress Bar in Project List

**User Story:** As a platform user, I want to see a progress bar in the project list when a project is being destroyed, so that I can monitor deletion progress.

#### Acceptance Criteria

1. WHILE a project has status DESTROYING, THE Frontend SHALL render a Progress_Bar in the project list table actions column showing the current step, total steps, and step description.
2. WHILE a project has status DESTROYING, THE Frontend SHALL display the percentage complete calculated as `Math.round((currentStep / totalSteps) * 100)`.
3. WHEN at least one project in the list has a transitional status (DEPLOYING, DESTROYING, or UPDATING), THE Frontend SHALL activate Polling to refresh the project list at the configured poll interval.
4. WHEN the project status transitions from DESTROYING to ARCHIVED, THE Frontend SHALL display a toast notification indicating the project has been archived.

### Requirement 6: Project Destruction Progress Tracking Consistency

**User Story:** As a platform user, I want the project destruction workflow to continue reporting accurate progress, so that the progress bar reflects the actual state of the destruction process.

#### Acceptance Criteria

1. THE Project_Destroy_Workflow SHALL update the project record Progress_Fields at the beginning of each step with the current step number and a human-readable step description.
2. THE Project_Destroy_Workflow SHALL use a `totalSteps` value of 5, matching the five destruction steps (validate state, start CDK destroy, check destroy status, clear infrastructure, archive project).
3. IF the Project_Destroy_Workflow fails at any step, THEN THE Project_Destroy_Workflow SHALL preserve the Progress_Fields at the last successfully started step.

### Requirement 7: State Transition Detection for Destruction

**User Story:** As a platform user, I want to be notified when a destruction process completes, so that I know the resource has been removed.

#### Acceptance Criteria

1. WHEN the Frontend detects a cluster status transition from DESTROYING to DESTROYED via Polling, THE Frontend SHALL display a toast notification with the message "Cluster has been destroyed".
2. WHEN the Frontend detects a project status transition from DESTROYING to ARCHIVED via Polling, THE Frontend SHALL display a toast notification with the message "Project has been archived".
3. WHEN the Frontend detects a status transition from DESTROYING to a terminal state, THE Frontend SHALL refresh the relevant list view to reflect the updated state.

### Requirement 9: Concurrent Deletion Prevention

**User Story:** As a platform user, I want the system to prevent multiple users from initiating deletion of the same cluster or project simultaneously, so that duplicate destruction workflows are not started.

#### Acceptance Criteria

1. WHEN the Cluster_API receives a DELETE request for a cluster, THE Cluster_API SHALL use a DynamoDB conditional update to atomically transition the cluster status from ACTIVE or FAILED to DESTROYING, ensuring only one request succeeds if multiple arrive concurrently.
2. IF the conditional update fails because the cluster status has already been changed by another request, THEN THE Cluster_API SHALL return a 409 Conflict error indicating the cluster is already being destroyed.
3. WHEN the Project_API receives a destroy request for a project, THE Project_API SHALL use a DynamoDB conditional update to atomically transition the project status from ACTIVE to DESTROYING, ensuring only one request succeeds if multiple arrive concurrently.
4. IF the conditional update fails because the project status has already been changed by another request, THEN THE Project_API SHALL return a 409 Conflict error indicating the project is already being destroyed.
5. WHEN the Frontend receives a 409 Conflict response for a deletion request, THE Frontend SHALL display a toast notification informing the user that the resource is already being destroyed.

### Requirement 10: Documentation Updates

**User Story:** As a platform user, I want the documentation to describe the deletion progress tracking behaviour, so that I understand what to expect when destroying clusters and projects.

#### Acceptance Criteria

1. THE documentation SHALL describe the progress bar behaviour during cluster destruction, including the number of steps and the types of operations performed.
2. THE documentation SHALL describe the progress bar behaviour during project destruction, including the number of steps and the types of operations performed.
3. THE documentation SHALL describe the concurrent deletion prevention behaviour, including the error message shown when a resource is already being destroyed.
4. THE documentation SHALL be updated in the `docs/` directory alongside the code changes.
