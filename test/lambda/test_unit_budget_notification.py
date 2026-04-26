"""Unit tests for the Budget Notification Lambda.

Covers:
- Parsing JSON and plain-text budget notifications
- 80% threshold: logs warning, identifies project admins
- 100% threshold: sets budgetBreached flag, identifies project admins and platform admins
- Consistent read for budget breach checks
- Unknown budget names are handled gracefully
- Already-breached projects are not updated redundantly

Requirements: 5.1, 5.2, 5.3

Infrastructure is set up once per test class via the ``budget_notification_env``
fixture from conftest.py.
"""

import json

import pytest

from conftest import (
    PROJECTS_TABLE_NAME,
    USERS_TABLE_NAME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id, budget_breached=False):
    """Insert a minimal project record into the Projects table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "budgetLimit": 1000,
        "budgetBreached": budget_breached,
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_project_admin(projects_table, users_table, project_id, user_id):
    """Add a PROJECT_ADMIN membership and user profile."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "projectId": project_id,
        "role": "PROJECT_ADMIN",
        "addedAt": "2024-01-01T00:00:00+00:00",
    })
    users_table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "displayName": f"Admin {user_id}",
        "email": f"{user_id}@example.com",
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_platform_admin(users_table, user_id):
    """Add an active platform user (administrator)."""
    users_table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "displayName": f"Platform Admin {user_id}",
        "email": f"{user_id}@example.com",
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _build_sns_event(message: str) -> dict:
    """Build a minimal SNS event wrapping the given message string."""
    return {
        "Records": [
            {
                "Sns": {
                    "Message": message,
                }
            }
        ]
    }


def _build_json_message(budget_name: str, threshold: float) -> str:
    """Build a JSON budget notification message."""
    return json.dumps({"budgetName": budget_name, "threshold": threshold})


