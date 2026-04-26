"""Cluster query business logic for the Cluster Operations API.

Provides functions for retrieving cluster records and checking
project budget breach status from DynamoDB.
"""

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import BudgetExceededError, InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")


def get_cluster(
    clusters_table_name: str,
    project_id: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Retrieve a single cluster record by project ID and cluster name.

    Raises ``NotFoundError`` if the cluster does not exist.
    """
    table = dynamodb.Table(clusters_table_name)
    response = table.get_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        },
    )
    item = response.get("Item")
    if not item:
        raise NotFoundError(
            f"Cluster '{cluster_name}' not found in project '{project_id}'.",
            {"projectId": project_id, "clusterName": cluster_name},
        )
    return _sanitise_record(item)


def list_clusters(
    clusters_table_name: str,
    project_id: str,
) -> list[dict[str, Any]]:
    """List all clusters for a project.

    Returns cluster records sorted by sort key (cluster name).
    """
    table = dynamodb.Table(clusters_table_name)
    response = table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
            & boto3.dynamodb.conditions.Key("SK").begins_with("CLUSTER#")
        ),
    )
    return [_sanitise_record(item) for item in response.get("Items", [])]


def check_budget_breach(
    projects_table_name: str,
    project_id: str,
) -> bool:
    """Check whether the project budget has been breached.

    Uses a DynamoDB consistent read to avoid stale data.

    Returns True if the budget is breached, False otherwise.
    Raises ``NotFoundError`` if the project does not exist.
    """
    table = dynamodb.Table(projects_table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )
    return bool(item.get("budgetBreached", False))


def _sanitise_record(item: dict[str, Any]) -> dict[str, Any]:
    """Remove DynamoDB key attributes from a cluster record for API response."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}
