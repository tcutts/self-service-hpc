"""Cluster destruction workflow step handlers.

Each function in this module implements a single step of the cluster
destruction Step Functions state machine.  Every handler receives an
``event`` dict (the state machine payload) and returns a dict that
Step Functions passes to the next step.

The destruction workflow:
    1. Create FSx data repository export task (sync data back to S3)
    2. Wait for export to complete (with failure handling)
    3. Initiate deletion of PCS compute node groups and queue
    4. Poll PCS sub-resource deletion status until complete
    5. Delete PCS cluster (after sub-resources confirmed deleted)
    6. Delete FSx for Lustre filesystem
    7. Deregister cluster name from ClusterNameRegistry
    8. Update DynamoDB cluster record (status DESTROYED, destroyedAt)

**Important**: Home_Directory (EFS) and Project_Storage (S3) are
retained after destruction — they are NOT deleted.

Environment variables
---------------------
CLUSTERS_TABLE_NAME                DynamoDB Clusters table
CLUSTER_NAME_REGISTRY_TABLE_NAME   DynamoDB ClusterNameRegistry table

Expected event keys
-------------------
projectId, clusterName
pcsClusterId, loginNodeGroupId, computeNodeGroupId, queueId
fsxFilesystemId
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

import cluster_names
from errors import InternalError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
ec2_client = boto3.client("ec2")
fsx_client = boto3.client("fsx")
iam_client = boto3.client("iam")
logs_client = boto3.client("logs")
pcs_client = boto3.client("pcs")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")

# ---------------------------------------------------------------------------
# Polling retry limits
# ---------------------------------------------------------------------------
MAX_PCS_DELETION_RETRIES = 120  # 120 iterations × 30s wait = ~60 minutes
MAX_EXPORT_RETRIES = 60         # 60 iterations × 60s wait = ~60 minutes

# ---------------------------------------------------------------------------
# PCS vended log delivery suffixes
# ---------------------------------------------------------------------------
_PCS_LOG_SUFFIXES = ["scheduler-logs", "scheduler-audit-logs", "jobcomp-logs"]

# ---------------------------------------------------------------------------
# Step progress tracking
# ---------------------------------------------------------------------------
TOTAL_STEPS = 8

STEP_LABELS: dict[int, str] = {
    1: "Exporting data to S3",
    2: "Checking export status",
    3: "Deleting compute resources",
    4: "Waiting for resource cleanup",
    5: "Deleting cluster",
    6: "Deleting filesystem",
    7: "Cleaning up IAM and templates",
    8: "Finalising destruction",
}


def _update_step_progress(
    project_id: str,
    cluster_name: str,
    step_number: int,
) -> None:
    """Write the current step progress to the DynamoDB Clusters record.

    Creates or updates the cluster record with ``currentStep``,
    ``totalSteps``, and ``stepDescription`` so the GET endpoint can
    report progress to the UI.
    """
    step_description = STEP_LABELS.get(step_number, f"Step {step_number}")

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    try:
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            UpdateExpression=(
                "SET currentStep = :step, totalSteps = :total, "
                "stepDescription = :desc, #st = :status"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":step": step_number,
                ":total": TOTAL_STEPS,
                ":desc": step_description,
                ":status": "DESTROYING",
            },
        )
        logger.info(
            "Progress updated for cluster '%s': step %d/%d — %s",
            cluster_name,
            step_number,
            TOTAL_STEPS,
            step_description,
        )
    except ClientError as exc:
        # Progress tracking failure is non-fatal — log and continue
        logger.warning(
            "Failed to update progress for cluster '%s': %s",
            cluster_name,
            exc,
        )


# ---------------------------------------------------------------------------
# Step dispatcher — maps step names from the Step Functions state machine
# to the corresponding handler functions in this module.
# ---------------------------------------------------------------------------
_STEP_DISPATCH: dict[str, Any] = {}  # populated after function definitions


def step_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry-point invoked by the cluster destruction state machine.

    The state machine sends ``{"step": "<step_name>", "payload": {...}}``.
    This dispatcher routes to the matching function and passes the payload.
    """
    step = event.get("step", "")
    payload = event.get("payload", event)

    handler_fn = _STEP_DISPATCH.get(step)
    if handler_fn is None:
        raise ValueError(f"Unknown cluster-destruction step: '{step}'")

    logger.info("Dispatching cluster-destruction step: %s", step)
    return handler_fn(payload)


# ===================================================================
# Step 1 — Create FSx data repository export task
# ===================================================================

