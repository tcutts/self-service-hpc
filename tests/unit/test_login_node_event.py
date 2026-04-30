"""Unit tests for login_node_event Lambda.

Validates that the handler correctly processes EC2 Instance State-change
Notification events, filters by PCS login node group tags, and updates
DynamoDB cluster records with new instance details.
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

def _state_change_event(
    instance_id: str = "i-0abc123def456789a",
    state: str = "running",
) -> dict:
    """Build a minimal EC2 Instance State-change Notification event."""
    return {
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "detail": {
            "instance-id": instance_id,
            "state": state,
        },
    }


def _active_cluster(
    project_id: str = "proj-1",
    cluster_name: str = "cluster-1",
    login_node_group_id: str = "lng-001",
    compute_node_group_id: str = "cng-001",
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
        "computeNodeGroupId": compute_node_group_id,
        "loginNodeInstanceId": instance_id,
        "loginNodeIp": ip,
        "status": "ACTIVE",
    }


def _describe_tags_response(tag_value: str | None = None) -> dict:
    """Build a mock EC2 describe_tags response."""
    if tag_value is None:
        return {"Tags": []}
    return {
        "Tags": [
            {
                "Key": "aws:pcs:compute-node-group-id",
                "ResourceId": "i-0abc123def456789a",
                "ResourceType": "instance",
                "Value": tag_value,
            },
        ],
    }


def _ec2_response(instance_id: str, public_ip: str = "") -> dict:
    """Build a mock EC2 describe_instances response."""
    instance: dict = {"InstanceId": instance_id}
    if public_ip:
        instance["PublicIpAddress"] = public_ip
    return {
        "Reservations": [{"Instances": [instance]}],
    }


def _client_error(
    code: str = "ServiceException",
    operation: str = "TestOperation",
) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "fail"}},
        operation,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLoginNodeEvent:
    """Tests for the login_node_event.handler function."""

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_no_pcs_tag_skipped(self, mock_dynamodb, mock_ec2):
        """Instance with no PCS tag is skipped, no DynamoDB call."""
        mock_ec2.describe_tags.return_value = _describe_tags_response(None)

        from login_node_event import handler

        result = handler(_state_change_event(), None)

        assert result["action"] == "skipped"
        assert "no PCS" in result["reason"]
        mock_dynamodb.Table.return_value.scan.assert_not_called()
        mock_dynamodb.Table.return_value.update_item.assert_not_called()

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_no_matching_active_cluster_skipped(
        self, mock_dynamodb, mock_ec2,
    ):
        """Valid PCS tag but no matching ACTIVE cluster is skipped."""
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            "lng-unknown",
        )
        mock_table = MagicMock()
        # No cluster matches loginNodeGroupId
        mock_table.scan.return_value = {"Items": []}
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(), None)

        assert result["action"] == "skipped"
        assert "no matching" in result["reason"]
        mock_table.update_item.assert_not_called()

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_compute_node_group_only_skipped(
        self, mock_dynamodb, mock_ec2,
    ):
        """PCS tag matches only computeNodeGroupId, not loginNodeGroupId."""
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            "cng-001",
        )
        mock_table = MagicMock()
        # First scan: no match by loginNodeGroupId
        # Second scan (_is_compute_node_group_only): match by computeNodeGroupId
        cluster = _active_cluster()
        mock_table.scan.side_effect = [
            {"Items": []},
            {"Items": [cluster]},
        ]
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(), None)

        assert result["action"] == "skipped"
        assert "compute node group" in result["reason"]
        mock_table.update_item.assert_not_called()

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_valid_match_updates_dynamodb(self, mock_dynamodb, mock_ec2):
        """Valid match updates DynamoDB with new instance ID and IP."""
        instance_id = "i-0abc123def456789a"
        new_ip = "5.6.7.8"

        mock_ec2.describe_tags.return_value = _describe_tags_response(
            "lng-001",
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id, new_ip,
        )

        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(instance_id), None)

        assert result["action"] == "updated"
        assert result["clusters_updated"] == 1
        mock_table.update_item.assert_called_once_with(
            Key={
                "PK": "PROJECT#proj-1",
                "SK": "CLUSTER#cluster-1",
            },
            UpdateExpression=(
                "SET loginNodeInstanceId = :iid, loginNodeIp = :ip"
            ),
            ExpressionAttributeValues={
                ":iid": instance_id,
                ":ip": new_ip,
            },
        )

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_unchanged_instance_no_update(self, mock_dynamodb, mock_ec2):
        """When instance ID and IP are unchanged, no DynamoDB update."""
        instance_id = "i-old111"
        same_ip = "1.2.3.4"

        mock_ec2.describe_tags.return_value = _describe_tags_response(
            "lng-001",
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id, same_ip,
        )

        cluster = _active_cluster(instance_id=instance_id, ip=same_ip)
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(instance_id), None)

        assert result["action"] == "updated"
        assert result["clusters_updated"] == 0
        mock_table.update_item.assert_not_called()

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_describe_tags_failure(self, mock_dynamodb, mock_ec2):
        """EC2 DescribeTags failure returns error, no DynamoDB update."""
        mock_ec2.describe_tags.side_effect = _client_error(
            operation="DescribeTags",
        )

        from login_node_event import handler

        result = handler(_state_change_event(), None)

        assert result["action"] == "error"
        mock_dynamodb.Table.return_value.update_item.assert_not_called()

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_describe_instances_failure(self, mock_dynamodb, mock_ec2):
        """EC2 DescribeInstances failure returns error, no DynamoDB update."""
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            "lng-001",
        )
        mock_ec2.describe_instances.side_effect = _client_error(
            operation="DescribeInstances",
        )

        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(), None)

        assert result["action"] == "error"
        mock_table.update_item.assert_not_called()

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_dynamodb_update_failure(self, mock_dynamodb, mock_ec2):
        """DynamoDB update failure returns error response."""
        instance_id = "i-0abc123def456789a"

        mock_ec2.describe_tags.return_value = _describe_tags_response(
            "lng-001",
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id, "5.6.7.8",
        )

        cluster = _active_cluster()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster]}
        mock_table.update_item.side_effect = _client_error(
            operation="UpdateItem",
        )
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(instance_id), None)

        assert result["action"] == "error"
        assert len(result["errors"]) == 1

    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_multiple_clusters_all_updated(
        self, mock_dynamodb, mock_ec2,
    ):
        """Multiple clusters matching same loginNodeGroupId are all updated."""
        instance_id = "i-0abc123def456789a"
        new_ip = "9.9.9.9"
        shared_lng = "lng-shared"

        mock_ec2.describe_tags.return_value = _describe_tags_response(
            shared_lng,
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id, new_ip,
        )

        clusters = [
            _active_cluster(
                "proj-1", "c1", shared_lng, "cng-a", "i-old1", "1.1.1.1",
            ),
            _active_cluster(
                "proj-2", "c2", shared_lng, "cng-b", "i-old2", "2.2.2.2",
            ),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": clusters}
        mock_dynamodb.Table.return_value = mock_table

        from login_node_event import handler

        result = handler(_state_change_event(instance_id), None)

        assert result["action"] == "updated"
        assert result["clusters_updated"] == 2
        assert mock_table.update_item.call_count == 2
