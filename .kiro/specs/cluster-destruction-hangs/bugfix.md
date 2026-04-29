# Bugfix Requirements Document

## Introduction

The cluster destruction Step Functions workflow (`hpc-cluster-destruction`) can get stuck indefinitely in its PCS sub-resource deletion polling loop. Concrete evidence from execution `tim-tiny5-destroy-1777483500` (cluster `tiny5` in project `tim`, started 2026-04-29T18:25:00) shows the root cause: the `delete_pcs_resources` step uses best-effort deletion — when `_delete_pcs_node_group` fails to delete the compute node group `pcs_ti4m46yu5g`, it returns `compute_node_group:pcs_ti4m46yu5g:failed` but the Lambda still succeeds. The workflow then proceeds to `check_pcs_deletion_status`, which polls `get_compute_node_group` and finds the resource still in ACTIVE status (deletion was never initiated). Since the resource is not in a DELETING state and will never reach `ResourceNotFoundException`, the `CheckPcsDeletionStatus → WaitForPcsDeletion` loop repeats every 30 seconds with no maximum retry count until the 2-hour state machine timeout expires. At timeout, no catch handler transitions the cluster's DynamoDB status from DESTROYING to a failed state, leaving the cluster permanently stuck.

The same unbounded-loop pattern exists in the FSx export status check loop (`CheckFsxExportStatus → WaitForExport`). Additionally, the `_is_pcs_resource_deleted` helper treats all non-`ResourceNotFoundException` errors (including throttling, access denied, or unexpected error codes) as "resource still exists", which masks real failures and extends the polling loop.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `_delete_pcs_node_group` fails to delete a PCS compute node group (returning a `:failed` result string) THEN the system swallows the failure because `delete_pcs_resources` uses best-effort deletion and always returns successfully, causing the workflow to proceed to `check_pcs_deletion_status` for a resource whose deletion was never initiated

1.2 WHEN `check_pcs_deletion_status` polls a PCS sub-resource that is still in ACTIVE status (because its deletion was never successfully initiated in `delete_pcs_resources`) THEN the system loops the `CheckPcsDeletionStatus → WaitForPcsDeletion` cycle indefinitely because `get_compute_node_group` succeeds (no `ResourceNotFoundException`), `_is_pcs_resource_deleted` returns False, and there is no maximum retry count — as observed in execution `tim-tiny5-destroy-1777483500` events 24–413+ looping every 30 seconds

1.3 WHEN the FSx data repository export task enters a non-terminal lifecycle state (e.g. PENDING or EXECUTING) and remains there indefinitely THEN the system loops the `CheckFsxExportStatus → WaitForExport` cycle with no maximum retry count, causing the destruction workflow to appear stuck until the 2-hour state machine timeout expires

1.4 WHEN the `_is_pcs_resource_deleted` helper encounters an unexpected API error (e.g. throttling, access denied, or the parent PCS cluster being deleted causing a different error code) THEN the system treats the error as "resource still exists" and returns False, causing the PCS deletion wait loop to keep polling indefinitely rather than surfacing the failure

1.5 WHEN the destruction workflow's 2-hour state machine timeout expires after being stuck in a polling loop THEN the system does not transition the cluster's DynamoDB status from DESTROYING to a failed state, leaving the cluster permanently stuck in DESTROYING status with no way for the user to retry or recover

1.6 WHEN the destruction workflow is re-run for a cluster that was partially destroyed (e.g. after a previous timeout or failure) THEN the `delete_pcs_resources` step may fail to delete sub-resources that are already gone (returning `:failed` results), and `check_pcs_deletion_status` does not distinguish between "resource never existed" and "resource still being deleted", potentially causing the same infinite polling loop on a retry

### Expected Behavior (Correct)

2.1 WHEN `_delete_pcs_node_group` fails to delete a PCS sub-resource during `delete_pcs_resources` THEN the system SHALL detect the `:failed` result and either retry the deletion or propagate the failure so the workflow does not proceed to poll for a deletion that was never initiated

2.2 WHEN the PCS sub-resource deletion polling loop has exceeded a maximum retry count THEN the system SHALL raise an error or return a failure status that causes the workflow to route to the `DestructionFailed` state rather than continuing to poll indefinitely

2.3 WHEN the FSx data repository export task has been polled beyond a maximum retry count THEN the system SHALL treat the export as failed, set `exportComplete` to true and `exportFailed` to true, and proceed to the next step so the workflow does not hang indefinitely

2.4 WHEN the `_is_pcs_resource_deleted` helper encounters an unexpected API error (any error code other than `ResourceNotFoundException`) THEN the system SHALL raise the error so it propagates to the state machine's error handler, rather than silently treating it as "resource still exists"

2.5 WHEN the destruction workflow times out or fails for any reason THEN the system SHALL transition the cluster's DynamoDB status from DESTROYING to DESTRUCTION_FAILED (or equivalent) so the user can see the failure and take corrective action (e.g. retry or force-fail)

2.6 WHEN the destruction workflow is re-run for a cluster that was partially destroyed (e.g. some PCS sub-resources already deleted, FSx already gone, IAM roles already removed) THEN each step SHALL be idempotent — it SHALL treat already-deleted resources as successful no-ops and only attempt to delete resources that still exist, so that a retry can pick up where a previous failed run left off and complete the destruction

### Unchanged Behavior (Regression Prevention)

3.1 WHEN PCS sub-resources are confirmed deleted via `ResourceNotFoundException` within the normal retry window THEN the system SHALL CONTINUE TO proceed to the `DeletePcsCluster` step as it does today

3.2 WHEN the FSx export task completes successfully (SUCCEEDED lifecycle) within the normal retry window THEN the system SHALL CONTINUE TO proceed to PCS resource deletion as it does today

3.3 WHEN the FSx export task fails or is cancelled (FAILED or CANCELED lifecycle) THEN the system SHALL CONTINUE TO set `exportFailed` to true and proceed, preserving the existing failure-handling behaviour

3.4 WHEN PCS resource deletion fails with an `InternalError` THEN the system SHALL CONTINUE TO route to the `DestructionFailed` state via the existing `addCatch` error handler

3.5 WHEN the destruction workflow completes successfully (all resources cleaned up) THEN the system SHALL CONTINUE TO mark the cluster as DESTROYED in DynamoDB and report SUCCESS

3.6 WHEN the FSx filesystem is not found during export task creation THEN the system SHALL CONTINUE TO skip the export step gracefully by setting `exportSkipped` to true

3.7 WHEN the cluster has no PCS resources (empty `pcsClusterId`) THEN the system SHALL CONTINUE TO skip PCS polling and proceed directly through the remaining cleanup steps

3.8 WHEN `_delete_pcs_node_group` succeeds and the sub-resource transitions to DELETING status THEN the system SHALL CONTINUE TO poll via `check_pcs_deletion_status` until `ResourceNotFoundException` confirms deletion, as it does today

3.9 WHEN individual destruction steps already handle `ResourceNotFoundException`, `FileSystemNotFound`, `NoSuchEntity`, or similar "already deleted" responses gracefully THEN the system SHALL CONTINUE TO treat those as successful no-ops, preserving the existing idempotent handling in `delete_fsx_filesystem`, `delete_pcs_cluster_step`, `delete_iam_resources`, and `delete_launch_templates`
