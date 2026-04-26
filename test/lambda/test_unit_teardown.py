"""Unit tests for the teardown workloads helper script.

Covers:
- DynamoDB scan logic for active clusters and projects
- Error handling (cluster destruction failure continues with remaining)
- Retry logic for CDK stack destroy
- DynamoDB record cleanup

Requirements: 20.5, 20.7

Infrastructure is set up once per test class via class-scoped mock_aws
fixtures, avoiding repeated DynamoDB table creation.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch, call

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from conftest import (
    AWS_REGION,
    CLUSTERS_TABLE_NAME,
    PROJECTS_TABLE_NAME,
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    create_clusters_table,
    create_projects_table,
    create_cluster_name_registry_table,
)

# ---------------------------------------------------------------------------
# Import the teardown module
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

import teardown_workloads  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_cluster(clusters_table, project_id, cluster_name, status="ACTIVE", **extra):
    """Insert a cluster record into the Clusters table."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": status,
        "pcsClusterId": extra.get("pcsClusterId", ""),
        "computeNodeGroupId": extra.get("computeNodeGroupId", ""),
        "loginNodeGroupId": extra.get("loginNodeGroupId", ""),
        "queueId": extra.get("queueId", ""),
        "fsxFilesystemId": extra.get("fsxFilesystemId", ""),
        "createdAt": "2024-01-01T00:00:00+00:00",
    }
    item.update(extra)
    clusters_table.put_item(Item=item)


def _seed_project(projects_table, project_id):
    """Insert a minimal project METADATA record into the Projects table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_member(projects_table, project_id, user_id):
    """Insert a membership record into the Projects table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "role": "PROJECT_USER",
        "addedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster_name(registry_table, cluster_name, project_id):
    """Insert a cluster name registry record."""
    registry_table.put_item(Item={
        "PK": f"CLUSTERNAME#{cluster_name}",
        "SK": "REGISTRY",
        "clusterName": cluster_name,
        "projectId": project_id,
        "registeredAt": "2024-01-01T00:00:00+00:00",
    })


