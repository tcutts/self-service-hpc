"""Cluster destruction workflow step handlers.

Each function in this module implements a single step of the cluster
destruction Step Functions state machine.  Every handler receives an
``event`` dict (the state machine payload) and returns a dict that
Step Functions passes to the next step.

The destruction workflow:
    1. Create FSx data repository export task (sync data back to S3)
    2. Wait for export to complete (with failure handling)
    3. Delete PCS compute node groups, queue, and cluster
    4. Delete FSx for Lustre filesystem
    5. Update DynamoDB cluster record (status DESTROYED, destroyedAt)

**Important**: Home_Directory (EFS) and Project_Storage (S3) are
retained after destruction — they are NOT deleted.

Environment variables
---------------------
CLUSTERS_TABLE_NAME    DynamoDB Clusters table

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

from errors import InternalError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
fsx_client = boto3.client("fsx")
pcs_client = boto3.client("pcs")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")

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
    if event.get("exportSkipped", False):
        logger.info("Export was skipped — marking as complete")
        return {**event, "exportComplete": True, "exportFailed": False}

    export_task_id: str = event.get("exportTaskId", "")
    fsx_filesystem_id: str = event.get("fsxFilesystemId", "")

    if not export_task_id:
        logger.info("No export task ID — marking as complete")
        return {**event, "exportComplete": True, "exportFailed": False}

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
        "FSx export task '%s' for filesystem '%s' status: %s",
        export_task_id,
        fsx_filesystem_id,
        lifecycle,
    )

    if lifecycle == "SUCCEEDED":
        return {**event, "exportComplete": True, "exportFailed": False}

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
        }

    # Still in progress (PENDING, EXECUTING, etc.)
    return {**event, "exportComplete": False, "exportFailed": False}


# ===================================================================
# Step 3 — Delete PCS resources
# ===================================================================

def delete_pcs_resources(event: dict[str, Any]) -> dict[str, Any]:
    """Delete PCS compute node groups, queue, and cluster (in order).

    Deletion order matters:
    1. Compute node group (workers)
    2. Login node group (head node)
    3. Queue
    4. Cluster

    Each deletion is best-effort — failures are logged but do not
    prevent subsequent deletions from being attempted.

    Adds ``pcsCleanupResults`` to the returned event.
    """
    pcs_cluster_id: str = event.get("pcsClusterId", "")
    compute_node_group_id: str = event.get("computeNodeGroupId", "")
    login_node_group_id: str = event.get("loginNodeGroupId", "")
    queue_id: str = event.get("queueId", "")
    cluster_name: str = event.get("clusterName", "")

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

    # 4. Delete cluster
    if pcs_cluster_id:
        cleanup_results.append(
            _delete_pcs_cluster(pcs_cluster_id)
        )

    logger.info(
        "PCS resource cleanup for cluster '%s': %s",
        cluster_name,
        "; ".join(cleanup_results),
    )

    return {**event, "pcsCleanupResults": cleanup_results}


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
    fsx_filesystem_id: str = event.get("fsxFilesystemId", "")
    cluster_name: str = event.get("clusterName", "")

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
# Step 5 — Record cluster as destroyed in DynamoDB
# ===================================================================

def record_cluster_destroyed(event: dict[str, Any]) -> dict[str, Any]:
    """Update the DynamoDB cluster record to DESTROYED status.

    Sets:
    - ``status`` → ``DESTROYED``
    - ``destroyedAt`` → current UTC ISO 8601 timestamp
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]
    now = datetime.now(timezone.utc).isoformat()

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)

    try:
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            UpdateExpression="SET #s = :status, destroyedAt = :ts",
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
# Internal PCS cleanup helpers
# ===================================================================

def _delete_pcs_node_group(
    cluster_id: str, node_group_id: str, label: str
) -> str:
    """Best-effort deletion of a PCS compute node group."""
    try:
        pcs_client.delete_compute_node_group(
            clusterIdentifier=cluster_id,
            computeNodeGroupIdentifier=node_group_id,
        )
        logger.info("Deleted PCS %s node group '%s'", label, node_group_id)
        return f"{label}_node_group:{node_group_id}:deleted"
    except ClientError as exc:
        logger.warning(
            "Failed to delete PCS %s node group '%s': %s",
            label,
            node_group_id,
            exc,
        )
        return f"{label}_node_group:{node_group_id}:failed"


def _delete_pcs_queue(cluster_id: str, queue_id: str) -> str:
    """Best-effort deletion of a PCS queue."""
    try:
        pcs_client.delete_queue(
            clusterIdentifier=cluster_id,
            queueIdentifier=queue_id,
        )
        logger.info("Deleted PCS queue '%s'", queue_id)
        return f"queue:{queue_id}:deleted"
    except ClientError as exc:
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

# ---------------------------------------------------------------------------
# Populate the step dispatch table now that all functions are defined.
# ---------------------------------------------------------------------------
_STEP_DISPATCH.update({
    "create_fsx_export_task": create_fsx_export_task,
    "check_fsx_export_status": check_fsx_export_status,
    "delete_pcs_resources": delete_pcs_resources,
    "delete_fsx_filesystem": delete_fsx_filesystem,
    "record_cluster_destroyed": record_cluster_destroyed,
})
