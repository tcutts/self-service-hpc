"""FSx Cleanup Lambda handler.

Orchestrates the detection and deletion of orphaned FSx for Lustre
filesystems. Triggered by an EventBridge scheduled rule.

The handler follows a fail-fast strategy for infrastructure
unavailability (FSx API or DynamoDB unreachable) and best-effort
per-filesystem processing once the initial scan succeeds.

Environment variables:
    CLUSTERS_TABLE_NAME: DynamoDB Clusters table name
    SNS_TOPIC_ARN: SNS topic ARN for cleanup notifications
"""

import logging
import time
from typing import Any

from botocore.exceptions import ClientError

from cleanup import (
    build_cleanup_summary,
    build_notification_message,
    classify_filesystem,
    delete_filesystem,
    delete_filesystem_dras,
    filter_tagged_filesystems,
    lookup_cluster_record,
    publish_notification,
    scan_fsx_filesystems,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context: Any) -> dict:
    """EventBridge scheduled event handler.

    Orchestrates the full orphaned FSx cleanup workflow:
    1. Scan all FSx Lustre filesystems (fail-fast if FSx API unreachable)
    2. Filter to filesystems with Project + ClusterName tags
    3. Classify each as orphaned or active (fail-fast if DynamoDB unreachable)
    4. Delete orphans: DRAs first, then filesystem
    5. Build summary and publish notification if deletions occurred

    Returns:
        Summary dict with counts of scanned, orphaned, deleted, and
        failed filesystems, plus error details.
    """
    start_time = time.time()
    logger.info("FSx cleanup started")

    # ------------------------------------------------------------------
    # Step 1: Scan all FSx Lustre filesystems (fail-fast)
    # ------------------------------------------------------------------
    try:
        all_filesystems = scan_fsx_filesystems()
    except (ClientError, Exception):
        logger.exception("FSx API unreachable during initial scan — aborting")
        return _error_result("FSx API unreachable during initial scan")

    total_scanned = len(all_filesystems)
    logger.info("Discovered %d FSx Lustre filesystems", total_scanned)

    # ------------------------------------------------------------------
    # Step 2: Filter to tagged filesystems
    # ------------------------------------------------------------------
    tagged_filesystems = filter_tagged_filesystems(all_filesystems)
    logger.info(
        "%d of %d filesystems have Project and ClusterName tags",
        len(tagged_filesystems),
        total_scanned,
    )

    # ------------------------------------------------------------------
    # Step 3: Classify each filesystem
    # ------------------------------------------------------------------
    orphaned: list[dict] = []
    dynamo_checked = False

    for fs in tagged_filesystems:
        fs_id = fs.get("FileSystemId", "unknown")
        tags = {tag["Key"]: tag["Value"] for tag in fs.get("Tags", [])}
        project_id = tags.get("Project", "")
        cluster_name = tags.get("ClusterName", "")

        try:
            cluster_record = lookup_cluster_record(project_id, cluster_name)
            dynamo_checked = True
        except (ClientError, Exception):
            if not dynamo_checked:
                # First lookup failed — DynamoDB may be unreachable, fail-fast
                logger.exception(
                    "DynamoDB unreachable on first cluster lookup "
                    "(filesystem %s, project %s, cluster %s) — aborting",
                    fs_id,
                    project_id,
                    cluster_name,
                )
                return _error_result("DynamoDB unreachable")
            # Subsequent lookup failure — skip this filesystem, continue
            logger.exception(
                "Failed to look up cluster record for filesystem %s "
                "(project %s, cluster %s) — skipping",
                fs_id,
                project_id,
                cluster_name,
            )
            continue

        is_orphaned, reason = classify_filesystem(tags, cluster_record)

        if is_orphaned:
            logger.info(
                "Filesystem %s classified as orphaned: project=%s, "
                "cluster=%s, reason=%s",
                fs_id,
                project_id,
                cluster_name,
                reason,
            )
            orphaned.append(
                {
                    "filesystem_id": fs_id,
                    "project_id": project_id,
                    "cluster_name": cluster_name,
                    "reason": reason,
                }
            )
        else:
            logger.info(
                "Filesystem %s is active: project=%s, cluster=%s",
                fs_id,
                project_id,
                cluster_name,
            )

    # ------------------------------------------------------------------
    # Step 4: Delete orphaned filesystems (DRAs first, then filesystem)
    # ------------------------------------------------------------------
    deleted: list[dict] = []
    failed: list[dict] = []

    for record in orphaned:
        fs_id = record["filesystem_id"]

        # Delete DRAs first — skip filesystem if DRA cleanup fails
        dra_success = delete_filesystem_dras(fs_id)
        if not dra_success:
            logger.error(
                "DRA deletion failed for filesystem %s — skipping deletion",
                fs_id,
            )
            failed.append({**record, "error": "DRA deletion failed"})
            continue

        # Delete the filesystem
        fs_success = delete_filesystem(fs_id)
        if fs_success:
            deleted.append(record)
        else:
            failed.append({**record, "error": "Filesystem deletion failed"})

    # ------------------------------------------------------------------
    # Step 5: Build summary and publish notification
    # ------------------------------------------------------------------
    summary = build_cleanup_summary(total_scanned, orphaned, deleted, failed)

    elapsed = time.time() - start_time
    logger.info(
        "FSx cleanup completed in %.1fs: scanned=%d, orphaned=%d, "
        "deleted=%d, failed=%d",
        elapsed,
        summary["total_scanned"],
        summary["total_orphaned"],
        summary["total_deleted"],
        summary["total_failed"],
    )

    # Publish notification only if deletions occurred
    if deleted or failed:
        try:
            subject, message = build_notification_message(deleted, failed)
            publish_notification(subject, message)
        except (ClientError, Exception):
            logger.exception("Failed to publish cleanup notification — continuing")

    result = {**summary, "errors": failed}
    return result


def _error_result(error_message: str) -> dict:
    """Build an error result dict for fail-fast termination."""
    return {
        "total_scanned": 0,
        "total_tagged": 0,
        "total_orphaned": 0,
        "total_deleted": 0,
        "total_failed": 0,
        "errors": [{"error": error_message}],
    }
