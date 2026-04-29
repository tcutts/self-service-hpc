# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Destruction Workflow Hangs on Failed Sub-Resource Deletion, Unbounded Polling, and Masked Errors
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the five bug conditions: swallowed deletion failures, unbounded PCS polling, unbounded FSx export polling, masked API errors, and missing failure status transition
  - **Scoped PBT Approach**: Scope properties to concrete failing cases for each bug condition
  - **Test file**: `tests/test_bug_condition_cluster_destruction_hangs.py` using hypothesis
  - **Test setup**: Import `delete_pcs_resources`, `check_pcs_deletion_status`, `check_fsx_export_status`, `_is_pcs_resource_deleted` from `cluster_destruction`. Mock PCS and FSx clients.
  - **Bug Condition 1 — Swallowed deletion failure**: Generate events with non-empty `pcsClusterId` and at least one non-empty sub-resource ID. Mock `_delete_pcs_node_group` to return `:failed` result. Assert `delete_pcs_resources` raises `InternalError` (from `isBugCondition`: any result ENDS WITH `:failed`). On unfixed code this will FAIL because the function returns successfully.
  - **Bug Condition 2 — Unbounded PCS polling**: Generate events with `pcsRetryCount` exceeding `MAX_PCS_DELETION_RETRIES` (e.g. 121) and `pcsSubResourcesDeleted=False`. Assert `check_pcs_deletion_status` raises an error. On unfixed code this will FAIL because no retry count is tracked or checked.
  - **Bug Condition 3 — Unbounded FSx export polling**: Generate events with `exportRetryCount` exceeding `MAX_EXPORT_RETRIES` (e.g. 61) and `exportComplete=False`. Assert `check_fsx_export_status` returns `exportComplete=True, exportFailed=True`. On unfixed code this will FAIL because no retry count is tracked.
  - **Bug Condition 4 — Masked API errors**: Generate non-`ResourceNotFoundException` error codes (`ThrottlingException`, `AccessDeniedException`, `ConflictException`, `InternalServerError`, `ServiceException`). Call `_is_pcs_resource_deleted` with a describe function that raises the error. Assert the error is re-raised. On unfixed code this will FAIL because all `ClientError` exceptions return False.
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bugs exist)
  - Document counterexamples found (e.g., "`delete_pcs_resources` returns successfully with `compute_node_group:cng-001:failed` in results instead of raising")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Normal Deletion and Polling Paths Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Test file**: `tests/test_preservation_cluster_destruction_hangs.py` using hypothesis
  - **Observe on UNFIXED code**:
    - Observe: `delete_pcs_resources` with all deletions succeeding (`:deleted` results) returns event with `pcsCleanupResults` list, no error raised
    - Observe: `check_pcs_deletion_status` with all sub-resources raising `ResourceNotFoundException` returns `pcsSubResourcesDeleted=True`
    - Observe: `check_pcs_deletion_status` with empty `pcsClusterId` returns `pcsSubResourcesDeleted=True` without calling PCS APIs
    - Observe: `check_fsx_export_status` with `exportSkipped=True` returns `exportComplete=True, exportFailed=False`
    - Observe: `check_fsx_export_status` with SUCCEEDED lifecycle returns `exportComplete=True, exportFailed=False`
    - Observe: `check_fsx_export_status` with FAILED/CANCELED lifecycle returns `exportComplete=True, exportFailed=True`
    - Observe: `check_fsx_export_status` with PENDING/EXECUTING lifecycle returns `exportComplete=False, exportFailed=False`
    - Observe: `_is_pcs_resource_deleted` with `ResourceNotFoundException` returns True
    - Observe: `_is_pcs_resource_deleted` with successful describe call returns False
  - **Write property-based tests**:
    - Generate random events with non-empty resource IDs where all PCS delete calls succeed. Assert `delete_pcs_resources` returns successfully with `pcsCleanupResults` containing only `:deleted` entries and all original event keys preserved.
    - Generate random events where all sub-resources raise `ResourceNotFoundException`. Assert `check_pcs_deletion_status` returns `pcsSubResourcesDeleted=True` with all original event keys preserved.
    - Generate random events with `exportSkipped=True` or successful export lifecycle. Assert `check_fsx_export_status` returns correct `exportComplete`/`exportFailed` values.
    - Assert `_is_pcs_resource_deleted` returns True for `ResourceNotFoundException` and False for successful describe calls.
  - Verify tests pass on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 3. Fix for cluster destruction workflow hanging on failed sub-resource deletion, unbounded polling, masked errors, and missing failure status

  - [x] 3.1 Make `_delete_pcs_node_group` and `_delete_pcs_queue` idempotent by handling `ResourceNotFoundException` as success
    - In `_delete_pcs_node_group`: catch `ResourceNotFoundException` separately from the general `ClientError` handler and return `f"{label}_node_group:{node_group_id}:deleted"` (success) instead of `:failed`
    - In `_delete_pcs_queue`: catch `ResourceNotFoundException` separately and return `f"queue:{queue_id}:deleted"` (success) instead of `:failed`
    - This ensures retries after partial destruction don't trigger the infinite loop
    - _Bug_Condition: `_delete_pcs_node_group`/`_delete_pcs_queue` return `:failed` when resource already deleted, triggering same infinite loop on retry_
    - _Expected_Behavior: Already-deleted resources treated as successful no-ops_
    - _Preservation: Successful deletion and genuine failure paths unchanged_
    - _Requirements: 2.6, 3.9_

  - [x] 3.2 Add failure detection in `delete_pcs_resources` to propagate `:failed` results
    - After collecting `pcsCleanupResults`, check if any result string ends with `:failed`
    - If so, raise `InternalError` with details of which sub-resources failed, so the state machine's `addCatch` handler routes to `DestructionFailed`
    - _Bug_Condition: `isBugCondition(input)` where ANY result IN `pcsCleanupResults` ENDS WITH `:failed`_
    - _Expected_Behavior: `delete_pcs_resources` raises `InternalError` when any sub-resource deletion fails_
    - _Preservation: When all deletions succeed (no `:failed` results), function returns normally as before_
    - _Requirements: 2.1, 3.1_

  - [x] 3.3 Add bounded retry count to `check_pcs_deletion_status`
    - Define `MAX_PCS_DELETION_RETRIES = 120` constant (120 iterations × 30s = ~60 minutes)
    - Read `pcsRetryCount` from event (default 0), increment it, include in returned event
    - If `pcsRetryCount` exceeds `MAX_PCS_DELETION_RETRIES`, raise `InternalError` with timeout message
    - _Bug_Condition: `isBugCondition(input)` where `pcsRetryCount` IS UNDEFINED AND `pcsSubResourcesDeleted == false`_
    - _Expected_Behavior: Polling loop terminates after `MAX_PCS_DELETION_RETRIES` iterations with error_
    - _Preservation: Normal deletion paths that complete within retry bounds are unaffected_
    - _Requirements: 2.2, 3.1, 3.8_

  - [x] 3.4 Add bounded retry count to `check_fsx_export_status`
    - Define `MAX_EXPORT_RETRIES = 60` constant (60 iterations × 60s = ~60 minutes)
    - Read `exportRetryCount` from event (default 0), increment it, include in returned event
    - If `exportRetryCount` exceeds `MAX_EXPORT_RETRIES`, return `exportComplete=True, exportFailed=True` with timeout reason
    - _Bug_Condition: `isBugCondition(input)` where `exportRetryCount` IS UNDEFINED AND `exportComplete == false`_
    - _Expected_Behavior: Export polling terminates after `MAX_EXPORT_RETRIES` iterations, treated as failed export_
    - _Preservation: Successful exports and existing FAILED/CANCELED handling unchanged_
    - _Requirements: 2.3, 3.2, 3.3_

  - [x] 3.5 Fix `_is_pcs_resource_deleted` to re-raise unexpected API errors
    - Change the `ClientError` handler to only catch `ResourceNotFoundException` (return True)
    - For all other `ClientError` exceptions, re-raise so they propagate to the state machine's error handler
    - _Bug_Condition: `_is_pcs_resource_deleted` CATCHES non-`ResourceNotFoundException` AND RETURNS false INSTEAD OF RAISING_
    - _Expected_Behavior: Unexpected errors propagate to state machine error handler_
    - _Preservation: `ResourceNotFoundException` still returns True; successful describe still returns False_
    - _Requirements: 2.4, 3.1_

  - [x] 3.6 Add `record_cluster_destruction_failed` handler and register in step dispatch
    - New function in `cluster_destruction.py` that updates DynamoDB cluster record: set `status=DESTRUCTION_FAILED`, set `destructionFailedAt` timestamp, remove progress fields (`currentStep`, `totalSteps`, `stepDescription`)
    - Register as `"record_cluster_destruction_failed"` in `_STEP_DISPATCH`
    - _Bug_Condition: Workflow times out AND `cluster.status == "DESTROYING"` with no transition_
    - _Expected_Behavior: Cluster transitions to `DESTRUCTION_FAILED` on workflow failure/timeout_
    - _Preservation: Existing `record_cluster_destroyed` handler unchanged_
    - _Requirements: 2.5_

  - [x] 3.7 Add failure handler state machine path in `lib/constructs/cluster-operations.ts`
    - Add a new `RecordClusterDestructionFailed` Lambda invoke step that calls `record_cluster_destruction_failed`
    - Change the `DestructionFailed` state from `sfn.Fail` to route through `RecordClusterDestructionFailed` first, then to a new `sfn.Fail` state
    - Add `addCatch` on the FSx export polling steps (`checkFsxExportStatus`) to route to the failure handler
    - Ensure the state machine timeout also routes to the failure handler (add a top-level catch or use the existing timeout behavior with a failure state)
    - _Bug_Condition: State machine times out or fails with no DynamoDB status update_
    - _Expected_Behavior: All failure paths update cluster status to DESTRUCTION_FAILED before failing_
    - _Preservation: Existing `addCatch` on PCS steps continues to work; successful path unchanged_
    - _Requirements: 2.5, 3.4, 3.5_

  - [x] 3.8 Write unit tests for new and modified functions
    - Test `_delete_pcs_node_group` returns `:deleted` on `ResourceNotFoundException` (idempotent)
    - Test `_delete_pcs_queue` returns `:deleted` on `ResourceNotFoundException` (idempotent)
    - Test `delete_pcs_resources` raises `InternalError` when any result ends with `:failed`
    - Test `delete_pcs_resources` succeeds when all results end with `:deleted`
    - Test `check_pcs_deletion_status` increments `pcsRetryCount` in returned event
    - Test `check_pcs_deletion_status` raises `InternalError` when `pcsRetryCount > MAX_PCS_DELETION_RETRIES`
    - Test `check_fsx_export_status` increments `exportRetryCount` in returned event
    - Test `check_fsx_export_status` returns `exportComplete=True, exportFailed=True` when `exportRetryCount > MAX_EXPORT_RETRIES`
    - Test `_is_pcs_resource_deleted` re-raises `ThrottlingException`, `AccessDeniedException`, etc.
    - Test `_is_pcs_resource_deleted` returns True on `ResourceNotFoundException` (unchanged)
    - Test `_is_pcs_resource_deleted` returns False when describe succeeds (unchanged)
    - Test `record_cluster_destruction_failed` sets status to `DESTRUCTION_FAILED` in DynamoDB
    - Add tests to `tests/unit/test_cluster_destruction.py`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 3.9 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Destruction Workflow Terminates on Failures
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior: failed deletions raise errors, polling loops are bounded, unexpected API errors propagate
    - Run bug condition exploration test from step 1: `.venv/bin/python3 -m pytest tests/test_bug_condition_cluster_destruction_hangs.py -v`
    - **EXPECTED OUTCOME**: Test PASSES (confirms all five bug conditions are fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 3.10 Verify preservation tests still pass
    - **Property 2: Preservation** - Normal Deletion and Polling Paths Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2: `.venv/bin/python3 -m pytest tests/test_preservation_cluster_destruction_hangs.py -v`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions — normal deletion flows, successful exports, idempotent handling all work as before)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full Python test suite: `.venv/bin/python3 -m pytest tests/ -v`
  - Ensure all existing tests (`test_cluster_destruction.py`, `test_cluster_destruction_properties.py`, integration tests) continue to pass
  - Ensure the new property tests (bug condition + preservation) both pass
  - Run CDK synth to verify infrastructure changes: `npx cdk synth`
  - Update documentation in `docs/project-admin/cluster-management.md` to describe the `DESTRUCTION_FAILED` status and recovery options
  - Ask the user if questions arise
