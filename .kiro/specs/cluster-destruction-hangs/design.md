# Cluster Destruction Hangs Bugfix Design

## Overview

The cluster destruction Step Functions workflow (`hpc-cluster-destruction`) hangs indefinitely when PCS sub-resource deletion fails silently. The `delete_pcs_resources` step uses best-effort deletion that swallows failures, then `check_pcs_deletion_status` enters an unbounded polling loop for resources whose deletion was never initiated. The same unbounded-loop pattern exists in the FSx export status check. Additionally, `_is_pcs_resource_deleted` masks real API errors by treating them as "resource still exists", and no failure handler transitions the cluster status from DESTROYING to a failed state when the workflow times out.

The fix introduces: (1) failure detection in `delete_pcs_resources` so it raises when any sub-resource deletion fails, (2) bounded retry counts on both polling loops, (3) proper error propagation in `_is_pcs_resource_deleted`, (4) a timeout/failure handler that marks the cluster as DESTRUCTION_FAILED, and (5) idempotent handling of already-deleted resources on retry.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the hang — a PCS sub-resource deletion fails silently (`:failed` result) or a polling loop exceeds its maximum retry count, or `_is_pcs_resource_deleted` encounters an unexpected API error
- **Property (P)**: The desired behavior — polling loops terminate within bounded retries, deletion failures are propagated, unexpected errors are raised, and the cluster transitions to DESTRUCTION_FAILED on timeout/failure
- **Preservation**: Existing behavior that must remain unchanged — normal deletion paths, successful export handling, existing idempotent `ResourceNotFoundException` handling, and the happy-path DESTROYED status recording
- **`delete_pcs_resources`**: The function in `lambda/cluster_operations/cluster_destruction.py` that initiates best-effort deletion of PCS compute node groups and queue
- **`check_pcs_deletion_status`**: The function that polls PCS describe APIs to check whether sub-resources have finished deleting
- **`check_fsx_export_status`**: The function that polls the FSx data repository export task status
- **`_is_pcs_resource_deleted`**: Helper that calls a PCS describe function and returns True if `ResourceNotFoundException` is raised
- **`pcsRetryCount`**: New event field tracking the number of PCS deletion polling iterations
- **`exportRetryCount`**: New event field tracking the number of FSx export polling iterations
- **MAX_PCS_DELETION_RETRIES**: Maximum number of polling iterations for PCS sub-resource deletion (e.g. 120 = 60 minutes at 30s intervals)
- **MAX_EXPORT_RETRIES**: Maximum number of polling iterations for FSx export status (e.g. 60 = 60 minutes at 60s intervals)

## Bug Details

### Bug Condition

The bug manifests when a PCS sub-resource deletion fails during `delete_pcs_resources` (returning a `:failed` result string) but the Lambda succeeds anyway, causing the workflow to proceed to `check_pcs_deletion_status` for a resource whose deletion was never initiated. The polling loop then runs indefinitely because the resource remains in ACTIVE status (no `ResourceNotFoundException` will ever occur). The same unbounded pattern exists in the FSx export polling loop. Additionally, `_is_pcs_resource_deleted` treats unexpected API errors (throttling, access denied, parent cluster deleted) as "resource still exists", extending the loop further.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type DestructionWorkflowState
  OUTPUT: boolean

  -- Condition 1: Sub-resource deletion failed but was swallowed
  deletionFailed := ANY result IN input.pcsCleanupResults
                    WHERE result ENDS WITH ":failed"

  -- Condition 2: PCS polling loop has no bound
  pcsPollingUnbounded := input.pcsRetryCount IS UNDEFINED
                         AND input.pcsSubResourcesDeleted == false

  -- Condition 3: FSx export polling loop has no bound
  exportPollingUnbounded := input.exportRetryCount IS UNDEFINED
                            AND input.exportComplete == false

  -- Condition 4: Unexpected API error treated as "still exists"
  unexpectedErrorMasked := _is_pcs_resource_deleted CATCHES non-ResourceNotFoundException
                           AND RETURNS false INSTEAD OF RAISING

  -- Condition 5: Timeout with no status transition
  timeoutWithNoTransition := workflow TIMED OUT
                             AND cluster.status == "DESTROYING"

  RETURN deletionFailed
         OR pcsPollingUnbounded
         OR exportPollingUnbounded
         OR unexpectedErrorMasked
         OR timeoutWithNoTransition
