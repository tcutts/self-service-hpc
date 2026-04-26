"""Cluster name validation, suggestion, and registry.

Provides functions for:
- Validating cluster name format (alphanumeric, hyphens, underscores)
- Suggesting cluster names based on project ID with a random suffix
- Registering cluster names in DynamoDB with cross-project uniqueness
- Looking up cluster name ownership
"""

import logging
import random
import re
import string
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import ConflictError, ValidationError

logger = logging.getLogger(__name__)

_CLUSTER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_SUFFIX_LENGTH = 6
_SUFFIX_CHARS = string.ascii_lowercase + string.digits

dynamodb = boto3.resource("dynamodb")


def validate_cluster_name(name: str) -> bool:
    """Check whether a cluster name matches the allowed format.

    A valid cluster name is non-empty and contains only alphanumeric
    characters, hyphens, and underscores.

    Returns True if valid, False otherwise.
    """
    if not name:
        return False
    return _CLUSTER_NAME_PATTERN.match(name) is not None


def suggest_cluster_name(project_id: str) -> str:
    """Generate a suggested cluster name for a project.

    Returns ``{project_id}-{random_suffix}`` where the suffix is
    six random lowercase-alphanumeric characters.
    """
    suffix = "".join(random.choices(_SUFFIX_CHARS, k=_SUFFIX_LENGTH))
    return f"{project_id}-{suffix}"


def register_cluster_name(
    table_name: str,
    cluster_name: str,
    project_id: str,
) -> dict[str, Any]:
    """Register a cluster name in the ClusterNameRegistry.

    Uses a DynamoDB conditional put so that:
    - First registration succeeds (attribute_not_exists).
    - Same-project re-registration succeeds (projectId matches).
    - Different-project registration fails with ConflictError.

    Returns the registry record on success.
    """
    if not validate_cluster_name(cluster_name):
        raise ValidationError(
            f"Invalid cluster name '{cluster_name}'. "
            "Names must be non-empty and contain only alphanumeric characters, hyphens, and underscores.",
            {"clusterName": cluster_name},
        )

    now = datetime.now(timezone.utc).isoformat()

    record = {
        "PK": f"CLUSTERNAME#{cluster_name}",
        "SK": "REGISTRY",
        "clusterName": cluster_name,
        "projectId": project_id,
        "registeredAt": now,
    }

    table = dynamodb.Table(table_name)
    try:
        table.put_item(
            Item=record,
            ConditionExpression="attribute_not_exists(PK) OR projectId = :pid",
            ExpressionAttributeValues={":pid": project_id},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConflictError(
                f"Cluster name '{cluster_name}' is already reserved by a different project.",
                {"clusterName": cluster_name},
            )
        raise

    return _sanitise_record(record)


def lookup_cluster_name(
    table_name: str,
    cluster_name: str,
) -> dict[str, Any] | None:
    """Look up a cluster name in the registry.

    Returns the registry record if found, or None if the name
    has not been registered.
    """
    table = dynamodb.Table(table_name)
    response = table.get_item(
        Key={"PK": f"CLUSTERNAME#{cluster_name}", "SK": "REGISTRY"},
    )
    item = response.get("Item")
    if item is None:
        return None
    return _sanitise_record(item)


def _sanitise_record(item: dict[str, Any]) -> dict[str, Any]:
    """Remove DynamoDB key attributes from a registry record."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}
