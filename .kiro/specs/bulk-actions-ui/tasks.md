# Implementation Plan: Bulk Actions UI

## Overview

This plan implements multi-select capability in the `TableModule` (`frontend/js/table-module.js`), bulk action toolbars for Projects/Users/Templates views in `frontend/js/app.js`, batch API endpoints in each Lambda handler (`lambda/project_management/handler.py`, `lambda/user_management/handler.py`, `lambda/template_management/handler.py`), foundation stack timestamp for staleness detection via a CDK custom resource in `FoundationStack` (`lib/foundation-stack.ts`), and corresponding API Gateway routes. Tasks are ordered so each builds on the previous: backend batch logic first, then infrastructure wiring, then frontend selection, then bulk UI, then staleness detection.

                                                                                                                                                                                                                                                                                                                                                                            ## Tasks

- [x] 1. Implement batch processing helpers and batch endpoints in project management Lambda
  - [x] 1.1 Add batch handler functions to `lambda/project_management/handler.py`
    - Add `_handle_batch_update`, `_handle_batch_deploy`, `_handle_batch_destroy` functions
    - Each validates admin auth via `is_administrator(event)` (imported from `auth.py`), parses body for `projectIds` array, validates non-empty and max 25; on validation failure raise `ValidationError` (from `lambda/project_management/errors.py`) which is caught by the existing exception handler and formatted via `build_error_response()` as `{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": {}}}`
    - `_handle_batch_update`: For each project ID sequentially, call `get_project(table_name=PROJECTS_TABLE_NAME, project_id=pid)` to verify ACTIVE status, then `lifecycle.transition_project(table_name=PROJECTS_TABLE_NAME, project_id=pid, target_status="UPDATING")`, then `sfn_client.start_execution(stateMachineArn=PROJECT_UPDATE_STATE_MACHINE_ARN, ...)` — mirroring existing `_handle_update_project()`
    - `_handle_batch_deploy`: For each project ID sequentially, call `get_project()` to verify CREATED status, then `lifecycle.transition_project(..., target_status="DEPLOYING")`, then `sfn_client.start_execution(stateMachineArn=PROJECT_DEPLOY_STATE_MACHINE_ARN, ...)` — mirroring existing `_handle_deploy_project()`
    - `_handle_batch_destroy`: For each project ID sequentially, call `get_project()` to verify ACTIVE status, then `_get_active_clusters(CLUSTERS_TABLE_NAME, pid)` (from `lambda/project_management/projects.py`) to check for both ACTIVE and CREATING clusters, then `lifecycle.transition_project(..., target_status="DESTROYING")`, then `sfn_client.start_execution(stateMachineArn=PROJECT_DESTROY_STATE_MACHINE_ARN, ...)` — mirroring existing `_handle_destroy_project_infra()`
    - Each item operation wrapped in `try/except` catching `ApiError` subclasses (`NotFoundError`, `ConflictError`, `ValidationError` from `lambda/project_management/errors.py`); failures recorded as `{"id": pid, "status": "error", "message": str(exc)}` and processing continues
    - Returns `BatchResult` with `results` array and `summary` object (`total`, `succeeded`, `failed`) with HTTP 200
    - Add routing for `POST /projects/batch/update`, `POST /projects/batch/deploy`, `POST /projects/batch/destroy` in the `handler()` function's routing block
    - _Requirements: 3.1–3.10, 4.1–4.8, 5.1–5.9, 9.1–9.6, 10.1–10.3_

  - [x] 1.2 Write property test for batch project eligibility (Property 7)
    - **Property 7: Batch project eligibility — only projects in the required status succeed**
    - Generate projects with random statuses from `VALID_TRANSITIONS` in `lambda/project_management/lifecycle.py` (CREATED, DEPLOYING, ACTIVE, UPDATING, DESTROYING, ARCHIVED), call batch update/deploy/destroy handlers with mocked DynamoDB and Step Functions, verify only eligible projects get `"success"` entries (ACTIVE for update/destroy, CREATED for deploy); destroy also checks `_get_active_clusters()` returns empty
    - Use `@settings(max_examples=20)` for Hypothesis
    - Test file: `test/lambda/test_batch_project_eligibility.py`
    - **Validates: Requirements 3.3, 3.6, 4.3, 4.6, 5.4, 5.7**

  - [x] 1.3 Write property test for batch response format consistency (Property 6)
    - **Property 6: Batch response format consistency**
    - Generate random ID arrays (1–25 items), mock single-item operations with random success/failure, verify response has exactly N results, each with `id`/`status`/`message` fields, `status` is `"success"` or `"error"`, and `summary.total == N`, `summary.succeeded + summary.failed == N`
    - Use `@settings(max_examples=20)` for Hypothesis
    - Test file: `test/lambda/test_batch_response_format.py`
    - **Validates: Requirements 3.5, 4.5, 5.6, 9.1, 9.2, 9.3**

  - [x] 1.4 Write property test for batch error isolation (Property 10)
    - **Property 10: Batch error isolation — failures do not block remaining items**
    - Generate batches where some items raise `NotFoundError`, `ConflictError`, or `ValidationError` (from `lambda/project_management/errors.py`), verify all N items are processed and exactly N result entries returned regardless of failures
    - Use `@settings(max_examples=20)` for Hypothesis
    - Test file: `test/lambda/test_batch_error_isolation.py`
    - **Validates: Requirements 10.1**

