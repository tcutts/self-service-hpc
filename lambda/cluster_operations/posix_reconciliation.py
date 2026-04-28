"""Daily POSIX reconciliation Lambda — full membership audit.

Performs a comprehensive audit of Linux accounts on all ACTIVE cluster
nodes against current project membership.  Detects and corrects drift
by creating missing accounts and disabling stale accounts.  Also
retries PENDING_PROPAGATION and PENDING_RESTORATION records.

This Lambda is designed to be invoked daily via an EventBridge rule
(e.g. ``cron(0 2 * * ? *)``).

Environment variables
---------------------
PROJECTS_TABLE_NAME    DynamoDB Projects table name
USERS_TABLE_NAME       DynamoDB PlatformUsers table name
CLUSTERS_TABLE_NAME    DynamoDB Clusters table name
"""

import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from posix_provisioning import (
    PROPAGATION_PENDING,
    PROPAGATION_SUCCESS,
    generate_user_creation_commands,
    propagate_user_to_clusters,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "PlatformUsers")
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SSM_COMMAND_TIMEOUT = 30  # seconds to wait for SSM command result
_SSM_POLL_INTERVAL = 2     # seconds between polls
_MIN_POSIX_UID = 10000     # UIDs >= this are platform-managed accounts


def handler(event: Any, context: Any) -> dict[str, Any]:
    """Daily reconciliation entry point.

    1. Scan all ACTIVE clusters across all projects.
    2. For each cluster, compare Linux accounts vs. project membership.
    3. Create missing accounts, disable stale accounts.
    4. Continue retrying PENDING_PROPAGATION and PENDING_RESTORATION records.
    5. Log summary.
    """
    logger.info("Starting daily POSIX reconciliation run")

    summary = {
        "clusters_audited": 0,
        "accounts_created": 0,
        "accounts_disabled": 0,
        "pending_resolved": 0,
        "errors": 0,
    }

    # --- Phase 1: Full membership audit across all active clusters ---
    active_clusters = _scan_all_active_clusters()
    logger.info("Found %d active clusters to audit", len(active_clusters))

    # Group clusters by project for efficient member lookups
    clusters_by_project: dict[str, list[dict]] = {}
    for cluster in active_clusters:
        project_id = cluster.get("projectId", "")
        if project_id:
            clusters_by_project.setdefault(project_id, []).append(cluster)

    for project_id, clusters in clusters_by_project.items():
        try:
            _audit_project_clusters(project_id, clusters, summary)
        except Exception as exc:
            logger.error(
                "Error auditing project '%s': %s", project_id, exc,
            )
            summary["errors"] += 1

    # --- Phase 2: Retry PENDING_PROPAGATION and PENDING_RESTORATION records ---
    pending_members = _scan_pending_members()
    logger.info(
        "Found %d pending propagation/restoration records",
        len(pending_members),
    )

    for member in pending_members:
        project_id = member.get("projectId", "")
        user_id = member.get("userId", "")
        if not project_id or not user_id:
            summary["errors"] += 1
            continue

        try:
            status = _retry_propagation(project_id, user_id)
            if status == PROPAGATION_SUCCESS:
                _clear_propagation_status(project_id, user_id)
                summary["pending_resolved"] += 1
                logger.info(
                    "Resolved pending record for user '%s' in project '%s'",
                    user_id, project_id,
                )
            else:
                logger.warning(
                    "User '%s' in project '%s' still pending",
                    user_id, project_id,
                )
        except Exception as exc:
            summary["errors"] += 1
            logger.error(
                "Error retrying propagation for user '%s' in project '%s': %s",
                user_id, project_id, exc,
            )

    logger.info("Reconciliation complete: %s", summary)
    return summary


# ===================================================================
# Phase 1 — Full membership audit
# ===================================================================

