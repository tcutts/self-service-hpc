"""Unit tests for cleanup_scheduler_log_delivery and its helpers.

Tests cover:
- All delivery resources exist → deleted in correct order
- Some delivery resources already deleted (ResourceNotFoundException) → step continues
- Log group does not exist → step continues without error
- DeleteDelivery raises unexpected error → error propagates
- Return value is original event
- cleanup_scheduler_log_delivery is the first step in consolidated_cleanup
"""

from unittest.mock import MagicMock, call, patch

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
load_lambda_module("cluster_operations", "cluster_names")
cluster_destruction = load_lambda_module("cluster_operations", "cluster_destruction")
cleanup_scheduler_log_delivery = cluster_destruction.cleanup_scheduler_log_delivery
consolidated_cleanup = cluster_destruction.consolidated_cleanup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resource_not_found_error():
    """Create a ResourceNotFoundException ClientError."""
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "DeleteResource",
    )


def _generic_client_error(code="InternalServerError"):
    """Create a generic ClientError."""
    return ClientError(
        {"Error": {"Code": code, "Message": "something went wrong"}},
        "SomeOperation",
    )


def _base_event(**overrides):
    """Build a minimal destruction event with optional overrides."""
    event = {
        "projectId": "proj-abc",
        "clusterName": "my-cluster",
    }
    event.update(overrides)
    return event


def _build_mock_logs_client(cluster_name):
    """Build a mock logs client with deliveries matching the cluster."""
    mock_logs = MagicMock()
    deliveries = [
        {"id": f"del-{i}", "deliverySourceName": f"{cluster_name}-{suffix}"}
        for i, suffix in enumerate(
            ["scheduler-logs", "scheduler-audit-logs", "jobcomp-logs"]
        )
    ]
    mock_logs.describe_deliveries.return_value = {"deliveries": deliveries}
    mock_logs.delete_delivery.return_value = {}
    mock_logs.delete_delivery_destination.return_value = {}
    mock_logs.delete_delivery_source.return_value = {}
    mock_logs.delete_log_group.return_value = {}
    return mock_logs


# ===================================================================
# Test: All resources exist → deleted in correct order
# ===================================================================

class TestCleanupAllResourcesExist:
    """All delivery resources exist and are deleted in the correct order."""

    def test_all_resources_deleted_correct_counts(self):
        """All API calls succeed → correct number of deletions."""
        mock_logs = _build_mock_logs_client("my-cluster")
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            result = cleanup_scheduler_log_delivery(event)

        assert mock_logs.delete_delivery.call_count == 3
        assert mock_logs.delete_delivery_destination.call_count == 3
        assert mock_logs.delete_delivery_source.call_count == 3
        assert mock_logs.delete_log_group.call_count == 1

    def test_deletion_order_deliveries_before_destinations(self):
        """Deliveries are deleted before destinations, sources, and log group."""
        mock_logs = _build_mock_logs_client("my-cluster")
        event = _base_event()

        call_order = []
        mock_logs.delete_delivery.side_effect = (
            lambda **kw: call_order.append("delete_delivery")
        )
        mock_logs.delete_delivery_destination.side_effect = (
            lambda **kw: call_order.append("delete_delivery_destination")
        )
        mock_logs.delete_delivery_source.side_effect = (
            lambda **kw: call_order.append("delete_delivery_source")
        )
        mock_logs.delete_log_group.side_effect = (
            lambda **kw: call_order.append("delete_log_group")
        )

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            cleanup_scheduler_log_delivery(event)

        # Verify ordering: all deliveries first, then destinations, then sources, then log group
        delivery_indices = [
            i for i, c in enumerate(call_order) if c == "delete_delivery"
        ]
        destination_indices = [
            i for i, c in enumerate(call_order)
            if c == "delete_delivery_destination"
        ]
        source_indices = [
            i for i, c in enumerate(call_order)
            if c == "delete_delivery_source"
        ]
        log_group_indices = [
            i for i, c in enumerate(call_order) if c == "delete_log_group"
        ]

        assert max(delivery_indices) < min(destination_indices)
        assert max(destination_indices) < min(source_indices)
        assert max(source_indices) < min(log_group_indices)


# ===================================================================
# Test: ResourceNotFoundException on delete_delivery → continues
# ===================================================================

class TestCleanupDeliveryAlreadyDeleted:
    """ResourceNotFoundException on delete_delivery → step continues."""

    def test_delivery_not_found_continues(self):
        """One delivery raises ResourceNotFoundException → still deletes rest."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_delivery.side_effect = [
            _resource_not_found_error(),
            {},
            {},
        ]
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            result = cleanup_scheduler_log_delivery(event)

        # All three deliveries attempted
        assert mock_logs.delete_delivery.call_count == 3
        # Destinations, sources, and log group still cleaned up
        assert mock_logs.delete_delivery_destination.call_count == 3
        assert mock_logs.delete_delivery_source.call_count == 3
        assert mock_logs.delete_log_group.call_count == 1


# ===================================================================
# Test: ResourceNotFoundException on delete_delivery_destination → continues
# ===================================================================

class TestCleanupDestinationAlreadyDeleted:
    """ResourceNotFoundException on delete_delivery_destination → continues."""

    def test_destination_not_found_continues(self):
        """Destination raises ResourceNotFoundException → still deletes sources and log group."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_delivery_destination.side_effect = [
            _resource_not_found_error(),
            {},
            {},
        ]
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            result = cleanup_scheduler_log_delivery(event)

        assert mock_logs.delete_delivery_destination.call_count == 3
        assert mock_logs.delete_delivery_source.call_count == 3
        assert mock_logs.delete_log_group.call_count == 1