END FUNCTION
```

### Examples

- **Compute node group deletion fails**: `_delete_pcs_node_group("pcs_abc", "ng_123", "compute")` returns `"compute_node_group:ng_123:failed"` → `delete_pcs_resources` succeeds → `check_pcs_deletion_status` polls `get_compute_node_group("ng_123")` which returns ACTIVE → loop repeats every 30s forever
- **Throttling during polling**: `get_compute_node_group` raises `ThrottlingException` → `_is_pcs_resource_deleted` catches it, returns False → loop continues instead of surfacing the error
- **FSx export stuck in PENDING**: `describe_data_repository_tasks` returns lifecycle=PENDING indefinitely → `check_fsx_export_status` returns `exportComplete=False` → loop repeats every 60s forever
- **Workflow timeout**: After 2 hours the state machine times out → no handler updates DynamoDB → cluster stuck in DESTROYING status permanently
- **Retry after partial failure**: Previous run deleted compute node group but failed on queue → retry calls `_delete_pcs_node_group` for already-deleted compute node group → PCS returns `ResourceNotFoundException` → current code treats this as `:failed` → same infinite loop

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- When PCS sub-resources are confirmed deleted via `ResourceNotFoundException` within the normal retry window, the workflow proceeds to `DeletePcsCluster` as today
- When the FSx export task completes successfully (SUCCEEDED lifecycle) within the normal retry window, the workflow proceeds to PCS resource deletion as today
- When the FSx export task fails or is cancelled (FAILED or CANCELED lifecycle), `exportFailed` is set to true and the workflow proceeds, preserving existing failure-handling behaviour
- When PCS resource deletion fails with an `InternalError`, the workflow routes to `DestructionFailed` via the existing `addCatch` error handler
- When the destruction workflow completes successfully, the cluster is marked as DESTROYED in DynamoDB
- When the FSx filesystem is not found during export task creation, the export step is skipped gracefully (`exportSkipped=True`)
- When the cluster has no PCS resources (empty `pcsClusterId`), PCS polling is skipped
- When `_delete_pcs_node_group` succeeds and the sub-resource transitions to DELETING status, polling continues until `ResourceNotFoundException` confirms deletion
- Existing idempotent handling of `ResourceNotFoundException`, `FileSystemNotFound`, `NoSuchEntity` in `delete_fsx_filesystem`, `delete_pcs_cluster_step`, `delete_iam_resources`, and `delete_launch_templates` continues to work

**Scope:**
All inputs that do NOT involve failed sub-resource deletions, exceeded retry counts, unexpected API errors, or workflow timeouts should be completely unaffected by this fix. This includes:
- Normal deletion flows that complete within retry bounds
- Mouse/API-initiated destruction requests
- Clusters with no FSx filesystem (mountpoint storage mode)
- Clusters with no PCS resources

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Best-effort deletion swallows failures**: `delete_pcs_resources` (line ~300) calls `_delete_pcs_node_group` and `_delete_pcs_queue` which return `:failed` result strings on error, but the function always returns successfully. The workflow proceeds to `check_pcs_deletion_status` for resources whose deletion was never initiated, creating an infinite polling loop since the resource remains in ACTIVE status.

2. **Unbounded PCS polling loop**: `check_pcs_deletion_status` has no retry counter. The Step Functions state machine loops `CheckPcsDeletionStatus → WaitForPcsDeletion` with no maximum iteration count. The only exit conditions are: all resources return `ResourceNotFoundException` (success) or the 2-hour state machine timeout expires.

3. **Unbounded FSx export polling loop**: `check_fsx_export_status` has the same pattern — the `CheckFsxExportStatus → WaitForExport` loop has no maximum iteration count. If the export task gets stuck in PENDING or EXECUTING, the loop runs until the 2-hour timeout.

4. **Error masking in `_is_pcs_resource_deleted`**: The helper (line ~420) catches all `ClientError` exceptions. Only `ResourceNotFoundException` returns True (deleted). All other errors — including `ThrottlingException`, `AccessDeniedException`, or errors caused by the parent PCS cluster being deleted — return False ("still exists"), which keeps the polling loop running instead of surfacing the real failure.

5. **No timeout/failure status transition**: The state machine has a 2-hour timeout (`cdk.Duration.hours(2)`) but no catch handler or failure state that updates the cluster's DynamoDB status from DESTROYING to a failed state. When the timeout fires, the execution simply stops, leaving the cluster permanently stuck.

6. **Non-idempotent retry on already-deleted resources**: `_delete_pcs_node_group` does not handle `ResourceNotFoundException` from the delete API — if a resource was already deleted in a previous run, the delete call fails and returns `:failed`, triggering the same infinite loop on retry.

## Correctness Properties

Property 1: Bug Condition - Failed Sub-Resource Deletion Propagates Error

_For any_ input where `delete_pcs_resources` initiates deletion of PCS sub-resources and any deletion returns a `:failed` result (i.e. the delete API call raised a non-`ResourceNotFoundException` error), the fixed `delete_pcs_resources` function SHALL raise an `InternalError` so the state machine routes to the `DestructionFailed` state, rather than silently proceeding to the polling loop.

**Validates: Requirements 2.1**

Property 2: Bug Condition - PCS Polling Loop Terminates Within Bounded Retries

_For any_ input where `check_pcs_deletion_status` is called and the retry count (tracked via `pcsRetryCount` in the event) exceeds `MAX_PCS_DELETION_RETRIES`, the fixed function SHALL raise an error that causes the state machine to route to the `DestructionFailed` state, rather than returning `pcsSubResourcesDeleted=False` for another iteration.

**Validates: Requirements 2.2**

Property 3: Bug Condition - FSx Export Polling Loop Terminates Within Bounded Retries

_For any_ input where `check_fsx_export_status` is called and the retry count (tracked via `exportRetryCount` in the event) exceeds `MAX_EXPORT_RETRIES`, the fixed function SHALL return `exportComplete=True` and `exportFailed=True` so the workflow proceeds rather than polling indefinitely.

**Validates: Requirements 2.3**

Property 4: Bug Condition - Unexpected API Errors Are Raised

_For any_ call to `_is_pcs_resource_deleted` where the PCS describe API raises a `ClientError` with an error code other than `ResourceNotFoundException`, the fixed function SHALL re-raise the error so it propagates to the state machine's error handler, rather than returning False.

**Validates: Requirements 2.4**

Property 5: Preservation - Normal Deletion Path Unchanged

_For any_ input where all PCS sub-resource deletions succeed (no `:failed` results) and the polling loop completes within the retry bound (all resources reach `ResourceNotFoundException`), the fixed functions SHALL produce the same result as the original functions, preserving the existing successful deletion flow.

**Validates: Requirements 3.1, 3.5, 3.8**

Property 6: Preservation - Idempotent Handling of Already-Deleted Resources

_For any_ input where a PCS sub-resource has already been deleted (delete API raises `ResourceNotFoundException`), the fixed `_delete_pcs_node_group` and `_delete_pcs_queue` functions SHALL treat this as a successful deletion (not a failure), preserving idempotent retry behavior.

**Validates: Requirements 2.6, 3.9**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lambda/cluster_operations/cluster_destruction.py`