def _scan_all_active_clusters() -> list[dict[str, Any]]:
    """Scan the Clusters table for all items with status=ACTIVE.

    Returns a list of cluster record dicts, each containing at least
    projectId, clusterName, loginNodeInstanceId, and status.
    """
    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    clusters: list[dict[str, Any]] = []

    try:
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": boto3.dynamodb.conditions.Attr("status").eq("ACTIVE"),
        }
        response = table.scan(**scan_kwargs)
        clusters.extend(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = table.scan(**scan_kwargs)
            clusters.extend(response.get("Items", []))

    except ClientError as exc:
        logger.error("Failed to scan for active clusters: %s", exc)

    return clusters


def _get_project_members(project_id: str) -> dict[str, dict[str, Any]]:
    """Get current project members with their POSIX identities.

    Returns a dict mapping userId -> {posixUid, posixGid} for all
    members who have POSIX identities assigned.
    """
    table = dynamodb.Table(PROJECTS_TABLE_NAME)

    # Fetch membership records
    member_user_ids: list[str] = []
    try:
        response = table.query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
                & boto3.dynamodb.conditions.Key("SK").begins_with("MEMBER#")
            ),
        )
        for item in response.get("Items", []):
            uid = item.get("userId", "")
            if uid:
                member_user_ids.append(uid)
    except ClientError as exc:
        logger.error(
            "Failed to fetch members for project '%s': %s",
            project_id, exc,
        )
        return {}

    # Look up POSIX identities
    users_table = dynamodb.Table(USERS_TABLE_NAME)
    members: dict[str, dict[str, Any]] = {}

    for user_id in member_user_ids:
        try:
            resp = users_table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
            )
            item = resp.get("Item")
            if item and "posixUid" in item and "posixGid" in item:
                members[user_id] = {
                    "posixUid": int(item["posixUid"]),
                    "posixGid": int(item["posixGid"]),
                }
        except ClientError as exc:
            logger.warning(
                "Failed to look up POSIX identity for user '%s': %s",
                user_id, exc,
            )

    return members


def _get_linux_accounts_on_node(instance_id: str) -> set[str] | None:
    """Query existing Linux accounts on a cluster node via SSM.

    Sends ``getent passwd | awk -F: '$3 >= 10000 {print $1}'`` to the
    instance and returns the set of usernames.  Returns None if the
    command fails or times out.
    """
    command_str = f"getent passwd | awk -F: '$3 >= {_MIN_POSIX_UID} {{print $1}}'"

    try:
        send_resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command_str]},
            Comment="POSIX reconciliation — list platform-managed accounts",
        )
        command_id = send_resp["Command"]["CommandId"]
    except ClientError as exc:
        logger.warning(
            "SSM send_command failed for instance '%s': %s",
            instance_id, exc,
        )
        return None

    # Poll for command completion
    elapsed = 0
    while elapsed < _SSM_COMMAND_TIMEOUT:
        time.sleep(_SSM_POLL_INTERVAL)
        elapsed += _SSM_POLL_INTERVAL
        try:
            result = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
            status = result.get("Status", "")
            if status == "Success":
                output = result.get("StandardOutputContent", "")
                accounts = {
                    line.strip()
                    for line in output.splitlines()
                    if line.strip()
                }
                return accounts
            if status in ("Failed", "TimedOut", "Cancelled"):
                logger.warning(
                    "SSM command %s on instance '%s' ended with status '%s'",
                    command_id, instance_id, status,
                )
                return None
        except ClientError as exc:
            # InvocationDoesNotExist means the command hasn't reached the
            # instance yet — keep polling.
            if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                continue
            logger.warning(
                "SSM get_command_invocation failed for instance '%s': %s",
                instance_id, exc,
            )
            return None

    logger.warning(
        "SSM command %s on instance '%s' timed out after %ds",
        command_id, instance_id, _SSM_COMMAND_TIMEOUT,
    )
    return None


def _create_account_on_node(
    instance_id: str,
    user_id: str,
    uid: int,
    gid: int,
    cluster_name: str,
) -> bool:
    """Create a missing Linux account on a cluster node via SSM."""
    commands = generate_user_creation_commands(user_id, uid, gid)
    if not commands:
        return True

    script = "\n".join(commands)
    try:
        ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script]},
            Comment=f"Reconciliation — create account '{user_id}' on '{cluster_name}'",
        )
        logger.info(
            "Created account for user '%s' on cluster '%s' (instance '%s')",
            user_id, cluster_name, instance_id,
        )
        return True
    except ClientError as exc:
        logger.warning(
            "Failed to create account for user '%s' on instance '%s': %s",
            user_id, instance_id, exc,
        )
        return False