def _make_client_error(code="InternalError", message="Something failed"):
    """Create a botocore ClientError for testing."""
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# Test: destroy_active_clusters — DynamoDB scan and PCS/FSx cleanup
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestDestroyActiveClusters:
    """Validates: Requirements 20.5, 20.7"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

            clusters_table = create_clusters_table()

            session = boto3.Session(region_name=AWS_REGION)

            yield {
                "clusters_table": clusters_table,
                "session": session,
            }

    def test_no_active_clusters_returns_empty(self, _env):
        """When no ACTIVE/CREATING clusters exist, returns empty failures list."""
        # Seed a DESTROYED cluster — should be ignored
        _seed_cluster(_env["clusters_table"], "proj-a", "old-cl", status="DESTROYED")

        with patch.object(teardown_workloads, "_destroy_cluster") as mock_destroy:
            failures = teardown_workloads.destroy_active_clusters(_env["session"])

        assert failures == []
        mock_destroy.assert_not_called()

    def test_scans_active_and_creating_clusters(self, _env):
        """Finds clusters in ACTIVE and CREATING status."""
        _seed_cluster(_env["clusters_table"], "proj-b", "active-cl", status="ACTIVE")
        _seed_cluster(_env["clusters_table"], "proj-b", "creating-cl", status="CREATING")
        _seed_cluster(_env["clusters_table"], "proj-b", "destroyed-cl", status="DESTROYED")

        with patch.object(teardown_workloads, "_destroy_cluster", return_value=None) as mock_destroy:
            failures = teardown_workloads.destroy_active_clusters(_env["session"])

        assert failures == []
        # Should have been called for active-cl and creating-cl, not destroyed-cl
        assert mock_destroy.call_count == 2

    def test_failure_in_one_cluster_continues_with_remaining(self, _env):
        """If one cluster fails to destroy, the script continues with the rest."""
        _seed_cluster(_env["clusters_table"], "proj-c", "fail-cl", status="ACTIVE",
                      pcsClusterId="pcs-fail")
        _seed_cluster(_env["clusters_table"], "proj-c", "ok-cl", status="ACTIVE",
                      pcsClusterId="pcs-ok")

        def side_effect(pcs_client, fsx_client, clusters_table, cluster):
            if cluster.get("clusterName") == "fail-cl":
                return "Failed to destroy fail-cl"
            return None

        with patch.object(teardown_workloads, "_destroy_cluster", side_effect=side_effect) as mock_destroy:
            failures = teardown_workloads.destroy_active_clusters(_env["session"])

        # At least 2 calls (fail-cl and ok-cl); may include clusters from prior tests
        assert mock_destroy.call_count >= 2
        # Exactly one failure from fail-cl
        assert len(failures) == 1
        assert "fail-cl" in failures[0]

    def test_destroy_cluster_calls_pcs_and_fsx(self, _env):
        """_destroy_cluster calls PCS and FSx APIs in the correct order."""
        cluster = {
            "projectId": "proj-d",
            "clusterName": "full-cl",
            "pcsClusterId": "pcs-123",
            "computeNodeGroupId": "cng-1",
            "loginNodeGroupId": "lng-1",
            "queueId": "q-1",
            "fsxFilesystemId": "fs-123",
        }

        mock_pcs = MagicMock()
        mock_fsx = MagicMock()
        clusters_table = _env["clusters_table"]

        # Seed the cluster so the DynamoDB update works
        _seed_cluster(clusters_table, "proj-d", "full-cl", status="ACTIVE",
                      pcsClusterId="pcs-123", computeNodeGroupId="cng-1",
                      loginNodeGroupId="lng-1", queueId="q-1",
                      fsxFilesystemId="fs-123")

        error = teardown_workloads._destroy_cluster(
            mock_pcs, mock_fsx, clusters_table, cluster,
        )

        assert error is None

        # PCS node groups deleted first
        mock_pcs.delete_compute_node_group.assert_any_call(
            clusterIdentifier="pcs-123", computeNodeGroupIdentifier="cng-1",
        )
        mock_pcs.delete_compute_node_group.assert_any_call(
            clusterIdentifier="pcs-123", computeNodeGroupIdentifier="lng-1",
        )
        # Then queue
        mock_pcs.delete_queue.assert_called_once_with(
            clusterIdentifier="pcs-123", queueIdentifier="q-1",
        )
        # Then cluster
        mock_pcs.delete_cluster.assert_called_once_with(clusterIdentifier="pcs-123")
        # Then FSx
        mock_fsx.delete_file_system.assert_called_once_with(FileSystemId="fs-123")

        # Verify DynamoDB status updated to DESTROYED
        item = clusters_table.get_item(
            Key={"PK": "PROJECT#proj-d", "SK": "CLUSTER#full-cl"}
        )
        assert item["Item"]["status"] == "DESTROYED"

    def test_destroy_cluster_pcs_error_continues(self, _env):
        """PCS errors are logged but don't stop FSx cleanup or DynamoDB update."""
        cluster = {
            "projectId": "proj-e",
            "clusterName": "pcs-err-cl",
            "pcsClusterId": "pcs-bad",
            "computeNodeGroupId": "cng-bad",
            "loginNodeGroupId": "",
            "queueId": "",
            "fsxFilesystemId": "fs-456",
        }

        mock_pcs = MagicMock()
        mock_pcs.delete_compute_node_group.side_effect = _make_client_error()
        mock_fsx = MagicMock()
        clusters_table = _env["clusters_table"]

        _seed_cluster(clusters_table, "proj-e", "pcs-err-cl", status="ACTIVE")

        error = teardown_workloads._destroy_cluster(
            mock_pcs, mock_fsx, clusters_table, cluster,
        )

        # Should return error string but still call FSx and update DynamoDB
        assert error is not None
        assert "cng-bad" in error
        mock_fsx.delete_file_system.assert_called_once_with(FileSystemId="fs-456")

        item = clusters_table.get_item(
            Key={"PK": "PROJECT#proj-e", "SK": "CLUSTER#pcs-err-cl"}
        )
        assert item["Item"]["status"] == "DESTROYED"

    def test_destroy_cluster_fsx_not_found_ignored(self, _env):
        """FSx FileSystemNotFound is treated as already gone, not an error."""
        cluster = {
            "projectId": "proj-f",
            "clusterName": "fsx-gone-cl",
            "pcsClusterId": "",
            "computeNodeGroupId": "",
            "loginNodeGroupId": "",
            "queueId": "",
            "fsxFilesystemId": "fs-gone",
        }

        mock_pcs = MagicMock()
        mock_fsx = MagicMock()
        mock_fsx.delete_file_system.side_effect = _make_client_error("FileSystemNotFound")
        clusters_table = _env["clusters_table"]

        _seed_cluster(clusters_table, "proj-f", "fsx-gone-cl", status="ACTIVE")

        error = teardown_workloads._destroy_cluster(
            mock_pcs, mock_fsx, clusters_table, cluster,
        )

        # FileSystemNotFound should not be treated as an error
        assert error is None

    def test_destroy_cluster_skips_empty_ids(self, _env):
        """When PCS/FSx IDs are empty, those API calls are skipped."""
        cluster = {
            "projectId": "proj-g",
            "clusterName": "empty-ids-cl",
            "pcsClusterId": "",
            "computeNodeGroupId": "",
            "loginNodeGroupId": "",
            "queueId": "",
            "fsxFilesystemId": "",
        }

        mock_pcs = MagicMock()
        mock_fsx = MagicMock()
        clusters_table = _env["clusters_table"]

        _seed_cluster(clusters_table, "proj-g", "empty-ids-cl", status="ACTIVE")

        error = teardown_workloads._destroy_cluster(
            mock_pcs, mock_fsx, clusters_table, cluster,
        )

        assert error is None
        mock_pcs.delete_compute_node_group.assert_not_called()
        mock_pcs.delete_queue.assert_not_called()
        mock_pcs.delete_cluster.assert_not_called()
        mock_fsx.delete_file_system.assert_not_called()