**Function**: `_delete_pcs_node_group`, `_delete_pcs_queue`

**Specific Changes**:
1. **Handle `ResourceNotFoundException` as success in delete helpers**: In `_delete_pcs_node_group` and `_delete_pcs_queue`, catch `ResourceNotFoundException` separately and return a `:deleted` (success) result instead of `:failed`. This makes retries idempotent — if the resource was already deleted in a previous run, the step succeeds.

**Function**: `delete_pcs_resources`

2. **Detect and propagate deletion failures**: After collecting `pcsCleanupResults`, check if any result ends with `:failed`. If so, raise `InternalError` with details of which sub-resources failed, so the state machine's `addCatch` handler routes to `DestructionFailed`.

**Function**: `check_pcs_deletion_status`

3. **Add bounded retry count for PCS polling**: Read `pcsRetryCount` from the event (defaulting to 0), increment it, and include it in the returned event. If `pcsRetryCount` exceeds `MAX_PCS_DELETION_RETRIES` (e.g. 120 iterations = ~60 minutes at 30s intervals), raise `InternalError` to halt the loop.

**Function**: `check_fsx_export_status`

4. **Add bounded retry count for FSx export polling**: Read `exportRetryCount` from the event (defaulting to 0), increment it, and include it in the returned event. If `exportRetryCount` exceeds `MAX_EXPORT_RETRIES` (e.g. 60 iterations = ~60 minutes at 60s intervals), return `exportComplete=True, exportFailed=True` with a timeout reason.

