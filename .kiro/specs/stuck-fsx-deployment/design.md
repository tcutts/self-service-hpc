# Stuck FSx Deployment Bugfix Design

## Overview

A cluster deployment can become permanently stuck in CREATING status when the Step Functions execution terminates without successfully updating the DynamoDB cluster record to FAILED. This happens in two scenarios: (1) the rollback handler `handle_creation_failure` itself throws an exception, causing the catch to go directly to the `Fail` state without updating DynamoDB, and (2) the state machine's 2-hour timeout fires with no handler to update the record. The UI polls indefinitely showing stale progress because it has no mechanism to detect that the backend workflow has stopped. Users cannot destroy or recreate the stuck cluster because the API rejects actions on clusters not in ACTIVE or FAILED status.

The fix addresses all four layers: add a last-resort DynamoDB update in the state machine before the Fail state, add a timeout-detection mechanism, add staleness detection in the UI polling path, and allow users to force-fail stuck clusters.

## Glossary

- **Bug_Condition (C)**: A cluster record is in CREATING status in DynamoDB while the corresponding Step Functions execution has terminated (failed or timed out)
- **Property (P)**: The cluster record SHALL be updated to FAILED status with an error message whenever the Step Functions execution terminates without reaching ACTIVE, and the UI SHALL detect and display this terminal state
- **Preservation**: Normal cluster creation (success path), normal failure with successful rollback, legitimate in-progress CREATING polling, and all existing destroy/recreate flows must remain unchanged
- **`handle_creation_failure`**: Function in `lambda/cluster_operations/cluster_creation.py` that performs best-effort rollback of partially created resources and marks the cluster as FAILED in DynamoDB
- **`_record_failed_cluster`**: Helper in `cluster_creation.py` that writes FAILED status to the Clusters DynamoDB table
- **`handleCreationFailure`**: Step Functions LambdaInvoke task in `lib/foundation-stack.ts` that calls `handle_creation_failure`; its catch goes directly to `CreationFailed` (Fail state)
- **`CreationFailed`**: The terminal `sfn.Fail` state in the cluster creation state machine — currently performs no DynamoDB update
- **State machine timeout**: The `hpc-cluster-creation` state machine has a 2-hour timeout (`cdk.Duration.hours(2)`) that terminates the execution without invoking any handler
- **`clusterPollIntervalMs`**: 5-second polling interval configured in `frontend/js/config.js`

## Bug Details

### Bug Condition

The bug manifests when a cluster creation Step Functions execution terminates (via unhandled failure or timeout) without the DynamoDB cluster record being updated from CREATING to FAILED. The `_record_failed_cluster` call inside `handle_creation_failure` is the only code path that transitions the record to FAILED, but it is bypassed when: (a) `handle_creation_failure` itself throws, or (b) the state machine times out entirely.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type ClusterStatusCheck
  OUTPUT: boolean

  LET cluster = getDynamoDBRecord(input.projectId, input.clusterName)
  LET execution = getStepFunctionsExecution(input.projectId, input.clusterName)

  RETURN cluster.status == "CREATING"
         AND (execution.status IN ["FAILED", "TIMED_OUT", "ABORTED"]
              OR executionDoesNotExist(execution))
