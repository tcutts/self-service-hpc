"""Project lifecycle state machine logic.

Encapsulates the valid state transitions for project lifecycle management
and provides atomic DynamoDB-backed transition operations.

Valid transitions:
    CREATED    → DEPLOYING
    DEPLOYING  → ACTIVE      (deployment succeeds)
    DEPLOYING  → CREATED     (deployment fails)
    ACTIVE     → DESTROYING
    ACTIVE     → UPDATING
    ACTIVE     → ARCHIVED    (deactivation — no clusters)
    UPDATING   → ACTIVE      (update succeeds or fails)
    DESTROYING → ARCHIVED    (destruction succeeds)
    DESTROYING → ACTIVE      (destruction fails)
    ARCHIVED   → ACTIVE      (reactivation)
"""

import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")

VALID_TRANSITIONS: dict[str, list[str]] = {
    "CREATED": ["DEPLOYING"],
    "DEPLOYING": ["ACTIVE", "CREATED"],
    "ACTIVE": ["DESTROYING", "UPDATING", "ARCHIVED"],
    "UPDATING": ["ACTIVE"],
    "DESTROYING": ["ARCHIVED", "ACTIVE"],
    "ARCHIVED": ["ACTIVE"],
}

cognito = boto3.client("cognito-idp")


def validate_transition(current_status: str, target_status: str) -> None:
    """Validate that a status transition is allowed.

    Raises:
        ConflictError: If the transition is not permitted by the state machine.
    """
    allowed = VALID_TRANSITIONS.get(current_status, [])
    if target_status not in allowed:
        allowed_str = ", ".join(allowed) if allowed else "none"
        raise ConflictError(
            f"Cannot transition from {current_status} to {target_status}. "
            f"Valid transitions from {current_status}: {allowed_str}.",
            {
                "currentStatus": current_status,
                "targetStatus": target_status,
                "validTransitions": allowed,
            },
        )


def transition_project(
    table_name: str,
    project_id: str,
    target_status: str,
    error_message: str = "",
) -> dict[str, Any]:
    """Atomically transition a project to a new status.

    Uses a DynamoDB ConditionExpression to ensure the project is still in
    an expected state before applying the transition.  Sets both
    ``statusChangedAt`` and ``updatedAt`` to the current UTC timestamp.

    Returns:
        The DynamoDB update response attributes.

    Raises:
        ConflictError: If the current status does not allow the requested
            transition, or if a concurrent update changed the status first.
    """
    # Determine which current statuses can reach the target
    valid_sources = [
        src for src, targets in VALID_TRANSITIONS.items() if target_status in targets
    ]
    if not valid_sources:
        raise ConflictError(
            f"No valid source status exists for target status '{target_status}'.",
            {"targetStatus": target_status},
        )

    now = datetime.now(timezone.utc).isoformat()
    table = dynamodb.Table(table_name)

    # Build a condition that accepts any valid source status
    condition_values: dict[str, str] = {}
    source_placeholders: list[str] = []
    for idx, src in enumerate(valid_sources):
        placeholder = f":src{idx}"
        condition_values[placeholder] = src
        source_placeholders.append(placeholder)

    condition_expr = "#st IN (" + ", ".join(source_placeholders) + ")"

    update_expr = (
        "SET #st = :target, statusChangedAt = :ts, updatedAt = :ts"
    )
    expr_values: dict[str, Any] = {
        ":target": target_status,
        ":ts": now,
        **condition_values,
    }

    if error_message:
        update_expr += ", errorMessage = :err"
        expr_values[":err"] = error_message
    else:
        update_expr += ", errorMessage = :empty"
        expr_values[":empty"] = ""

    try:
        response = table.update_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
            UpdateExpression=update_expr,
            ConditionExpression=condition_expr,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConflictError(
                f"Cannot transition project '{project_id}' to {target_status}. "
                f"The project status has changed or is not in a valid state "
                f"for this transition.",
                {
                    "projectId": project_id,
                    "targetStatus": target_status,
                    "validSourceStatuses": valid_sources,
                },
            )
        raise

    logger.info(
        "Project '%s' transitioned to %s",
        project_id,
        target_status,
    )
    return response.get("Attributes", {})


