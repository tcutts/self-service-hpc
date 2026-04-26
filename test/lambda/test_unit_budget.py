"""Unit tests for the budget module — budget type and breach clearing.

Covers:
- set_budget() with budget_type="MONTHLY" creates budget with TimeUnit: "MONTHLY"
- set_budget() with budget_type="TOTAL" creates budget with TimeUnit: "ANNUALLY" and correct TimePeriod
- Budget breach clearing when new limit exceeds current spend
- Budget breach retained when new limit is below current spend
- Default budget type is MONTHLY when not specified
- Rejection of invalid budget type values
- Rejection of zero or negative budget limit

Requirements: 7.1, 7.3, 7.4, 8.1, 8.2, 8.3, 8.6, 8.7

The budget module calls AWS Budgets, STS, and Cost Explorer which are not
fully supported by moto, so we mock those clients at the module level while
using moto for DynamoDB.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    create_projects_table,
    _load_module_from,
    _PROJECT_MGMT_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id, **overrides):
    """Insert a project record for budget tests."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": "ACTIVE",
        "vpcId": "vpc-123",
        "efsFileSystemId": "fs-123",
        "s3BucketName": "bucket-123",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "budgetType": "MONTHLY",
        "cdkStackName": "stack-123",
        "createdAt": "2024-06-15T10:00:00+00:00",
        "updatedAt": "2024-06-15T10:00:00+00:00",
    }
    item.update(overrides)
    projects_table.put_item(Item=item)


def _make_mock_budgets_client(budget_exists=False):
    """Create a mock budgets client that simulates describe/create/update."""
    mock = MagicMock()
    if budget_exists:
        mock.describe_budget.return_value = {"Budget": {"BudgetName": "test"}}
    else:
        not_found = type("NotFoundException", (Exception,), {})
        mock.exceptions.NotFoundException = not_found
        mock.describe_budget.side_effect = not_found("Budget not found")
    mock.create_budget.return_value = {}
    mock.update_budget.return_value = {}
    return mock


def _make_mock_sts_client(account_id="123456789012"):
    """Create a mock STS client."""
    mock = MagicMock()
    mock.get_caller_identity.return_value = {"Account": account_id}
    return mock


def _make_mock_ce_client(spend_amount=0.0):
    """Create a mock Cost Explorer client returning a given spend amount."""
    mock = MagicMock()
    mock.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Total": {
                    "UnblendedCost": {
                        "Amount": str(spend_amount),
                        "Unit": "USD",
                    }
                }
            }
        ]
    }
    return mock


def _make_mock_ce_client_failing():
    """Create a mock Cost Explorer client that raises an exception."""
    mock = MagicMock()
    mock.get_cost_and_usage.side_effect = Exception("CE unavailable")
    return mock


