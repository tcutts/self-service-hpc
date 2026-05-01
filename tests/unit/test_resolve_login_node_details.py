"""Unit tests for resolve_login_node_details in cluster_creation.py.

Validates that the function correctly queries EC2 (via PCS tags) for login
node instances, retrieves the public IP, and raises InternalError on failures.

Requirements: 1.1, 1.2, 1.4, 1.5
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(os.path.dirname(__file__), "..", "conftest.py"))
_tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_tc)
load_lambda_module = _tc.load_lambda_module
_ensure_shared_modules = _tc._ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
errors = load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "tagging")
load_lambda_module("cluster_operations", "posix_provisioning")
cluster_creation = load_lambda_module("cluster_operations", "cluster_creation")
InternalError = errors.InternalError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_event() -> dict:
    """Build a minimal event payload for resolve_login_node_details."""
    return {
        "projectId": "proj-123",
        "clusterName": "test-cluster",
        "pcsClusterId": "pcs-abc",
        "loginNodeGroupId": "lng-001",
    }


def _client_error(code: str = "ServiceException", message: str = "fail") -> ClientError:
    """Build a botocore ClientError for testing."""
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResolveLoginNodeDetails:
    """Validates: Requirements 1.1, 1.2, 1.4, 1.5"""

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "ec2_client")
    def test_happy_path_returns_instance_id_and_ip(
        self, mock_ec2, mock_progress
    ):
        """EC2 tag query returns one running instance with a public IP."""
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0abc123def456789a",
                            "PublicIpAddress": "54.123.45.67",
                        }
                    ]
                }
            ]
        }

        result = cluster_creation.resolve_login_node_details(_base_event())

        assert result["loginNodeInstanceId"] == "i-0abc123def456789a"
        assert result["loginNodeIp"] == "54.123.45.67"
        assert result["projectId"] == "proj-123"
        assert result["clusterName"] == "test-cluster"

        mock_ec2.describe_instances.assert_called_once_with(
            Filters=[
                {
                    "Name": "tag:aws:pcs:compute-node-group-id",
                    "Values": ["lng-001"],
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["running"],
                },
            ],
        )
        mock_progress.assert_called_once_with("proj-123", "test-cluster", 10)

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "ec2_client")
    def test_empty_reservations_raises_internal_error(
        self, mock_ec2, mock_progress
    ):
        """EC2 returns no matching instances → InternalError."""
        mock_ec2.describe_instances.return_value = {
            "Reservations": [],
        }

        with pytest.raises(InternalError, match="no running instances"):
            cluster_creation.resolve_login_node_details(_base_event())

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "ec2_client")
    def test_ec2_client_error_raises_internal_error(
        self, mock_ec2, mock_progress
    ):
        """EC2 describe_instances raises ClientError → InternalError."""
        mock_ec2.describe_instances.side_effect = _client_error(
            "InternalError", "Service unavailable"
        )

        with pytest.raises(InternalError, match="Failed to describe login node group instances"):
            cluster_creation.resolve_login_node_details(_base_event())

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "ec2_client")
    def test_no_public_ip_raises_internal_error(
        self, mock_ec2, mock_progress
    ):
        """EC2 instance has no PublicIpAddress → InternalError."""
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0abc123def456789a",
                        }
                    ]
                }
            ]
        }

        with pytest.raises(InternalError, match="no public IP address"):
            cluster_creation.resolve_login_node_details(_base_event())
