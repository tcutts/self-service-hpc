# Requirements Document

## Introduction

The HPC Self-Service Portal currently requires administrators to perform actions on list items one at a time (e.g., updating project stacks individually). This feature adds multi-select capability to the reusable TableModule and exposes bulk actions on the Projects, Users, and Templates list views, allowing administrators to select multiple items and perform a single action across all of them at once. The backend APIs are extended with batch endpoints that accept arrays of item identifiers and execute the corresponding single-item operations sequentially, returning per-item success/failure results.

## Glossary

- **TableModule**: The reusable vanilla JS module (`frontend/js/table-module.js`) that renders sortable, filterable, scroll-contained tables across all list views. Currently exposes `render(tableId, config, data, container)`, `clearState(tableId)`, and `clearAllState()` as its public API.
- **Bulk_Action_Toolbar**: A UI toolbar that appears above the table when one or more rows are selected, displaying the available bulk actions and the count of selected items.
- **Selection_State**: The internal per-table state tracking which row identifiers are currently selected via checkboxes, managed inside the TableModule IIFE alongside existing sort/filter state.
- **Batch_Endpoint**: A new backend API route that accepts an array of item identifiers and performs the same operation on each item, returning per-item results.
- **Batch_Result**: A JSON response object containing a `results` array of per-item outcomes (each with the item identifier, a `status` field of "success" or "error", and an optional `message` field) and a `summary` object with `total`, `succeeded`, and `failed` counts.
- **Project_Lifecycle**: The state machine governing project status transitions defined in `lambda/project_management/lifecycle.py`: CREATED → DEPLOYING → ACTIVE → UPDATING/DESTROYING; DEPLOYING → CREATED (failure); DESTROYING → ARCHIVED (success) or ACTIVE (failure); UPDATING → ACTIVE (success or failure).
- **Eligible_Item**: An item whose current state permits the requested bulk action (e.g., only ACTIVE projects are eligible for Update or Destroy; only CREATED projects are eligible for Deploy).
- **Portal**: The HPC Self-Service Portal frontend application (`frontend/js/app.js`), a vanilla JS single-page application using Cognito authentication.
- **Administrator**: A user belonging to the Cognito "Administrators" group who has permission to manage projects, users, and templates. Authorization is checked via `is_administrator(event)` in each Lambda handler.
- **Foundation_Stack_Timestamp**: A timestamp recorded in a DynamoDB metadata record in the Projects table (PK=`PLATFORM`, SK=`FOUNDATION_TIMESTAMP`) each time the FoundationStack CDK stack is deployed or updated, used to determine whether project stacks are up to date.
- **Stale_Project**: An ACTIVE project whose `statusChangedAt` field (set by `lifecycle.transition_project()` on each status transition) is earlier than the most recent Foundation_Stack_Timestamp.

## Requirements

### Requirement 1: Row Selection in TableModule

**User Story:** As an administrator, I want to select multiple rows in any table view using checkboxes, so that I can act on several items at once.

#### Acceptance Criteria

1. WHEN a table configuration includes a `selectable` flag set to true, THE TableModule SHALL render a checkbox in the first column of every data row.
2. WHEN a table configuration includes a `selectable` flag set to true, THE TableModule SHALL render a "select all" checkbox in the header of the first column.
3. WHEN the "select all" checkbox is checked, THE TableModule SHALL select all rows that are currently visible after filtering.
4. WHEN the "select all" checkbox is unchecked, THE TableModule SHALL deselect all rows.
5. WHEN an individual row checkbox is toggled, THE TableModule SHALL update the Selection_State for that row's identifier.
6. WHEN all visible rows are individually selected, THE TableModule SHALL check the "select all" checkbox automatically.
7. WHEN any selected row is deselected, THE TableModule SHALL uncheck the "select all" checkbox.
8. WHEN the filter text changes, THE TableModule SHALL preserve the Selection_State of previously selected rows that are no longer visible.
9. THE TableModule SHALL expose a method `getSelectedIds(tableId)` that returns an array of the currently selected row identifiers.
10. THE TableModule SHALL expose a method `clearSelection(tableId)` that deselects all rows and unchecks all checkboxes for the given table.
11. WHEN a table configuration includes a `rowId` property, THE TableModule SHALL use that property as the unique identifier for each row in the Selection_State.

### Requirement 2: Bulk Action Toolbar