# ---------------------------------------------------------------------------
# Test class — Budget Type and Breach Clearing
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestBudgetTypeAndBreachClearing:
    """Validates: Requirements 7.1, 7.3, 7.4, 8.1, 8.2, 8.3, 8.6, 8.7"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set up moto DynamoDB and reload budget module with mocked AWS clients."""
        with mock_aws():
            self.projects_table = create_projects_table()

            # Reload the budget module inside the mock context so DynamoDB binds to moto
            self.mock_budgets = _make_mock_budgets_client(budget_exists=False)
            self.mock_sts = _make_mock_sts_client()
            self.mock_ce = _make_mock_ce_client(spend_amount=0.0)

            # Load errors module first (dependency)
            _load_module_from(_PROJECT_MGMT_DIR, "errors")
            # Load budget module
            self.budget_mod = _load_module_from(_PROJECT_MGMT_DIR, "budget")

            # Patch the module-level clients
            self.budget_mod.budgets_client = self.mock_budgets
            self.budget_mod.sts_client = self.mock_sts
            self.budget_mod.ce_client = self.mock_ce

            yield

    # -- Budget type: MONTHLY -----------------------------------------------

    def test_monthly_budget_creates_with_monthly_time_unit(self):
        """Validates: Requirements 8.1, 8.2"""
        _seed_project(self.projects_table, "proj-monthly")

        result = self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-monthly",
            budget_limit=100.0,
            budget_type="MONTHLY",
        )

        assert result["budgetType"] == "MONTHLY"
        assert result["budgetLimit"] == 100.0

        # Verify the budget was created with MONTHLY TimeUnit
        call_args = self.mock_budgets.create_budget.call_args
        budget_def = call_args.kwargs.get("Budget") or call_args[1].get("Budget")
        assert budget_def["TimeUnit"] == "MONTHLY"
        assert "TimePeriod" not in budget_def

    # -- Budget type: TOTAL -------------------------------------------------

    def test_total_budget_creates_with_annually_time_unit(self):
        """Validates: Requirements 8.1, 8.3"""
        _seed_project(
            self.projects_table, "proj-total",
            createdAt="2024-06-15T10:00:00+00:00",
        )

        result = self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-total",
            budget_limit=5000.0,
            budget_type="TOTAL",
        )

        assert result["budgetType"] == "TOTAL"
        assert result["budgetLimit"] == 5000.0

        # Verify the budget was created with ANNUALLY TimeUnit and TimePeriod
        call_args = self.mock_budgets.create_budget.call_args
        budget_def = call_args.kwargs.get("Budget") or call_args[1].get("Budget")
        assert budget_def["TimeUnit"] == "ANNUALLY"
        assert "TimePeriod" in budget_def
        assert budget_def["TimePeriod"]["Start"] == "2024-06-15T00:00:00+00:00"
        assert budget_def["TimePeriod"]["End"] == "2099-12-31T00:00:00+00:00"

    # -- Budget breach clearing ---------------------------------------------

    def test_breach_cleared_when_new_limit_exceeds_spend(self):
        """Validates: Requirements 7.1, 7.3"""
        _seed_project(
            self.projects_table, "proj-breach-clear",
            budgetBreached=True,
            budgetLimit=50,
        )
        # Current spend is $30, new limit is $100 → should clear breach
        self.mock_ce = _make_mock_ce_client(spend_amount=30.0)
        self.budget_mod.ce_client = self.mock_ce

        self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-breach-clear",
            budget_limit=100.0,
            budget_type="MONTHLY",
        )

        # Verify budgetBreached was cleared in DynamoDB
        item = self.projects_table.get_item(
            Key={"PK": "PROJECT#proj-breach-clear", "SK": "METADATA"}
        )["Item"]
        assert item["budgetBreached"] is False

    def test_breach_retained_when_new_limit_below_spend(self):
        """Validates: Requirements 7.4"""
        _seed_project(
            self.projects_table, "proj-breach-retain",
            budgetBreached=True,
            budgetLimit=50,
        )
        # Current spend is $200, new limit is $100 → breach should remain
        self.mock_ce = _make_mock_ce_client(spend_amount=200.0)
        self.budget_mod.ce_client = self.mock_ce

        self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-breach-retain",
            budget_limit=100.0,
            budget_type="MONTHLY",
        )

        # Verify budgetBreached was NOT cleared (remains True from seed)
        item = self.projects_table.get_item(
            Key={"PK": "PROJECT#proj-breach-retain", "SK": "METADATA"}
        )["Item"]
        assert item["budgetBreached"] is True

    def test_breach_retained_when_limit_equals_spend(self):
        """Validates: Requirements 7.4 — edge case: limit == spend"""
        _seed_project(
            self.projects_table, "proj-breach-equal",
            budgetBreached=True,
            budgetLimit=50,
        )
        # Current spend is $100, new limit is $100 → breach should remain
        self.mock_ce = _make_mock_ce_client(spend_amount=100.0)
        self.budget_mod.ce_client = self.mock_ce

        self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-breach-equal",
            budget_limit=100.0,
            budget_type="MONTHLY",
        )

        item = self.projects_table.get_item(
            Key={"PK": "PROJECT#proj-breach-equal", "SK": "METADATA"}
        )["Item"]
        assert item["budgetBreached"] is True

    # -- Default budget type ------------------------------------------------

    def test_default_budget_type_is_monthly(self):
        """Validates: Requirements 8.6"""
        _seed_project(self.projects_table, "proj-default-type")

        result = self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-default-type",
            budget_limit=100.0,
            # budget_type not specified — should default to MONTHLY
        )

        assert result["budgetType"] == "MONTHLY"

        # Verify DynamoDB record has MONTHLY
        item = self.projects_table.get_item(
            Key={"PK": "PROJECT#proj-default-type", "SK": "METADATA"}
        )["Item"]
        assert item["budgetType"] == "MONTHLY"

    # -- Validation: invalid budget type ------------------------------------

    def test_invalid_budget_type_raises_validation_error(self):
        """Validates: Requirements 8.1"""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-bad-type")

        with pytest.raises(ValidationError) as exc_info:
            self.budget_mod.set_budget(
                projects_table_name=PROJECTS_TABLE_NAME,
                budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
                project_id="proj-bad-type",
                budget_limit=100.0,
                budget_type="WEEKLY",
            )
        assert "budgetType" in str(exc_info.value)

    def test_empty_budget_type_raises_validation_error(self):
        """Validates: Requirements 8.1"""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-empty-type")

        with pytest.raises(ValidationError):
            self.budget_mod.set_budget(
                projects_table_name=PROJECTS_TABLE_NAME,
                budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
                project_id="proj-empty-type",
                budget_limit=100.0,
                budget_type="",
            )

    # -- Validation: zero or negative budget limit --------------------------

    def test_zero_budget_limit_raises_validation_error(self):
        """Validates: Requirements 8.7"""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-zero-limit")

        with pytest.raises(ValidationError) as exc_info:
            self.budget_mod.set_budget(
                projects_table_name=PROJECTS_TABLE_NAME,
                budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
                project_id="proj-zero-limit",
                budget_limit=0,
                budget_type="MONTHLY",
            )
        assert "positive" in str(exc_info.value).lower() or "budgetLimit" in str(exc_info.value)

    def test_negative_budget_limit_raises_validation_error(self):
        """Validates: Requirements 8.7"""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-neg-limit")

        with pytest.raises(ValidationError) as exc_info:
            self.budget_mod.set_budget(
                projects_table_name=PROJECTS_TABLE_NAME,
                budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
                project_id="proj-neg-limit",
                budget_limit=-50.0,
                budget_type="MONTHLY",
            )
        assert "positive" in str(exc_info.value).lower() or "budgetLimit" in str(exc_info.value)

    # -- Budget type stored in DynamoDB -------------------------------------

    def test_budget_type_stored_in_dynamodb(self):
        """Validates: Requirements 8.4"""
        _seed_project(self.projects_table, "proj-store-type")

        self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-store-type",
            budget_limit=200.0,
            budget_type="TOTAL",
        )

        item = self.projects_table.get_item(
            Key={"PK": "PROJECT#proj-store-type", "SK": "METADATA"}
        )["Item"]
        assert item["budgetType"] == "TOTAL"
        assert item["budgetLimit"] == 200

    # -- Budget update (existing budget) ------------------------------------

    def test_existing_budget_is_updated_not_created(self):
        """When a budget already exists, it should be updated rather than created."""
        # Set up mock to indicate budget exists
        self.mock_budgets = _make_mock_budgets_client(budget_exists=True)
        self.budget_mod.budgets_client = self.mock_budgets

        _seed_project(self.projects_table, "proj-update")

        self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-update",
            budget_limit=300.0,
            budget_type="MONTHLY",
        )

        # update_budget should have been called, not create_budget
        self.mock_budgets.update_budget.assert_called_once()
        self.mock_budgets.create_budget.assert_not_called()

    # -- Cost Explorer failure does not break set_budget --------------------

    def test_ce_failure_does_not_clear_breach(self):
        """When Cost Explorer is unavailable, budgetBreached should not be modified."""
        _seed_project(
            self.projects_table, "proj-ce-fail",
            budgetBreached=True,
        )
        self.mock_ce = _make_mock_ce_client_failing()
        self.budget_mod.ce_client = self.mock_ce

        # Should not raise — CE failure is handled gracefully
        self.budget_mod.set_budget(
            projects_table_name=PROJECTS_TABLE_NAME,
            budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
            project_id="proj-ce-fail",
            budget_limit=1000.0,
            budget_type="MONTHLY",
        )

        # budgetBreached should remain True since CE couldn't confirm spend
        item = self.projects_table.get_item(
            Key={"PK": "PROJECT#proj-ce-fail", "SK": "METADATA"}
        )["Item"]
        assert item["budgetBreached"] is True

    # -- Project not found --------------------------------------------------

    def test_nonexistent_project_raises_not_found(self):
        """set_budget should raise NotFoundError for a missing project."""
        from errors import NotFoundError

        with pytest.raises(NotFoundError):
            self.budget_mod.set_budget(
                projects_table_name=PROJECTS_TABLE_NAME,
                budget_sns_topic_arn="arn:aws:sns:us-east-1:123456789012:topic",
                project_id="proj-ghost",
                budget_limit=100.0,
                budget_type="MONTHLY",
            )