def create_fsx_export_task(event: dict[str, Any]) -> dict[str, Any]:
    """Create an FSx data repository export task to sync data back to S3.

    Before the FSx filesystem is deleted, all data must be exported
    back to the associated S3 bucket via a data repository task.

    Adds ``exportTaskId`` to the returned event.
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    _update_step_progress(project_id, cluster_name, 1)

    fsx_filesystem_id: str = event.get("fsxFilesystemId", "")

    if not fsx_filesystem_id:
        logger.info("No FSx filesystem to export — skipping export step")
        return {**event, "exportTaskId": "", "exportSkipped": True}

    try:
        response = fsx_client.create_data_repository_task(
            Type="EXPORT_TO_REPOSITORY",
            FileSystemId=fsx_filesystem_id,
            Report={
                "Enabled": True,
                "Path": f"/{fsx_filesystem_id}/export-reports/",
                "Format": "REPORT_CSV_20191124",
                "Scope": "FAILED_FILES_ONLY",
            },
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        # If the filesystem is already gone, treat as skipped
        if error_code in ("FileSystemNotFound", "BadRequest"):
            logger.warning(
                "FSx filesystem '%s' not found or invalid — skipping export: %s",
                fsx_filesystem_id,
                exc,
            )
            return {**event, "exportTaskId": "", "exportSkipped": True}
        raise InternalError(
            f"Failed to create FSx export task for '{fsx_filesystem_id}': {exc}"
        )

    task_id = response["DataRepositoryTask"]["TaskId"]
    logger.info(
        "FSx export task '%s' created for filesystem '%s'",
        task_id,
        fsx_filesystem_id,
    )

    return {**event, "exportTaskId": task_id, "exportSkipped": False}


# ===================================================================
# Step 2 — Check FSx export task status
# ===================================================================

def check_fsx_export_status(event: dict[str, Any]) -> dict[str, Any]:
    """Poll the FSx data repository export task status.

    Returns the event with:
    - ``exportComplete``: True when the export has finished (success or skip)
    - ``exportFailed``: True if the export failed — Step Functions
      should pause and alert the Project Administrator before
      proceeding with filesystem deletion.

    On failure the FSx filesystem is NOT deleted automatically so
    that the administrator can investigate and retry or accept data
    loss.
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    _update_step_progress(project_id, cluster_name, 2)

    if event.get("exportSkipped", False):
        logger.info("Export was skipped — marking as complete")
        return {**event, "exportComplete": True, "exportFailed": False}

    export_task_id: str = event.get("exportTaskId", "")
    fsx_filesystem_id: str = event.get("fsxFilesystemId", "")

    if not export_task_id:
        logger.info("No export task ID — marking as complete")
        return {**event, "exportComplete": True, "exportFailed": False}

    # Track and enforce bounded retry count
    export_retry_count: int = event.get("exportRetryCount", 0) + 1

    if export_retry_count > MAX_EXPORT_RETRIES:
        logger.error(
            "FSx export polling timed out for filesystem '%s' after "
            "%d iterations (~%d minutes)",
            fsx_filesystem_id,
            MAX_EXPORT_RETRIES,
            MAX_EXPORT_RETRIES,
        )
        return {
            **event,
            "exportComplete": True,
            "exportFailed": True,
            "exportRetryCount": export_retry_count,
            "exportFailureReason": (
                f"Export polling timed out after {MAX_EXPORT_RETRIES} "
                f"iterations (~{MAX_EXPORT_RETRIES} minutes)"
            ),
        }

    try:
        response = fsx_client.describe_data_repository_tasks(
            TaskIds=[export_task_id],
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to describe FSx export task '{export_task_id}': {exc}"
        )

    tasks = response.get("DataRepositoryTasks", [])
    if not tasks:
        raise InternalError(f"FSx export task '{export_task_id}' not found.")

    task = tasks[0]
    lifecycle = task.get("Lifecycle", "")

    logger.info(
        "FSx export task '%s' for filesystem '%s' status: %s (retry=%d/%d)",
        export_task_id,
        fsx_filesystem_id,
        lifecycle,
        export_retry_count,
        MAX_EXPORT_RETRIES,
    )

    if lifecycle == "SUCCEEDED":
        return {
            **event,
            "exportComplete": True,
            "exportFailed": False,
            "exportRetryCount": export_retry_count,
        }

    if lifecycle in ("FAILED", "CANCELED"):
        failure_reason = task.get("FailureDetails", {}).get("Message", "Unknown")
        logger.error(
            "FSx export task '%s' %s: %s",
            export_task_id,
            lifecycle,
            failure_reason,
        )
        return {
            **event,
            "exportComplete": True,
            "exportFailed": True,
            "exportFailureReason": failure_reason,
            "exportRetryCount": export_retry_count,
        }

    # Still in progress (PENDING, EXECUTING, etc.)
    return {
        **event,
        "exportComplete": False,
        "exportFailed": False,
        "exportRetryCount": export_retry_count,
    }


# ===================================================================
# Step 3 — Delete PCS resources
# ===================================================================

