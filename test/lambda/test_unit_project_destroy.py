"""Unit tests for the project destroy step handlers.

Covers:
- validate_and_check_clusters succeeds with no active clusters, fails with active clusters
- clear_infrastructure clears all infrastructure fields
- archive_project transitions to ARCHIVED
- handle_destroy_failure transitions back to ACTIVE and stores error message

Requirements: 3.1, 3.4, 3.5

The destroy module calls CodeBuild which is not fully supported by moto,
so we mock that client at the module level while using moto for DynamoDB.
"""

from unittest.mock import MagicMock

import pytest
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    create_projects_table,
    create_clusters_table,
    _load_module_from,
    _PROJECT_MGMT_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id, status="DESTROYING", **overrides):
    """Insert a project record for destroy tests."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "vpcId": "vpc-abc123",
        "efsFileSystemId": "fs-def456",
        "s3BucketName": "my-bucket-789",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "budgetType": "MONTHLY",
        "cdkStackName": f"HpcProject-{project_id}",
        "currentStep": 0,
        "totalSteps": 0,
        "stepDescription": "",
        "errorMessage": "",
        "createdAt": "2024-06-15T10:00:00+00:00",
        "updatedAt": "2024-06-15T10:00:00+00:00",
        "statusChangedAt": "2024-06-15T10:00:00+00:00",
    }
    item.update(overrides)
    projects_table.put_item(Item=item)


def _seed_cluster(clusters_table, project_id, cluster_name, status="ACTIVE"):
    """Insert a cluster record under a project."""
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": status,
    })


def _get_project(projects_table, project_id):
    """Retrieve a project record from DynamoDB."""
    response = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"}
    )
    return response.get("Item")


# ---------------------------------------------------------------------------
# Test class — Project Destroy Step Handlers
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestProjectDestroyStepHandlers:
    """Validates: Requirements 3.1, 3.4, 3.5"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set up moto DynamoDB and reload destroy modules with mocked AWS clients."""
        with mock_aws():
            self.projects_table = create_projects_table()
            self.clusters_table = create_clusters_table()

            # Load dependency modules first
            _load_module_from(_PROJECT_MGMT_DIR, "errors")
            _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")

            # Load the destroy module
            self.destroy_mod = _load_module_from(_PROJECT_MGMT_DIR, "project_destroy")

            # Mock CodeBuild client (not supported by moto)
            self.mock_codebuild = MagicMock()
            self.destroy_mod.codebuild_client = self.mock_codebuild

            yield

    # ===================================================================
    # validate_and_check_clusters — success (no active clusters)
    # ===================================================================

    def test_validate_succeeds_destroying_no_clusters(self):
        """Validates: Requirement 3.1 — DESTROYING project with no clusters passes."""
        _seed_project(self.projects_table, "proj-ok", status="DESTROYING")

        result = self.destroy_mod.validate_and_check_clusters({"projectId": "proj-ok"})

        assert result["projectId"] == "proj-ok"

    def test_validate_succeeds_destroying_only_terminated_clusters(self):
        """Validates: Requirement 3.1 — terminated/deleted clusters are ignored."""
        _seed_project(self.projects_table, "proj-term", status="DESTROYING")
        _seed_cluster(self.clusters_table, "proj-term", "cluster-a", status="TERMINATED")
        _seed_cluster(self.clusters_table, "proj-term", "cluster-b", status="DELETE_COMPLETE")

        result = self.destroy_mod.validate_and_check_clusters({"projectId": "proj-term"})

        assert result["projectId"] == "proj-term"

    # ===================================================================
    # validate_and_check_clusters — failure (active clusters exist)
    # ===================================================================

    def test_validate_fails_with_active_cluster(self):
        """Validates: Requirement 3.1 — active cluster blocks destruction."""
        from errors import ConflictError

        _seed_project(self.projects_table, "proj-active-cl", status="DESTROYING")
        _seed_cluster(self.clusters_table, "proj-active-cl", "my-cluster", status="ACTIVE")

        with pytest.raises(ConflictError) as exc_info:
            self.destroy_mod.validate_and_check_clusters({"projectId": "proj-active-cl"})
        assert "active clusters" in str(exc_info.value).lower()

    def test_validate_fails_with_creating_cluster(self):
        """Validates: Requirement 3.1 — CREATING cluster also blocks destruction."""
        from errors import ConflictError

        _seed_project(self.projects_table, "proj-creating-cl", status="DESTROYING")
        _seed_cluster(self.clusters_table, "proj-creating-cl", "new-cluster", status="CREATING")

        with pytest.raises(ConflictError) as exc_info:
            self.destroy_mod.validate_and_check_clusters({"projectId": "proj-creating-cl"})
        assert "active clusters" in str(exc_info.value).lower()

    def test_validate_fails_with_mixed_clusters(self):
        """Validates: Requirement 3.1 — mix of active and terminated clusters still fails."""
        from errors import ConflictError

        _seed_project(self.projects_table, "proj-mixed", status="DESTROYING")
        _seed_cluster(self.clusters_table, "proj-mixed", "dead-cluster", status="TERMINATED")
        _seed_cluster(self.clusters_table, "proj-mixed", "live-cluster", status="ACTIVE")

        with pytest.raises(ConflictError):
            self.destroy_mod.validate_and_check_clusters({"projectId": "proj-mixed"})

    # ===================================================================
    # validate_and_check_clusters — wrong project status
    # ===================================================================

    def test_validate_fails_for_active_project(self):
        """Validates: Requirement 3.1 — ACTIVE project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-wrong-status", status="ACTIVE")

        with pytest.raises(ValidationError) as exc_info:
            self.destroy_mod.validate_and_check_clusters({"projectId": "proj-wrong-status"})
        assert "ACTIVE" in str(exc_info.value)

    def test_validate_fails_for_missing_project_id(self):
        """Validates: Requirement 3.1 — missing projectId raises ValidationError."""
        from errors import ValidationError

        with pytest.raises(ValidationError):
            self.destroy_mod.validate_and_check_clusters({})

    # ===================================================================
    # clear_infrastructure — clears all infrastructure fields
    # ===================================================================

    def test_clear_infrastructure_clears_all_fields(self):
        """Validates: Requirement 3.4 — infrastructure IDs are cleared."""
        _seed_project(
            self.projects_table, "proj-clear", status="DESTROYING",
            vpcId="vpc-111", efsFileSystemId="fs-222",
            s3BucketName="bucket-333", cdkStackName="HpcProject-proj-clear",
        )

        result = self.destroy_mod.clear_infrastructure({"projectId": "proj-clear"})

        assert result["projectId"] == "proj-clear"

        item = _get_project(self.projects_table, "proj-clear")
        assert item["vpcId"] == ""
        assert item["efsFileSystemId"] == ""
        assert item["s3BucketName"] == ""
        assert item["cdkStackName"] == ""

    def test_clear_infrastructure_updates_progress_to_step_4(self):
        """Validates: Requirement 3.4 — step 4 progress is written."""
        _seed_project(self.projects_table, "proj-clear-prog", status="DESTROYING")

        self.destroy_mod.clear_infrastructure({"projectId": "proj-clear-prog"})

        item = _get_project(self.projects_table, "proj-clear-prog")
        assert item["currentStep"] == 4
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Clearing infrastructure records"

    # ===================================================================
    # archive_project — transitions to ARCHIVED
    # ===================================================================

    def test_archive_project_transitions_to_archived(self):
        """Validates: Requirement 3.4 — project transitions to ARCHIVED."""
        _seed_project(self.projects_table, "proj-archive", status="DESTROYING")

        result = self.destroy_mod.archive_project({"projectId": "proj-archive"})

        assert result["status"] == "ARCHIVED"

        item = _get_project(self.projects_table, "proj-archive")
        assert item["status"] == "ARCHIVED"

    def test_archive_project_updates_progress_to_step_5(self):
        """Validates: Requirement 3.4 — step 5 progress is written."""
        _seed_project(self.projects_table, "proj-archive-prog", status="DESTROYING")

        self.destroy_mod.archive_project({"projectId": "proj-archive-prog"})

        item = _get_project(self.projects_table, "proj-archive-prog")
        assert item["currentStep"] == 5
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Archiving project"

    # ===================================================================
    # handle_destroy_failure — transitions back to ACTIVE
    # ===================================================================

    def test_handle_destroy_failure_transitions_to_active(self):
        """Validates: Requirement 3.5 — failure transitions project back to ACTIVE."""
        _seed_project(self.projects_table, "proj-fail", status="DESTROYING")

        event = {
            "projectId": "proj-fail",
            "error": {"Cause": "CDK destroy timed out"},
        }

        result = self.destroy_mod.handle_destroy_failure(event)

        assert result["status"] == "ACTIVE"
        assert result["errorMessage"] == "CDK destroy timed out"

        item = _get_project(self.projects_table, "proj-fail")
        assert item["status"] == "ACTIVE"
        assert item["errorMessage"] == "CDK destroy timed out"

    def test_handle_destroy_failure_uses_error_message_field(self):
        """Validates: Requirement 3.5 — fallback to errorMessage when error.Cause missing."""
        _seed_project(self.projects_table, "proj-fail-msg", status="DESTROYING")

        event = {
            "projectId": "proj-fail-msg",
            "errorMessage": "Build failed with status FAILED",
        }

        result = self.destroy_mod.handle_destroy_failure(event)

        assert result["status"] == "ACTIVE"
        assert result["errorMessage"] == "Build failed with status FAILED"

        item = _get_project(self.projects_table, "proj-fail-msg")
        assert item["status"] == "ACTIVE"
        assert item["errorMessage"] == "Build failed with status FAILED"

    def test_handle_destroy_failure_defaults_to_unknown_error(self):
        """Validates: Requirement 3.5 — defaults to 'Unknown error' when no error info."""
        _seed_project(self.projects_table, "proj-fail-unknown", status="DESTROYING")

        event = {"projectId": "proj-fail-unknown"}

        result = self.destroy_mod.handle_destroy_failure(event)

        assert result["errorMessage"] == "Unknown error"

    def test_handle_destroy_failure_survives_missing_project_id(self):
        """Validates: Requirement 3.5 — gracefully handles missing projectId."""
        event = {"error": {"Cause": "Something broke"}}

        # Should not raise — failure handler is best-effort
        result = self.destroy_mod.handle_destroy_failure(event)

        assert result["status"] == "ACTIVE"
        assert result["errorMessage"] == "Something broke"

    # ===================================================================
    # Progress tracking for validate step
    # ===================================================================

    def test_validate_updates_progress_to_step_1(self):
        """Validates: Requirements 3.7, 3.8 — step 1 progress written."""
        _seed_project(self.projects_table, "proj-prog-1", status="DESTROYING")

        self.destroy_mod.validate_and_check_clusters({"projectId": "proj-prog-1"})

        item = _get_project(self.projects_table, "proj-prog-1")
        assert item["currentStep"] == 1
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Validating project state"