END FUNCTION
```

### Examples

- **Rollback handler failure**: FSx creation succeeds, PCS cluster creation fails, `handle_creation_failure` runs but throws an exception during IAM cleanup. The catch sends execution to `CreationFailed` (Fail state). DynamoDB still shows `CREATING` with step "Waiting for FSx". UI polls forever.
- **State machine timeout**: FSx polling loop runs for 30 minutes, PCS cluster creation hangs. After 2 hours the state machine times out. No handler runs. DynamoDB still shows `CREATING`. UI polls forever.
- **Transient DynamoDB failure in rollback**: `handle_creation_failure` completes all resource cleanup but `_record_failed_cluster` fails due to a transient DynamoDB `ProvisionedThroughputExceededException`. The function throws, catch goes to Fail state. DynamoDB still shows `CREATING`.
- **User impact**: User sees "Waiting for FSx (step 6/12)" indefinitely. Clicking Destroy returns "Cluster cannot be destroyed in its current state (status: CREATING)". No recovery path exists.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- When the cluster creation workflow succeeds (all steps complete normally), the cluster status SHALL continue to be set to ACTIVE with all resource IDs recorded in DynamoDB
- When the cluster creation workflow fails and the rollback handler succeeds, the cluster status SHALL continue to be set to FAILED with the error message and partially created resources cleaned up
- When the UI polls a cluster that is legitimately in CREATING status (workflow still running), the UI SHALL continue to display accurate progress information (current step, total steps, step description) updated every 5 seconds
- When a cluster is in ACTIVE or FAILED status, the user SHALL continue to be able to destroy or recreate the cluster via the existing API endpoints
- When the FSx polling loop is running normally within the 30-minute window, the system SHALL continue to poll every 30 seconds and report progress without prematurely marking the cluster as failed
- Mouse clicks, cluster listing, cluster detail retrieval, and all non-creation API operations SHALL remain unchanged

**Scope:**
All inputs that do NOT involve a terminated Step Functions execution with a stale CREATING record should be completely unaffected by this fix. This includes:
- Normal successful cluster creation
- Normal failure with successful rollback
- Cluster destruction workflows
- Cluster recreation workflows
- Project management operations
- User management operations

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **No DynamoDB update before Fail state**: In `lib/foundation-stack.ts`, when `handleCreationFailure` itself fails, the catch goes directly to `creationFailed` (a `sfn.Fail` state). The Fail state is a terminal state that performs no logic — it cannot invoke a Lambda or update DynamoDB. The comment in the code acknowledges this: "The initial CREATING record will remain, but the Step Functions execution will be in FAILED status so operators can investigate."

2. **No timeout handler**: The state machine has `timeout: cdk.Duration.hours(2)` but Step Functions timeout termination does not trigger any catch or handler — it simply aborts the execution. There is no mechanism (EventBridge rule, post-execution hook, or periodic scanner) to detect timed-out executions and update DynamoDB.

3. **UI has no staleness detection**: The frontend polling in `app.js` (`startClusterListPolling`, `startClusterDetailPolling`) polls every 5 seconds indefinitely for CREATING clusters. It only stops polling when the status transitions to ACTIVE, FAILED, or DESTROYED. There is no timeout, no `createdAt` comparison, and no Step Functions status check to detect that the backend has stopped.

4. **API blocks actions on CREATING clusters**: In `handler.py`, `_handle_delete_cluster` rejects destroy requests unless `cluster.get("status") in ("ACTIVE", "FAILED")`. There is no force-fail or manual override mechanism for stuck CREATING clusters.

## Correctness Properties

Property 1: Bug Condition - Stuck CREATING clusters are transitioned to FAILED

_For any_ cluster where the DynamoDB record is in CREATING status and the corresponding Step Functions execution has terminated (failed, timed out, or aborted), the system SHALL update the cluster record to FAILED status with an appropriate error message, so the UI can reflect the terminal state and the user can take corrective action.

**Validates: Requirements 2.1, 2.2, 2.4**

Property 2: Preservation - Normal creation and failure flows are unchanged

_For any_ cluster creation where the Step Functions execution is still running, or where the execution completes successfully (ACTIVE), or where the rollback handler succeeds (FAILED), the system SHALL produce the same DynamoDB record state and UI behavior as the original code, preserving all existing creation, polling, and status transition logic.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lib/foundation-stack.ts`

**Change 1: Add a last-resort DynamoDB update before the Fail state**

Insert a new Step Functions task (`MarkClusterFailed`) between `handleCreationFailure.addCatch(...)` and `creationFailed`. This task uses a native DynamoDB `UpdateItem` SDK integration (not a Lambda) to set `status = FAILED` and `errorMessage` on the cluster record. The chain becomes: `handleCreationFailure` catch → `MarkClusterFailed` → `CreationFailed`. Since this is a direct SDK call (not Lambda), it has no code to fail — only the DynamoDB call itself could fail, which is far less likely than a Lambda exception.

**Change 2: Add an EventBridge rule to detect timed-out executions**

Create an EventBridge rule that matches Step Functions execution status change events for the cluster creation state machine with `status: TIMED_OUT` (and optionally `FAILED`, `ABORTED`). Target a new Lambda function (or a new step in the existing `cluster_operations` Lambda) that reads the execution input to extract `projectId` and `clusterName`, then updates the DynamoDB record to FAILED.

---

**File**: `lambda/cluster_operations/cluster_creation.py`

**Change 3: Add a `mark_cluster_failed` handler for EventBridge**

Add a new function that accepts an EventBridge event containing the Step Functions execution ARN, describes the execution to extract the input payload (`projectId`, `clusterName`), and calls `_record_failed_cluster` to set the cluster to FAILED status. This handles the timeout case and acts as a safety net for any execution termination that bypasses the rollback handler.

---

**File**: `frontend/js/app.js`