**User Story:** As an administrator, I want to see a toolbar showing available bulk actions when I have items selected, so that I know what operations I can perform on my selection.

#### Acceptance Criteria

1. WHEN one or more rows are selected in a selectable table, THE Portal SHALL display a Bulk_Action_Toolbar above the table.
2. THE Bulk_Action_Toolbar SHALL display the count of currently selected items (e.g., "3 selected").
3. WHEN zero rows are selected, THE Portal SHALL hide the Bulk_Action_Toolbar.
4. THE Bulk_Action_Toolbar SHALL contain action buttons specific to the current list view (e.g., "Update All", "Deploy All", "Destroy All" for projects; "Deactivate All", "Reactivate All" for users; "Delete All" for templates).
5. WHEN a bulk action button is clicked, THE Portal SHALL pass the array of selected item identifiers to the corresponding bulk operation handler.
6. THE Bulk_Action_Toolbar SHALL include a "Clear Selection" button that deselects all rows.

### Requirement 3: Bulk Project Update

**User Story:** As an administrator, I want to update multiple ACTIVE projects at once, so that I do not have to trigger stack updates one at a time.

#### Acceptance Criteria

1. WHEN the "Update All" bulk action is triggered with a list of project identifiers, THE Portal SHALL send a single POST request to the Batch_Endpoint for project updates.
2. THE Batch_Endpoint for project updates SHALL accept a JSON body containing an array of project identifiers.
3. FOR EACH project identifier in the request, THE Batch_Endpoint SHALL verify the project exists and has status ACTIVE before initiating the update.
4. FOR EACH Eligible_Item, THE Batch_Endpoint SHALL call `lifecycle.transition_project()` to transition the project to UPDATING status and start the update Step Functions execution.
5. THE Batch_Endpoint SHALL return a Batch_Result containing the outcome for each project identifier.
6. IF a project identifier does not exist or is not in ACTIVE status, THEN THE Batch_Endpoint SHALL include an error entry for that project in the Batch_Result and continue processing the remaining projects.
7. WHEN the Portal receives the Batch_Result, THE Portal SHALL display a summary toast indicating how many projects started updating and how many were skipped or failed.
8. WHEN the bulk update completes, THE Portal SHALL clear the Selection_State and refresh the projects list.
9. AFTER a bulk update is initiated, THE Portal SHALL display independent progress bars for each project that transitioned to UPDATING status, using the same `progress-container compact` markup pattern already used for individual project progress rendering, with each progress bar reflecting that project's own `currentStep`, `totalSteps`, and `stepDescription` from the backend.
10. THE Portal's existing project list polling mechanism (using `projectPollIntervalMs` from config and `projectStatusCache` for transition detection) SHALL update all in-progress project progress bars independently, so that each project's progress advances at its own pace.

### Requirement 4: Bulk Project Deploy

**User Story:** As an administrator, I want to deploy multiple newly created projects at once, so that I can onboard several projects efficiently.

#### Acceptance Criteria

1. WHEN the "Deploy All" bulk action is triggered with a list of project identifiers, THE Portal SHALL send a single POST request to the Batch_Endpoint for project deployments.
2. THE Batch_Endpoint for project deployments SHALL accept a JSON body containing an array of project identifiers.
3. FOR EACH project identifier in the request, THE Batch_Endpoint SHALL verify the project exists and has status CREATED before initiating deployment.
4. FOR EACH Eligible_Item, THE Batch_Endpoint SHALL call `lifecycle.transition_project()` to transition the project to DEPLOYING status and start the deploy Step Functions execution.
5. THE Batch_Endpoint SHALL return a Batch_Result containing the outcome for each project identifier.
6. IF a project identifier does not exist or is not in CREATED status, THEN THE Batch_Endpoint SHALL include an error entry for that project in the Batch_Result and continue processing the remaining projects.
7. WHEN the Portal receives the Batch_Result, THE Portal SHALL display a summary toast indicating how many projects started deploying and how many were skipped or failed.
8. AFTER a bulk deploy is initiated, THE Portal SHALL display independent progress bars for each project that transitioned to DEPLOYING status, using the same `progress-container compact` markup pattern already used for individual project progress rendering, with each progress bar reflecting that project's own `currentStep`, `totalSteps`, and `stepDescription` from the backend.

### Requirement 5: Bulk Project Destroy

**User Story:** As an administrator, I want to destroy multiple ACTIVE projects at once, so that I can decommission projects efficiently.

