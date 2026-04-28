"""Unit tests for lambda/shared/authorization.py.

Validates all public functions of the shared authorization module with
various Cognito group claim formats, edge cases, and role combinations.

Requirements: 1.3, 2.1, 2.2, 2.3, 2.4
"""

import os
import sys

import pytest

# Load the shared module directly from its file path.
_SHARED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "shared",
)
sys.path.insert(0, _SHARED_DIR)

from authorization import (  # noqa: E402
    get_caller_identity,
    get_caller_groups,
    is_administrator,
    is_authenticated,
    is_project_admin,
    is_project_user,
    get_admin_project_ids,
    get_member_project_ids,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    username: str | None = "alice",
    sub: str | None = "sub-alice",
    groups=None,
) -> dict:
    """Build a minimal API Gateway proxy event with Cognito claims."""
    claims: dict = {}
    if username is not None:
        claims["cognito:username"] = username
    if sub is not None:
        claims["sub"] = sub
    if groups is not None:
        claims["cognito:groups"] = groups
    return {"requestContext": {"authorizer": {"claims": claims}}}


# ---------------------------------------------------------------------------
# Tests — get_caller_identity
# ---------------------------------------------------------------------------

class TestGetCallerIdentity:
    """Validates: Requirements 1.3"""

    def test_prefers_cognito_username(self):
        assert get_caller_identity(_event(username="bob", sub="sub-bob")) == "bob"

    def test_falls_back_to_sub(self):
        assert get_caller_identity(_event(username=None, sub="sub-123")) == "sub-123"

    def test_returns_unknown_when_no_claims(self):
        assert get_caller_identity({"requestContext": {}}) == "unknown"

    def test_returns_unknown_for_empty_event(self):
        assert get_caller_identity({}) == "unknown"

    def test_returns_unknown_when_both_missing(self):
        assert get_caller_identity(_event(username=None, sub=None)) == "unknown"


# ---------------------------------------------------------------------------
# Tests — get_caller_groups
# ---------------------------------------------------------------------------

class TestGetCallerGroups:
    """Validates: Requirements 1.3, 2.1"""

    def test_list_format(self):
        groups = ["Administrators", "ProjectAdmin-proj1"]
        assert get_caller_groups(_event(groups=groups)) == groups

    def test_comma_separated_string(self):
        result = get_caller_groups(_event(groups="Administrators,ProjectUser-proj1"))
        assert result == ["Administrators", "ProjectUser-proj1"]

    def test_bracket_wrapped_string(self):
        result = get_caller_groups(_event(groups="[Administrators, ProjectAdmin-proj2]"))
        assert result == ["Administrators", "ProjectAdmin-proj2"]

    def test_empty_string(self):
        assert get_caller_groups(_event(groups="")) == []

    def test_missing_claim(self):
        assert get_caller_groups(_event(groups=None)) == []

    def test_single_group_string(self):
        assert get_caller_groups(_event(groups="Administrators")) == ["Administrators"]

    def test_whitespace_handling(self):
        result = get_caller_groups(_event(groups=" Administrators , ProjectUser-x "))
        assert result == ["Administrators", "ProjectUser-x"]


# ---------------------------------------------------------------------------
# Tests — is_administrator
# ---------------------------------------------------------------------------

class TestIsAdministrator:
    """Validates: Requirements 2.1, 2.2"""

    def test_true_when_in_administrators_group(self):
        assert is_administrator(_event(groups=["Administrators"])) is True

    def test_false_when_not_in_administrators_group(self):
        assert is_administrator(_event(groups=["ProjectAdmin-proj1"])) is False

    def test_false_with_no_groups(self):
        assert is_administrator(_event(groups="")) is False

    def test_true_with_mixed_groups(self):
        assert is_administrator(_event(groups=["ProjectUser-x", "Administrators"])) is True


# ---------------------------------------------------------------------------
# Tests — is_authenticated
# ---------------------------------------------------------------------------

class TestIsAuthenticated:
    """Validates: Requirements 1.3"""

    def test_true_with_username(self):
        assert is_authenticated(_event(username="alice", sub=None)) is True

    def test_true_with_sub_only(self):
        assert is_authenticated(_event(username=None, sub="sub-123")) is True

    def test_false_with_neither(self):
        assert is_authenticated(_event(username=None, sub=None)) is False

    def test_false_with_empty_event(self):
        assert is_authenticated({}) is False