- [x] 2. Implement batch endpoints in user management Lambda
  - [x] 2.1 Add batch handler functions to `lambda/user_management/handler.py`
    - Add `_handle_batch_deactivate` and `_handle_batch_reactivate` functions
    - Each validates admin auth via `is_administrator(event)` (imported from `lambda/user_management/auth.py`), parses body for `userIds` array, validates non-empty and max 25; on validation failure raise `ValidationError` (from `lambda/user_management/errors.py`) formatted via `build_error_response()` as `{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": {}}}`
    - `_handle_batch_deactivate`: For each user ID sequentially, call `deactivate_user(table_name=USERS_TABLE_NAME, user_pool_id=USER_POOL_ID, user_id=uid)` from `lambda/user_management/users.py` — which verifies user exists, sets DynamoDB status to INACTIVE, and disables the Cognito user
    - `_handle_batch_reactivate`: For each user ID sequentially, call `reactivate_user(table_name=USERS_TABLE_NAME, user_pool_id=USER_POOL_ID, user_id=uid)` from `lambda/user_management/users.py` — which verifies user exists and is INACTIVE, sets DynamoDB status to ACTIVE, and re-enables the Cognito user
    - Each item operation wrapped in `try/except` catching `ApiError` subclasses (`NotFoundError`, `ValidationError` from `lambda/user_management/errors.py`); failures recorded and processing continues
    - Returns `BatchResult` with `results` array and `summary` object with HTTP 200
    - Add routing for `POST /users/batch/deactivate`, `POST /users/batch/reactivate` in the `handler()` function's routing block
    - _Requirements: 6.1–6.11, 9.1–9.6, 10.1–10.3_

  - [x] 2.2 Write property test for batch user eligibility (Property 8)
    - **Property 8: Batch user eligibility — only users in the required status succeed**
    - Generate users with random statuses (ACTIVE/INACTIVE), call batch deactivate/reactivate with mocked DynamoDB and Cognito, verify `deactivate_user()` succeeds only for ACTIVE users and `reactivate_user()` succeeds only for INACTIVE users (which raises `ValidationError` for already-active users)
    - Use `@settings(max_examples=20)` for Hypothesis
    - Test file: `test/lambda/test_batch_user_eligibility.py`
    - **Validates: Requirements 6.3, 6.5, 6.8, 6.10**