# ---------------------------------------------------------------------------
# Test: destroy_project_stacks — DynamoDB scan and CDK destroy with retry
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestDestroyProjectStacks:
    """Validates: Requirements 20.5, 20.7"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

            projects_table = create_projects_table()

            session = boto3.Session(region_name=AWS_REGION)

            yield {
                "projects_table": projects_table,
                "session": session,
            }

    def test_no_projects_returns_empty(self, _env):
        """When no project METADATA records exist, returns empty failures list."""
        # Seed a membership record (not METADATA) — should be ignored
        _seed_member(_env["projects_table"], "proj-x", "user-1")

        with patch.object(teardown_workloads, "_run_cdk_destroy") as mock_cdk:
            failures = teardown_workloads.destroy_project_stacks(_env["session"], "thecutts")

        assert failures == []
        mock_cdk.assert_not_called()

    def test_scans_project_metadata_records(self, _env):
        """Finds project METADATA records and calls CDK destroy for each."""
        _seed_project(_env["projects_table"], "proj-y")
        _seed_project(_env["projects_table"], "proj-z")

        with patch.object(teardown_workloads, "_run_cdk_destroy", return_value=None) as mock_cdk:
            failures = teardown_workloads.destroy_project_stacks(_env["session"], "thecutts")

        assert failures == []
        assert mock_cdk.call_count == 2

    def test_cdk_destroy_failure_continues_with_remaining(self, _env):
        """If CDK destroy fails for one project, continues with the rest."""
        _seed_project(_env["projects_table"], "proj-fail")
        _seed_project(_env["projects_table"], "proj-pass")

        def side_effect(project_id, profile):
            if project_id == "proj-fail":
                return "CDK stack 'HpcProject-proj-fail' destroy failed after 2 attempts."
            return None

        with patch.object(teardown_workloads, "_run_cdk_destroy", side_effect=side_effect):
            failures = teardown_workloads.destroy_project_stacks(_env["session"], "thecutts")

        assert len(failures) == 1
        assert "proj-fail" in failures[0]

    def test_skips_projects_without_project_id(self, _env):
        """Projects without a projectId field are skipped."""
        _env["projects_table"].put_item(Item={
            "PK": "PROJECT#no-id",
            "SK": "METADATA",
            "projectName": "No ID Project",
            "status": "ACTIVE",
        })

        with patch.object(teardown_workloads, "_run_cdk_destroy", return_value=None) as mock_cdk:
            # This will also pick up previously seeded projects, but the no-id one should be skipped
            teardown_workloads.destroy_project_stacks(_env["session"], "thecutts")

        # Verify _run_cdk_destroy was never called with empty project_id
        for c in mock_cdk.call_args_list:
            assert c[0][0] != ""


# ---------------------------------------------------------------------------
# Test: _run_cdk_destroy — retry logic
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestRunCdkDestroy:
    """Validates: Requirements 20.5, 20.7 — CDK destroy retry logic."""

    @patch("teardown_workloads.time.sleep")
    @patch("teardown_workloads.subprocess.run")
    def test_success_on_first_attempt(self, mock_run, mock_sleep):
        """CDK destroy succeeds on first attempt — no retry needed."""
        mock_run.return_value = MagicMock(returncode=0)

        result = teardown_workloads._run_cdk_destroy("proj-1", "thecutts")

        assert result is None
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("teardown_workloads.time.sleep")
    @patch("teardown_workloads.subprocess.run")
    def test_success_on_second_attempt(self, mock_run, mock_sleep):
        """CDK destroy fails first, succeeds on retry."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="Stack in UPDATE_ROLLBACK", stdout=""),
            MagicMock(returncode=0),
        ]

        result = teardown_workloads._run_cdk_destroy("proj-2", "thecutts")

        assert result is None
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(teardown_workloads.CDK_DESTROY_RETRY_DELAY_SECONDS)

    @patch("teardown_workloads.time.sleep")
    @patch("teardown_workloads.subprocess.run")
    def test_failure_after_two_attempts(self, mock_run, mock_sleep):
        """CDK destroy fails both attempts — returns error message."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="Error 1", stdout=""),
            MagicMock(returncode=1, stderr="Error 2", stdout=""),
        ]

        result = teardown_workloads._run_cdk_destroy("proj-3", "thecutts")

        assert result is not None
        assert "HpcProject-proj-3" in result
        assert "failed after 2 attempts" in result
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("teardown_workloads.time.sleep")
    @patch("teardown_workloads.subprocess.run")
    def test_timeout_triggers_retry(self, mock_run, mock_sleep):
        """subprocess.TimeoutExpired triggers a retry."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=["npx"], timeout=600),
            MagicMock(returncode=0),
        ]

        result = teardown_workloads._run_cdk_destroy("proj-4", "thecutts")

        assert result is None
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("teardown_workloads.time.sleep")
    @patch("teardown_workloads.subprocess.run")
    def test_os_error_triggers_retry(self, mock_run, mock_sleep):
        """OSError triggers a retry."""
        mock_run.side_effect = [
            OSError("npx not found"),
            MagicMock(returncode=0),
        ]

        result = teardown_workloads._run_cdk_destroy("proj-5", "thecutts")

        assert result is None
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("teardown_workloads.time.sleep")
    @patch("teardown_workloads.subprocess.run")
    def test_correct_command_constructed(self, mock_run, mock_sleep):
        """Verify the subprocess command includes the correct stack name and profile."""
        mock_run.return_value = MagicMock(returncode=0)

        teardown_workloads._run_cdk_destroy("my-project", "thecutts")

        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "npx", "cdk", "destroy", "HpcProject-my-project",
            "--force", "--profile", "thecutts",
        ]