def deactivate_project(
    table_name: str,
    user_pool_id: str,
    project_id: str,
    clusters_table_name: str,
) -> dict:
    """Deactivate a project by revoking Cognito access and archiving it.

    Steps:
        1. Verify the project exists and is ACTIVE.
        2. Check that no active (non-DESTROYED) clusters remain.
        3. Delete the ProjectAdmin-{projectId} and ProjectUser-{projectId}
           Cognito groups (log failures and continue per Req 14.6).
        4. Transition the project status from ACTIVE to ARCHIVED.
        5. Return the updated project record.

    Membership records are preserved in DynamoDB for future reactivation
    (Req 14.3).

    Raises:
        NotFoundError: If the project does not exist.
        ConflictError: If the project is not ACTIVE or active clusters exist.
    """
    # 1. Verify project exists and is ACTIVE
    table = dynamodb.Table(table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )
    project = response["Item"]
    if project.get("status") != "ACTIVE":
        raise ConflictError(
            f"Cannot deactivate project '{project_id}': status is "
            f"'{project.get('status')}', expected 'ACTIVE'.",
            {
                "projectId": project_id,
                "currentStatus": project.get("status"),
                "requiredStatus": "ACTIVE",
            },
        )

    # 2. Check for non-DESTROYED clusters
    clusters_table = dynamodb.Table(clusters_table_name)
    clusters_response = clusters_table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
            & boto3.dynamodb.conditions.Key("SK").begins_with("CLUSTER#")
        ),
    )
    active_clusters = [
        item for item in clusters_response.get("Items", [])
        if item.get("status") != "DESTROYED"
    ]
    if active_clusters:
        cluster_names = [c.get("clusterName", c.get("SK", "")) for c in active_clusters]
        raise ConflictError(
            f"Cannot deactivate project '{project_id}': "
            f"active clusters exist. Destroy all clusters first.",
            {"projectId": project_id, "activeClusters": cluster_names},
        )

    # 3. Delete Cognito groups (log failures, continue — Req 14.6)
    for group_name in (
        f"ProjectAdmin-{project_id}",
        f"ProjectUser-{project_id}",
    ):
        try:
            cognito.delete_group(
                GroupName=group_name,
                UserPoolId=user_pool_id,
            )
            logger.info("Deleted Cognito group '%s'.", group_name)
        except Exception:
            logger.warning(
                "Failed to delete Cognito group '%s' during deactivation "
                "of project '%s'. Continuing.",
                group_name,
                project_id,
                exc_info=True,
            )

    # 4. Transition ACTIVE → ARCHIVED
    updated = transition_project(
        table_name=table_name,
        project_id=project_id,
        target_status="ARCHIVED",
    )

    return updated


def reactivate_project(
    table_name: str,
    user_pool_id: str,
    project_id: str,
) -> dict:
    """Reactivate an archived project by restoring Cognito groups and memberships.

    Steps:
        1. Verify the project exists and is ARCHIVED.
        2. Recreate the ProjectAdmin-{projectId} and ProjectUser-{projectId}
           Cognito groups (Req 14.4).
        3. Query all MEMBER# records for the project from DynamoDB.
        4. For each member, add them to the appropriate Cognito group based
           on their stored role (Req 14.5).
        5. If adding a member to a Cognito group fails, mark the membership
           record with PENDING_RESTORATION status and log the failure (Req 14.7).
        6. Transition project status from ARCHIVED to ACTIVE.
        7. Return the updated project record.

    Raises:
        NotFoundError: If the project does not exist.
        ConflictError: If the project is not ARCHIVED.
    """
    # 1. Verify project exists and is ARCHIVED
    table = dynamodb.Table(table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )
    project = response["Item"]
    if project.get("status") != "ARCHIVED":
        raise ConflictError(
            f"Cannot reactivate project '{project_id}': status is "
            f"'{project.get('status')}', expected 'ARCHIVED'.",
            {
                "projectId": project_id,
                "currentStatus": project.get("status"),
                "requiredStatus": "ARCHIVED",
            },
        )

    # 2. Recreate Cognito groups (Req 14.4)
    for group_name in (
        f"ProjectAdmin-{project_id}",
        f"ProjectUser-{project_id}",
    ):
        try:
            cognito.create_group(
                GroupName=group_name,
                UserPoolId=user_pool_id,
                Description=f"Auto-created group for project membership",
            )
            logger.info("Recreated Cognito group '%s'.", group_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "GroupExistsException":
                logger.info(
                    "Cognito group '%s' already exists — skipping creation.",
                    group_name,
                )
            else:
                raise

    # 3. Query all MEMBER# records for the project
    members_response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": f"PROJECT#{project_id}",
            ":sk_prefix": "MEMBER#",
        },
    )
    members = members_response.get("Items", [])

    # 4 & 5. Restore each member to the appropriate Cognito group
    for member in members:
        user_id = member.get("userId", "")
        role = member.get("role", "PROJECT_USER")

        if role == "PROJECT_ADMIN":
            group_name = f"ProjectAdmin-{project_id}"
        else:
            group_name = f"ProjectUser-{project_id}"

        try:
            cognito.admin_add_user_to_group(
                UserPoolId=user_pool_id,
                Username=user_id,
                GroupName=group_name,
            )
            logger.info(
                "Restored member '%s' to Cognito group '%s'.",
                user_id,
                group_name,
            )
            # Clear any previous PENDING_RESTORATION status on success
            if member.get("propagationStatus") == "PENDING_RESTORATION":
                table.update_item(
                    Key={
                        "PK": f"PROJECT#{project_id}",
                        "SK": f"MEMBER#{user_id}",
                    },
                    UpdateExpression="REMOVE propagationStatus",
                )
        except Exception:
            logger.warning(
                "Failed to restore member '%s' to Cognito group '%s' "
                "during reactivation of project '%s'. Marking as "
                "PENDING_RESTORATION.",
                user_id,
                group_name,
                project_id,
                exc_info=True,
            )
            # Mark the membership record with PENDING_RESTORATION (Req 14.7)
            table.update_item(
                Key={
                    "PK": f"PROJECT#{project_id}",
                    "SK": f"MEMBER#{user_id}",
                },
                UpdateExpression="SET propagationStatus = :ps",
                ExpressionAttributeValues={":ps": "PENDING_RESTORATION"},
            )

    # 6. Transition ARCHIVED → ACTIVE
    updated = transition_project(
        table_name=table_name,
        project_id=project_id,
        target_status="ACTIVE",
    )

    return updated