- [x] 3. Implement batch endpoint in template management Lambda
  - [x] 3.1 Add batch handler function to `lambda/template_management/handler.py`
    - Add `_handle_batch_delete` function
    - Validates admin auth via `is_administrator(event)` (imported from `lambda/template_management/auth.py`), parses body for `templateIds` array, validates non-empty and max 25; on validation failure raise `ValidationError` (from `lambda/template_management/errors.py`) formatted via `build_error_response()` as `{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": {}}}`
    - For each template ID sequentially, call `delete_template(table_name=TEMPLATES_TABLE_NAME, template_id=tid)` from `lambda/template_management/templates.py` — which calls `get_template()` to verify existence before deleting
    - Each item operation wrapped in `try/except` catching `ApiError` subclasses (`NotFoundError` from `lambda/template_management/errors.py`); failures recorded and processing continues
    - Returns `BatchResult` with `results` array and `summary` object with HTTP 200
    - Add routing for `POST /templates/batch/delete` in the `handler()` function's routing block
    - _Requirements: 7.1–7.7, 9.1–9.6, 10.1–10.3_

  - [x] 3.2 Write property test for batch template eligibility (Property 9)
    - **Property 9: Batch template eligibility — only existing templates succeed**
    - Generate mix of existing and non-existing template IDs, call `_handle_batch_delete` with mocked DynamoDB, verify only existing templates (where `get_template()` does not raise `NotFoundError`) get `"success"` entries
    - Use `@settings(max_examples=20)` for Hypothesis
    - Test file: `test/lambda/test_batch_template_eligibility.py`
    - **Validates: Requirements 7.4, 7.6**

- [x] 4. Checkpoint — Verify all backend batch endpoints
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Add foundation stack timestamp and update list_projects response
  - [x] 5.1 Add CDK custom resource for foundation stack timestamp in `lib/foundation-stack.ts`
    - In the `FoundationStack` class constructor, add a `cr.AwsCustomResource` with `onUpdate` that calls DynamoDB `putItem` to write `{ PK: { S: "PLATFORM" }, SK: { S: "FOUNDATION_TIMESTAMP" }, timestamp: { S: new Date().toISOString() } }` to `this.projectsTable`
    - Use `cr.PhysicalResourceId.of('FoundationStackTimestamp-' + Date.now())` to force execution on every CDK deploy/update
    - Grant DynamoDB write access via `cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [this.projectsTable.tableArn] })`
    - _Requirements: 8.1_

  - [x] 5.2 Update `list_projects()` in `lambda/project_management/projects.py` to include foundation timestamp
    - After scanning projects, also read the record with `PK="PLATFORM", SK="FOUNDATION_TIMESTAMP"` from the same Projects table via `table.get_item(Key={"PK": "PLATFORM", "SK": "FOUNDATION_TIMESTAMP"})`
    - Extract the `timestamp` field from the record
    - Update `_handle_list_projects()` in `lambda/project_management/handler.py` to return `{"projects": [...], "foundationStackTimestamp": "..."}` instead of just `{"projects": [...]}`
    - Handle missing timestamp record gracefully (return `null` if not found)
    - _Requirements: 8.2_

  - [x] 5.3 Write unit tests for foundation timestamp in list_projects response
    - Test that the response includes `foundationStackTimestamp` when the `PK=PLATFORM, SK=FOUNDATION_TIMESTAMP` record exists in the Projects table
    - Test that the response handles missing timestamp record gracefully (returns `null`)
    - Test file: `test/lambda/test_foundation_timestamp.py`
    - _Requirements: 8.2_

- [x] 6. Add API Gateway routes for batch endpoints in `lib/foundation-stack.ts`
  - In the `FoundationStack` class constructor, add `batch` sub-resource under the existing `projectsResource` with `update`, `deploy`, `destroy` child resources, each with POST method wired to `projectManagementIntegration` with Cognito auth via `this.cognitoAuthorizer`
  - Add `batch` sub-resource under the existing `usersResource` with `deactivate`, `reactivate` child resources, each with POST method wired to `userManagementIntegration` with Cognito auth
  - Add `batch` sub-resource under the existing `templatesResource` with `delete` child resource, with POST method wired to `templateManagementIntegration` with Cognito auth
  - _Requirements: 3.1, 4.1, 5.2, 6.1, 6.6, 7.2, 9.6_