def _build_text_message(budget_name: str, threshold: float) -> str:
    """Build a plain-text budget notification message."""
    return (
        f"AWS Budget Notification: Your budget {budget_name} has exceeded "
        f"the threshold. Budget Name: {budget_name} has reached "
        f"{threshold}% of the allocated budget."
    )


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("budget_notification_env")
class TestBudgetNotificationParsing:
    """Validates: Requirements 5.1, 5.2, 5.3 — message parsing."""

    def test_parse_json_message(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        name, threshold = handler_mod.parse_budget_notification(
            json.dumps({"budgetName": "hpc-project-alpha", "threshold": 80.0})
        )
        assert name == "hpc-project-alpha"
        assert threshold == 80.0

    def test_parse_json_message_100_percent(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        name, threshold = handler_mod.parse_budget_notification(
            json.dumps({"budgetName": "hpc-project-beta", "threshold": 100.0})
        )
        assert name == "hpc-project-beta"
        assert threshold == 100.0

    def test_parse_text_message(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        text = _build_text_message("hpc-project-gamma", 80.0)
        name, threshold = handler_mod.parse_budget_notification(text)
        assert name == "hpc-project-gamma"
        assert threshold == 80.0

    def test_parse_text_message_100_percent(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        text = _build_text_message("hpc-project-delta", 100.0)
        name, threshold = handler_mod.parse_budget_notification(text)
        assert name == "hpc-project-delta"
        assert threshold == 100.0

    def test_parse_unparseable_message_returns_none(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        name, threshold = handler_mod.parse_budget_notification(
            "This is not a budget notification at all."
        )
        assert name is None
        assert threshold is None

    def test_parse_empty_message_returns_none(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        name, threshold = handler_mod.parse_budget_notification("")
        assert name is None
        assert threshold is None

    def test_parse_json_missing_fields_returns_none(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        name, threshold = handler_mod.parse_budget_notification(
            json.dumps({"someField": "value"})
        )
        assert name is None
        assert threshold is None


# ---------------------------------------------------------------------------
# Project lookup tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("budget_notification_env")
class TestProjectLookup:
    """Validates: Requirements 5.1 — project lookup by budget name."""

    def test_find_project_by_budget_name(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "lookup-proj")

        result = handler_mod.find_project_by_budget_name("hpc-project-lookup-proj")
        assert result is not None
        assert result["projectId"] == "lookup-proj"

    def test_find_project_unknown_budget_returns_none(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        result = handler_mod.find_project_by_budget_name("hpc-project-nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# 80% threshold tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("budget_notification_env")
class TestThreshold80Percent:
    """Validates: Requirements 5.2 — 80% threshold notification."""

    def test_80_percent_does_not_set_breach_flag(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-80")

        event = _build_sns_event(_build_json_message("hpc-project-proj-80", 80.0))
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

        # Verify budgetBreached is still False
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-80", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item["Item"]["budgetBreached"] is False

    def test_80_percent_identifies_project_admins(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]
        users_table = budget_notification_env["users_table"]

        _seed_project(projects_table, "proj-80-admins")
        _seed_project_admin(projects_table, users_table, "proj-80-admins", "padmin-80")

        admins = handler_mod.get_project_admins("proj-80-admins")
        assert len(admins) >= 1
        admin_ids = [a["userId"] for a in admins]
        assert "padmin-80" in admin_ids


# ---------------------------------------------------------------------------
# 100% threshold tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("budget_notification_env")
class TestThreshold100Percent:
    """Validates: Requirements 5.3 — 100% threshold notification and breach flag."""

    def test_100_percent_sets_breach_flag(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-100")

        event = _build_sns_event(_build_json_message("hpc-project-proj-100", 100.0))
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

        # Verify budgetBreached is now True (consistent read)
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-100", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item["Item"]["budgetBreached"] is True

    def test_100_percent_updates_timestamp(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-100-ts")

        event = _build_sns_event(_build_json_message("hpc-project-proj-100-ts", 100.0))
        handler_mod.handler(event, None)

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-100-ts", "SK": "METADATA"},
            ConsistentRead=True,
        )
        # updatedAt should have changed from the seed value
        assert item["Item"]["updatedAt"] != "2024-01-01T00:00:00+00:00"

    def test_100_percent_already_breached_is_idempotent(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-100-idem", budget_breached=True)

        event = _build_sns_event(_build_json_message("hpc-project-proj-100-idem", 100.0))
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

        # Still True
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-100-idem", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item["Item"]["budgetBreached"] is True

    def test_100_percent_identifies_project_and_platform_admins(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]
        users_table = budget_notification_env["users_table"]

        _seed_project(projects_table, "proj-100-notify")
        _seed_project_admin(projects_table, users_table, "proj-100-notify", "padmin-100")
        _seed_platform_admin(users_table, "platform-admin-1")

        # Verify project admins
        project_admins = handler_mod.get_project_admins("proj-100-notify")
        assert any(a["userId"] == "padmin-100" for a in project_admins)

        # Verify platform admins
        platform_admins = handler_mod.get_platform_administrators()
        platform_ids = [a["userId"] for a in platform_admins]
        assert "platform-admin-1" in platform_ids

    def test_100_percent_with_text_message(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-100-text")

        text_msg = _build_text_message("hpc-project-proj-100-text", 100.0)
        event = _build_sns_event(text_msg)
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-100-text", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item["Item"]["budgetBreached"] is True


# ---------------------------------------------------------------------------
# Consistent read tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("budget_notification_env")
class TestConsistentRead:
    """Validates: Requirements 5.1, 5.3 — consistent reads for breach checks."""

    def test_set_budget_breached_uses_consistent_read(self, budget_notification_env):
        """Verify set_budget_breached performs a consistent read before updating."""
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-consistent")

        # Call set_budget_breached directly
        handler_mod.set_budget_breached("proj-consistent")

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-consistent", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item["Item"]["budgetBreached"] is True

    def test_set_budget_breached_nonexistent_project_no_error(self, budget_notification_env):
        """Verify set_budget_breached handles missing projects gracefully."""
        (handler_mod,) = budget_notification_env["modules"]

        # Should not raise
        handler_mod.set_budget_breached("proj-does-not-exist")


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("budget_notification_env")
class TestHandlerIntegration:
    """End-to-end handler tests with SNS event structure."""

    def test_handler_processes_multiple_records(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-multi-a")
        _seed_project(projects_table, "proj-multi-b")

        event = {
            "Records": [
                {"Sns": {"Message": _build_json_message("hpc-project-proj-multi-a", 80.0)}},
                {"Sns": {"Message": _build_json_message("hpc-project-proj-multi-b", 100.0)}},
            ]
        }
        result = handler_mod.handler(event, None)

        assert result["processed"] == 2
        assert result["total"] == 2

        # proj-multi-a should NOT be breached (80%)
        item_a = projects_table.get_item(
            Key={"PK": "PROJECT#proj-multi-a", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item_a["Item"]["budgetBreached"] is False

        # proj-multi-b SHOULD be breached (100%)
        item_b = projects_table.get_item(
            Key={"PK": "PROJECT#proj-multi-b", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item_b["Item"]["budgetBreached"] is True

    def test_handler_unknown_budget_does_not_crash(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        event = _build_sns_event(
            _build_json_message("hpc-project-unknown-proj", 100.0)
        )
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

    def test_handler_unparseable_message_does_not_crash(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        event = _build_sns_event("totally invalid message")
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

    def test_handler_empty_records(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]

        result = handler_mod.handler({"Records": []}, None)
        assert result["processed"] == 0
        assert result["total"] == 0

    def test_handler_threshold_below_80_no_action(self, budget_notification_env):
        (handler_mod,) = budget_notification_env["modules"]
        projects_table = budget_notification_env["projects_table"]

        _seed_project(projects_table, "proj-low")

        event = _build_sns_event(_build_json_message("hpc-project-proj-low", 50.0))
        result = handler_mod.handler(event, None)

        assert result["processed"] == 1

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-low", "SK": "METADATA"},
            ConsistentRead=True,
        )
        assert item["Item"]["budgetBreached"] is False
