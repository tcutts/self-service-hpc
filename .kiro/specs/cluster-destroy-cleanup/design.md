# Cluster Destroy Cleanup Bugfix Design

## Overview

The cluster destruction Step Functions workflow has three interrelated defects that leave orphaned AWS resources and stale DynamoDB entries after a cluster is destroyed. The `delete_pcs_resources` step fires async PCS deletion API calls but does not wait for them to complete, causing the subsequent PCS cluster deletion to fail with a dependency error. Despite these failures, the workflow uses best-effort error handling that swallows errors and continues to mark the cluster as DESTROYED. Additionally, the workflow never removes the cluster name from the `ClusterNameRegistry` table, permanently blocking cross-project name reuse.

The fix involves three changes: (1) add polling logic to `delete_pcs_resources` so it waits for async PCS deletions to complete before deleting the cluster, (2) propagate PCS cleanup failures so the state machine halts and does not mark the cluster as DESTROYED, and (3) add a `deregister_cluster_name` step to the destruction workflow that removes the cluster name from the registry.

## Glossary

- **Bug_Condition (C)**: The set of inputs where the destruction workflow either (a) attempts PCS cluster deletion before sub-resources finish deleting, (b) swallows PCS cleanup failures and marks the cluster DESTROYED, or (c) skips cluster name deregistration
- **Property (P)**: The desired behavior — PCS deletions are awaited, failures halt the workflow, and cluster names are deregistered
- **Preservation**: Existing behaviors that must remain unchanged — FSx export ordering, IAM/launch template best-effort cleanup, EFS/S3 retention, successful-path DESTROYED marking
- **`delete_pcs_resources`**: The function in `lambda/cluster_operations/cluster_destruction.py` that deletes PCS compute node groups, queues, and the cluster
- **`record_cluster_destroyed`**: The function in `lambda/cluster_operations/cluster_destruction.py` that marks the cluster as DESTROYED in DynamoDB
- **`ClusterNameRegistry`**: A DynamoDB table that tracks cluster name ownership across projects, with entries keyed by `CLUSTERNAME#{name}`
- **PCS**: AWS Parallel Computing Service — the managed HPC cluster service whose resources (clusters, node groups, queues) are being cleaned up

## Bug Details

### Bug Condition

The bug manifests in three scenarios during cluster destruction:

1. **Async deletion race**: `delete_pcs_resources` calls `pcs_client.delete_compute_node_group()` and `pcs_client.delete_queue()` which return immediately while the resources are still deleting. The function then immediately calls `pcs_client.delete_cluster()` which fails because dependencies still exist.

2. **Silent failure**: The `_delete_pcs_node_group`, `_delete_pcs_queue`, and `_delete_pcs_cluster` helpers catch all `ClientError` exceptions, log warnings, and return `:failed` result strings. The parent function `delete_pcs_resources` collects these strings but never checks them — it always returns successfully, allowing the state machine to proceed to `record_cluster_destroyed`.

3. **Missing deregistration**: The `_STEP_DISPATCH` table and the CDK state machine definition have no step for removing the cluster name from `ClusterNameRegistry`. No `deregister_cluster_name` function exists.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type DestructionWorkflowEvent
  OUTPUT: boolean
  
  hasPcsResources := input.pcsClusterId != ""
                     AND (input.computeNodeGroupId != "" OR input.loginNodeGroupId != "" OR input.queueId != "")
  
  hasClusterName := input.clusterName != ""
  
  RETURN hasPcsResources OR hasClusterName