# ---------------------------------------------------------------------------
# Test: cleanup_dynamodb_records — deletes all items from tables
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestCleanupDynamodbRecords:
    """Validates: Requirements 20.5"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            registry_table = create_cluster_name_registry_table()

            session = boto3.Session(region_name=AWS_REGION)

            yield {
                "clusters_table": clusters_table,
                "projects_table": projects_table,
                "registry_table": registry_table,
                "session": session,
            }

    def test_deletes_all_cluster_records(self, _env):
        """All items in the Clusters table are deleted."""
        _seed_cluster(_env["clusters_table"], "proj-1", "cl-1", status="ACTIVE")
        _seed_cluster(_env["clusters_table"], "proj-1", "cl-2", status="DESTROYED")

        failures = teardown_workloads.cleanup_dynamodb_records(_env["session"])

        assert failures == []

        # Verify Clusters table is empty
        items = _env["clusters_table"].scan()["Items"]
        assert len(items) == 0

    def test_deletes_all_project_and_member_records(self, _env):
        """All METADATA and MEMBER# records in the Projects table are deleted."""
        _seed_project(_env["projects_table"], "proj-2")
        _seed_member(_env["projects_table"], "proj-2", "user-a")
        _seed_member(_env["projects_table"], "proj-2", "user-b")

        failures = teardown_workloads.cleanup_dynamodb_records(_env["session"])

        assert failures == []

        items = _env["projects_table"].scan()["Items"]
        assert len(items) == 0

    def test_deletes_all_cluster_name_registry_records(self, _env):
        """All items in the ClusterNameRegistry table are deleted."""
        _seed_cluster_name(_env["registry_table"], "name-1", "proj-1")
        _seed_cluster_name(_env["registry_table"], "name-2", "proj-2")

        failures = teardown_workloads.cleanup_dynamodb_records(_env["session"])

        assert failures == []

        items = _env["registry_table"].scan()["Items"]
        assert len(items) == 0

    def test_empty_tables_no_errors(self, _env):
        """Cleanup on already-empty tables returns no failures."""
        failures = teardown_workloads.cleanup_dynamodb_records(_env["session"])
        assert failures == []