- [x] 7. Checkpoint — Verify infrastructure and backend integration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Extend TableModule with selection support in `frontend/js/table-module.js`
  - [x] 8.1 Add selection state and checkbox rendering to TableModule
    - Add `selectedIds: new Set()` to per-table state in the `getState()` function (alongside existing `sortColumn`, `sortDirection`, `filterText` in the `tableStates` map)
    - When config has `selectable: true` and `rowId`, prepend a checkbox column to header (select-all) and each body row in `buildThead()` and `buildTbody()`
    - Select-all checkbox: `aria-label="Select all rows"`, checks/unchecks all visible (post-filter) rows
    - Row checkbox: `aria-label="Select {rowId} {value}"` where the entity type comes from the `rowId` property name (e.g., `"Select projectId my-project"`)
    - Individual toggle: add/remove from `selectedIds` Set, invoke `config.onSelectionChange(Array.from(selectedIds))`
    - Select-all toggle: add/remove all visible row IDs from `selectedIds`, invoke callback
    - Auto-check select-all when all visible rows are individually selected; uncheck when any deselected
    - Filter change in the existing filter input handler: preserve `selectedIds` of hidden rows, re-render checkboxes for visible rows reflecting current selection state
    - Add public methods `getSelectedIds(tableId)` returning `Array.from(selectedIds)` and `clearSelection(tableId)` that clears the Set and unchecks all checkboxes
    - Export new methods and internals via the existing `_internals` object and `TableModule` public API for testing
    - _Requirements: 1.1–1.11, 11.1, 11.2_

  - [x] 8.2 Write property test for selection rendering (Property 1)
    - **Property 1: Selection rendering matches data rows**
    - Generate random data arrays, render with `selectable: true` and a `rowId` property, verify checkbox count equals row count and each checkbox's associated identifier matches the `rowId` value for that row
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/table-module-selection.property.test.js`
    - **Validates: Requirements 1.1, 1.11**

  - [x] 8.3 Write property test for select-all (Property 2)
    - **Property 2: Select-all selects exactly the visible rows**
    - Generate random data and filter text, check select-all, verify `getSelectedIds()` returns exactly the set of visible row IDs (union with previously selected hidden rows)
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/table-module-selection.property.test.js`
    - **Validates: Requirements 1.3**

  - [x] 8.4 Write property test for individual toggle (Property 3)
    - **Property 3: Individual toggle updates selection state**
    - Generate random data, toggle a random row's checkbox, verify it is added/removed from `getSelectedIds()`
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/table-module-selection.property.test.js`
    - **Validates: Requirements 1.5**

  - [x] 8.5 Write property test for filter preserving selection (Property 4)
    - **Property 4: Filter preserves selection of hidden rows**
    - Generate data, select some rows, change filter to hide some selected rows, verify no selected IDs are lost from `getSelectedIds()`
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/table-module-selection.property.test.js`
    - **Validates: Requirements 1.8**

  - [x] 8.6 Write property test for accessibility labels (Property 13)
    - **Property 13: Accessibility labels on row checkboxes**
    - Generate random data, render with `selectable: true`, verify each row checkbox has an `aria-label` attribute containing the row's identifier value
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/table-module-selection.property.test.js`
    - **Validates: Requirements 11.1**

  - [x] 8.7 Write unit tests for TableModule selection
    - Test select-all auto-check/uncheck behavior
    - Test `clearSelection()` method clears the `selectedIds` Set and unchecks all checkboxes
    - Test `getSelectedIds()` returns correct array
    - Test that non-selectable tables (no `selectable: true` in config) have no checkboxes
    - Test file: `test/frontend/table-module-selection.test.js`
    - _Requirements: 1.1–1.11_

- [x] 9. Implement bulk action toolbar and handlers in `frontend/js/app.js`
  - [x] 9.1 Add bulk action toolbar rendering and selection callbacks for all three pages
    - Update `usersTableConfig`, `projectsTableConfig`, `templatesTableConfig` (defined at the top of `app.js`) to include `selectable: true`, `rowId` (e.g., `'projectId'`, `'userId'`, `'templateId'`), and `onSelectionChange` callback
    - In each page renderer (`renderProjectsPage`, `renderUsersPage`, `renderTemplatesPage` in `app.js`), add a `<div class="bulk-action-toolbar" role="toolbar" aria-label="Bulk actions" aria-live="polite" style="display:none">` above the table container
    - Show/hide toolbar based on selection count via the `onSelectionChange` callback; display `"{N} selected"` count in a `<span class="bulk-selection-count">`
    - Projects toolbar buttons: "Deploy All", "Update All", "Destroy All", "Clear Selection"
    - Users toolbar buttons: "Deactivate All", "Reactivate All", "Clear Selection"
    - Templates toolbar buttons: "Delete All", "Clear Selection"
    - "Clear Selection" calls `TableModule.clearSelection(tableId)` and hides toolbar
    - All buttons are keyboard-focusable and activatable via Enter or Space keys (native `<button>` elements)
    - _Requirements: 2.1–2.6, 11.3, 11.4_

  - [x] 9.2 Implement bulk action handler functions in `app.js`
    - `bulkUpdateProjects(ids)`: POST to `/projects/batch/update` with `{ projectIds: ids }` via existing `apiCall()`, show summary toast `"X of Y succeeded, Z failed"`, clear selection via `TableModule.clearSelection('projects')`, call `loadProjects()` to refresh
    - `bulkDeployProjects(ids)`: POST to `/projects/batch/deploy` with `{ projectIds: ids }`, show summary toast, clear selection, reload
    - `bulkDestroyProjects(ids)`: show confirmation dialog requiring user to type "CONFIRM" (similar to existing `showDestroyConfirmation()` pattern), then POST to `/projects/batch/destroy` with `{ projectIds: ids }`, show summary toast, clear selection, reload
    - `bulkDeactivateUsers(ids)`: POST to `/users/batch/deactivate` with `{ userIds: ids }`, show summary toast, clear selection, call `loadUsers()` to refresh
    - `bulkReactivateUsers(ids)`: POST to `/users/batch/reactivate` with `{ userIds: ids }`, show summary toast, clear selection, reload
    - `bulkDeleteTemplates(ids)`: show confirmation dialog listing template IDs, then POST to `/templates/batch/delete` with `{ templateIds: ids }`, show summary toast, clear selection, call `loadTemplates()` to refresh
    - Each handler uses `showToast()` with `'error'` type if any items failed
    - On network error, preserve selection (don't call `clearSelection`) so user can retry
    - After bulk project operations, the existing polling mechanism (using `projectPollIntervalMs` from `frontend/js/config.js` and `projectStatusCache` in `state`) will automatically pick up transitional projects and render independent progress bars using the existing `progress-container compact` markup pattern in `projectsTableConfig`'s actions column renderer
    - _Requirements: 3.1, 3.7, 3.8, 4.1, 4.7, 5.1, 5.2, 5.8, 6.1, 6.6, 6.11, 7.1, 7.2, 7.7_

  - [x] 9.3 Write property test for toolbar selection count (Property 5)
    - **Property 5: Toolbar displays correct selection count**
    - Generate random non-empty selections, verify toolbar count text matches `getSelectedIds().length`
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/bulk-toolbar.property.test.js`
    - **Validates: Requirements 2.2**

  - [x] 9.4 Write unit tests for bulk action toolbar and handlers
    - Test toolbar visibility: shown when selection > 0, hidden when 0
    - Test correct buttons per page (projects: Deploy All/Update All/Destroy All/Clear Selection; users: Deactivate All/Reactivate All/Clear Selection; templates: Delete All/Clear Selection)
    - Test confirmation dialogs for Destroy All (requires "CONFIRM") and Delete All (lists template IDs)
    - Test toast messages after batch results: `"X of Y succeeded, Z failed"`
    - Test ARIA live region (`aria-live="polite"`) on toolbar
    - Test file: `test/frontend/bulk-toolbar.test.js`
    - _Requirements: 2.1–2.6, 5.1, 7.1, 11.3_

