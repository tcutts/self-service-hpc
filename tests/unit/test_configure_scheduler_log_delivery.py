"""Unit tests for configure_scheduler_log_delivery step handler.

Tests cover:
- Step registration in _STEP_DISPATCH (1.5)
- Missing required payload fields
- ResourceAlreadyExistsException on CreateLogGroup → continues
- ConflictException on PutDeliverySource → continues
- Unexpected ClientError on CreateDelivery → raises
- All three log types configured in a single invocation
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)

# Clear cached modules to ensure correct imports
for _mod in [
    "errors",
    "cluster_names",
    "cluster_creation",
]:
    if _mod in sys.modules:
        del sys.modules[_mod]

import cluster_creation  # noqa: E402
from cluster_creation import (  # noqa: E402
    _STEP_DISPATCH,
    configure_scheduler_log_delivery,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resource_already_exists_error():
    """Create a ResourceAlreadyExistsException ClientError."""
    return ClientError(
        {"Error": {"Code": "ResourceAlreadyExistsException", "Message": "already exists"}},
        "CreateLogGroup",
    )


def _conflict_error():
    """Create a ConflictException ClientError."""
    return ClientError(
        {"Error": {"Code": "ConflictException", "Message": "conflict"}},
        "PutDeliverySource",
    )


def _generic_client_error(code="InternalServerError"):
    """Create a generic ClientError."""
    return ClientError(
        {"Error": {"Code": code, "Message": "something went wrong"}},
        "SomeOperation",
    )


def _base_event(**overrides):
    """Build a minimal creation event with optional overrides."""
    event = {
        "projectId": "proj-abc",
        "clusterName": "my-cluster",
        "pcsClusterId": "pcs_12345",
        "pcsClusterArn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs_12345",
    }
    event.update(overrides)
    return event


_LOG_GROUP_NAME = "/hpc-platform/clusters/proj-abc/scheduler-logs/my-cluster"
_LOG_GROUP_ARN = (
    "arn:aws:logs:us-east-1:123456789012:log-group:"
    "/hpc-platform/clusters/proj-abc/scheduler-logs/my-cluster:*"
)


def _build_mock_logs_client(log_group_name=_LOG_GROUP_NAME):
    """Build a MagicMock logs client with happy-path responses."""
    mock_logs = MagicMock()
    mock_logs.create_log_group.return_value = {}
    mock_logs.put_retention_policy.return_value = {}
    mock_logs.tag_log_group.return_value = {}
    mock_logs.describe_log_groups.return_value = {
        "logGroups": [
            {
                "logGroupName": log_group_name,
                "arn": (
                    f"arn:aws:logs:us-east-1:123456789012"
                    f":log-group:{log_group_name}:*"
                ),
            }
        ],
    }
    mock_logs.put_delivery_source.return_value = {}
    mock_logs.put_delivery_destination.return_value = {}
    mock_logs.create_delivery.return_value = {
        "delivery": {"id": "delivery-xxx"},
    }
    return mock_logs


# ===================================================================
# Step Registration
# ===================================================================

class TestStepRegistration:
    """Test that configure_scheduler_log_delivery is in _STEP_DISPATCH."""

    def test_configure_scheduler_log_delivery_in_dispatch(self):
        assert "configure_scheduler_log_delivery" in _STEP_DISPATCH
        assert _STEP_DISPATCH["configure_scheduler_log_delivery"] is configure_scheduler_log_delivery


# ===================================================================
# Missing Required Fields
# ===================================================================

class TestMissingRequiredFields:
    """Test that missing required payload fields raise KeyError."""

    def test_missing_project_id(self):
        mock_logs = _build_mock_logs_client()
        event = _base_event()
        del event["projectId"]

        with patch.object(cluster_creation, "logs_client", mock_logs):
            with pytest.raises(KeyError, match="projectId"):
                configure_scheduler_log_delivery(event)

    def test_missing_cluster_name(self):
        mock_logs = _build_mock_logs_client()
        event = _base_event()
        del event["clusterName"]

        with patch.object(cluster_creation, "logs_client", mock_logs):
            with pytest.raises(KeyError, match="clusterName"):
                configure_scheduler_log_delivery(event)

    def test_missing_pcs_cluster_arn(self):
        mock_logs = _build_mock_logs_client()
        event = _base_event()
        del event["pcsClusterArn"]

        with patch.object(cluster_creation, "logs_client", mock_logs):
            with pytest.raises(KeyError, match="pcsClusterArn"):
                configure_scheduler_log_delivery(event)


# ===================================================================
# ResourceAlreadyExistsException on CreateLogGroup
# ===================================================================

class TestLogGroupAlreadyExists:
    """Test that ResourceAlreadyExistsException on CreateLogGroup is handled."""

    def test_continues_when_log_group_exists(self):
        mock_logs = _build_mock_logs_client()
        mock_logs.create_log_group.side_effect = _resource_already_exists_error()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            result = configure_scheduler_log_delivery(event)

        # Step should complete successfully
        assert "schedulerLogGroupName" in result
        assert result["schedulerLogGroupName"] == _LOG_GROUP_NAME
        assert len(result["schedulerDeliveryIds"]) == 3

        # Retention and tagging should still be called
        mock_logs.put_retention_policy.assert_called_once()
        mock_logs.tag_log_group.assert_called_once()


# ===================================================================
# ConflictException on PutDeliverySource
# ===================================================================

class TestDeliverySourceConflict:
    """Test that ConflictException on PutDeliverySource is handled."""

    def test_continues_on_conflict(self):
        mock_logs = _build_mock_logs_client()
        mock_logs.put_delivery_source.side_effect = _conflict_error()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            result = configure_scheduler_log_delivery(event)

        # Step should complete — deliveries still created
        assert len(result["schedulerDeliveryIds"]) == 3
        # PutDeliveryDestination and CreateDelivery still called
        assert mock_logs.put_delivery_destination.call_count == 3
        assert mock_logs.create_delivery.call_count == 3


# ===================================================================
# Unexpected ClientError on CreateDelivery
# ===================================================================

class TestUnexpectedError:
    """Test that unexpected ClientError on CreateDelivery propagates."""

    def test_raises_on_unexpected_error(self):
        mock_logs = _build_mock_logs_client()
        mock_logs.create_delivery.side_effect = _generic_client_error(
            "InternalServerError",
        )

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            with pytest.raises(ClientError) as exc_info:
                configure_scheduler_log_delivery(event)

        assert exc_info.value.response["Error"]["Code"] == "InternalServerError"


# ===================================================================
# All Three Log Types Configured
# ===================================================================

class TestAllLogTypesConfigured:
    """Test that all three PCS log types are configured in one call."""

    def test_three_deliveries_created(self):
        mock_logs = _build_mock_logs_client()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            result = configure_scheduler_log_delivery(event)

        # Verify 3 delivery sources, destinations, and deliveries
        assert mock_logs.put_delivery_source.call_count == 3
        assert mock_logs.put_delivery_destination.call_count == 3
        assert mock_logs.create_delivery.call_count == 3

        # Verify result contains 3 delivery IDs
        assert len(result["schedulerDeliveryIds"]) == 3
        assert all(
            did == "delivery-xxx"
            for did in result["schedulerDeliveryIds"]
        )

    def test_correct_source_names(self):
        mock_logs = _build_mock_logs_client()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            configure_scheduler_log_delivery(event)

        source_names = [
            call.kwargs.get("name", call[1].get("name") if len(call[1]) > 0 else None)
            for call in mock_logs.put_delivery_source.call_args_list
        ]
        expected = [
            "my-cluster-scheduler-logs",
            "my-cluster-scheduler-audit-logs",
            "my-cluster-jobcomp-logs",
        ]
        assert source_names == expected

    def test_correct_destination_names(self):
        mock_logs = _build_mock_logs_client()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            configure_scheduler_log_delivery(event)

        dest_names = [
            call.kwargs.get("name", call[1].get("name") if len(call[1]) > 0 else None)
            for call in mock_logs.put_delivery_destination.call_args_list
        ]
        expected = [
            "proj-abc-my-cluster-scheduler-logs",
            "proj-abc-my-cluster-scheduler-audit-logs",
            "proj-abc-my-cluster-jobcomp-logs",
        ]
        assert dest_names == expected

    def test_log_group_name_in_result(self):
        mock_logs = _build_mock_logs_client()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            result = configure_scheduler_log_delivery(event)

        assert result["schedulerLogGroupName"] == _LOG_GROUP_NAME

    def test_original_event_fields_preserved(self):
        mock_logs = _build_mock_logs_client()

        event = _base_event()
        with patch.object(cluster_creation, "logs_client", mock_logs):
            result = configure_scheduler_log_delivery(event)

        # Original event fields should be preserved in the result
        assert result["projectId"] == "proj-abc"
        assert result["clusterName"] == "my-cluster"
        assert result["pcsClusterId"] == "pcs_12345"
        assert result["pcsClusterArn"] == event["pcsClusterArn"]