#### Acceptance Criteria

1. WHEN the "Destroy All" bulk action is triggered, THE Portal SHALL display a confirmation dialog listing the selected project identifiers and requiring the administrator to type "CONFIRM" before proceeding.
2. WHEN the administrator confirms the bulk destroy, THE Portal SHALL send a single POST request to the Batch_Endpoint for project destruction.
3. THE Batch_Endpoint for project destruction SHALL accept a JSON body containing an array of project identifiers.
4. FOR EACH project identifier in the request, THE Batch_Endpoint SHALL verify the project exists, has status ACTIVE, and has no active or creating clusters (checked via `_get_active_clusters()`) before initiating destruction.
5. FOR EACH Eligible_Item, THE Batch_Endpoint SHALL call `lifecycle.transition_project()` to transition the project to DESTROYING status and start the destroy Step Functions execution.
6. THE Batch_Endpoint SHALL return a Batch_Result containing the outcome for each project identifier.
7. IF a project has active or creating clusters or is not in ACTIVE status, THEN THE Batch_Endpoint SHALL include an error entry for that project in the Batch_Result and continue processing the remaining projects.
8. WHEN the Portal receives the Batch_Result, THE Portal SHALL display a summary toast indicating how many projects started destroying and how many were skipped or failed.
9. AFTER a bulk destroy is initiated, THE Portal SHALL display independent progress bars for each project that transitioned to DESTROYING status, using the same `progress-container compact` markup pattern already used for individual project progress rendering, with each progress bar reflecting that project's own `currentStep`, `totalSteps`, and `stepDescription` from the backend.

### Requirement 6: Bulk User Deactivate and Reactivate

**User Story:** As an administrator, I want to deactivate or reactivate multiple users at once, so that I can manage user access efficiently.

#### Acceptance Criteria

1. WHEN the "Deactivate All" bulk action is triggered with a list of user identifiers, THE Portal SHALL send a single POST request to the Batch_Endpoint for user deactivation.
2. THE Batch_Endpoint for user deactivation SHALL accept a JSON body containing an array of user identifiers.
3. FOR EACH user identifier in the request, THE Batch_Endpoint SHALL verify the user exists and has status ACTIVE before deactivating via the existing `deactivate_user()` function.
4. THE Batch_Endpoint SHALL return a Batch_Result containing the outcome for each user identifier.
5. IF a user does not exist or is not in ACTIVE status, THEN THE Batch_Endpoint SHALL include an error entry for that user in the Batch_Result and continue processing the remaining users.
6. WHEN the "Reactivate All" bulk action is triggered with a list of user identifiers, THE Portal SHALL send a single POST request to the Batch_Endpoint for user reactivation.
7. THE Batch_Endpoint for user reactivation SHALL accept a JSON body containing an array of user identifiers.
8. FOR EACH user identifier in the reactivation request, THE Batch_Endpoint SHALL verify the user exists and has status INACTIVE before reactivating via the existing `reactivate_user()` function.
9. THE Batch_Endpoint SHALL return a Batch_Result for the reactivation containing the outcome for each user identifier.
10. IF a user does not exist or is not in INACTIVE status, THEN THE Batch_Endpoint SHALL include an error entry for that user in the Batch_Result and continue processing the remaining users.
11. WHEN the Portal receives a bulk user Batch_Result, THE Portal SHALL display a summary toast and refresh the users list.

### Requirement 7: Bulk Template Delete

**User Story:** As an administrator, I want to delete multiple cluster templates at once, so that I can clean up unused templates efficiently.

#### Acceptance Criteria

1. WHEN the "Delete All" bulk action is triggered, THE Portal SHALL display a confirmation dialog listing the selected template identifiers before proceeding.
2. WHEN the administrator confirms the bulk delete, THE Portal SHALL send a single POST request to the Batch_Endpoint for template deletion.
3. THE Batch_Endpoint for template deletion SHALL accept a JSON body containing an array of template identifiers.
4. FOR EACH template identifier in the request, THE Batch_Endpoint SHALL verify the template exists before deleting via the existing `delete_template()` function.
5. THE Batch_Endpoint SHALL return a Batch_Result containing the outcome for each template identifier.
6. IF a template does not exist, THEN THE Batch_Endpoint SHALL include an error entry for that template in the Batch_Result and continue processing the remaining templates.
7. WHEN the Portal receives the Batch_Result, THE Portal SHALL display a summary toast and refresh the templates list.

