"""Unit tests for login_node_refresh Lambda.

Validates that the handler correctly scans active clusters, detects
changed login node instances, and updates DynamoDB accordingly.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Add the cluster_operations module to the path
_CLUSTER_OPS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "cluster_operations",
)
sys.path.insert(0, _CLUSTER_OPS_DIR)

_SHARED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "shared",
)
sys.path.insert(0, _SHARED_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_cluster(
    project_id: str = "proj-1",
    cluster_name: str = "cluster-1",
    login_node_group_id: str = "lng-001",
    instance_id: str = "i-old111",
    ip: str = "1.2.3.4",
) -> dict:
    """Build a minimal active cluster record."""
    return {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "projectId": project_id,
        "clusterName": cluster_name,
        "loginNodeGroupId": login_node_group_id,
        "loginNodeInstanceId": instance_id,
        "loginNodeIp": ip,
        "status": "ACTIVE",
    }


def _ec2_response(instance_id: str, public_ip: str = "") -> dict:
    """Build a mock EC2 describe_instances response."""
    instance: dict = {"InstanceId": instance_id}
    if public_ip:
        instance["PublicIpAddress"] = public_ip
    return {
        "Reservations": [{"Instances": [instance]}],
    }


def _client_error(code: str = "ServiceException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "fail"}},
        "DescribeInstances",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLoginNodeRefresh:
    """Tests for the login_node_refresh.handler function."""

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_no_change_does_not_update(self, mock_dynamodb, mock_ec2):
        """When instance ID and IP haven't changed, no DynamoDB update occurs."""
        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        mock_ec2.describe_instances.return_value = _ec2_response("i-old111", "1.2.3.4")

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 1
        assert result["clusters_updated"] == 0
        mock_table.update_item.assert_not_called()

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_changed_instance_triggers_update(self, mock_dynamodb, mock_ec2):
        """When instance ID changes, DynamoDB is updated with new values."""
        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        mock_ec2.describe_instances.return_value = _ec2_response(
            "i-new222", "5.6.7.8"
        )

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 1
        assert result["clusters_updated"] == 1
        mock_table.update_item.assert_called_once_with(
            Key={"PK": "PROJECT#proj-1", "SK": "CLUSTER#cluster-1"},
            UpdateExpression="SET loginNodeInstanceId = :iid, loginNodeIp = :ip",
            ExpressionAttributeValues={
                ":iid": "i-new222",
                ":ip": "5.6.7.8",
            },
        )

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_changed_ip_only_triggers_update(self, mock_dynamodb, mock_ec2):
        """When only the IP changes (same instance, new IP), DynamoDB is updated."""
        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        mock_ec2.describe_instances.return_value = _ec2_response(
            "i-old111", "9.9.9.9"
        )

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 1
        assert result["clusters_updated"] == 1

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_no_running_instance_counts_unreachable(
        self, mock_dynamodb, mock_ec2
    ):
        """When EC2 returns no running instances, cluster is counted as unreachable."""
        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        mock_ec2.describe_instances.return_value = {"Reservations": []}

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 1
        assert result["clusters_unreachable"] == 1
        assert result["clusters_updated"] == 0
        mock_table.update_item.assert_not_called()

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_ec2_error_counts_as_error(self, mock_dynamodb, mock_ec2):
        """When EC2 raises a ClientError, it's counted as an error."""
        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        mock_ec2.describe_instances.side_effect = _client_error()

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 1
        assert result["errors"] == 1
        assert result["clusters_updated"] == 0

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_cluster_without_login_node_group_id_skipped(
        self, mock_dynamodb, mock_ec2
    ):
        """Clusters without loginNodeGroupId are skipped entirely."""
        cluster = _active_cluster()
        del cluster["loginNodeGroupId"]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 0
        mock_ec2.describe_instances.assert_not_called()

    @patch("login_node_refresh.ec2_client")
    @patch("login_node_refresh.dynamodb")
    def test_multiple_clusters_processed(self, mock_dynamodb, mock_ec2):
        """Multiple active clusters are all checked and updated as needed."""
        clusters = [
            _active_cluster("proj-1", "c1", "lng-1", "i-aaa", "1.1.1.1"),
            _active_cluster("proj-2", "c2", "lng-2", "i-bbb", "2.2.2.2"),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": clusters}
        mock_dynamodb.Table.return_value = mock_table

        # First cluster unchanged, second cluster has new instance
        mock_ec2.describe_instances.side_effect = [
            _ec2_response("i-aaa", "1.1.1.1"),
            _ec2_response("i-ccc", "3.3.3.3"),
        ]

        from login_node_refresh import handler

        result = handler({}, None)

        assert result["clusters_checked"] == 2
        assert result["clusters_updated"] == 1