**Change 4: Add staleness detection to UI polling**

When polling a CREATING cluster, compare the current time against the cluster's `createdAt` timestamp. If the cluster has been in CREATING status for longer than a configurable threshold (e.g. 2.5 hours — slightly longer than the state machine timeout), display a warning message indicating the creation may have failed and offer a "Mark as Failed" action button.

---

**File**: `frontend/js/config.js`

**Change 5: Add staleness threshold configuration**

Add a `clusterCreationTimeoutMs` configuration value (e.g. `9000000` — 2.5 hours in milliseconds) that the UI uses to detect stale CREATING clusters.

---

**File**: `lambda/cluster_operations/handler.py`

**Change 6: Add a force-fail API endpoint**

Add a new route `POST /projects/{projectId}/clusters/{clusterName}/fail` that allows users to manually transition a stuck CREATING cluster to FAILED status. This provides a user-initiated recovery path when automated detection has not yet triggered. The endpoint should verify the cluster is in CREATING status and optionally check that the Step Functions execution is no longer running before allowing the transition.

---

**File**: `docs/project-admin/cluster-management.md`

**Change 7: Document stuck cluster recovery**

Update the cluster management documentation to describe the new staleness detection behavior, the automatic timeout recovery, and the manual force-fail option.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that simulate the two failure scenarios (rollback handler failure and state machine timeout) and verify that the DynamoDB record remains stuck in CREATING status on the unfixed code.

**Test Cases**:
1. **Rollback handler exception test**: Mock `handle_creation_failure` to raise an exception after partial cleanup. Verify the DynamoDB record remains in CREATING status (will demonstrate the bug on unfixed code).
2. **DynamoDB write failure in rollback test**: Mock `_record_failed_cluster` to raise `ClientError`. Verify the DynamoDB record remains in CREATING status (will demonstrate the bug on unfixed code).
3. **State machine timeout simulation test**: Create a cluster record in CREATING status with a `createdAt` timestamp older than 2 hours. Verify no mechanism exists to transition it to FAILED (will demonstrate the bug on unfixed code).
4. **API rejection test**: Create a cluster record in CREATING status. Call the destroy endpoint. Verify it returns a ConflictError (will demonstrate the bug on unfixed code).

**Expected Counterexamples**:
- DynamoDB cluster record remains in CREATING status after Step Functions execution has terminated
- Possible causes: no DynamoDB update in Fail state path, no timeout handler, no staleness detection

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := handleExecutionTermination_fixed(input)
  ASSERT result.clusterStatus == "FAILED"
  ASSERT result.errorMessage IS NOT EMPTY
  ASSERT uiDisplaysFailureState(input.projectId, input.clusterName)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT handleClusterCreation_original(input) == handleClusterCreation_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for normal creation success, normal rollback success, and legitimate CREATING polling, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Normal creation preservation**: Verify that a successful cluster creation still results in ACTIVE status with all resource IDs recorded — the new MarkClusterFailed task and EventBridge rule do not interfere
2. **Normal rollback preservation**: Verify that when `handle_creation_failure` succeeds, the cluster is still marked FAILED by the rollback handler (not by the new fallback mechanisms)
3. **Legitimate CREATING polling preservation**: Verify that a cluster in CREATING status with a running Step Functions execution is not prematurely marked as FAILED by the staleness detection
4. **Destroy/recreate preservation**: Verify that destroying and recreating ACTIVE and FAILED clusters continues to work unchanged

### Unit Tests

- Test `mark_cluster_failed` EventBridge handler with valid and invalid execution inputs
- Test the force-fail API endpoint with CREATING, ACTIVE, and FAILED cluster statuses
- Test UI staleness detection logic with timestamps inside and outside the threshold
- Test that `_record_failed_cluster` is called correctly from the new MarkClusterFailed state machine path

### Property-Based Tests

- Generate random cluster creation payloads and verify that successful completions always result in ACTIVE status (preservation)
- Generate random failure scenarios with varying rollback outcomes and verify the cluster always reaches FAILED status (fix checking)
- Generate random `createdAt` timestamps and verify the UI staleness detection correctly classifies clusters as stale or legitimate

### Integration Tests

- Test full cluster creation flow with injected rollback handler failure, verifying the MarkClusterFailed fallback updates DynamoDB
- Test EventBridge rule triggering on a simulated Step Functions timeout event
- Test the UI polling behavior when a cluster transitions from CREATING to FAILED via the new mechanisms
- Test the force-fail endpoint followed by a destroy and recreate cycle