- [x] 10. Add bulk action toolbar CSS styles in `frontend/css/styles.css`
  - Add `.bulk-action-toolbar` styles: flex layout, gap, padding, background, border, border-radius
  - Add `.bulk-selection-count` styles: font-weight, font-size
  - Ensure toolbar is visually distinct and positioned above the table
  - Add responsive styles for mobile
  - _Requirements: 2.1, 2.2_

- [x] 11. Implement staleness detection in `frontend/js/app.js`
  - [x] 11.1 Update projects page to use foundation timestamp for staleness
    - In `loadProjects()`, extract `foundationStackTimestamp` from the API response (returned alongside the `projects` array by the updated `_handle_list_projects()`)
    - Store it in a module-level variable for use by the table config and bulk handlers
    - Update `projectsTableConfig` actions column renderer: for ACTIVE projects, compare `row.statusChangedAt` (set by `lifecycle.transition_project()` in `lambda/project_management/lifecycle.py` when the project last transitioned to ACTIVE) against `foundationStackTimestamp`; if `statusChangedAt >= foundationStackTimestamp`, disable Update button with tooltip "Project is up to date" (greyed out); if `statusChangedAt < foundationStackTimestamp`, enable Update button (project is stale)
    - Update `bulkUpdateProjects()` handler to filter selected IDs to only stale projects (where `statusChangedAt < foundationStackTimestamp`) before sending request; show toast "All selected projects are already up to date" if none remain after filtering
    - Disable "Update All" button in toolbar when all selected ACTIVE projects are up to date
    - The existing polling mechanism (using `projectPollIntervalMs: 5000` from `frontend/js/config.js` and `projectStatusCache` in `state` for transition detection) continues to update progress bars for each transitional project independently using the `progress-container compact` markup pattern
    - _Requirements: 8.2–8.7_

  - [x] 11.2 Write property test for staleness classification (Property 11)
    - **Property 11: Staleness classification is correct**
    - Generate random ACTIVE projects with random `statusChangedAt` timestamps and random `foundationStackTimestamp` values, verify a project is classified as "up to date" if and only if `statusChangedAt >= foundationStackTimestamp`, and "stale" otherwise
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/staleness.property.test.js`
    - **Validates: Requirements 8.3, 8.4, 8.5**

  - [x] 11.3 Write property test for Update All filtering (Property 12)
    - **Property 12: Update All filters to stale projects only**
    - Generate selections of ACTIVE projects with mixed staleness, verify only stale IDs (where `statusChangedAt < foundationStackTimestamp`) are included in the batch request
    - Use `{ numRuns: 20 }` for fast-check
    - Test file: `test/frontend/staleness.property.test.js`
    - **Validates: Requirements 8.6**

  - [x] 11.4 Write unit tests for staleness detection
    - Test Update button disabled for up-to-date projects (where `statusChangedAt >= foundationStackTimestamp`)
    - Test Update button enabled for stale projects (where `statusChangedAt < foundationStackTimestamp`)
    - Test "Update All" button disabled when all selected ACTIVE projects are up to date
    - Test toast "All selected projects are already up to date" when no stale projects in selection
    - Test file: `test/frontend/staleness.test.js`
    - _Requirements: 8.3–8.7_

- [x] 12. Checkpoint — Verify full frontend integration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Update documentation
  - Update `docs/admin/project-management.md` with bulk project actions (Update All, Deploy All, Destroy All) and staleness detection (Update button greyed out for up-to-date projects)
  - Update `docs/admin/user-management.md` with bulk user actions (Deactivate All, Reactivate All)
  - Add bulk template delete documentation to relevant admin docs
  - _Requirements: 2.1–2.6, 3.1, 4.1, 5.1, 6.1, 7.1, 8.3–8.5_

- [x] 14. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- All property-based tests use small example counts: `@settings(max_examples=20)` for Hypothesis (Python) and `{ numRuns: 20 }` for fast-check (JavaScript)
- Unit tests validate specific examples and edge cases
- Backend tasks (1–3) come first so batch endpoints are available when frontend tasks wire up the UI
- fast-check may need to be installed for JavaScript property-based tests (`npm install --save-dev fast-check`)
- Python property tests use Hypothesis (already installed — see `.hypothesis/` directory)
- Batch handlers reuse existing single-item functions: `lifecycle.transition_project()`, `_get_active_clusters()`, `deactivate_user()`, `reactivate_user()`, `delete_template()`
- All batch endpoints process items sequentially (not in parallel) to avoid overwhelming DynamoDB, Cognito, and Step Functions
- Error response format matches existing pattern: `{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": {}}}` via `build_error_response()` in each handler's `errors.py`
- Authorization uses `is_administrator(event)` consistently across all batch endpoints
- Progress bars use existing `progress-container compact` markup pattern from `projectsTableConfig` actions column renderer
- Polling uses existing `projectPollIntervalMs: 5000` from `frontend/js/config.js` and `projectStatusCache` in `state` for transition detection
- Foundation_Stack_Timestamp stored in Projects DynamoDB table (`PK=PLATFORM`, `SK=FOUNDATION_TIMESTAMP`) via CDK custom resource in `FoundationStack` class (`lib/foundation-stack.ts`)
- Destroy checks for both ACTIVE and CREATING clusters via `_get_active_clusters()` from `lambda/project_management/projects.py`
