# Implementation Plan: Deletion Progress Tracking

## Overview

This plan implements step-by-step progress tracking for cluster and project destruction workflows, matching the existing pattern used by cluster creation and project deployment. It adds `STEP_LABELS`, `_update_step_progress()` calls, atomic conditional updates for concurrency prevention, a shared `renderProgressBar()` function in the frontend, and 409 Conflict handling. Property-based tests validate the five correctness properties from the design.

## Tasks

- [x] 1. Add progress tracking to cluster destruction step lambda
  - [x] 1.1 Add STEP_LABELS, TOTAL_STEPS, and _update_step_progress() to cluster_destruction.py
    - Add `TOTAL_STEPS = 8` constant and `STEP_LABELS` dict mapping steps 1–8 to human-readable descriptions
    - Add `_update_step_progress(project_id, cluster_name, step_number)` function mirroring `cluster_creation.py`
    - The function writes `currentStep`, `totalSteps`, `stepDescription`, and `status=DESTROYING` to the Clusters DynamoDB table
    - _Requirements: 1.2, 1.3_
  - [x] 1.2 Add _update_step_progress() calls to each destruction step handler
    - Call `_update_step_progress()` at the beginning of each step handler: `create_fsx_export_task` (step 1), `check_fsx_export_status` (step 2), `delete_pcs_resources` (step 3), `check_pcs_deletion_status` (step 4), `delete_pcs_cluster_step` (step 5), `delete_fsx_filesystem` (step 6), `delete_iam_resources` (step 7), `record_cluster_destroyed` (step 8)
    - Step 7 covers IAM, launch templates, S3 policy, and name deregistration — only the first function in the group (`delete_iam_resources`) updates progress
    - _Requirements: 1.2, 1.3_
  - [x] 1.3 Update record_cluster_destroyed() to clear progress fields
    - Extend the existing `UpdateExpression` in `record_cluster_destroyed()` to also remove `currentStep`, `totalSteps`, and `stepDescription` when setting status to DESTROYED
    - _Requirements: 1.4_
  - [x] 1.4 Write property test for destruction step labels completeness (Property 3)
    - **Property 3: Destruction step labels provide complete monotonic coverage**
    - Verify `STEP_LABELS` covers `[1, TOTAL_STEPS]` with no gaps and all non-empty string values
    - Create `tests/test_deletion_progress_properties.py`
    - **Validates: Requirements 1.3**

- [x] 2. Implement atomic conditional update for cluster deletion
  - [x] 2.1 Replace read-then-check with atomic conditional update in _handle_delete_cluster()
    - In `lambda/cluster_operations/handler.py`, replace the current `get_cluster()` + status check + `sfn_client.start_execution()` pattern
    - Use a single `update_item` with `ConditionExpression="#st IN (:active, :failed)"` to atomically transition to DESTROYING and initialize progress fields (`currentStep: 0`, `totalSteps: 8`, `stepDescription: "Starting cluster destruction"`)
    - Catch `ConditionalCheckFailedException` from botocore and raise `ConflictError` with a descriptive message
    - Keep the existing `get_cluster()` call to retrieve resource IDs needed for the Step Functions payload, but move the status check into the conditional update
    - _Requirements: 9.1, 9.2, 1.1_
  - [x] 2.2 Extend _handle_get_cluster() to include progress for DESTROYING status
    - Change the condition `if cluster.get("status") == "CREATING":` to `if cluster.get("status") in ("CREATING", "DESTROYING"):`
    - This ensures the `progress` object is included in GET responses for clusters being destroyed
    - _Requirements: 2.1, 2.2, 2.3_
  - [x] 2.3 Write property test for atomic cluster deletion (Property 1)
    - **Property 1: Atomic cluster deletion initializes progress and transitions status**
    - Generate random cluster records with various statuses; mock DynamoDB
    - Verify ACTIVE/FAILED statuses result in conditional update with correct progress fields
    - Verify other statuses result in ConflictError
    - Add to `tests/test_deletion_progress_properties.py`
    - **Validates: Requirements 1.1, 9.1, 9.2**
  - [x] 2.4 Write property test for cluster GET progress inclusion (Property 2)
    - **Property 2: Cluster GET includes progress object for transitional statuses**
    - Generate random cluster records with various statuses and progress field values (including Decimal types)
    - Verify progress object inclusion for CREATING/DESTROYING, omission for other statuses
    - Verify integer type conversion of currentStep and totalSteps
    - Add to `tests/test_deletion_progress_properties.py`
    - **Validates: Requirements 2.1, 2.2, 2.3**

