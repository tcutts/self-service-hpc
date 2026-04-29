"""Login node refresh Lambda — periodic instance ID reconciliation.

Scans all ACTIVE clusters and re-resolves the login node EC2 instance
by querying the ``aws:pcs:compute-node-group-id`` tag.  If the instance
ID or public IP has changed (e.g. PCS replaced the node), updates the
cluster record in DynamoDB so connection details stay current.

This Lambda is designed to be invoked every 5 minutes via an EventBridge
rule.

Environment variables
---------------------
CLUSTERS_TABLE_NAME    DynamoDB Clusters table name
"""

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
ec2_client = boto3.client("ec2")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")


def handler(event: Any, context: Any) -> dict[str, Any]:
    """Refresh login node details for all active clusters.

    For each ACTIVE cluster with a ``loginNodeGroupId``, queries EC2 for
    the current running instance tagged with that node group ID.  If the
    instance ID or IP differs from what's stored, updates DynamoDB.

    Returns a summary dict with counts of clusters checked and updated.
    """
    logger.info("Starting login node refresh")

    summary = {
        "clusters_checked": 0,
        "clusters_updated": 0,
        "clusters_unreachable": 0,
        "errors": 0,
    }

    active_clusters = _scan_active_clusters()
    logger.info("Found %d active clusters to check", len(active_clusters))

    for cluster in active_clusters:
        cluster_name = cluster.get("clusterName", "")
        project_id = cluster.get("projectId", "")
        login_node_group_id = cluster.get("loginNodeGroupId", "")

        if not login_node_group_id:
            logger.debug(
                "Cluster '%s' has no loginNodeGroupId — skipping.",
                cluster_name,
            )
            continue

        summary["clusters_checked"] += 1

        try:
            current_instance_id, current_ip = _resolve_login_node(
                login_node_group_id
            )
        except _NoRunningInstance:
            summary["clusters_unreachable"] += 1
            logger.warning(
                "Cluster '%s' login node group '%s' has no running instance.",
                cluster_name,
                login_node_group_id,
            )
            continue
        except ClientError as exc:
            summary["errors"] += 1
            logger.error(
                "EC2 error resolving login node for cluster '%s': %s",
                cluster_name,
                exc,
            )
            continue

        stored_instance_id = cluster.get("loginNodeInstanceId", "")
        stored_ip = cluster.get("loginNodeIp", "")

        if (
            current_instance_id == stored_instance_id
            and current_ip == stored_ip
        ):
            continue

        logger.info(
            "Cluster '%s' login node changed: instance %s→%s, ip %s→%s",
            cluster_name,
            stored_instance_id,
            current_instance_id,
            stored_ip,
            current_ip,
        )

        try:
            _update_cluster_login_node(
                project_id, cluster_name, current_instance_id, current_ip
            )
            summary["clusters_updated"] += 1
        except ClientError as exc:
            summary["errors"] += 1
            logger.error(
                "Failed to update login node for cluster '%s': %s",
                cluster_name,
                exc,
            )

    logger.info("Login node refresh complete: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _NoRunningInstance(Exception):
    """Raised when no running instance is found for a login node group."""


def _scan_active_clusters() -> list[dict[str, Any]]:
    """Scan the Clusters table for all ACTIVE cluster records."""
    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    clusters: list[dict[str, Any]] = []

    scan_kwargs: dict[str, Any] = {
        "FilterExpression": boto3.dynamodb.conditions.Attr("status").eq("ACTIVE"),
    }

    try:
        response = table.scan(**scan_kwargs)
        clusters.extend(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = table.scan(**scan_kwargs)
            clusters.extend(response.get("Items", []))
    except ClientError as exc:
        logger.error("Failed to scan for active clusters: %s", exc)

    return clusters


def _resolve_login_node(login_node_group_id: str) -> tuple[str, str]:
    """Query EC2 for the running instance in a login node group.

    Returns ``(instance_id, public_ip)``.  The public IP may be empty
    if the instance doesn't have one assigned.

    Raises:
        _NoRunningInstance: If no running instance is found.
        ClientError: On EC2 API errors.
    """
    response = ec2_client.describe_instances(
        Filters=[
            {
                "Name": "tag:aws:pcs:compute-node-group-id",
                "Values": [login_node_group_id],
            },
            {
                "Name": "instance-state-name",
                "Values": ["running"],
            },
        ],
    )

    instances = [
        inst
        for reservation in response.get("Reservations", [])
        for inst in reservation.get("Instances", [])
    ]

    if not instances:
        raise _NoRunningInstance(login_node_group_id)

    instance = instances[0]
    return instance["InstanceId"], instance.get("PublicIpAddress", "")


def _update_cluster_login_node(
    project_id: str,
    cluster_name: str,
    instance_id: str,
    public_ip: str,
) -> None:
    """Update the login node instance ID and IP in DynamoDB."""
    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        },
        UpdateExpression=(
            "SET loginNodeInstanceId = :iid, loginNodeIp = :ip"
        ),
        ExpressionAttributeValues={
            ":iid": instance_id,
            ":ip": public_ip,
        },
    )