**Function**: `_is_pcs_resource_deleted`

5. **Raise unexpected API errors**: Change the catch-all `ClientError` handler to only catch `ResourceNotFoundException` (return True). For all other `ClientError` exceptions, re-raise so they propagate to the state machine's error handler.

**File**: `lib/constructs/cluster-operations.ts`

6. **Add failure handler for state machine timeout**: Add a catch-all error handler or use the Step Functions `timeout` event to invoke a Lambda that transitions the cluster's DynamoDB status from DESTROYING to DESTRUCTION_FAILED. This can be done by adding an `addCatch` on the overall state machine definition or by adding a CloudWatch Events rule that triggers on state machine execution failure/timeout.

**File**: `lambda/cluster_operations/cluster_destruction.py`

7. **Add `record_cluster_destruction_failed` handler**: New function that updates the DynamoDB cluster record to set `status=DESTRUCTION_FAILED` and clears progress fields. This is invoked by the failure handler in the state machine.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that exercise the deletion and polling functions with failure scenarios and assert that the current code does NOT propagate errors or bound retries. Run these tests on the UNFIXED code to observe the buggy behavior.

**Test Cases**:
1. **Swallowed deletion failure**: Call `delete_pcs_resources` with a mocked `_delete_pcs_node_group` that returns `:failed` — observe that the function succeeds instead of raising (will pass on unfixed code, confirming the bug)
2. **Unbounded PCS polling**: Call `check_pcs_deletion_status` with resources that always return ACTIVE — observe that it returns `pcsSubResourcesDeleted=False` with no retry tracking (will pass on unfixed code, confirming no bound)
3. **Masked API error**: Call `_is_pcs_resource_deleted` with a `ThrottlingException` — observe that it returns False instead of raising (will pass on unfixed code, confirming error masking)
4. **Unbounded FSx polling**: Call `check_fsx_export_status` with a task stuck in PENDING — observe that it returns `exportComplete=False` with no retry tracking (will pass on unfixed code, confirming no bound)