END FUNCTION
```

Note: The bug condition is extremely broad — virtually every cluster destruction triggers at least one of the three defects. The `hasPcsResources` condition triggers bugs 1 and 2 (async race + silent failure). The `hasClusterName` condition triggers bug 3 (missing deregistration).

### Examples

- **Async race**: Cluster `tiny5` with `pcsClusterId=pcs_ejmlboy4nz`, two node groups, and a queue. `delete_pcs_resources` fires all four deletion calls, but `delete_cluster` fails because node groups are still `DELETING`. Result: `cluster:pcs_ejmlboy4nz:failed` in `pcsCleanupResults`, but workflow continues to SUCCESS.
- **Silent failure masking**: Cluster `tiny4` destruction completes with `pcsCleanupResults` containing `:failed` entries. DynamoDB shows `status=DESTROYED` but the PCS cluster is still ACTIVE in AWS, accruing costs.
- **Name registry leak**: Cluster `my-tiny-cluster` in project `proj-A` is destroyed. The `ClusterNameRegistry` entry `CLUSTERNAME#my-tiny-cluster` still exists with `projectId=proj-A`. A user in `proj-B` tries to create a cluster with the same name and gets `ConflictError`.
- **Successful path (not buggy for deregistration)**: A cluster with no PCS resources (e.g., `pcsClusterId=""`) still has the name registry leak but PCS cleanup succeeds vacuously.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- When all PCS resource deletions succeed, the workflow must continue to mark the cluster as DESTROYED and report SUCCESS (requirement 3.1)
- FSx data repository export must continue to be awaited before PCS deletion begins (requirement 3.2)
- FSx `FileSystemNotFound` must continue to be handled gracefully as already-deleted (requirement 3.3)
- IAM role and instance profile cleanup must continue to use best-effort deletion with logging (requirement 3.4)
- Launch template cleanup must continue to use best-effort deletion, handling `InvalidLaunchTemplateName.NotFoundException` gracefully (requirement 3.5)
- Cluster name registration during creation must continue to use the existing conditional put logic (requirement 3.6)
- Mountpoint S3 policy removal must continue to silently ignore `NoSuchEntity` errors (requirement 3.7)
- Home_Directory (EFS) and Project_Storage (S3) must continue to be retained after destruction (requirement 3.8)

**Scope:**
All inputs that do NOT involve PCS resource deletion or cluster name management should be completely unaffected by this fix. This includes:
- FSx export task creation and polling
- FSx filesystem deletion
- IAM resource cleanup
- Launch template cleanup
- Mountpoint S3 policy removal

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **No polling after async PCS deletions**: The `_delete_pcs_node_group` and `_delete_pcs_queue` helpers call the PCS API and return immediately. PCS `delete_compute_node_group` and `delete_queue` are async operations — they initiate deletion and return while the resource transitions through `DELETING` state. The code never polls `pcs_client.describe_cluster` or checks resource status before proceeding to delete the cluster itself. The cluster deletion then fails with a dependency/conflict error because sub-resources still exist.

2. **Best-effort error handling prevents failure propagation**: The `_delete_pcs_node_group`, `_delete_pcs_queue`, and `_delete_pcs_cluster` helpers wrap all PCS API calls in `try/except ClientError` blocks that log warnings and return `:failed` strings. The parent `delete_pcs_resources` function collects these strings into `pcsCleanupResults` but never inspects them for failures. It always returns a successful dict, so the Step Functions state machine proceeds to the next step.

3. **Missing deregistration step**: The destruction workflow was implemented without a cluster name deregistration step. The `cluster_names.py` module has `register_cluster_name` and `lookup_cluster_name` but no `deregister_cluster_name` function. The `_STEP_DISPATCH` table in `cluster_destruction.py` has no entry for deregistration, and the CDK state machine definition in `cluster-operations.ts` has no corresponding step.

4. **Missing environment variable**: The destruction step Lambda's environment only includes `CLUSTERS_TABLE_NAME`. It does not have `CLUSTER_NAME_REGISTRY_TABLE_NAME`, which would be needed for the new deregistration step.

## Correctness Properties

Property 1: Bug Condition - PCS Deletion Awaits Sub-Resource Completion

_For any_ destruction workflow input where `pcsClusterId` is non-empty and at least one of `computeNodeGroupId`, `loginNodeGroupId`, or `queueId` is non-empty, the fixed `delete_pcs_resources` function SHALL wait for node group and queue deletions to reach a terminal state (deleted or not-found) before attempting to delete the PCS cluster.

**Validates: Requirements 2.1**

Property 2: Bug Condition - PCS Cleanup Failures Propagate

_For any_ destruction workflow input where PCS resource deletion fails after polling/retries (e.g., a node group is stuck in a non-deletable state), the fixed `delete_pcs_resources` function SHALL raise an error or return a failure indicator that prevents the state machine from reaching `record_cluster_destroyed`.

**Validates: Requirements 2.2, 2.3**

Property 3: Bug Condition - Cluster Name Deregistered on Destruction

