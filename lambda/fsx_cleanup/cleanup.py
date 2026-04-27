"""FSx cleanup core logic.

Pure functions for classifying orphaned FSx for Lustre filesystems,
building cleanup summaries, and formatting administrator notifications.
I/O functions for AWS API calls (scan, lookup, delete, notify).
"""

import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Terminal cluster statuses — clusters in these states will never recover.
TERMINAL_STATUSES = frozenset({"FAILED", "DESTROYED"})

# Active cluster statuses — clusters in these states are operational or provisioning.
ACTIVE_STATUSES = frozenset({"CREATING", "ACTIVE"})


def filter_tagged_filesystems(filesystems: list[dict]) -> list[dict]:
    """Filter filesystems to only those with both Project and ClusterName tags.

    Args:
        filesystems: Raw FSx describe_file_systems response items.
            Each item may have a ``Tags`` list of ``{"Key": ..., "Value": ...}`` dicts.

    Returns:
        List of filesystem dicts that have both required tags.
    """
    result = []
    for fs in filesystems:
        tags = fs.get("Tags", [])
        tag_keys = {tag.get("Key") for tag in tags}
        if "Project" in tag_keys and "ClusterName" in tag_keys:
            result.append(fs)
    return result


def classify_filesystem(
    filesystem_tags: dict[str, str],
    cluster_record: dict | None,
) -> tuple[bool, str]:
    """Classify a filesystem as orphaned or active.

    Args:
        filesystem_tags: Dict of tag key to value for the filesystem.
        cluster_record: The DynamoDB cluster record, or None if not found.

    Returns:
        (is_orphaned, reason) where reason is one of:
        - ``"cluster_not_found"``
        - ``"terminal_status:{status}"``
        - ``"active"`` (not orphaned)
    """
    if cluster_record is None:
        return (True, "cluster_not_found")

    status = cluster_record.get("status", "")
    if status in TERMINAL_STATUSES:
        return (True, f"terminal_status:{status}")

    return (False, "active")


def build_cleanup_summary(
    total_scanned: int,
    orphaned: list[dict],
    deleted: list[dict],
    failed: list[dict],
) -> dict:
    """Build the cleanup execution summary.

    Args:
        total_scanned: Total FSx Lustre filesystems found in the account.
        orphaned: List of filesystem records classified as orphaned.
        deleted: List of filesystem records successfully deleted.
        failed: List of filesystem records that failed deletion.

    Returns:
        Dict with ``total_scanned``, ``total_tagged``, ``total_orphaned``,
        ``total_deleted``, and ``total_failed`` counts.

    Invariants:
        ``total_orphaned == total_deleted + total_failed``
    """
    total_orphaned = len(orphaned)
    total_deleted = len(deleted)
    total_failed = len(failed)

    return {
        "total_scanned": total_scanned,
        "total_tagged": total_orphaned,
        "total_orphaned": total_orphaned,
        "total_deleted": total_deleted,
        "total_failed": total_failed,
    }


def build_notification_message(
    deleted: list[dict],
    failed: list[dict],
) -> tuple[str, str]:
    """Build the SNS notification subject and message body.

    Args:
        deleted: List of successfully deleted filesystem records.
            Each dict should have ``filesystem_id``, ``project_id``,
            ``cluster_name``, and optionally ``reason``.
        failed: List of failed filesystem records.
            Each dict should have ``filesystem_id``, ``project_id``,
            ``cluster_name``, and ``error``.

    Returns:
        ``(subject, message_body)`` tuple for SNS publish.
    """
    deleted_count = len(deleted)
    subject = (
        f"[HPC Platform] Orphaned FSx Cleanup: "
        f"{deleted_count} filesystem(s) deleted"
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "Orphaned FSx Cleanup Report",
        "============================",
        f"Time: {now}",
        "",
    ]

    # Successfully deleted section
    lines.append(f"Successfully Deleted ({deleted_count}):")
    if deleted:
        for fs in deleted:
            fs_id = fs.get("filesystem_id", "unknown")
            project = fs.get("project_id", "unknown")
            cluster = fs.get("cluster_name", "unknown")
            reason = fs.get("reason", "")
            entry = f"  - {fs_id} (Project: {project}, Cluster: {cluster})"
            if reason:
                entry += f" \u2014 {reason}"
            lines.append(entry)
    else:
        lines.append("  (none)")

    lines.append("")

    # Errors section
    failed_count = len(failed)
    lines.append(f"Errors ({failed_count}):")
    if failed:
        for fs in failed:
            fs_id = fs.get("filesystem_id", "unknown")
            project = fs.get("project_id", "unknown")
            cluster = fs.get("cluster_name", "unknown")
            error = fs.get("error", "unknown error")
            lines.append(
                f"  - {fs_id} (Project: {project}, Cluster: {cluster})"
                f" \u2014 {error}"
            )
    else:
        lines.append("  (none)")

    message_body = "\n".join(lines)
    return (subject, message_body)


