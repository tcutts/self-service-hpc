"""Unit tests for deregister_cluster_name in cluster_names.py.

**Validates: Requirements 2.4, 2.5, 2.6**

Tests:
- Successful deletion returns True
- Item-not-found (ConditionalCheckFailedException) returns False gracefully
- Unexpected ClientError propagates to the caller
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# cluster_operations must come FIRST so its errors.py (which has ConflictError)
# is found before template_management's errors.py.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

# Add cluster_operations first so its errors.py is found
sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)

# If errors was already imported from template_management, clear it so
# cluster_operations/errors.py is picked up instead.
_cached_errors = sys.modules.get("errors")
if _cached_errors is not None:
    _errors_file = getattr(_cached_errors, "__file__", "") or ""
    if "cluster_operations" not in _errors_file:
        del sys.modules["errors"]

# Also clear cluster_names if it was cached with the wrong errors module
if "cluster_names" in sys.modules:
    del sys.modules["cluster_names"]

from cluster_names import deregister_cluster_name  # noqa: E402


TABLE_NAME = "ClusterNameRegistry"


class TestDeregisterClusterNameSuccess:
    """Validates: Requirements 2.4"""

    def test_returns_true_when_item_deleted(self):
        """delete_item succeeds (item existed) — should return True."""
        mock_table = MagicMock()
        mock_table.delete_item.return_value = {}

        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        with patch("cluster_names.dynamodb", mock_dynamodb):
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

        with patch("cluster_names.dynamodb", mock_dynamodb):
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

        with patch("cluster_names.dynamodb", mock_dynamodb):
            with pytest.raises(ClientError) as exc_info:
                deregister_cluster_name(TABLE_NAME, "my-cluster")

        assert exc_info.value.response["Error"]["Code"] == "InternalServerError"
