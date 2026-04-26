"""Periodic reconciliation Lambda for PENDING_PROPAGATION users.

Scans the Projects DynamoDB table for membership records that have
``propagationStatus=PENDING_PROPAGATION``, retries POSIX user
propagation via SSM Run Command, and clears the flag on success.

This Lambda is designed to be invoked on a schedule (e.g., every 5
minutes via EventBridge) to ensure that users who could not be
propagated at membership-add time are eventually provisioned on
active cluster nodes.

Environment variables
---------------------
PROJECTS_TABLE_NAME    DynamoDB Projects table name
USERS_TABLE_NAME       DynamoDB PlatformUsers table name
CLUSTERS_TABLE_NAME    DynamoDB Clusters table name
"""

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from posix_provisioning import (
    PROPAGATION_PENDING,
    PROPAGATION_SUCCESS,
    propagate_user_to_clusters,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "PlatformUsers")
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")


def handler(event: Any, context: Any) -> dict[str, Any]:
    """Lambda entry point for POSIX reconciliation.

    Scans for membership records with propagationStatus=PENDING_PROPAGATION,
    retries propagation for each, and updates the status on success.

    Returns a summary of the reconciliation run.
    """
    logger.info("Starting POSIX reconciliation run")

    pending_members = _scan_pending_members()
    logger.info("Found %d pending propagation records", len(pending_members))

    results = {
        "total": len(pending_members),
        "succeeded": 0,
        "still_pending": 0,
        "errors": 0,
    }

    for member in pending_members:
        project_id = member.get("projectId", "")
        user_id = member.get("userId", "")

        if not project_id or not user_id:
            results["errors"] += 1
            continue

        try:
            status = _retry_propagation(project_id, user_id)
            if status == PROPAGATION_SUCCESS:
                _clear_propagation_status(project_id, user_id)
                results["succeeded"] += 1
                logger.info(
                    "Reconciled user '%s' in project '%s'",
                    user_id,
                    project_id,
                )
            else:
                results["still_pending"] += 1
                logger.warning(
                    "User '%s' in project '%s' still pending",
                    user_id,
                    project_id,
                )
        except Exception as exc:
            results["errors"] += 1
            logger.error(
                "Error reconciling user '%s' in project '%s': %s",
                user_id,
                project_id,
                exc,
            )

    logger.info("Reconciliation complete: %s", results)
    return results


def _scan_pending_members() -> list[dict[str, Any]]:
    """Scan the Projects table for membership records with PENDING_PROPAGATION.

    Performs a full table scan with a filter expression. This is
    acceptable because PENDING_PROPAGATION records should be rare
    and short-lived.

    Returns a list of membership record dicts.
    """
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    pending = []

    try:
        scan_kwargs = {
            "FilterExpression": (
                boto3.dynamodb.conditions.Attr("propagationStatus").eq(
                    "PENDING_PROPAGATION"
                )
                & boto3.dynamodb.conditions.Key("SK").begins_with("MEMBER#")
            ),
        }
        response = table.scan(**scan_kwargs)
        pending.extend(response.get("Items", []))

        # Handle pagination
        while "LastEvaluatedKey" in response:
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = table.scan(**scan_kwargs)
            pending.extend(response.get("Items", []))

    except ClientError as exc:
        logger.error("Failed to scan for pending propagation records: %s", exc)

    return pending


def _retry_propagation(project_id: str, user_id: str) -> str:
    """Retry POSIX user propagation for a single user.

    Looks up the user's POSIX UID/GID and delegates to
    ``propagate_user_to_clusters``.

    Returns PROPAGATION_SUCCESS or PROPAGATION_PENDING.
    """
    # Look up user's POSIX identity
    users_table = dynamodb.Table(USERS_TABLE_NAME)
    try:
        response = users_table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
        )
    except ClientError as exc:
        logger.warning(
            "Failed to look up POSIX identity for user '%s': %s",
            user_id,
            exc,
        )
        return PROPAGATION_PENDING

    item = response.get("Item")
    if not item or "posixUid" not in item or "posixGid" not in item:
        logger.warning(
            "User '%s' has no POSIX identity — cannot propagate.",
            user_id,
        )
        return PROPAGATION_PENDING

    uid = int(item["posixUid"])
    gid = int(item["posixGid"])

    return propagate_user_to_clusters(
        user_id=user_id,
        uid=uid,
        gid=gid,
        project_id=project_id,
        clusters_table_name=CLUSTERS_TABLE_NAME,
    )


def _clear_propagation_status(project_id: str, user_id: str) -> None:
    """Remove the propagationStatus attribute from a membership record."""
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    try:
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"MEMBER#{user_id}",
            },
            UpdateExpression="REMOVE propagationStatus",
        )
    except ClientError as exc:
        logger.warning(
            "Failed to clear propagation status for user '%s' in project '%s': %s",
            user_id,
            project_id,
            exc,
        )
