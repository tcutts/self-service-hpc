"""Project lifecycle state machine logic.

Encapsulates the valid state transitions for project lifecycle management
and provides atomic DynamoDB-backed transition operations.

Valid transitions:
    CREATED   → DEPLOYING
    DEPLOYING → ACTIVE      (deployment succeeds)
    DEPLOYING → CREATED     (deployment fails)
    ACTIVE    → DESTROYING
    DESTROYING → ARCHIVED   (destruction succeeds)
    DESTROYING → ACTIVE     (destruction fails)
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
    "ACTIVE": ["DESTROYING"],
    "DESTROYING": ["ARCHIVED", "ACTIVE"],
    "ARCHIVED": [],
}


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