def delete_pcs_resources(event: dict[str, Any]) -> dict[str, Any]:
    """Initiate deletion of PCS compute node groups and queue.

    This step only *initiates* async deletion of sub-resources (node
    groups and queue).  It does NOT delete the PCS cluster itself —
    that is handled by the separate ``delete_pcs_cluster_step`` after
    the state machine has confirmed sub-resources are fully deleted
    via ``check_pcs_deletion_status``.

    Deletion order:
    1. Compute node group (workers)
    2. Login node group (head node)
    3. Queue

    Each deletion is best-effort — failures are logged but do not
    prevent subsequent deletions from being attempted.

    Adds ``pcsCleanupResults`` to the returned event.
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    _update_step_progress(project_id, cluster_name, 3)

    pcs_cluster_id: str = event.get("pcsClusterId", "")
    compute_node_group_id: str = event.get("computeNodeGroupId", "")
    login_node_group_id: str = event.get("loginNodeGroupId", "")
    queue_id: str = event.get("queueId", "")

    cleanup_results: list[str] = []

    # 1. Delete compute node group
    if compute_node_group_id and pcs_cluster_id:
        cleanup_results.append(
            _delete_pcs_node_group(pcs_cluster_id, compute_node_group_id, "compute")
        )

    # 2. Delete login node group
    if login_node_group_id and pcs_cluster_id:
        cleanup_results.append(
            _delete_pcs_node_group(pcs_cluster_id, login_node_group_id, "login")
        )

    # 3. Delete queue
    if queue_id and pcs_cluster_id:
        cleanup_results.append(
            _delete_pcs_queue(pcs_cluster_id, queue_id)
        )

    logger.info(
        "PCS sub-resource deletion initiated for cluster '%s': %s",
        cluster_name,
        "; ".join(cleanup_results) if cleanup_results else "no sub-resources",
    )

    # Detect failed sub-resource deletions and propagate the error
    failed_results = [r for r in cleanup_results if r.endswith(":failed")]
    if failed_results:
        raise InternalError(
            f"PCS sub-resource deletion failed for cluster '{cluster_name}': "
            f"{'; '.join(failed_results)}"
        )

    return {**event, "pcsCleanupResults": cleanup_results}


# ===================================================================
# Step 3b — Check PCS sub-resource deletion status
# ===================================================================

def check_pcs_deletion_status(event: dict[str, Any]) -> dict[str, Any]:
    """Poll PCS to check whether node groups and queues have finished deleting.

    For each non-empty sub-resource ID, calls the corresponding PCS
    describe API.  A ``ResourceNotFoundException`` confirms the resource
    has been deleted.  Any other response means the resource is still
    in a transitional state (e.g. DELETING).

    Tracks the number of polling iterations via ``pcsRetryCount`` in the
    event.  If the count exceeds ``MAX_PCS_DELETION_RETRIES``, raises
    ``InternalError`` to halt the loop and route to the failure handler.

    Returns the event with ``pcsSubResourcesDeleted`` set to True when
    all sub-resources are confirmed deleted, or False if any are still
    in progress.  Empty IDs are skipped (treated as already deleted).
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    _update_step_progress(project_id, cluster_name, 4)

    pcs_cluster_id: str = event.get("pcsClusterId", "")
    compute_node_group_id: str = event.get("computeNodeGroupId", "")
    login_node_group_id: str = event.get("loginNodeGroupId", "")
    queue_id: str = event.get("queueId", "")

    # If there is no PCS cluster, nothing to poll
    if not pcs_cluster_id:
        logger.info("No PCS cluster ID — skipping sub-resource polling")
        return {**event, "pcsSubResourcesDeleted": True}

    # Track and enforce bounded retry count
    pcs_retry_count: int = event.get("pcsRetryCount", 0) + 1

    if pcs_retry_count > MAX_PCS_DELETION_RETRIES:
        raise InternalError(
            f"PCS sub-resource deletion polling timed out for cluster "
            f"'{cluster_name}' after {MAX_PCS_DELETION_RETRIES} iterations "
            f"(~{MAX_PCS_DELETION_RETRIES * 30 // 60} minutes)"
        )

    all_deleted = True

    # Check compute node group
    if compute_node_group_id:
        if not _is_pcs_resource_deleted(
            lambda: pcs_client.get_compute_node_group(
                clusterIdentifier=pcs_cluster_id,
                computeNodeGroupIdentifier=compute_node_group_id,
            ),
            f"compute node group '{compute_node_group_id}'",
        ):
            all_deleted = False

    # Check login node group
    if login_node_group_id:
        if not _is_pcs_resource_deleted(
            lambda: pcs_client.get_compute_node_group(
                clusterIdentifier=pcs_cluster_id,
                computeNodeGroupIdentifier=login_node_group_id,
            ),
            f"login node group '{login_node_group_id}'",
        ):
            all_deleted = False

    # Check queue
    if queue_id:
        if not _is_pcs_resource_deleted(
            lambda: pcs_client.get_queue(
                clusterIdentifier=pcs_cluster_id,
                queueIdentifier=queue_id,
            ),
            f"queue '{queue_id}'",
        ):
            all_deleted = False

    logger.info(
        "PCS sub-resource deletion status: all_deleted=%s (cluster=%s, retry=%d/%d)",
        all_deleted,
        pcs_cluster_id,
        pcs_retry_count,
        MAX_PCS_DELETION_RETRIES,
    )

    return {**event, "pcsSubResourcesDeleted": all_deleted, "pcsRetryCount": pcs_retry_count}