- [x] 3. Checkpoint — Ensure all backend cluster tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement atomic conditional update for project destruction
  - [x] 4.1 Replace read-then-check with atomic conditional update in _handle_destroy_project_infra()
    - In `lambda/project_management/handler.py`, replace the current `get_project()` + status check + `lifecycle.transition_project()` + separate `update_item` for progress
    - Use a single `update_item` with `ConditionExpression="#st = :active"` to atomically transition to DESTROYING and initialize progress fields (`currentStep: 0`, `totalSteps: 5`, `stepDescription: "Starting project destruction"`)
    - Set `statusChangedAt` and `updatedAt` in the same atomic update
    - Catch `ConditionalCheckFailedException` and raise `ConflictError`
    - Keep the active clusters check before the atomic update (business rule, not concurrency concern)
    - _Requirements: 9.3, 9.4_
  - [x] 4.2 Write property test for atomic project destruction (Property 5)
    - **Property 5: Atomic project destruction initializes progress and transitions status**
    - Generate random project records with various statuses; mock DynamoDB
    - Verify ACTIVE status results in conditional update with correct progress fields
    - Verify other statuses result in ConflictError
    - Add to `tests/test_deletion_progress_properties.py`
    - **Validates: Requirements 9.3, 9.4**

- [x] 5. Checkpoint — Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Extract shared renderProgressBar() and add DESTROYING handling in frontend
  - [x] 6.1 Add shared renderProgressBar() function to app.js
    - Add `renderProgressBar(currentStep, totalSteps, stepDescription, defaultLabel, compact = true)` function
    - The function calculates percentage, handles missing/zero values gracefully (totalSteps defaults to 1 to prevent division by zero), and returns progress bar HTML
    - Place the function in the utility/helper section of app.js (near `esc()` or `showToast()`)
    - _Requirements: 3.1, 3.2, 5.1, 5.2_
  - [x] 6.2 Replace duplicated progress bar HTML in projectsTableConfig with renderProgressBar()
    - Replace the DEPLOYING, UPDATING, and DESTROYING inline progress bar HTML in `projectsTableConfig._actions` render function with calls to `renderProgressBar()`
    - DEPLOYING: `renderProgressBar(row.currentStep, row.totalSteps, row.stepDescription, 'Deploying…')`
    - UPDATING: `renderProgressBar(row.currentStep, row.totalSteps, row.stepDescription, 'Updating…')`
    - DESTROYING: `renderProgressBar(row.currentStep, row.totalSteps, row.stepDescription, 'Destroying…')`
    - _Requirements: 5.1, 5.2_
  - [x] 6.3 Add DESTROYING status handling to cluster list table config
    - Add a DESTROYING case to the clusters table `_progress` column render function that calls `renderProgressBar(row.currentStep, row.totalSteps, row.stepDescription, 'Destroying…')`
    - Replace existing CREATING inline progress bar HTML with `renderProgressBar()` call (keeping stale warning logic outside)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [x] 6.4 Add DESTROYING progress section to cluster detail page
    - Add a DESTROYING block mirroring the existing CREATING progress section in `renderClusterDetailPage()`
    - Use `renderProgressBar(progress.currentStep, progress.totalSteps, progress.stepDescription, 'Destroying…', false)` with compact=false
    - Include auto-refresh message and start polling via `startClusterDetailPolling()`
    - _Requirements: 4.1, 4.2_
  - [x] 6.5 Add DESTROYING → DESTROYED transition detection
    - Add transition detection in the cluster list polling callback: when previous status was DESTROYING and new status is DESTROYED, show success toast
    - Add transition detection in the cluster detail polling callback: same pattern
    - Add DESTROYING → ARCHIVED transition detection for projects in the project list polling callback
    - _Requirements: 4.3, 4.4, 7.1, 7.2, 7.3_
  - [x] 6.6 Add 409 Conflict handling for destroy actions
    - Update `destroyCluster()` (or the cluster DELETE call) to catch 409 responses and show a user-friendly toast "This resource is already being destroyed"
    - Update `showDestroyConfirmation()` / project destroy to catch 409 responses similarly
    - _Requirements: 9.5_
  - [x] 6.7 Write property test for renderProgressBar percentage calculation (Property 4)
    - **Property 4: Progress bar percentage calculation is correct**
    - Since this is a frontend function, write a Python-based test that validates the percentage formula `Math.round((currentStep / totalSteps) * 100)` for random currentStep/totalSteps pairs
    - Verify the formula produces correct values for edge cases (0/N, N/N, boundary values)
    - Add to `tests/test_deletion_progress_properties.py`
    - **Validates: Requirements 3.1, 3.2, 5.1, 5.2**

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Update documentation
  - [x] 8.1 Update cluster management documentation
    - Update `docs/project-admin/cluster-management.md` to describe the destruction progress bar behaviour
    - Document the 8 destruction steps and the types of operations performed at each step
    - Document the concurrent deletion prevention behaviour and the 409 error message
    - _Requirements: 10.1, 10.3_
  - [x] 8.2 Update project management documentation
    - Update `docs/admin/project-management.md` to describe the destruction progress bar behaviour
    - Document the 5 destruction steps and the types of operations performed at each step
    - Document the concurrent deletion prevention behaviour
    - _Requirements: 10.2, 10.3_

- [x] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The backend uses Python (Lambda functions), the frontend uses vanilla JavaScript
- All property-based tests use Hypothesis and are collected in `tests/test_deletion_progress_properties.py`
