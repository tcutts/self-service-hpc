"""Unit tests for foundation timestamp in the GET /projects response.

Verifies that the ``_handle_list_projects`` endpoint returns the
``foundationStackTimestamp`` field alongside the ``projects`` array,
both when the ``PK=PLATFORM, SK=FOUNDATION_TIMESTAMP`` record exists
in the Projects table and when it is missing.

Requirements: 8.2
"""

import json

import pytest

from conftest import (
    build_admin_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id, status="ACTIVE"):
    """Insert a minimal project record into the Projects table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_foundation_timestamp(projects_table, timestamp):
    """Insert the PLATFORM/FOUNDATION_TIMESTAMP record."""
    projects_table.put_item(Item={
        "PK": "PLATFORM",
        "SK": "FOUNDATION_TIMESTAMP",
        "timestamp": timestamp,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFoundationTimestamp:
    """Tests for foundationStackTimestamp in GET /projects response."""

    def test_list_projects_includes_foundation_timestamp(self, project_mgmt_env):
        """Response includes foundationStackTimestamp when the record exists."""
        projects_table = project_mgmt_env["projects_table"]
        handler_mod = project_mgmt_env["modules"][0]

        # Seed a project and the foundation timestamp record
        _seed_project(projects_table, "ts-proj-1")
        ts_value = "2025-01-15T10:30:00.000Z"
        _seed_foundation_timestamp(projects_table, ts_value)

        event = build_admin_event("GET", "/projects")
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "projects" in body
        assert "foundationStackTimestamp" in body
        assert body["foundationStackTimestamp"] == ts_value

        # Clean up seeded data so other tests in this class aren't affected
        projects_table.delete_item(Key={"PK": "PROJECT#ts-proj-1", "SK": "METADATA"})
        projects_table.delete_item(Key={"PK": "PLATFORM", "SK": "FOUNDATION_TIMESTAMP"})

    def test_list_projects_missing_timestamp_returns_null(self, project_mgmt_env):
        """Response returns null for foundationStackTimestamp when record is absent."""
        projects_table = project_mgmt_env["projects_table"]
        handler_mod = project_mgmt_env["modules"][0]

        # Ensure no foundation timestamp record exists
        projects_table.delete_item(Key={"PK": "PLATFORM", "SK": "FOUNDATION_TIMESTAMP"})

        event = build_admin_event("GET", "/projects")
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "projects" in body
        assert "foundationStackTimestamp" in body
        assert body["foundationStackTimestamp"] is None