def _is_pcs_resource_deleted(describe_fn, resource_label: str) -> bool:
    """Call a PCS describe function and return True if the resource is gone.

    ``ResourceNotFoundException`` means the resource has been deleted.
    Any other response (including success) means it still exists.
    Any unexpected ``ClientError`` is re-raised so it propagates to
    the state machine's error handler.
    """
    try:
        describe_fn()
        logger.info("PCS %s still exists (DELETING)", resource_label)
        return False
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info("PCS %s confirmed deleted", resource_label)
            return True
        # Unexpected error — re-raise so the state machine error handler
        # can route to DestructionFailed instead of polling forever
        logger.warning(
            "Unexpected error checking PCS %s: %s", resource_label, exc
        )
        raise


# ===================================================================
# Step 3c — Delete PCS cluster
# ===================================================================

def delete_pcs_cluster_step(event: dict[str, Any]) -> dict[str, Any]:
    """Delete the PCS cluster after sub-resources are confirmed deleted.

    This step is only invoked by the state machine after
    ``check_pcs_deletion_status`` confirms all sub-resources are gone.

    If ``pcsClusterId`` is empty, the step is a no-op.
    ``ResourceNotFoundException`` is treated as already deleted (success).
    Any other failure raises ``InternalError`` to halt the workflow.
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    _update_step_progress(project_id, cluster_name, 5)

    pcs_cluster_id: str = event.get("pcsClusterId", "")

    if not pcs_cluster_id:
        logger.info("No PCS cluster to delete — skipping")
        return {**event, "pcsClusterDeleted": True}

    try:
        pcs_client.delete_cluster(clusterIdentifier=pcs_cluster_id)
        logger.info(
            "PCS cluster '%s' deletion initiated for cluster '%s'",
            pcs_cluster_id,
            cluster_name,
        )
        return {**event, "pcsClusterDeleted": True}
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(
                "PCS cluster '%s' already deleted — treating as success",
                pcs_cluster_id,
            )
            return {**event, "pcsClusterDeleted": True}
        raise InternalError(
            f"Failed to delete PCS cluster '{pcs_cluster_id}': {exc}"
        )


# ===================================================================
# Step — Deregister cluster name from ClusterNameRegistry
# ===================================================================

def deregister_cluster_name_step(event: dict[str, Any]) -> dict[str, Any]:
    """Remove the cluster name from the ClusterNameRegistry.

    Reads ``CLUSTER_NAME_REGISTRY_TABLE_NAME`` from the environment and
    calls ``cluster_names.deregister_cluster_name`` to free the name
    for reuse by any project.

    Adds ``clusterNameDeregistered`` to the returned event.
    """
    cluster_name_val: str = event.get("clusterName", "")
    table_name = os.environ.get("CLUSTER_NAME_REGISTRY_TABLE_NAME", "")

    if not cluster_name_val or not table_name:
        logger.info(
            "Skipping cluster name deregistration — clusterName='%s', tableName='%s'",
            cluster_name_val,
            table_name,
        )
        return {**event, "clusterNameDeregistered": False}

    result = cluster_names.deregister_cluster_name(table_name, cluster_name_val)
    logger.info(
        "Cluster name '%s' deregistration result: %s",
        cluster_name_val,
        "removed" if result else "not found",
    )

    return {**event, "clusterNameDeregistered": result}


# ===================================================================
# Step 4 — Delete FSx filesystem
# ===================================================================

def delete_fsx_filesystem(event: dict[str, Any]) -> dict[str, Any]:
    """Delete the FSx for Lustre filesystem.

    This step runs after the data repository export has completed
    (or been acknowledged as failed by the administrator).

    Home_Directory (EFS) and Project_Storage (S3) are intentionally
    NOT deleted — they persist beyond cluster lifecycle.

    Adds ``fsxDeleted`` to the returned event.
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    _update_step_progress(project_id, cluster_name, 6)

    fsx_filesystem_id: str = event.get("fsxFilesystemId", "")

    if not fsx_filesystem_id:
        logger.info("No FSx filesystem to delete — skipping")
        return {**event, "fsxDeleted": False}

    try:
        fsx_client.delete_file_system(FileSystemId=fsx_filesystem_id)
        logger.info(
            "FSx filesystem '%s' deletion initiated for cluster '%s'",
            fsx_filesystem_id,
            cluster_name,
        )
        return {**event, "fsxDeleted": True}
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code in ("FileSystemNotFound", "BadRequest"):
            logger.warning(
                "FSx filesystem '%s' not found — already deleted: %s",
                fsx_filesystem_id,
                exc,
            )
            return {**event, "fsxDeleted": False}
        raise InternalError(
            f"Failed to delete FSx filesystem '{fsx_filesystem_id}': {exc}"
        )


# ===================================================================
# Step 5 — Delete cluster-specific IAM resources
# ===================================================================

# Managed policy ARNs attached to PCS node roles (must match cluster_creation.py)
_PCS_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
]

