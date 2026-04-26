"""Project budget management business logic.

Handles creating and updating AWS Budgets with cost allocation
tag filters and SNS notification thresholds (80% and 100%).
Supports MONTHLY and TOTAL budget types, and immediate budget
breach clearing when the new limit exceeds current spend.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
budgets_client = boto3.client("budgets")
sts_client = boto3.client("sts")
ce_client = boto3.client("ce")

VALID_BUDGET_TYPES = ("MONTHLY", "TOTAL")


def set_budget(
    projects_table_name: str,
    budget_sns_topic_arn: str,
    project_id: str,
    budget_limit: float,
    budget_type: str = "MONTHLY",
    caller_identity: str = "",
) -> dict[str, Any]:
    """Create or update an AWS Budget for a project.

    1. Validate inputs (budget_limit > 0, budget_type valid).
    2. Retrieve the project record for cost allocation tag and creation date.
    3. Create or update the AWS Budget with the appropriate time configuration.
    4. Query current spend via Cost Explorer and clear budgetBreached if applicable.
    5. Update the project record with budgetLimit, budgetType, and breach status.
    """
    if budget_limit <= 0:
        raise ValidationError(
            "budgetLimit must be a positive number.",
            {"field": "budgetLimit"},
        )

    if budget_type not in VALID_BUDGET_TYPES:
        raise ValidationError(
            f"budgetType must be one of {VALID_BUDGET_TYPES}.",
            {"field": "budgetType", "validValues": list(VALID_BUDGET_TYPES)},
        )

    # Retrieve project to get cost allocation tag and creation date
    table = dynamodb.Table(projects_table_name)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )

    project = response["Item"]
    cost_tag = project.get("costAllocationTag", project_id)
    previous_limit = project.get("budgetLimit", 0)

    # Get AWS account ID for the budgets API
    account_id = _get_account_id()

    budget_name = f"hpc-project-{project_id}"

    # Build the budget definition
    budget_definition = {
        "BudgetName": budget_name,
        "BudgetLimit": {
            "Amount": str(budget_limit),
            "Unit": "USD",
        },
        "BudgetType": "COST",
        "CostFilters": {
            "TagKeyValue": [f"user:Project${cost_tag}"],
        },
    }

    if budget_type == "TOTAL":
        # TOTAL budget: use ANNUALLY with a TimePeriod spanning the project lifetime
        created_at = project.get("createdAt", "")
        start_date = _parse_start_date(created_at)
        budget_definition["TimeUnit"] = "ANNUALLY"
        budget_definition["TimePeriod"] = {
            "Start": start_date,
            "End": "2099-12-31T00:00:00+00:00",
        }
    else:
        # MONTHLY budget: resets each calendar month
        budget_definition["TimeUnit"] = "MONTHLY"

    # Notification thresholds with SNS subscribers
    notifications_with_subscribers = [
        {
            "Notification": {
                "NotificationType": "ACTUAL",
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": 80.0,
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": [
                {
                    "SubscriptionType": "SNS",
                    "Address": budget_sns_topic_arn,
                },
            ],
        },
        {
            "Notification": {
                "NotificationType": "ACTUAL",
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": 100.0,
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": [
                {
                    "SubscriptionType": "SNS",
                    "Address": budget_sns_topic_arn,
                },
            ],
        },
    ]

    # Try to update existing budget, create if it doesn't exist
    try:
        budgets_client.describe_budget(
            AccountId=account_id,
            BudgetName=budget_name,
        )
        # Budget exists — update it
        budgets_client.update_budget(
            AccountId=account_id,
            NewBudget=budget_definition,
        )
        logger.info("Updated budget '%s' for project '%s'", budget_name, project_id)
    except budgets_client.exceptions.NotFoundException:
        # Budget doesn't exist — create it
        budgets_client.create_budget(
            AccountId=account_id,
            Budget=budget_definition,
            NotificationsWithSubscribers=notifications_with_subscribers,
        )
        logger.info("Created budget '%s' for project '%s'", budget_name, project_id)
    except ClientError as exc:
        raise InternalError(f"Failed to manage budget: {exc}")

    # Attempt to get current spend and determine breach status
    clear_breach = False
    current_spend = _get_current_spend(project_id, cost_tag)
    if current_spend is not None:
        if budget_limit > current_spend:
            clear_breach = True
            logger.info(
                "Clearing budget breach for project '%s': "
                "new_limit=%.2f > current_spend=%.2f "
                "(previous_limit=%s, caller=%s)",
                project_id,
                budget_limit,
                current_spend,
                previous_limit,
                caller_identity or "unknown",
            )
        else:
            logger.info(
                "Budget remains exceeded for project '%s': "
                "new_limit=%.2f <= current_spend=%.2f "
                "(previous_limit=%s, caller=%s)",
                project_id,
                budget_limit,
                current_spend,
                previous_limit,
                caller_identity or "unknown",
            )

    # Update the project record with the new budget limit, type, and breach status
    now = datetime.now(timezone.utc).isoformat()
    update_expr = "SET budgetLimit = :limit, budgetType = :btype, updatedAt = :now"
    expr_values: dict[str, Any] = {
        ":limit": int(budget_limit) if budget_limit == int(budget_limit) else budget_limit,
        ":btype": budget_type,
        ":now": now,
    }

    if clear_breach:
        update_expr += ", budgetBreached = :breached"
        expr_values[":breached"] = False

    table.update_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )

    return {
        "projectId": project_id,
        "budgetName": budget_name,
        "budgetLimit": budget_limit,
        "budgetType": budget_type,
        "thresholds": [80, 100],
        "snsTopicArn": budget_sns_topic_arn,
    }


def _get_current_spend(project_id: str, cost_tag: str) -> float | None:
    """Query Cost Explorer for the current month's spend on a project.

    Returns the spend amount as a float, or None if Cost Explorer
    is unavailable or the query fails.
    """
    try:
        now = datetime.now(timezone.utc)
        start_of_month = now.strftime("%Y-%m-01")
        end_date = now.strftime("%Y-%m-%d")

        # If start and end are the same (1st of month), push end forward
        if start_of_month == end_date:
            end_date = now.strftime("%Y-%m-02")

        response = ce_client.get_cost_and_usage(
            TimePeriod={
                "Start": start_of_month,
                "End": end_date,
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={
                "Tags": {
                    "Key": "Project",
                    "Values": [cost_tag],
                },
            },
        )

        results = response.get("ResultsByTime", [])
        if results:
            amount_str = results[0].get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")
            return float(amount_str)
        return 0.0
    except Exception:
        logger.warning(
            "Failed to query Cost Explorer for project '%s'. "
            "Budget breach status will not be updated.",
            project_id,
            exc_info=True,
        )
        return None


def _parse_start_date(created_at: str) -> str:
    """Parse the project creation date into a budget TimePeriod start date.

    Returns an ISO 8601 date string. Falls back to the current date
    if parsing fails.
    """
    try:
        dt = datetime.fromisoformat(created_at)
        return dt.strftime("%Y-%m-%dT00:00:00+00:00")
    except (ValueError, TypeError):
        logger.warning(
            "Could not parse createdAt '%s', using current date as start.",
            created_at,
        )
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


def _get_account_id() -> str:
    """Retrieve the current AWS account ID."""
    try:
        return sts_client.get_caller_identity()["Account"]
    except ClientError as exc:
        raise InternalError(f"Failed to retrieve AWS account ID: {exc}")