_For any_ destruction workflow input where `clusterName` is non-empty, the fixed destruction workflow SHALL remove the corresponding `CLUSTERNAME#{clusterName}` entry from the `ClusterNameRegistry` DynamoDB table, freeing the name for reuse by any project.

**Validates: Requirements 2.4, 2.5, 2.6**

Property 4: Preservation - Successful Cleanup Still Marks DESTROYED

_For any_ destruction workflow input where all PCS resource deletions succeed (all sub-resources reach terminal state and cluster deletion succeeds), the fixed workflow SHALL continue to mark the cluster as DESTROYED in DynamoDB and the state machine SHALL report SUCCESS, preserving the existing successful-path behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lambda/cluster_operations/cluster_destruction.py`

**Function**: `delete_pcs_resources`

**Specific Changes**:

1. **Add PCS resource status polling**: After calling `_delete_pcs_node_group` for each node group and `_delete_pcs_queue`, add a polling loop that calls `pcs_client.describe_compute_node_group` (or catches `ResourceNotFoundException` to confirm deletion) before proceeding. Use a configurable max-wait with backoff. The Step Functions wait loop pattern (return a status flag and let the state machine re-invoke) is preferred over in-Lambda polling to avoid Lambda timeout issues.

2. **Split PCS deletion into initiate + poll steps**: Refactor `delete_pcs_resources` into two functions:
   - `delete_pcs_resources` — initiates deletion of node groups and queues (fire-and-forget as today, but without attempting cluster deletion)
   - `check_pcs_deletion_status` — polls PCS to check if node groups and queues have finished deleting; returns `pcsSubResourcesDeleted: true/false`
   - Once sub-resources are confirmed deleted, a third invocation or the existing function deletes the PCS cluster
   - Alternatively, add a `delete_pcs_cluster` step that only runs after sub-resources are confirmed gone

3. **Propagate failures**: Change the PCS deletion helpers to raise errors (or change `delete_pcs_resources` to inspect `pcsCleanupResults` for `:failed` entries and raise `InternalError`) so that the Step Functions state machine catches the error and does NOT proceed to `record_cluster_destroyed`.

4. **Add `deregister_cluster_name` function**: In `lambda/cluster_operations/cluster_names.py`, add a function that deletes the `CLUSTERNAME#{clusterName}` item from the `ClusterNameRegistry` table.

5. **Add `deregister_cluster_name` step handler**: In `lambda/cluster_operations/cluster_destruction.py`, add a step handler that calls the new deregister function. Add it to `_STEP_DISPATCH`. This step should run after PCS cleanup succeeds but before (or alongside) `record_cluster_destroyed`.

6. **Update CDK state machine**: In `lib/constructs/cluster-operations.ts`:
   - Add a `CheckPcsDeletionStatus` step with a wait loop (similar to the FSx export wait loop)
   - Split the PCS deletion into initiate → wait → delete cluster
   - Add a `DeregisterClusterName` step in the chain before `RecordClusterDestroyed`
   - Add error handling / catch on PCS steps so failures route to a Fail state instead of continuing

7. **Update Lambda environment**: Add `CLUSTER_NAME_REGISTRY_TABLE_NAME` to the destruction step Lambda's environment variables. Grant the Lambda `dynamodb:DeleteItem` permission on the `ClusterNameRegistry` table.

**File**: `lambda/cluster_operations/cluster_names.py`

**Function**: New `deregister_cluster_name`