# Inline policy name (must match cluster_creation.py)
_PCS_INLINE_POLICY_NAME = "PCSRegisterComputeNodeGroupInstance"


def _delete_role_and_instance_profile(role_name: str) -> list[str]:
    """Best-effort deletion of an IAM role and its instance profile.

    Cleanup order:
    1. Remove role from instance profile
    2. Delete instance profile
    3. Detach managed policies
    4. Delete inline policies
    5. Delete IAM role

    Each step logs and continues on ``NoSuchEntity`` or ``ClientError``
    so that subsequent steps are always attempted.

    Returns a list of result strings for logging.
    """
    results: list[str] = []

    # 1. Remove role from instance profile
    try:
        iam_client.remove_role_from_instance_profile(
            InstanceProfileName=role_name,
            RoleName=role_name,
        )
        logger.info("Removed role '%s' from instance profile", role_name)
        results.append(f"remove_role_from_profile:{role_name}:done")
    except ClientError as exc:
        logger.warning(
            "Failed to remove role '%s' from instance profile: %s",
            role_name,
            exc,
        )
        results.append(f"remove_role_from_profile:{role_name}:skipped")

    # 2. Delete instance profile
    try:
        iam_client.delete_instance_profile(InstanceProfileName=role_name)
        logger.info("Deleted instance profile '%s'", role_name)
        results.append(f"instance_profile:{role_name}:deleted")
    except ClientError as exc:
        logger.warning(
            "Failed to delete instance profile '%s': %s",
            role_name,
            exc,
        )
        results.append(f"instance_profile:{role_name}:failed")

    # 3. Detach managed policies
    for policy_arn in _PCS_MANAGED_POLICIES:
        try:
            iam_client.detach_role_policy(
                RoleName=role_name,
                PolicyArn=policy_arn,
            )
            logger.info(
                "Detached policy '%s' from role '%s'", policy_arn, role_name
            )
        except ClientError as exc:
            logger.warning(
                "Failed to detach policy '%s' from role '%s': %s",
                policy_arn,
                role_name,
                exc,
            )

    # 4. Delete inline policies
    try:
        iam_client.delete_role_policy(
            RoleName=role_name,
            PolicyName=_PCS_INLINE_POLICY_NAME,
        )
        logger.info(
            "Deleted inline policy '%s' from role '%s'",
            _PCS_INLINE_POLICY_NAME,
            role_name,
        )
    except ClientError as exc:
        logger.warning(
            "Failed to delete inline policy '%s' from role '%s': %s",
            _PCS_INLINE_POLICY_NAME,
            role_name,
            exc,
        )

    # 5. Delete IAM role
    try:
        iam_client.delete_role(RoleName=role_name)
        logger.info("Deleted IAM role '%s'", role_name)
        results.append(f"role:{role_name}:deleted")
    except ClientError as exc:
        logger.warning("Failed to delete IAM role '%s': %s", role_name, exc)
        results.append(f"role:{role_name}:failed")

    return results


def delete_iam_resources(event: dict[str, Any]) -> dict[str, Any]:
    """Delete cluster-specific IAM roles and instance profiles.

    Cleans up the two IAM roles and instance profiles created during
    cluster creation:
    - ``AWSPCS-{projectId}-{clusterName}-login``
    - ``AWSPCS-{projectId}-{clusterName}-compute``

    Uses best-effort approach — each deletion step logs and continues
    on failure so that all resources are attempted regardless of
    individual errors.

    Adds ``iamCleanupResults`` to the returned event.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]
    _update_step_progress(project_id, cluster_name, 7)

    login_role_name = f"AWSPCS-{project_id}-{cluster_name}-login"
    compute_role_name = f"AWSPCS-{project_id}-{cluster_name}-compute"

    cleanup_results: list[str] = []

    logger.info(
        "Deleting IAM resources for cluster '%s': %s, %s",
        cluster_name,
        login_role_name,
        compute_role_name,
    )

    cleanup_results.extend(_delete_role_and_instance_profile(login_role_name))
    cleanup_results.extend(_delete_role_and_instance_profile(compute_role_name))

    logger.info(
        "IAM cleanup for cluster '%s': %s",
        cluster_name,
        "; ".join(cleanup_results),
    )

    return {**event, "iamCleanupResults": cleanup_results}


# ===================================================================
# Step 5b — Delete cluster-scoped launch templates
# ===================================================================

def _delete_launch_template_by_name(template_name: str) -> str:
    """Best-effort deletion of a single EC2 launch template by name.

    Uses ``describe_launch_templates`` to resolve the name to an ID,
    then ``delete_launch_template`` to remove it.  Returns a result
    string for logging.
    """
    try:
        response = ec2_client.describe_launch_templates(
            LaunchTemplateNames=[template_name],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "InvalidLaunchTemplateName.NotFoundException":
            logger.warning(
                "Launch template '%s' not found — already deleted",
                template_name,
            )
            return f"launch_template:{template_name}:not_found"
        logger.warning(
            "Failed to describe launch template '%s': %s",
            template_name,
            exc,
        )
        return f"launch_template:{template_name}:describe_failed"

    templates = response.get("LaunchTemplates", [])
    if not templates:
        logger.warning(
            "Launch template '%s' not found in response — skipping",
            template_name,
        )
        return f"launch_template:{template_name}:not_found"

    template_id = templates[0]["LaunchTemplateId"]

    try:
        ec2_client.delete_launch_template(LaunchTemplateId=template_id)
        logger.info("Deleted launch template '%s' (%s)", template_name, template_id)
        return f"launch_template:{template_name}:deleted"
    except ClientError as exc:
        logger.warning(
            "Failed to delete launch template '%s' (%s): %s",
            template_name,
            template_id,
            exc,
        )
        return f"launch_template:{template_name}:delete_failed"


def delete_launch_templates(event: dict[str, Any]) -> dict[str, Any]:
    """Delete cluster-scoped EC2 launch templates.

    Cleans up the two launch templates created during cluster creation:
    - ``hpc-{projectId}-{clusterName}-login``
    - ``hpc-{projectId}-{clusterName}-compute``

    Uses best-effort approach — each template is handled independently
    so that a failure on one does not prevent deletion of the other.

    Adds ``launchTemplateCleanupResults`` to the returned event.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    login_template_name = f"hpc-{project_id}-{cluster_name}-login"
    compute_template_name = f"hpc-{project_id}-{cluster_name}-compute"

    cleanup_results: list[str] = []

    logger.info(
        "Deleting launch templates for cluster '%s': %s, %s",
        cluster_name,
        login_template_name,
        compute_template_name,
    )

    cleanup_results.append(_delete_launch_template_by_name(login_template_name))
    cleanup_results.append(_delete_launch_template_by_name(compute_template_name))

    logger.info(
        "Launch template cleanup for cluster '%s': %s",
        cluster_name,
        "; ".join(cleanup_results),
    )

    return {**event, "launchTemplateCleanupResults": cleanup_results}


