"""Login node event handler — event-driven instance ID reconciliation.

Processes EC2 Instance State-change Notification events from EventBridge.
When an EC2 instance enters the ``running`` state, queries its
``aws:pcs:compute-node-group-id`` tag to determine whether it belongs to
a login node group.  If a matching ACTIVE cluster is found in DynamoDB,
updates the ``loginNodeInstanceId`` and ``loginNodeIp`` fields so that
connection details are current within seconds of a node replacement.

Instances without PCS tags, instances belonging to compute node groups,
and instances with no matching ACTIVE cluster are silently skipped with
DEBUG-level logging.

Environment variables
---------------------
CLUSTERS_TABLE_NAME    DynamoDB Clusters table name
"""

import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
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


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process an EC2 Instance State-change Notification event.

    Parameters
    ----------
    event : dict
        EventBridge event with structure::

            {
                "detail-type": "EC2 Instance State-change Notification",
                "source": "aws.ec2",
                "detail": {
                    "instance-id": "i-0123456789abcdef0",
                    "state": "running"
                }
            }

    Returns
    -------
    dict
        Summary with keys: instance_id, action (updated|skipped|error),
        and optional clusters_updated, details, reason.
    """
    try:
        return _process_event(event)
    except Exception as exc:
        instance_id = (event.get("detail") or {}).get("instance-id", "unknown")
        logger.error(
            "Unhandled error processing event for instance '%s': %s",
            instance_id,
            exc,
        )
        return {
            "instance_id": instance_id,
            "action": "error",
            "reason": str(exc),
        }


def _process_event(event: dict[str, Any]) -> dict[str, Any]:
    """Core event processing logic.

    Separated from ``handler`` so that the outer function can catch any
    unhandled exception and return a safe response dict.
    """
    detail = event.get("detail", {})
    instance_id = detail.get("instance-id", "")
    state = detail.get("state", "")

    logger.info(
        "Processing EC2 state-change: instance='%s', state='%s'",
        instance_id,
        state,
    )

    # --- Step 1: Retrieve the PCS node group tag -------------------------
    try:
        node_group_id = _get_instance_node_group_tag(instance_id)
    except ClientError as exc:
        logger.error(
            "Failed to describe tags for instance '%s': %s",
            instance_id,
            exc,
        )
        return {
            "instance_id": instance_id,
            "action": "error",
            "reason": str(exc),
        }

    if node_group_id is None:
        logger.debug(
            "Instance '%s' has no PCS node group tag — skipping.",
            instance_id,
        )
        return {
            "instance_id": instance_id,
            "action": "skipped",
            "reason": "no PCS node group tag",
        }

    # --- Step 2: Find ACTIVE clusters by loginNodeGroupId ----------------
    try:
        clusters = _find_clusters_by_login_node_group(node_group_id)
    except ClientError as exc:
        logger.error(
            "Failed to scan clusters for node group '%s': %s",
            node_group_id,
            exc,
        )
        return {
            "instance_id": instance_id,
            "action": "error",
            "reason": str(exc),
        }

    if not clusters:
        # Distinguish compute-only match from no match at all.
        if _is_compute_node_group_only(node_group_id):
            logger.debug(
                "Instance '%s' belongs to compute node group '%s', "
                "not a login node group — skipping.",
                instance_id,
                node_group_id,
            )
            return {
                "instance_id": instance_id,
                "action": "skipped",
                "reason": "instance belongs to compute node group",
            }

        logger.debug(
            "No ACTIVE cluster with loginNodeGroupId='%s' for "
            "instance '%s' — skipping.",
            node_group_id,
            instance_id,
        )
        return {
            "instance_id": instance_id,
            "action": "skipped",
            "reason": "no matching ACTIVE cluster by loginNodeGroupId",
        }

    # --- Step 3: Resolve instance details --------------------------------
    try:
        resolved_id, public_ip = _resolve_instance_details(instance_id)
    except ClientError as exc:
        logger.error(
            "Failed to describe instance '%s': %s",
            instance_id,
            exc,
        )
        return {
            "instance_id": instance_id,
            "action": "error",
            "reason": str(exc),
        }

    # --- Step 4: Update all matching cluster records ---------------------
    if len(clusters) > 1:
        logger.warning(
            "Multiple clusters (%d) share loginNodeGroupId='%s'.",
            len(clusters),
            node_group_id,
        )

    updated_details: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for cluster in clusters:
        project_id = cluster.get("projectId", "")
        cluster_name = cluster.get("clusterName", "")
        old_instance_id = cluster.get("loginNodeInstanceId", "")
        old_ip = cluster.get("loginNodeIp", "")

        if old_instance_id == resolved_id and old_ip == public_ip:
            logger.debug(
                "Cluster '%s' (project '%s') already has "
                "instance_id='%s' and ip='%s' — skipping update.",
                cluster_name,
                project_id,
                resolved_id,
                public_ip,
            )
            continue

        try:
            _update_cluster_login_node(
                project_id, cluster_name, resolved_id, public_ip,
            )
        except ClientError as exc:
            logger.error(
                "Failed to update cluster '%s' (project '%s') "
                "for instance '%s': %s",
                cluster_name,
                project_id,
                instance_id,
                exc,
            )
            errors.append({
                "cluster_name": cluster_name,
                "project_id": project_id,
                "reason": str(exc),
            })
            continue

        logger.info(
            "Updated cluster '%s' (project '%s'): "
            "instance_id='%s', state='%s', "
            "old_instance_id='%s', new_instance_id='%s', "
            "old_ip='%s', new_ip='%s'",
            cluster_name,
            project_id,
            instance_id,
            state,
            old_instance_id,
            resolved_id,
            old_ip,
            public_ip,
        )
        updated_details.append({
            "cluster_name": cluster_name,
            "project_id": project_id,
            "old_instance_id": old_instance_id,
            "new_instance_id": resolved_id,
            "old_ip": old_ip,
            "new_ip": public_ip,
        })

    if errors:
        return {
            "instance_id": instance_id,
            "action": "error",
            "clusters_updated": len(updated_details),
            "details": updated_details,
            "errors": errors,
        }

    return {
        "instance_id": instance_id,
        "action": "updated",
        "clusters_updated": len(updated_details),
        "details": updated_details,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_compute_node_group_only(node_group_id: str) -> bool:
    """Check whether a node group ID matches only computeNodeGroupId.

    Scans DynamoDB for any ACTIVE cluster whose ``computeNodeGroupId``
    equals the given value.  Returns ``True`` if at least one match is
    found, indicating the instance belongs to a compute node group
    rather than a login node group.
    """
    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    response = table.scan(
        FilterExpression=(
            Attr("computeNodeGroupId").eq(node_group_id)
            & Attr("status").eq("ACTIVE")
        ),
        Limit=1,
    )
    return bool(response.get("Items"))


def _get_instance_node_group_tag(instance_id: str) -> str | None:
    """Query EC2 DescribeTags for the PCS node group tag.

    Parameters
    ----------
    instance_id : str
        The EC2 instance ID to query.

    Returns
    -------
    str or None
        The value of the ``aws:pcs:compute-node-group-id`` tag, or
        ``None`` if the tag is not present.

    Raises
    ------
    ClientError
        On EC2 API errors.
    """
    response = ec2_client.describe_tags(
        Filters=[
            {"Name": "resource-id", "Values": [instance_id]},
            {
                "Name": "key",
                "Values": ["aws:pcs:compute-node-group-id"],
            },
        ],
    )

    tags = response.get("Tags", [])
    if not tags:
        return None

    return tags[0].get("Value")


def _find_clusters_by_login_node_group(
    node_group_id: str,
) -> list[dict[str, Any]]:
    """Scan DynamoDB for ACTIVE clusters matching a login node group ID.

    Parameters
    ----------
    node_group_id : str
        The PCS node group ID to match against ``loginNodeGroupId``.

    Returns
    -------
    list[dict]
        Matching cluster records.  Empty if none found.

    Raises
    ------
    ClientError
        On DynamoDB API errors.
    """
    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    clusters: list[dict[str, Any]] = []

    scan_kwargs: dict[str, Any] = {
        "FilterExpression": (
            Attr("loginNodeGroupId").eq(node_group_id)
            & Attr("status").eq("ACTIVE")
        ),
    }

    response = table.scan(**scan_kwargs)
    clusters.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response:
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = table.scan(**scan_kwargs)
        clusters.extend(response.get("Items", []))

    return clusters


def _resolve_instance_details(instance_id: str) -> tuple[str, str]:
    """Call EC2 DescribeInstances to get the public IP.

    Parameters
    ----------
    instance_id : str
        The EC2 instance ID to describe.

    Returns
    -------
    tuple[str, str]
        ``(instance_id, public_ip)``.  The public IP may be an empty
        string if the instance doesn't have one assigned.

    Raises
    ------
    ClientError
        On EC2 API errors.
    """
    response = ec2_client.describe_instances(
        InstanceIds=[instance_id],
    )

    instances = [
        inst
        for reservation in response.get("Reservations", [])
        for inst in reservation.get("Instances", [])
    ]

    if not instances:
        return instance_id, ""

    instance = instances[0]
    return instance["InstanceId"], instance.get("PublicIpAddress", "")


def _update_cluster_login_node(
    project_id: str,
    cluster_name: str,
    instance_id: str,
    public_ip: str,
) -> None:
    """Update the login node instance ID and IP in DynamoDB.

    Parameters
    ----------
    project_id : str
        The project ID (used to construct the partition key).
    cluster_name : str
        The cluster name (used to construct the sort key).
    instance_id : str
        The new login node EC2 instance ID.
    public_ip : str
        The new login node public IP address.

    Raises
    ------
    ClientError
        On DynamoDB API errors.
    """
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