**Expected Counterexamples**:
- `delete_pcs_resources` returns successfully even when sub-resource deletions fail
- `check_pcs_deletion_status` has no mechanism to detect or limit retry iterations
- `_is_pcs_resource_deleted` silently swallows non-`ResourceNotFoundException` errors
- `check_fsx_export_status` has no mechanism to detect or limit retry iterations

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT expectedBehavior(result)
END FOR
```

Specifically:
- For all inputs where any sub-resource deletion fails → `delete_pcs_resources` raises `InternalError`
- For all inputs where `pcsRetryCount > MAX_PCS_DELETION_RETRIES` → `check_pcs_deletion_status` raises `InternalError`
- For all inputs where `exportRetryCount > MAX_EXPORT_RETRIES` → `check_fsx_export_status` returns `exportComplete=True, exportFailed=True`
- For all inputs where `_is_pcs_resource_deleted` receives a non-`ResourceNotFoundException` → the error is re-raised

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain (combinations of resource IDs, retry counts, deletion outcomes)
- It catches edge cases that manual unit tests might miss (e.g. empty IDs combined with non-zero retry counts)
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for successful deletion paths and normal polling, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Normal PCS deletion preservation**: Observe that when all deletions succeed and resources reach `ResourceNotFoundException` within retry bounds, the workflow proceeds to `DeletePcsCluster` — verify this continues after fix
2. **Normal FSx export preservation**: Observe that when export succeeds (SUCCEEDED lifecycle) within retry bounds, the workflow proceeds — verify this continues after fix
3. **Existing idempotent handling preservation**: Observe that `delete_fsx_filesystem`, `delete_pcs_cluster_step`, `delete_iam_resources`, and `delete_launch_templates` handle already-deleted resources gracefully — verify this continues after fix
4. **Empty resource ID skip preservation**: Observe that empty `pcsClusterId`, `fsxFilesystemId`, etc. cause steps to skip gracefully — verify this continues after fix

### Unit Tests

- Test `delete_pcs_resources` raises `InternalError` when any sub-resource deletion returns `:failed`
- Test `delete_pcs_resources` succeeds when all deletions return `:deleted`
- Test `_delete_pcs_node_group` treats `ResourceNotFoundException` as success (`:deleted`)
- Test `_delete_pcs_queue` treats `ResourceNotFoundException` as success (`:deleted`)
- Test `check_pcs_deletion_status` increments `pcsRetryCount` in returned event
- Test `check_pcs_deletion_status` raises `InternalError` when `pcsRetryCount > MAX_PCS_DELETION_RETRIES`
- Test `check_fsx_export_status` increments `exportRetryCount` in returned event
- Test `check_fsx_export_status` returns `exportComplete=True, exportFailed=True` when `exportRetryCount > MAX_EXPORT_RETRIES`
- Test `_is_pcs_resource_deleted` raises on `ThrottlingException`, `AccessDeniedException`, etc.
- Test `_is_pcs_resource_deleted` returns True on `ResourceNotFoundException` (unchanged)
- Test `_is_pcs_resource_deleted` returns False when describe succeeds (resource still exists, unchanged)
- Test `record_cluster_destruction_failed` sets status to DESTRUCTION_FAILED in DynamoDB

### Property-Based Tests

- Generate random combinations of sub-resource deletion outcomes (success/failed/already-deleted) and verify `delete_pcs_resources` raises if and only if any result is `:failed`
- Generate random retry counts and verify `check_pcs_deletion_status` raises if and only if count exceeds `MAX_PCS_DELETION_RETRIES`
- Generate random retry counts and verify `check_fsx_export_status` returns timeout failure if and only if count exceeds `MAX_EXPORT_RETRIES`
- Generate random `ClientError` codes and verify `_is_pcs_resource_deleted` raises for all codes except `ResourceNotFoundException`
- Generate random event payloads with valid resource IDs and verify that when all deletions succeed and retry counts are within bounds, the output shape matches the original function's output (preservation)

### Integration Tests

- Test full destruction state machine definition includes catch handlers on polling loops
- Test that the state machine failure/timeout path invokes the `record_cluster_destruction_failed` handler
- Test end-to-end destruction flow with mocked AWS clients: successful path completes and records DESTROYED
- Test end-to-end destruction flow with mocked deletion failure: routes to DestructionFailed and records DESTRUCTION_FAILED