# ---------------------------------------------------------------------------
# Tests — is_project_admin
# ---------------------------------------------------------------------------

class TestIsProjectAdmin:
    """Validates: Requirements 2.1, 2.2, 2.3"""

    def test_true_with_project_admin_group(self):
        assert is_project_admin(_event(groups=["ProjectAdmin-proj1"]), "proj1") is True

    def test_true_for_platform_administrator(self):
        assert is_project_admin(_event(groups=["Administrators"]), "proj1") is True

    def test_false_for_project_user(self):
        assert is_project_admin(_event(groups=["ProjectUser-proj1"]), "proj1") is False

    def test_false_for_wrong_project(self):
        assert is_project_admin(_event(groups=["ProjectAdmin-proj2"]), "proj1") is False

    def test_false_with_no_groups(self):
        assert is_project_admin(_event(groups=""), "proj1") is False


# ---------------------------------------------------------------------------
# Tests — is_project_user
# ---------------------------------------------------------------------------

class TestIsProjectUser:
    """Validates: Requirements 2.1, 2.3, 2.4"""

    def test_true_with_project_user_group(self):
        assert is_project_user(_event(groups=["ProjectUser-proj1"]), "proj1") is True

    def test_true_with_project_admin_group(self):
        assert is_project_user(_event(groups=["ProjectAdmin-proj1"]), "proj1") is True

    def test_true_for_platform_administrator(self):
        assert is_project_user(_event(groups=["Administrators"]), "proj1") is True

    def test_false_for_wrong_project(self):
        assert is_project_user(_event(groups=["ProjectUser-proj2"]), "proj1") is False

    def test_false_with_no_groups(self):
        assert is_project_user(_event(groups=""), "proj1") is False


# ---------------------------------------------------------------------------
# Tests — get_admin_project_ids
# ---------------------------------------------------------------------------

class TestGetAdminProjectIds:
    """Validates: Requirements 2.1"""

    def test_extracts_multiple_project_ids(self):
        groups = ["ProjectAdmin-alpha", "ProjectAdmin-beta", "ProjectUser-gamma"]
        result = get_admin_project_ids(_event(groups=groups))
        assert sorted(result) == ["alpha", "beta"]

    def test_returns_empty_with_no_admin_groups(self):
        assert get_admin_project_ids(_event(groups=["ProjectUser-proj1"])) == []

    def test_returns_empty_with_no_groups(self):
        assert get_admin_project_ids(_event(groups="")) == []

    def test_ignores_administrators_group(self):
        result = get_admin_project_ids(_event(groups=["Administrators"]))
        assert result == []

    def test_handles_mixed_groups(self):
        groups = ["Administrators", "ProjectAdmin-proj1", "ProjectUser-proj2"]
        result = get_admin_project_ids(_event(groups=groups))
        assert result == ["proj1"]


# ---------------------------------------------------------------------------
# Tests — get_member_project_ids
# ---------------------------------------------------------------------------

class TestGetMemberProjectIds:
    """Validates: Requirements 2.1, 2.3"""

    def test_extracts_from_both_admin_and_user_groups(self):
        groups = ["ProjectAdmin-alpha", "ProjectUser-beta"]
        result = get_member_project_ids(_event(groups=groups))
        assert sorted(result) == ["alpha", "beta"]

    def test_deduplicates_when_both_admin_and_user(self):
        groups = ["ProjectAdmin-proj1", "ProjectUser-proj1"]
        result = get_member_project_ids(_event(groups=groups))
        assert result == ["proj1"]

    def test_returns_empty_with_no_project_groups(self):
        assert get_member_project_ids(_event(groups=["Administrators"])) == []

    def test_returns_empty_with_no_groups(self):
        assert get_member_project_ids(_event(groups="")) == []

    def test_handles_many_projects(self):
        groups = [
            "ProjectAdmin-a", "ProjectUser-b",
            "ProjectAdmin-c", "ProjectUser-a",
        ]
        result = get_member_project_ids(_event(groups=groups))
        assert sorted(result) == ["a", "b", "c"]