# ---------------------------------------------------------------------------
# I/O functions — AWS API calls
# ---------------------------------------------------------------------------

# Module-level clients for reuse across invocations
fsx_client = boto3.client("fsx")
sns_client = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")


def scan_fsx_filesystems() -> list[dict]:
    """Retrieve all FSx for Lustre filesystems via paginated API calls.

    Uses ``describe_file_systems`` with pagination and filters the results
    to only LUSTRE type filesystems.

    Returns:
        List of filesystem dicts from the FSx API response.
    """
    filesystems: list[dict] = []
    next_token: str | None = None

    while True:
        kwargs: dict = {}
        if next_token:
            kwargs["NextToken"] = next_token

        response = fsx_client.describe_file_systems(**kwargs)

        for fs in response.get("FileSystems", []):
            if fs.get("FileSystemType") == "LUSTRE":
                filesystems.append(fs)

        next_token = response.get("NextToken")
        if not next_token:
            break

    logger.info("Scanned %d FSx Lustre filesystems", len(filesystems))
    return filesystems


def lookup_cluster_record(project_id: str, cluster_name: str) -> dict | None:
    """Query the Clusters DynamoDB table for a cluster record.

    Args:
        project_id: The project identifier (value of the ``Project`` tag).
        cluster_name: The cluster name (value of the ``ClusterName`` tag).

    Returns:
        The cluster record dict if found, or ``None`` if the item does not exist.
    """
    table_name = os.environ["CLUSTERS_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    response = table.get_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        }
    )

    return response.get("Item")


def delete_filesystem_dras(filesystem_id: str) -> bool:
    """Delete all data repository associations for a filesystem.

    Describes all DRAs for the given filesystem and deletes each one.
    If any DRA deletion fails, the failure is logged and the function
    returns ``False`` so the caller can skip filesystem deletion.

    Args:
        filesystem_id: The FSx filesystem ID.

    Returns:
        ``True`` if all DRAs were deleted successfully (or none existed),
        ``False`` if any deletion failed.
    """
    try:
        response = fsx_client.describe_data_repository_associations(
            Filters=[{"Name": "file-system-id", "Values": [filesystem_id]}]
        )
    except ClientError:
        logger.exception(
            "Failed to describe DRAs for filesystem %s", filesystem_id
        )
        return False

    associations = response.get("Associations", [])
    if not associations:
        logger.info("No DRAs found for filesystem %s", filesystem_id)
        return True

    all_succeeded = True
    for assoc in associations:
        assoc_id = assoc.get("AssociationId", "unknown")
        try:
            fsx_client.delete_data_repository_association(
                AssociationId=assoc_id,
                DeleteDataInFileSystem=False,
            )
            logger.info(
                "Deleted DRA %s for filesystem %s", assoc_id, filesystem_id
            )
        except ClientError:
            logger.exception(
                "Failed to delete DRA %s for filesystem %s",
                assoc_id,
                filesystem_id,
            )
            all_succeeded = False

    return all_succeeded


def delete_filesystem(filesystem_id: str) -> bool:
    """Delete an FSx filesystem.

    Args:
        filesystem_id: The FSx filesystem ID to delete.

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    try:
        fsx_client.delete_file_system(FileSystemId=filesystem_id)
        logger.info("Deleted filesystem %s", filesystem_id)
        return True
    except ClientError:
        logger.exception("Failed to delete filesystem %s", filesystem_id)
        return False


def publish_notification(subject: str, message: str) -> None:
    """Publish a cleanup notification to the SNS topic.

    Args:
        subject: The SNS message subject.
        message: The SNS message body.
    """
    topic_arn = os.environ["SNS_TOPIC_ARN"]
    sns_client.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=message,
    )
    logger.info("Published cleanup notification to %s", topic_arn)
