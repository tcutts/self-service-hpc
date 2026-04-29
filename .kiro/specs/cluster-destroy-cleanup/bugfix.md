# Bugfix Requirements Document

## Introduction

The cluster destruction Step Functions workflow has three defects that leave orphaned AWS resources after a cluster is destroyed. The `delete_pcs_resources` step fires async PCS deletion calls but does not wait for them to complete, causing the subsequent cluster deletion to fail. Despite these failures, the workflow continues to mark the cluster as DESTROYED in DynamoDB, masking the fact that PCS resources (clusters, node groups, queues) are still running and accruing costs. Additionally, the destroy workflow never removes the cluster name from the `ClusterNameRegistry` DynamoDB table, permanently consuming those names and preventing reuse.

Evidence: 4 orphaned PCS clusters remain ACTIVE in AWS (tiny4, tiny5, my-tiny-cluster, testy), 5 orphaned ClusterNameRegistry entries exist for destroyed clusters, and the Step Functions execution for tiny5 shows `cluster:pcs_ejmlboy4nz:failed` in `pcsCleanupResults` yet the workflow reported SUCCESS.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `delete_pcs_resources` calls `delete_compute_node_group` and `delete_queue` (which are async operations that return immediately) THEN the system immediately attempts to delete the PCS cluster before node groups and queues have finished deleting, causing the cluster deletion to fail with a dependency error

1.2 WHEN PCS resource deletion fails (node groups, queues, or cluster deletion returns a `:failed` result in `pcsCleanupResults`) THEN the system continues the workflow to subsequent steps without raising an error or retrying, because the `delete_pcs_resources` handler uses best-effort deletion and always returns successfully

1.3 WHEN the destruction workflow completes with PCS cleanup failures THEN the system marks the cluster as DESTROYED in DynamoDB via `record_cluster_destroyed` and the state machine reports SUCCESS, even though PCS resources (clusters, node groups) are still ACTIVE in AWS

1.4 WHEN a cluster is destroyed THEN the system never removes the cluster name from the `ClusterNameRegistry` DynamoDB table, because no `deregister_cluster_name` step exists in the destruction workflow's `_STEP_DISPATCH` table or state machine definition

1.5 WHEN a previously destroyed cluster's name is used to create a new cluster in the same project THEN the `register_cluster_name` conditional put succeeds (same project re-registration is allowed), but the stale registry entry from the destroyed cluster was never cleaned up, leaving unnecessary data in the table

1.6 WHEN a previously destroyed cluster's name is used to create a new cluster in a different project THEN the system rejects the name with a `ConflictError` because the stale `ClusterNameRegistry` entry from the destroyed cluster still exists and belongs to the original project

### Expected Behavior (Correct)

2.1 WHEN `delete_pcs_resources` initiates deletion of compute node groups and queues THEN the system SHALL wait for each async deletion to complete (by polling PCS resource status) before attempting to delete the PCS cluster, ensuring dependencies are removed first

2.2 WHEN any PCS resource deletion fails after retries/polling THEN the system SHALL propagate the failure by raising an error or returning a failure status that prevents the workflow from continuing to `record_cluster_destroyed`

2.3 WHEN the destruction workflow encounters unrecoverable PCS cleanup failures THEN the system SHALL NOT mark the cluster as DESTROYED in DynamoDB, and the state machine SHALL report FAILED so that administrators are aware of orphaned resources

2.4 WHEN a cluster is destroyed THEN the system SHALL remove the cluster name from the `ClusterNameRegistry` DynamoDB table as part of the destruction workflow, freeing the name for reuse by any project

2.5 WHEN a previously destroyed cluster's name is used to create a new cluster in the same project THEN the system SHALL allow it without encountering stale registry data, because the name was deregistered during destruction

2.6 WHEN a previously destroyed cluster's name is used to create a new cluster in a different project THEN the system SHALL allow it without a `ConflictError`, because the name was deregistered during destruction and is available for any project

### Unchanged Behavior (Regression Prevention)

3.1 WHEN PCS resource deletion succeeds for all resources (node groups, queues, and cluster) THEN the system SHALL CONTINUE TO mark the cluster as DESTROYED in DynamoDB and the state machine SHALL report SUCCESS

3.2 WHEN the FSx data repository export task is created and polled THEN the system SHALL CONTINUE TO wait for export completion before proceeding to PCS resource deletion, preserving the existing export-then-delete ordering

3.3 WHEN the FSx filesystem is deleted after PCS cleanup THEN the system SHALL CONTINUE TO handle `FileSystemNotFound` gracefully by treating it as already deleted

3.4 WHEN cluster-specific IAM resources (roles, instance profiles) are deleted THEN the system SHALL CONTINUE TO use best-effort deletion with logging, attempting all resources regardless of individual failures

3.5 WHEN cluster-specific launch templates are deleted THEN the system SHALL CONTINUE TO use best-effort deletion with logging, handling `InvalidLaunchTemplateName.NotFoundException` gracefully

3.6 WHEN a cluster name is registered during cluster creation THEN the system SHALL CONTINUE TO use the existing conditional put logic that allows same-project re-registration and rejects cross-project conflicts

3.7 WHEN the Mountpoint S3 inline policy removal step runs for mountpoint-mode clusters THEN the system SHALL CONTINUE TO silently ignore `NoSuchEntity` errors for clusters that were created in lustre mode

3.8 WHEN Home_Directory (EFS) and Project_Storage (S3) resources exist for a destroyed cluster THEN the system SHALL CONTINUE TO retain them — they are NOT deleted during cluster destruction