# ===================================================================
# Step 6 — Record cluster as destroyed in DynamoDB
# ===================================================================

def record_cluster_destroyed(event: dict[str, Any]) -> dict[str, Any]:
    """Update the DynamoDB cluster record to DESTROYED status.

    Sets:
    - ``status`` → ``DESTROYED``
    - ``destroyedAt`` → current UTC ISO 8601 timestamp

    Removes:
    - ``currentStep``, ``totalSteps``, ``stepDescription`` — progress
      fields are cleared so stale data does not appear if the record
      is queried after destruction.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]
    _update_step_progress(project_id, cluster_name, 8)
    now = datetime.now(timezone.utc).isoformat()

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)

    try:
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            UpdateExpression=(
                "SET #s = :status, destroyedAt = :ts "
                "REMOVE currentStep, totalSteps, stepDescription"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "DESTROYED",
                ":ts": now,
            },
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to update cluster '{cluster_name}' status to DESTROYED: {exc}"
        )

    logger.info(
        "Cluster '%s' in project '%s' marked as DESTROYED at %s",
        cluster_name,
        project_id,
        now,
    )

    return {**event, "status": "DESTROYED", "destroyedAt": now}


# ===================================================================
# Step 6b — Record cluster destruction as FAILED in DynamoDB
# ===================================================================

def record_cluster_destruction_failed(event: dict[str, Any]) -> dict[str, Any]:
    """Update the DynamoDB cluster record to DESTRUCTION_FAILED status.

    Invoked by the state machine failure handler when the destruction
    workflow times out or encounters an unrecoverable error.

    Sets:
    - ``status`` → ``DESTRUCTION_FAILED``
    - ``destructionFailedAt`` → current UTC ISO 8601 timestamp
    - ``errorMessage`` → the destruction error cause extracted from
      the Step Functions error payload (``$.error.Cause``).

    Removes:
    - ``currentStep``, ``totalSteps``, ``stepDescription`` — progress
      fields are cleared so stale data does not appear if the record
      is queried after the failure.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]
    now = datetime.now(timezone.utc).isoformat()

    # Extract the destruction error from the Step Functions catch payload.
    error_info = event.get("error", {})
    error_message = error_info.get("Cause", error_info.get("Error", "Unknown destruction error"))

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)

    try:
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            UpdateExpression=(
                "SET #s = :status, destructionFailedAt = :ts, "
                "errorMessage = :err "
                "REMOVE currentStep, totalSteps, stepDescription"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "DESTRUCTION_FAILED",
                ":ts": now,
                ":err": error_message,
            },
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to update cluster '{cluster_name}' status to "
            f"DESTRUCTION_FAILED: {exc}"
        )

    logger.info(
        "Cluster '%s' in project '%s' marked as DESTRUCTION_FAILED at %s",
        cluster_name,
        project_id,
        now,
    )

    return {**event, "status": "DESTRUCTION_FAILED", "destructionFailedAt": now}


# ===================================================================
# Step — Remove Mountpoint S3 inline policy from IAM roles
# ===================================================================

