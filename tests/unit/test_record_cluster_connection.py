"""Unit tests for record_cluster connection details in cluster_creation.py.

Validates that record_cluster stores loginNodeInstanceId in DynamoDB and
includes the SSM command in the lifecycle notification when appropriate.

Requirements: 2.1, 2.2, 2.3, 6.1, 6.2
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

# Add the cluster_operations module to the path
_CLUSTER_OPS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "cluster_operations",
)
sys.path.insert(0, _CLUSTER_OPS_DIR)

# Add the shared module path (imported by cluster_creation)
_SHARED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "shared",
)
sys.path.insert(0, _SHARED_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_event(**overrides) -> dict:
    """Build a minimal event payload for record_cluster."""
    event = {
        "projectId": "proj-123",
        "clusterName": "test-cluster",
        "templateId": "tpl-001",
        "pcsClusterId": "pcs-abc",
        "pcsClusterArn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-abc",
        "loginNodeGroupId": "lng-001",
        "computeNodeGroupId": "cng-001",
        "queueId": "q-001",
        "fsxFilesystemId": "fs-001",
        "loginNodeIp": "54.123.45.67",
        "loginNodeInstanceId": "i-0abc123def456789a",
        "sshPort": 22,
        "dcvPort": 8443,
        "createdBy": "user@example.com",
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordClusterConnectionDetails:
    """Validates: Requirements 2.1, 2.2, 2.3, 6.1, 6.2"""

    @patch("cluster_creation._publish_lifecycle_notification")
    @patch("cluster_creation._lookup_user_email", return_value="user@example.com")
    @patch("cluster_creation._update_step_progress")
    @patch("cluster_creation.dynamodb")
    def test_put_item_includes_login_node_instance_id(
        self, mock_dynamodb, mock_progress, mock_email, mock_notify
    ):
        """Event with loginNodeInstanceId → DynamoDB put_item includes the field.

        Validates: Requirement 2.1
        """
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        from cluster_creation import record_cluster

        event = _base_event(loginNodeInstanceId="i-0abc123def456789a")
        record_cluster(event)

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["loginNodeInstanceId"] == "i-0abc123def456789a"
        assert item["loginNodeIp"] == "54.123.45.67"

    @patch("cluster_creation._publish_lifecycle_notification")
    @patch("cluster_creation._lookup_user_email", return_value="user@example.com")
    @patch("cluster_creation._update_step_progress")
    @patch("cluster_creation.dynamodb")
    def test_empty_login_node_instance_id_stores_empty_string(
        self, mock_dynamodb, mock_progress, mock_email, mock_notify
    ):
        """Event with empty loginNodeInstanceId → empty string stored.

        Validates: Requirement 2.3
        """
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        from cluster_creation import record_cluster

        event = _base_event(loginNodeInstanceId="")
        record_cluster(event)

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["loginNodeInstanceId"] == ""

    @patch("cluster_creation._publish_lifecycle_notification")
    @patch("cluster_creation._lookup_user_email", return_value="user@example.com")
    @patch("cluster_creation._update_step_progress")
    @patch("cluster_creation.dynamodb")
    def test_notification_contains_ssh_dcv_and_ssm_when_all_present(
        self, mock_dynamodb, mock_progress, mock_email, mock_notify
    ):
        """Non-empty loginNodeInstanceId and loginNodeIp → notification has SSH, DCV, and SSM.

        Validates: Requirements 6.1, 6.2
        """
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        from cluster_creation import record_cluster

        event = _base_event(
            loginNodeIp="54.123.45.67",
            loginNodeInstanceId="i-0abc123def456789a",
            sshPort=22,
            dcvPort=8443,
        )
        record_cluster(event)

        mock_notify.assert_called_once()
        message = mock_notify.call_args[1]["message"]

        assert "ssh -p 22 <username>@54.123.45.67" in message
        assert "https://54.123.45.67:8443" in message
        assert "aws ssm start-session --target i-0abc123def456789a" in message

    @patch("cluster_creation._publish_lifecycle_notification")
    @patch("cluster_creation._lookup_user_email", return_value="user@example.com")
    @patch("cluster_creation._update_step_progress")
    @patch("cluster_creation.dynamodb")
    def test_notification_omits_ssm_when_instance_id_empty(
        self, mock_dynamodb, mock_progress, mock_email, mock_notify
    ):
        """Empty loginNodeInstanceId → notification omits SSM command.

        Validates: Requirement 6.1
        """
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        from cluster_creation import record_cluster

        event = _base_event(
            loginNodeIp="54.123.45.67",
            loginNodeInstanceId="",
        )
        record_cluster(event)

        mock_notify.assert_called_once()
        message = mock_notify.call_args[1]["message"]

        # SSH and DCV should still be present
        assert "ssh -p 22 <username>@54.123.45.67" in message
        assert "https://54.123.45.67:8443" in message
        # SSM should NOT be present
        assert "aws ssm start-session" not in message