**Specific Changes**:
1. Add a `deregister_cluster_name(table_name, cluster_name)` function that calls `table.delete_item(Key={"PK": f"CLUSTERNAME#{cluster_name}", "SK": "REGISTRY"})` with best-effort error handling (log and continue if the item doesn't exist).

**File**: `lib/constructs/cluster-operations.ts`

**Specific Changes**:
1. Add `CLUSTER_NAME_REGISTRY_TABLE_NAME` environment variable to `clusterDestructionStepLambda`
2. Grant `clusterNameRegistryTable.grantReadWriteData(clusterDestructionStepLambda)`
3. Add new Step Functions steps for PCS deletion polling and cluster name deregistration
4. Add error catching on PCS deletion steps that routes to a Fail state
5. Insert `DeregisterClusterName` step before `RecordClusterDestroyed`

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bugs on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write unit tests that mock PCS API calls and verify the current behavior of `delete_pcs_resources` and the absence of cluster name deregistration. Run these tests on the UNFIXED code to observe the defective behavior.

**Test Cases**:
1. **Async Race Test**: Mock `pcs_client.delete_compute_node_group` to return success, then mock `pcs_client.delete_cluster` to raise `ConflictException` (dependency still exists). Verify `delete_pcs_resources` returns successfully despite the cluster deletion failure (will demonstrate bug on unfixed code).
2. **Silent Failure Test**: Mock all PCS deletion calls to raise `ClientError`. Verify `delete_pcs_resources` returns a successful dict with `:failed` entries in `pcsCleanupResults` but no exception raised (will demonstrate bug on unfixed code).
3. **Missing Deregistration Test**: Call the destruction workflow steps and verify no call is made to delete from `ClusterNameRegistry` (will demonstrate bug on unfixed code).
4. **State Machine Chain Test**: Verify the CDK state machine definition has no error catching between `DeletePcsResources` and `RecordClusterDestroyed` (will demonstrate bug on unfixed code).

**Expected Counterexamples**:
- `delete_pcs_resources` returns successfully even when `_delete_pcs_cluster` returns `:failed`
- No `deregister_cluster_name` function exists in `cluster_names.py`
- Possible causes: best-effort error handling, missing polling logic, missing deregistration step

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed functions produce the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := delete_pcs_resources_fixed(input)
  ASSERT pcsSubResourcesAwaitedBeforeClusterDeletion(result)
  ASSERT failuresPropagate(result) OR allDeletionsSucceeded(result)
  
  IF input.clusterName != "" THEN
    ASSERT clusterNameDeregistered(input.clusterName)
  END IF
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold (or where all deletions succeed), the fixed functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT delete_pcs_resources_original(input) = delete_pcs_resources_fixed(input)
END FOR

FOR ALL input WHERE allPcsDeletionsSucceed(input) DO
  ASSERT workflowReachesRecordClusterDestroyed(input)
  ASSERT clusterStatusSetToDestroyed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss (e.g., empty string IDs, missing fields)
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for successful PCS deletions and non-PCS steps, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Successful PCS Cleanup Preservation**: Verify that when all PCS API calls succeed, the fixed `delete_pcs_resources` still returns a successful result with the same event structure
2. **FSx Export Preservation**: Verify that FSx export task creation and polling behavior is unchanged
3. **IAM Cleanup Preservation**: Verify that IAM best-effort deletion behavior is unchanged
4. **Launch Template Preservation**: Verify that launch template best-effort deletion behavior is unchanged
5. **Registration Logic Preservation**: Verify that `register_cluster_name` conditional put logic is unchanged

### Unit Tests

- Test `delete_pcs_resources` with mocked PCS client: verify polling waits for sub-resources to be deleted
- Test `delete_pcs_resources` with mocked PCS client: verify failure propagation when deletion fails
- Test `deregister_cluster_name` with mocked DynamoDB: verify item deletion
- Test `deregister_cluster_name` with non-existent item: verify graceful handling
- Test `check_pcs_deletion_status` with various resource states (DELETING, DELETED, not found)
- Test `record_cluster_destroyed` is not reached when PCS cleanup fails (integration with state machine)

### Property-Based Tests

- Generate random destruction event payloads (varying combinations of pcsClusterId, nodeGroupIds, queueId, clusterName) and verify the fixed `delete_pcs_resources` always waits for sub-resources before cluster deletion
- Generate random PCS API response sequences (success, failure, not-found) and verify failure propagation is consistent
- Generate random cluster names and verify `deregister_cluster_name` correctly removes the registry entry
- Generate random destruction events where all PCS calls succeed and verify the workflow still marks DESTROYED (preservation)

### Integration Tests

- Test full destruction workflow with mocked AWS services: verify PCS resources are awaited, failures halt the workflow, and cluster name is deregistered
- Test destruction of a cluster with no PCS resources (empty IDs): verify the workflow still completes and deregisters the name
- Test destruction of a lustre-mode cluster vs mountpoint-mode cluster: verify both paths work correctly with the new steps
- Test CDK synthesis: verify the updated state machine definition includes the new steps, wait loops, and error handling