def remove_mountpoint_s3_policy(event: dict[str, Any]) -> dict[str, Any]:
    """Remove the MountpointS3Access inline policy from login and compute roles.

    For clusters created with ``storageMode == "mountpoint"``, an inline
    IAM policy named ``MountpointS3Access`` was attached to both the
    login and compute roles during creation.  This step removes those
    policies before the roles themselves are deleted.

    ``NoSuchEntity`` errors are silently ignored — the policy may not
    exist if the cluster was created in lustre mode.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    for role_suffix in ["login", "compute"]:
        role_name = f"AWSPCS-{project_id}-{cluster_name}-{role_suffix}"
        try:
            iam_client.delete_role_policy(
                RoleName=role_name,
                PolicyName="MountpointS3Access",
            )
            logger.info(
                "Removed MountpointS3Access policy from role '%s'", role_name
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                logger.warning(
                    "Failed to remove S3 policy from %s: %s", role_name, exc
                )

    return event


# ===================================================================
# Internal PCS cleanup helpers
# ===================================================================

def _delete_pcs_node_group(
    cluster_id: str, node_group_id: str, label: str
) -> str:
    """Best-effort deletion of a PCS compute node group.

    Returns a ``:<label>_node_group:<id>:deleted`` result on success or
    when the resource has already been deleted (``ResourceNotFoundException``).
    Returns ``:<label>_node_group:<id>:failed`` for genuine API errors.
    """
    try:
        pcs_client.delete_compute_node_group(
            clusterIdentifier=cluster_id,
            computeNodeGroupIdentifier=node_group_id,
        )
        logger.info("Deleted PCS %s node group '%s'", label, node_group_id)
        return f"{label}_node_group:{node_group_id}:deleted"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(
                "PCS %s node group '%s' already deleted — treating as success",
                label,
                node_group_id,
            )
            return f"{label}_node_group:{node_group_id}:deleted"
        logger.warning(
            "Failed to delete PCS %s node group '%s': %s",
            label,
            node_group_id,
            exc,
        )
        return f"{label}_node_group:{node_group_id}:failed"


def _delete_pcs_queue(cluster_id: str, queue_id: str) -> str:
    """Best-effort deletion of a PCS queue.

    Returns ``queue:<id>:deleted`` on success or when the resource has
    already been deleted (``ResourceNotFoundException``).
    Returns ``queue:<id>:failed`` for genuine API errors.
    """
    try:
        pcs_client.delete_queue(
            clusterIdentifier=cluster_id,
            queueIdentifier=queue_id,
        )
        logger.info("Deleted PCS queue '%s'", queue_id)
        return f"queue:{queue_id}:deleted"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(
                "PCS queue '%s' already deleted — treating as success",
                queue_id,
            )
            return f"queue:{queue_id}:deleted"
        logger.warning("Failed to delete PCS queue '%s': %s", queue_id, exc)
        return f"queue:{queue_id}:failed"


def _delete_pcs_cluster(cluster_id: str) -> str:
    """Best-effort deletion of a PCS cluster."""
    try:
        pcs_client.delete_cluster(clusterIdentifier=cluster_id)
        logger.info("Deleted PCS cluster '%s'", cluster_id)
        return f"cluster:{cluster_id}:deleted"
    except ClientError as exc:
        logger.warning("Failed to delete PCS cluster '%s': %s", cluster_id, exc)
        return f"cluster:{cluster_id}:failed"

# ===================================================================
# Scheduler log delivery cleanup helpers
# ===================================================================

def _delete_deliveries_by_name(source_names: list[str]) -> None:
    """Delete deliveries whose source name matches any in *source_names*.

    Lists all deliveries via ``describe_deliveries`` (paginated) and
    deletes those whose ``deliverySourceName`` is in *source_names*.
    ``ResourceNotFoundException`` is treated as already deleted.
    """
    next_token: str | None = None

    while True:
        kwargs: dict[str, Any] = {}
        if next_token:
            kwargs["nextToken"] = next_token

        response = logs_client.describe_deliveries(**kwargs)

        for delivery in response.get("deliveries", []):
            if delivery.get("deliverySourceName") in source_names:
                delivery_id = delivery["id"]
                try:
                    logs_client.delete_delivery(id=delivery_id)
                    logger.info(
                        "Deleted delivery '%s' (source: %s)",
                        delivery_id,
                        delivery.get("deliverySourceName"),
                    )
                except ClientError as exc:
                    if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                        logger.info(
                            "Delivery '%s' already deleted", delivery_id
                        )
                    else:
                        raise

        next_token = response.get("nextToken")
        if not next_token:
            break


def _delete_delivery_destinations(destination_names: list[str]) -> None:
    """Delete delivery destinations by name.

    ``ResourceNotFoundException`` is treated as already deleted.
    """
    for name in destination_names:
        try:
            logs_client.delete_delivery_destination(name=name)
            logger.info("Deleted delivery destination '%s'", name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(
                    "Delivery destination '%s' already deleted", name
                )
            else:
                raise


def _delete_delivery_sources(source_names: list[str]) -> None:
    """Delete delivery sources by name.

    ``ResourceNotFoundException`` is treated as already deleted.
    """
    for name in source_names:
        try:
            logs_client.delete_delivery_source(name=name)
            logger.info("Deleted delivery source '%s'", name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(
                    "Delivery source '%s' already deleted", name
                )
            else:
                raise


def _delete_scheduler_log_group(project_id: str, cluster_name: str) -> None:
    """Delete the scheduler log group for a cluster.

    ``ResourceNotFoundException`` is treated as already deleted.
    """
    log_group_name = (
        f"/hpc-platform/clusters/{project_id}"
        f"/scheduler-logs/{cluster_name}"
    )
    try:
        logs_client.delete_log_group(logGroupName=log_group_name)
        logger.info("Deleted scheduler log group '%s'", log_group_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(
                "Scheduler log group '%s' already deleted", log_group_name
            )
        else:
            raise


def cleanup_scheduler_log_delivery(event: dict[str, Any]) -> dict[str, Any]:
    """Delete all vended log delivery resources and the scheduler log group.

    Deletes in the required order:
    deliveries → destinations → sources → log group.

    Handles ``ResourceNotFoundException`` for idempotency (already deleted).

    Parameters
    ----------
    event : dict
        State machine payload containing:
        - projectId: str
        - clusterName: str

    Returns
    -------
    dict
        Original event (unchanged — cleanup is side-effect only).
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    source_names = [
        f"{cluster_name}-{suffix}" for suffix in _PCS_LOG_SUFFIXES
    ]
    destination_names = [
        f"{project_id}-{cluster_name}-{suffix}"
        for suffix in _PCS_LOG_SUFFIXES
    ]

    logger.info(
        "Cleaning up scheduler log delivery for cluster '%s' "
        "in project '%s'",
        cluster_name,
        project_id,
    )

    _delete_deliveries_by_name(source_names)
    _delete_delivery_destinations(destination_names)
    _delete_delivery_sources(source_names)
    _delete_scheduler_log_group(project_id, cluster_name)

    logger.info(
        "Scheduler log delivery cleanup complete for cluster '%s'",
        cluster_name,
    )

    return event