### Requirement 8: Project Update Staleness Detection

**User Story:** As an administrator, I want the Update button to be greyed out for projects that are already up to date with the foundation stack, so that I do not trigger unnecessary updates.

#### Acceptance Criteria

1. THE FoundationStack CDK stack SHALL record a Foundation_Stack_Timestamp via a CDK custom resource that writes to the Projects DynamoDB table (PK=`PLATFORM`, SK=`FOUNDATION_TIMESTAMP`) on every deploy or update.
2. THE `list_projects` function in `lambda/project_management/projects.py` SHALL read the Foundation_Stack_Timestamp record from the Projects table and include a `foundationStackTimestamp` field in the GET /projects response alongside the `projects` array.
3. WHEN rendering the projects list, THE Portal SHALL compare each ACTIVE project's `statusChangedAt` (set by `lifecycle.transition_project()` when the project last transitioned to ACTIVE) against the `foundationStackTimestamp` from the API response.
4. IF a project's `statusChangedAt` is equal to or later than the `foundationStackTimestamp`, THEN THE Portal SHALL render the Update button as disabled with a visual indication that the project is up to date (e.g., greyed out with a tooltip "Project is up to date").
5. IF a project's `statusChangedAt` is earlier than the `foundationStackTimestamp`, THEN THE Portal SHALL render the Update button as enabled.
6. THE Bulk_Action_Toolbar "Update All" button SHALL only include Stale_Projects in the batch request, excluding projects that are already up to date.
7. IF all selected ACTIVE projects are up to date, THE "Update All" button in the Bulk_Action_Toolbar SHALL be disabled.

### Requirement 9: Batch Endpoint Response Format

**User Story:** As a developer, I want a consistent response format from all batch endpoints, so that the frontend can handle results uniformly.

#### Acceptance Criteria

1. THE Batch_Endpoint SHALL return HTTP status 200 for all batch requests that were processed, regardless of individual item failures.
2. THE Batch_Result SHALL contain a `results` array where each entry includes the item identifier, a `status` field ("success" or "error"), and an optional `message` field.
3. THE Batch_Result SHALL contain a `summary` object with `total`, `succeeded`, and `failed` integer counts.
4. IF the request body is missing or contains an empty array of identifiers, THEN THE Batch_Endpoint SHALL return HTTP status 400 with a validation error using the same error response format as existing endpoints (i.e., `{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": {}}}`).
5. IF the request body contains more than 25 identifiers, THEN THE Batch_Endpoint SHALL return HTTP status 400 with a validation error indicating the maximum batch size.
6. THE Batch_Endpoint SHALL require Administrator authorization via `is_administrator(event)`, consistent with the corresponding single-item endpoints.

### Requirement 10: Batch Endpoint Error Isolation

**User Story:** As an administrator, I want a single failing item in a batch to not prevent the remaining items from being processed, so that partial progress is made even when some items have issues.

#### Acceptance Criteria

1. IF an individual item operation raises an error during batch processing (e.g., `NotFoundError`, `ConflictError`, `ValidationError`), THEN THE Batch_Endpoint SHALL catch the error, record it in the Batch_Result for that item, and continue processing the remaining items.
2. THE Batch_Endpoint SHALL process items sequentially to avoid overwhelming downstream services (Step Functions, Cognito, DynamoDB).
3. IF an unrecoverable error occurs before any items are processed (e.g., `AuthorisationError` from `is_administrator()` check, malformed request body), THEN THE Batch_Endpoint SHALL return an appropriate HTTP error status without a Batch_Result.

### Requirement 11: Accessibility for Bulk Selection

**User Story:** As an administrator using assistive technology, I want the bulk selection controls to be keyboard-navigable and screen-reader-friendly, so that I can use bulk actions without a mouse.

#### Acceptance Criteria

1. THE TableModule SHALL associate each row checkbox with an accessible label identifying the row item (e.g., `aria-label="Select project my-project"`).
2. THE "select all" checkbox SHALL have an accessible label (e.g., `aria-label="Select all rows"`).
3. THE Bulk_Action_Toolbar SHALL use an ARIA live region so that screen readers announce when the toolbar appears and the selection count changes.
4. THE Bulk_Action_Toolbar action buttons SHALL be keyboard-focusable and activatable via Enter or Space keys.