def _disable_account_on_node(
    instance_id: str,
    username: str,
    cluster_name: str,
) -> bool:
    """Disable a stale Linux account on a cluster node via SSM."""
    script = f"usermod --lock --expiredate 1 {username}"
    try:
        ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script]},
            Comment=f"Reconciliation — disable account '{username}' on '{cluster_name}'",
        )
        logger.info(
            "Disabled account '%s' on cluster '%s' (instance '%s')",
            username, cluster_name, instance_id,
        )
        return True
    except ClientError as exc:
        logger.warning(
            "Failed to disable account '%s' on instance '%s': %s",
            username, instance_id, exc,
        )
        return False


def _audit_project_clusters(
    project_id: str,
    clusters: list[dict[str, Any]],
    summary: dict[str, int],
) -> None:
    """Audit all active clusters in a project against current membership.

    For each cluster:
    - Query Linux accounts on the node via SSM
    - Compare against project members
    - Create missing accounts (members without Linux accounts)
    - Disable stale accounts (Linux accounts for non-members)
    """
    members = _get_project_members(project_id)
    member_usernames = set(members.keys())

    for cluster in clusters:
        cluster_name = cluster.get("clusterName", "unknown")
        instance_id = cluster.get("loginNodeInstanceId", "")

        if not instance_id:
            logger.warning(
                "Cluster '%s' in project '%s' has no loginNodeInstanceId — skipping.",
                cluster_name, project_id,
            )
            summary["errors"] += 1
            continue

        summary["clusters_audited"] += 1

        # Get current Linux accounts on the node
        node_accounts = _get_linux_accounts_on_node(instance_id)
        if node_accounts is None:
            logger.warning(
                "Could not retrieve accounts from cluster '%s' (instance '%s') — skipping.",
                cluster_name, instance_id,
            )
            summary["errors"] += 1
            continue

        # Detect missing accounts: members who should have accounts but don't
        missing = member_usernames - node_accounts
        for user_id in missing:
            posix = members[user_id]
            ok = _create_account_on_node(
                instance_id, user_id,
                posix["posixUid"], posix["posixGid"],
                cluster_name,
            )
            if ok:
                summary["accounts_created"] += 1
            else:
                summary["errors"] += 1

        # Detect stale accounts: node accounts for non-members
        stale = node_accounts - member_usernames
        for username in stale:
            ok = _disable_account_on_node(instance_id, username, cluster_name)
            if ok:
                summary["accounts_disabled"] += 1
            else:
                summary["errors"] += 1


# ===================================================================
# Phase 2 — Retry pending propagation / restoration records
# ===================================================================

def _scan_pending_members() -> list[dict[str, Any]]:
    """Scan the Projects table for PENDING_PROPAGATION and PENDING_RESTORATION records.

    Performs a full table scan with a filter expression.  These records
    should be rare and short-lived.
    """
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    pending: list[dict[str, Any]] = []

    try:
        filter_expr = (
            boto3.dynamodb.conditions.Key("SK").begins_with("MEMBER#")
            & (
                boto3.dynamodb.conditions.Attr("propagationStatus").eq("PENDING_PROPAGATION")
                | boto3.dynamodb.conditions.Attr("propagationStatus").eq("PENDING_RESTORATION")
            )
        )
        scan_kwargs: dict[str, Any] = {"FilterExpression": filter_expr}
        response = table.scan(**scan_kwargs)
        pending.extend(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = table.scan(**scan_kwargs)
            pending.extend(response.get("Items", []))

    except ClientError as exc:
        logger.error("Failed to scan for pending records: %s", exc)

    return pending


def _retry_propagation(project_id: str, user_id: str) -> str:
    """Retry POSIX user propagation for a single user.

    Looks up the user's POSIX UID/GID and delegates to
    ``propagate_user_to_clusters``.

    Returns PROPAGATION_SUCCESS or PROPAGATION_PENDING.
    """
    users_table = dynamodb.Table(USERS_TABLE_NAME)
    try:
        response = users_table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
        )
    except ClientError as exc:
        logger.warning(
            "Failed to look up POSIX identity for user '%s': %s",
            user_id, exc,
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
            user_id, project_id, exc,
        )