# ===================================================================
# Consolidated step handlers
# ===================================================================

def consolidated_delete_resources(event: dict[str, Any]) -> dict[str, Any]:
    """Execute resource deletion steps sequentially in a single invocation.

    Calls delete_pcs_cluster_step, delete_fsx_filesystem, and
    conditionally remove_mountpoint_s3_policy (when storageMode == 'mountpoint').
    Each step receives the accumulated payload from prior steps.

    Raises the original error from whichever sub-step fails,
    preserving the error type and message for the catch block.

    Returns the merged payload with all fields from all steps.
    """
    steps = [
        delete_pcs_cluster_step,
        delete_fsx_filesystem,
    ]
    if event.get("storageMode") == "mountpoint":
        steps.append(remove_mountpoint_s3_policy)

    result: dict[str, Any] = {}
    for step_fn in steps:
        payload = {**event, **result}
        result = {**result, **step_fn(payload)}
    return result


def consolidated_cleanup(event: dict[str, Any]) -> dict[str, Any]:
    """Execute cleanup steps sequentially in a single invocation.

    Calls cleanup_scheduler_log_delivery, delete_iam_resources,
    delete_launch_templates, deregister_cluster_name_step, and
    record_cluster_destroyed in order.
    Each step receives the accumulated payload from prior steps.

    Raises the original error from whichever sub-step fails,
    preserving the error type and message for the catch block.

    Returns the merged payload with all fields from all five steps.
    """
    steps = [
        cleanup_scheduler_log_delivery,
        delete_iam_resources,
        delete_launch_templates,
        deregister_cluster_name_step,
        record_cluster_destroyed,
    ]

    result: dict[str, Any] = {}
    for step_fn in steps:
        payload = {**event, **result}
        result = {**result, **step_fn(payload)}
    return result


# ---------------------------------------------------------------------------
# Populate the step dispatch table now that all functions are defined.
# ---------------------------------------------------------------------------
_STEP_DISPATCH.update({
    "create_fsx_export_task": create_fsx_export_task,
    "check_fsx_export_status": check_fsx_export_status,
    "delete_pcs_resources": delete_pcs_resources,
    "check_pcs_deletion_status": check_pcs_deletion_status,
    "delete_pcs_cluster": delete_pcs_cluster_step,
    "deregister_cluster_name": deregister_cluster_name_step,
    "delete_fsx_filesystem": delete_fsx_filesystem,
    "remove_mountpoint_s3_policy": remove_mountpoint_s3_policy,
    "delete_iam_resources": delete_iam_resources,
    "delete_launch_templates": delete_launch_templates,
    "record_cluster_destroyed": record_cluster_destroyed,
    "record_cluster_destruction_failed": record_cluster_destruction_failed,
    "consolidated_delete_resources": consolidated_delete_resources,
    "consolidated_cleanup": consolidated_cleanup,
})