# ===================================================================
# Test: Log group does not exist → continues without error
# ===================================================================

class TestCleanupLogGroupNotFound:
    """Log group does not exist → step continues without error."""

    def test_log_group_not_found_continues(self):
        """delete_log_group raises ResourceNotFoundException → no error."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_log_group.side_effect = _resource_not_found_error()
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            result = cleanup_scheduler_log_delivery(event)

        # Should complete without raising
        assert result == event
        mock_logs.delete_log_group.assert_called_once()


# ===================================================================
# Test: Unexpected error on delete_delivery → propagates
# ===================================================================

class TestCleanupUnexpectedErrorPropagates:
    """Unexpected ClientError on delete_delivery → error propagates."""

    def test_unexpected_error_on_delete_delivery_raises(self):
        """InternalServerError on delete_delivery → raises ClientError."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_delivery.side_effect = _generic_client_error(
            "InternalServerError"
        )
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            with pytest.raises(ClientError) as exc_info:
                cleanup_scheduler_log_delivery(event)

        assert (
            exc_info.value.response["Error"]["Code"] == "InternalServerError"
        )

    def test_unexpected_error_on_delete_destination_raises(self):
        """InternalServerError on delete_delivery_destination → raises."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_delivery_destination.side_effect = (
            _generic_client_error("InternalServerError")
        )
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            with pytest.raises(ClientError):
                cleanup_scheduler_log_delivery(event)

    def test_unexpected_error_on_delete_source_raises(self):
        """InternalServerError on delete_delivery_source → raises."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_delivery_source.side_effect = _generic_client_error(
            "InternalServerError"
        )
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            with pytest.raises(ClientError):
                cleanup_scheduler_log_delivery(event)

    def test_unexpected_error_on_delete_log_group_raises(self):
        """InternalServerError on delete_log_group → raises."""
        mock_logs = _build_mock_logs_client("my-cluster")
        mock_logs.delete_log_group.side_effect = _generic_client_error(
            "InternalServerError"
        )
        event = _base_event()

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            with pytest.raises(ClientError):
                cleanup_scheduler_log_delivery(event)


# ===================================================================
# Test: Return value is original event
# ===================================================================

class TestCleanupReturnValue:
    """cleanup_scheduler_log_delivery returns the original event unchanged."""

    def test_returns_original_event(self):
        """Return value is the same event dict passed in."""
        mock_logs = _build_mock_logs_client("my-cluster")
        event = _base_event(extraField="keep-me")

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            result = cleanup_scheduler_log_delivery(event)

        assert result is event
        assert result["extraField"] == "keep-me"
        assert result["projectId"] == "proj-abc"
        assert result["clusterName"] == "my-cluster"


# ===================================================================
# Test: cleanup_scheduler_log_delivery is first in consolidated_cleanup
# ===================================================================

class TestConsolidatedCleanupOrder:
    """cleanup_scheduler_log_delivery is the first step in consolidated_cleanup."""

    def test_cleanup_log_delivery_is_first_step(self):
        """Verify cleanup_scheduler_log_delivery runs first in consolidated_cleanup."""
        step_order = []

        def _make_tracker(name, original_fn=None):
            def tracker(evt):
                step_order.append(name)
                return evt
            return tracker

        event = _base_event(
            pcsClusterId="pcs_12345",
            computeNodeGroupId="cng-001",
            loginNodeGroupId="lng-001",
            queueId="q-001",
            fsxFilesystemId="fs-001",
        )

        with (
            patch.object(
                cluster_destruction,
                "cleanup_scheduler_log_delivery",
                side_effect=_make_tracker("cleanup_scheduler_log_delivery"),
            ),
            patch.object(
                cluster_destruction,
                "delete_iam_resources",
                side_effect=_make_tracker("delete_iam_resources"),
            ),
            patch.object(
                cluster_destruction,
                "delete_launch_templates",
                side_effect=_make_tracker("delete_launch_templates"),
            ),
            patch.object(
                cluster_destruction,
                "deregister_cluster_name_step",
                side_effect=_make_tracker("deregister_cluster_name_step"),
            ),
            patch.object(
                cluster_destruction,
                "record_cluster_destroyed",
                side_effect=_make_tracker("record_cluster_destroyed"),
            ),
        ):
            consolidated_cleanup(event)

        assert step_order[0] == "cleanup_scheduler_log_delivery"
        assert len(step_order) == 5
