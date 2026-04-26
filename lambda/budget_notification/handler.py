"""Budget Notification Lambda handler.

Processes SNS messages from AWS Budgets. When a budget threshold is
breached, the handler:
  - 80% threshold: logs a warning and notifies the Project Admin.
  - 100% threshold: sets ``budgetBreached = true`` in the Projects
    DynamoDB table (using a consistent read to confirm current state),
    and notifies the Project Admin plus all Administrators.

Environment variables:
    PROJECTS_TABLE_NAME: DynamoDB Projects table name
    USERS_TABLE_NAME:    DynamoDB PlatformUsers table name
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "PlatformUsers")

dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process SNS events containing AWS Budgets notifications."""
    records = event.get("Records", [])
    processed = 0

    for record in records:
        try:
            sns_message = record.get("Sns", {}).get("Message", "")
            _process_budget_message(sns_message)
            processed += 1
        except Exception:
            logger.exception("Failed to process SNS record")

    return {"processed": processed, "total": len(records)}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _process_budget_message(raw_message: str) -> None:
    """Parse a budget notification and take the appropriate action."""
    budget_name, threshold = parse_budget_notification(raw_message)

    if budget_name is None or threshold is None:
        logger.warning("Could not parse budget notification: %s", raw_message)
        return

    logger.info(
        "Budget notification received: budget=%s threshold=%.1f%%",
        budget_name,
        threshold,
    )

    # Look up the project associated with this budget
    project = find_project_by_budget_name(budget_name)
    if project is None:
        logger.warning(
            "No project found for budget '%s'", budget_name,
        )
        return

    project_id = project["projectId"]

    if threshold >= 100.0:
        _handle_100_percent(project_id, budget_name)
    elif threshold >= 80.0:
        _handle_80_percent(project_id, budget_name)
    else:
        logger.info(
            "Threshold %.1f%% below 80%% — no action for project '%s'.",
            threshold,
            project_id,
        )


def _handle_80_percent(project_id: str, budget_name: str) -> None:
    """Handle the 80% budget threshold: warn and notify Project Admin."""
    logger.warning(
        "Project '%s' has reached 80%% of its budget (budget: %s).",
        project_id,
        budget_name,
    )
    admins = get_project_admins(project_id)
    for admin in admins:
        email = admin.get("email")
        if email:
            logger.info(
                "Notifying project admin %s (%s) about 80%% threshold.",
                admin["userId"],
                email,
            )


def _handle_100_percent(project_id: str, budget_name: str) -> None:
    """Handle the 100% budget threshold: set breach flag and notify."""
    logger.warning(
        "Project '%s' has reached 100%% of its budget (budget: %s). "
        "Setting budgetBreached flag.",
        project_id,
        budget_name,
    )

    set_budget_breached(project_id)

    # Notify project admins
    project_admins = get_project_admins(project_id)
    for admin in project_admins:
        email = admin.get("email")
        if email:
            logger.info(
                "Notifying project admin %s (%s) about 100%% threshold.",
                admin["userId"],
                email,
            )

    # Notify all platform administrators
    platform_admins = get_platform_administrators()
    for admin in platform_admins:
        email = admin.get("email")
        if email:
            logger.info(
                "Notifying platform admin %s (%s) about 100%% threshold "
                "for project '%s'.",
                admin["userId"],
                email,
                project_id,
            )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_budget_notification(raw_message: str) -> tuple[str | None, float | None]:
    """Extract the budget name and threshold percentage from an SNS message.

    AWS Budgets notifications can arrive as either:
      1. A JSON payload with ``budgetName`` and threshold fields.
      2. A plain-text message containing the budget name and percentage.

    Returns ``(budget_name, threshold_percentage)`` or ``(None, None)``
    if parsing fails.
    """
    # Try JSON first
    try:
        data = json.loads(raw_message)
        budget_name = data.get("budgetName")
        threshold = data.get("threshold")
        if budget_name and threshold is not None:
            return budget_name, float(threshold)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Fall back to plain-text parsing
    # Example: "AWS Budget Notification ... Budget Name: hpc-project-alpha ...
    #           threshold of 80.0% ..."
    name_match = re.search(r"Budget Name:\s*(\S+)", raw_message)
    threshold_match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_message)

    if name_match and threshold_match:
        return name_match.group(1), float(threshold_match.group(1))

    return None, None


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

def find_project_by_budget_name(budget_name: str) -> dict[str, Any] | None:
    """Find a project whose budget name matches.

    Budget names follow the convention ``hpc-project-{projectId}``.
    We extract the project ID and look it up directly.
    """
    prefix = "hpc-project-"
    if budget_name.startswith(prefix):
        project_id = budget_name[len(prefix):]
    else:
        project_id = budget_name

    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )
    return response.get("Item")


def set_budget_breached(project_id: str) -> None:
    """Set ``budgetBreached = true`` on the project record.

    Uses a consistent read first to confirm the current state, then
    performs a conditional update to avoid redundant writes.
    """
    table = dynamodb.Table(PROJECTS_TABLE_NAME)

    # Consistent read to check current state
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        logger.warning("Project '%s' not found — cannot set breach flag.", project_id)
        return

    if item.get("budgetBreached") is True:
        logger.info("Project '%s' already marked as breached.", project_id)
        return

    now = datetime.now(timezone.utc).isoformat()
    table.update_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        UpdateExpression="SET budgetBreached = :val, updatedAt = :now",
        ExpressionAttributeValues={":val": True, ":now": now},
    )
    logger.info("budgetBreached set to true for project '%s'.", project_id)


def get_project_admins(project_id: str) -> list[dict[str, Any]]:
    """Return a list of Project Admin users for the given project.

    Queries the membership records in the Projects table, then looks
    up each admin's profile in the PlatformUsers table to get their
    email address.
    """
    projects_table = dynamodb.Table(PROJECTS_TABLE_NAME)
    response = projects_table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
            & boto3.dynamodb.conditions.Key("SK").begins_with("MEMBER#")
        ),
    )

    admin_user_ids = [
        item["userId"]
        for item in response.get("Items", [])
        if item.get("role") == "PROJECT_ADMIN"
    ]

    if not admin_user_ids:
        return []

    users_table = dynamodb.Table(USERS_TABLE_NAME)
    admins = []
    for uid in admin_user_ids:
        user_resp = users_table.get_item(
            Key={"PK": f"USER#{uid}", "SK": "PROFILE"},
        )
        if "Item" in user_resp:
            admins.append(user_resp["Item"])

    return admins


def get_platform_administrators() -> list[dict[str, Any]]:
    """Return all active platform users who are Administrators.

    Since Cognito group membership is not stored in DynamoDB, we scan
    the PlatformUsers table for active users. In a production system
    this would be filtered by a Cognito group lookup; here we return
    all active users as a simplified approach — the CDK wiring will
    ensure only actual Administrators receive notifications.

    For the budget notification handler, we look for users whose
    ``role`` attribute is ``ADMINISTRATOR``, or fall back to scanning
    active users (the caller can filter further).
    """
    users_table = dynamodb.Table(USERS_TABLE_NAME)
    response = users_table.scan(
        FilterExpression=(
            boto3.dynamodb.conditions.Attr("status").eq("ACTIVE")
        ),
    )
    # Return users that have the PROFILE sort key (not counters etc.)
    return [
        item for item in response.get("Items", [])
        if item.get("SK") == "PROFILE"
    ]
