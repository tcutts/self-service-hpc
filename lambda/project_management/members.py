"""Project membership management business logic.

Handles adding and removing users from projects, including
Cognito group membership for role-based access control.

When a user is added to a project with active clusters, POSIX user
propagation is triggered via SSM Run Command to ensure the new user
can log in to running cluster nodes.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import DuplicateError, InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
cognito = boto3.client("cognito-idp")

CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")


def add_member(
    projects_table_name: str,
    users_table_name: str,
    user_pool_id: str,
    project_id: str,
    user_id: str,
    role: str = "PROJECT_USER",
) -> dict[str, Any]:
    """Add a user to a project.

    1. Validate the user exists on the platform.
    2. Validate the project exists.
    3. Create the membership record in DynamoDB.
    4. Add the user to the appropriate Cognito group.
    """
    if not user_id:
        raise ValidationError("userId is required.", {"field": "userId"})
    if role not in ("PROJECT_ADMIN", "PROJECT_USER"):
        raise ValidationError(
            "role must be PROJECT_ADMIN or PROJECT_USER.",
            {"field": "role"},
        )

    # Validate user exists on the platform
    _validate_user_exists(users_table_name, user_id)

    # Validate project exists
    _validate_project_exists(projects_table_name, project_id)

    now = datetime.now(timezone.utc).isoformat()
    membership_record = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "projectId": project_id,
        "role": role,
        "addedAt": now,
    }

    table = dynamodb.Table(projects_table_name)
    try:
        table.put_item(
            Item=membership_record,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise DuplicateError(
                f"User '{user_id}' is already a member of project '{project_id}'.",
                {"userId": user_id, "projectId": project_id},
            )
        raise InternalError(f"Failed to store membership record: {exc}")

    # Add user to the appropriate Cognito group
    cognito_group = _cognito_group_name(project_id, role)
    _add_to_cognito_group(user_pool_id, user_id, cognito_group)

    # Propagate POSIX user to active clusters in this project
    propagation_status = _propagate_posix_user(
        users_table_name, user_id, project_id,
    )

    result = {
        "userId": user_id,
        "projectId": project_id,
        "role": role,
        "addedAt": now,
    }

    # If propagation is pending, update the membership record and include
    # the status in the response so callers are aware.
    if propagation_status == "PENDING_PROPAGATION":
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"MEMBER#{user_id}",
            },
            UpdateExpression="SET propagationStatus = :ps",
            ExpressionAttributeValues={":ps": "PENDING_PROPAGATION"},
        )
        result["propagationStatus"] = "PENDING_PROPAGATION"

    return result


def remove_member(
    projects_table_name: str,
    user_pool_id: str,
    project_id: str,
    user_id: str,
) -> None:
    """Remove a user from a project.

    1. Verify the membership record exists.
    2. Delete the membership record from DynamoDB.
    3. Remove the user from the Cognito group.
    """
    table = dynamodb.Table(projects_table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": f"MEMBER#{user_id}"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"User '{user_id}' is not a member of project '{project_id}'.",
            {"userId": user_id, "projectId": project_id},
        )

    member = response["Item"]
    role = member.get("role", "PROJECT_USER")

    # Delete the membership record
    table.delete_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": f"MEMBER#{user_id}"},
    )

    # Remove from Cognito group
    cognito_group = _cognito_group_name(project_id, role)
    _remove_from_cognito_group(user_pool_id, user_id, cognito_group)


def _validate_user_exists(users_table_name: str, user_id: str) -> None:
    """Verify a user exists on the platform."""
    table = dynamodb.Table(users_table_name)
    response = table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"User '{user_id}' does not exist on the platform.",
            {"userId": user_id},
        )
    if response["Item"].get("status") != "ACTIVE":
        raise ValidationError(
            f"User '{user_id}' is not active.",
            {"userId": user_id},
        )


def _validate_project_exists(projects_table_name: str, project_id: str) -> None:
    """Verify a project exists."""
    table = dynamodb.Table(projects_table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )


def _cognito_group_name(project_id: str, role: str) -> str:
    """Map a membership role to a Cognito group name."""
    if role == "PROJECT_ADMIN":
        return f"ProjectAdmin-{project_id}"
    return f"ProjectUser-{project_id}"


def _add_to_cognito_group(
    user_pool_id: str, user_id: str, group_name: str
) -> None:
    """Add a user to a Cognito group, creating the group if needed."""
    try:
        cognito.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=user_id,
            GroupName=group_name,
        )
    except cognito.exceptions.ResourceNotFoundException:
        # Group doesn't exist yet — create it, then add the user
        try:
            cognito.create_group(
                UserPoolId=user_pool_id,
                GroupName=group_name,
                Description=f"Auto-created group for project membership",
            )
            cognito.admin_add_user_to_group(
                UserPoolId=user_pool_id,
                Username=user_id,
                GroupName=group_name,
            )
        except ClientError as exc:
            logger.warning(
                "Failed to create Cognito group %s: %s", group_name, exc
            )
    except ClientError as exc:
        logger.warning(
            "Failed to add user %s to Cognito group %s: %s",
            user_id,
            group_name,
            exc,
        )


def _remove_from_cognito_group(
    user_pool_id: str, user_id: str, group_name: str
) -> None:
    """Remove a user from a Cognito group."""
    try:
        cognito.admin_remove_user_from_group(
            UserPoolId=user_pool_id,
            Username=user_id,
            GroupName=group_name,
        )
    except ClientError as exc:
        logger.warning(
            "Failed to remove user %s from Cognito group %s: %s",
            user_id,
            group_name,
            exc,
        )


def _propagate_posix_user(
    users_table_name: str,
    user_id: str,
    project_id: str,
) -> str:
    """Propagate a new POSIX user to active clusters in the project.

    Looks up the user's POSIX UID/GID from the PlatformUsers table,
    then delegates to the posix_provisioning module to send SSM Run
    Commands to all active cluster nodes.

    Returns "SUCCESS" or "PENDING_PROPAGATION".
    """
    # Look up the user's POSIX identity
    table = dynamodb.Table(users_table_name)
    try:
        response = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
        )
    except ClientError as exc:
        logger.warning(
            "Failed to look up POSIX identity for user '%s': %s",
            user_id,
            exc,
        )
        return "PENDING_PROPAGATION"

    item = response.get("Item")
    if not item or "posixUid" not in item or "posixGid" not in item:
        logger.warning(
            "User '%s' has no POSIX identity — skipping propagation.",
            user_id,
        )
        return "SUCCESS"

    uid = int(item["posixUid"])
    gid = int(item["posixGid"])

    clusters_table_name = CLUSTERS_TABLE_NAME

    try:
        from posix_provisioning import propagate_user_to_clusters

        status = propagate_user_to_clusters(
            user_id=user_id,
            uid=uid,
            gid=gid,
            project_id=project_id,
            clusters_table_name=clusters_table_name,
        )
        return status
    except Exception as exc:
        logger.warning(
            "POSIX propagation failed for user '%s' in project '%s': %s",
            user_id,
            project_id,
            exc,
        )
        return "PENDING_PROPAGATION"
