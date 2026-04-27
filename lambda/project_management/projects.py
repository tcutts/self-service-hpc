"""Core project management business logic.

Handles project CRUD operations and DynamoDB persistence.
Project infrastructure provisioning (VPC, EFS, S3) is deferred
to a separate task — this module stores the project record only.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import ConflictError, DuplicateError, InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")


def create_project(
    table_name: str,
    project_id: str,
    project_name: str,
    cost_allocation_tag: str | None = None,
) -> dict[str, Any]:
    """Create a new project record in DynamoDB.

    The cost allocation tag defaults to the project ID if not provided.
    Actual infrastructure provisioning (VPC, EFS, S3) will be triggered
    in a later task.
    """
    if not project_id:
        raise ValidationError("projectId is required.", {"field": "projectId"})
    if not project_name:
        raise ValidationError("projectName is required.", {"field": "projectName"})

    tag_value = cost_allocation_tag or project_id
    now = datetime.now(timezone.utc).isoformat()

    project_record = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": project_name,
        "costAllocationTag": tag_value,
        "vpcId": "",
        "efsFileSystemId": "",
        "s3BucketName": "",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "budgetType": "MONTHLY",
        "cdkStackName": "",
        "status": "CREATED",
        "currentStep": 0,
        "totalSteps": 0,
        "stepDescription": "",
        "errorMessage": "",
        "statusChangedAt": now,
        "trustedCidrRanges": [],
        "createdAt": now,
        "updatedAt": now,
    }

    table = dynamodb.Table(table_name)
    try:
        table.put_item(
            Item=project_record,
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise DuplicateError(
                f"Project '{project_id}' already exists.",
                {"projectId": project_id},
            )
        raise InternalError(f"Failed to store project record: {exc}")

    return _sanitise_record(project_record)


def get_project(table_name: str, project_id: str) -> dict[str, Any]:
    """Retrieve a single project record by projectId."""
    table = dynamodb.Table(table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )
    return _sanitise_record(response["Item"])


def list_projects(table_name: str) -> list[dict[str, Any]]:
    """List all projects."""
    table = dynamodb.Table(table_name)
    response = table.scan(
        FilterExpression=boto3.dynamodb.conditions.Attr("SK").eq("METADATA")
        & boto3.dynamodb.conditions.Attr("PK").begins_with("PROJECT#"),
    )
    return [_sanitise_record(item) for item in response.get("Items", [])]


def delete_project(
    table_name: str,
    clusters_table_name: str,
    project_id: str,
) -> None:
    """Delete a project after verifying no active clusters exist.

    Raises ConflictError if any clusters are in ACTIVE or CREATING status.
    """
    # Verify project exists
    get_project(table_name, project_id)

    # Check for active clusters
    active_clusters = _get_active_clusters(clusters_table_name, project_id)
    if active_clusters:
        cluster_names = [c["clusterName"] for c in active_clusters]
        raise ConflictError(
            f"Cannot delete project '{project_id}': active clusters exist.",
            {"projectId": project_id, "activeClusters": cluster_names},
        )

    # Delete membership records first
    table = dynamodb.Table(table_name)
    members_response = table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
            & boto3.dynamodb.conditions.Key("SK").begins_with("MEMBER#")
        ),
    )
    for member in members_response.get("Items", []):
        table.delete_item(Key={"PK": member["PK"], "SK": member["SK"]})

    # Delete the project metadata record
    table.delete_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )


def _get_active_clusters(
    clusters_table_name: str,
    project_id: str,
) -> list[dict[str, Any]]:
    """Return clusters in ACTIVE or CREATING status for a project."""
    table = dynamodb.Table(clusters_table_name)
    response = table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
            & boto3.dynamodb.conditions.Key("SK").begins_with("CLUSTER#")
        ),
    )
    return [
        item
        for item in response.get("Items", [])
        if item.get("status") in ("ACTIVE", "CREATING")
    ]


def get_foundation_timestamp(table_name: str) -> str | None:
    """Read the foundation stack timestamp from the Projects table.

    Returns the timestamp string, or None if the record does not exist.
    """
    table = dynamodb.Table(table_name)
    response = table.get_item(
        Key={"PK": "PLATFORM", "SK": "FOUNDATION_TIMESTAMP"},
    )
    item = response.get("Item")
    if item is None:
        return None
    return item.get("timestamp")


def _sanitise_record(item: dict[str, Any]) -> dict[str, Any]:
    """Remove DynamoDB key attributes from a project record for API response."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}
