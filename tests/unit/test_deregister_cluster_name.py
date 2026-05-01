"""Unit tests for deregister_cluster_name in cluster_names.py.

**Validates: Requirements 2.4, 2.5, 2.6**

Tests:
- Successful deletion returns True
- Item-not-found (ConditionalCheckFailedException) returns False gracefully
- Unexpected ClientError propagates to the caller
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
load_lambda_module("cluster_operations", "errors")
cluster_names = load_lambda_module("cluster_operations", "cluster_names")
deregister_cluster_name = cluster_names.deregister_cluster_name


TABLE_NAME = "ClusterNameRegistry"


class TestDeregisterClusterNameSuccess:
    """Validates: Requirements 2.4"""

    def test_returns_true_when_item_deleted(self):
        """delete_item succeeds (item existed) — should return True."""
        mock_table = MagicMock()
        mock_table.delete_item.return_value = {}

        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        with patch.object(cluster_names, "dynamodb", mock_dynamodb):
            result = deregister_cluster_name(TABLE_NAME, "my-cluster")

        assert result is True
        mock_dynamodb.Table.assert_called_once_with(TABLE_NAME)
        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "CLUSTERNAME#my-cluster", "SK": "REGISTRY"},
            ConditionExpression="attribute_exists(PK)",
        )


class TestDeregisterClusterNameNotFound:
    """Validates: Requirements 2.5, 2.6"""

    def test_returns_false_when_item_not_found(self):
        """ConditionalCheckFailedException (item absent) — should return False."""
        mock_table = MagicMock()
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "DeleteItem",
        )

        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        with patch.object(cluster_names, "dynamodb", mock_dynamodb):
            result = deregister_cluster_name(TABLE_NAME, "nonexistent-cluster")

        assert result is False


class TestDeregisterClusterNameClientError:
    """Validates: Requirements 2.4"""

    def test_unexpected_client_error_propagates(self):
        """Non-condition errors (e.g. InternalServerError) should propagate."""
        mock_table = MagicMock()
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "boom"}},
            "DeleteItem",
        )

        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        with patch.object(cluster_names, "dynamodb", mock_dynamodb):
            with pytest.raises(ClientError) as exc_info:
                deregister_cluster_name(TABLE_NAME, "my-cluster")

        assert exc_info.value.response["Error"]["Code"] == "InternalServerError"
