"""Property-based tests for PCS scheduler log delivery configuration.

[PBT: Property 1] Log group creation correctness — for any valid projectId
and clusterName, configure_scheduler_log_delivery creates the correct log
group with 30-day retention and Project tag.

Feature: pcs-scheduler-log-delivery, Property 1: Log group creation correctness
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "posix_provisioning")
load_lambda_module("cluster_operations", "tagging")
cluster_creation = load_lambda_module("cluster_operations", "cluster_creation")
cluster_destruction = load_lambda_module("cluster_operations", "cluster_destruction")

configure_scheduler_log_delivery = cluster_creation.configure_scheduler_log_delivery
cleanup_scheduler_log_delivery = cluster_destruction.cleanup_scheduler_log_delivery

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------
project_id_strategy = st.from_regex(r"[a-z][a-z0-9-]{2,20}", fullmatch=True)
cluster_name_strategy = st.from_regex(r"[a-z][a-z0-9-]{2,20}", fullmatch=True)


def _build_mock_logs_client(log_group_name: str) -> MagicMock:
    """Create a mock CloudWatch Logs client with standard responses."""
    mock_logs = MagicMock()
    mock_logs.create_log_group.return_value = {}
    mock_logs.put_retention_policy.return_value = {}
    mock_logs.tag_log_group.return_value = {}
    mock_logs.describe_log_groups.return_value = {
        "logGroups": [
            {
                "logGroupName": log_group_name,
                "arn": f"arn:aws:logs:us-east-1:123456789012:log-group:{log_group_name}:*",
            }
        ]
    }
    mock_logs.put_delivery_source.return_value = {}
    mock_logs.put_delivery_destination.return_value = {
        "deliveryDestination": {
            "arn": (
                "arn:aws:logs:us-east-1:123456789012"
                ":delivery-destination:mock-destination"
            ),
            "name": "mock-destination",
        },
    }
    mock_logs.create_delivery.return_value = {
        "delivery": {"id": "delivery-xxx"}
    }
    mock_logs.describe_deliveries.return_value = {"deliveries": []}
    mock_logs.delete_delivery.return_value = {}
    mock_logs.delete_delivery_destination.return_value = {}
    mock_logs.delete_delivery_source.return_value = {}
    return mock_logs


def _build_event(project_id: str, cluster_name: str) -> dict:
    """Build a standard event dict for configure_scheduler_log_delivery."""
    return {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": "pcs_test123",
        "pcsClusterArn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs_test123",
    }


# ===================================================================
# [PBT: Property 1] Log group creation correctness
# Feature: pcs-scheduler-log-delivery, Property 1: Log group creation correctness
# ===================================================================

class TestLogGroupCreationCorrectness:
    """[PBT: Property 1] For any valid projectId and clusterName,
    configure_scheduler_log_delivery creates a CloudWatch Log Group
    at the correct path with 30-day retention and a Project tag.

    **Validates: Requirements 1.1, 1.2, 1.4**
    """

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_log_group_created_with_correct_name_retention_and_tag(
        self, project_id: str, cluster_name: str
    ):
        """CreateLogGroup is called with the correct name pattern,
        PutRetentionPolicy with 30 days, and TagLogGroup with
        {"Project": projectId}.

        **Validates: Requirements 1.1, 1.2, 1.4**
        """
        expected_log_group = (
            f"/hpc-platform/clusters/{project_id}"
            f"/scheduler-logs/{cluster_name}"
        )
        mock_logs = _build_mock_logs_client(expected_log_group)
        event = _build_event(project_id, cluster_name)

        with patch.object(cluster_creation, "logs_client", mock_logs):
            configure_scheduler_log_delivery(event)

        # 1. CreateLogGroup called once with the correct name
        mock_logs.create_log_group.assert_called_once_with(
            logGroupName=expected_log_group,
        )

        # 2. PutRetentionPolicy called once with 30 days
        mock_logs.put_retention_policy.assert_called_once_with(
            logGroupName=expected_log_group,
            retentionInDays=30,
        )

        # 3. TagLogGroup called once with Project tag
        mock_logs.tag_log_group.assert_called_once_with(
            logGroupName=expected_log_group,
            tags={"Project": project_id},
        )


# ===================================================================
# [PBT: Property 2] Delivery configuration completeness and correctness
# Feature: pcs-scheduler-log-delivery, Property 2: Delivery configuration completeness and correctness
# ===================================================================

_EXPECTED_LOG_TYPES = [
    {"logType": "PCS_SCHEDULER_LOGS", "suffix": "scheduler-logs"},
    {"logType": "PCS_SCHEDULER_AUDIT_LOGS", "suffix": "scheduler-audit-logs"},
    {"logType": "PCS_JOBCOMP_LOGS", "suffix": "jobcomp-logs"},
]


class TestDeliveryConfigurationCompleteness:
    """[PBT: Property 2] For any valid cluster details (clusterName,
    projectId, pcsClusterArn), configure_scheduler_log_delivery calls
    PutDeliverySource, PutDeliveryDestination, and CreateDelivery
    exactly once for each of the three PCS log types with correct
    naming patterns and resource ARNs.

    **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6**
    """

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_delivery_configuration_completeness_and_correctness(
        self, project_id: str, cluster_name: str
    ):
        """PutDeliverySource, PutDeliveryDestination, and CreateDelivery
        are each called exactly 3 times with correct arguments for all
        three PCS log types.

        **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6**
        """
        expected_log_group = (
            f"/hpc-platform/clusters/{project_id}"
            f"/scheduler-logs/{cluster_name}"
        )
        pcs_cluster_arn = (
            f"arn:aws:pcs:us-east-1:123456789012:cluster/pcs_test123"
        )
        mock_logs = _build_mock_logs_client(expected_log_group)
        event = _build_event(project_id, cluster_name)

        with patch.object(cluster_creation, "logs_client", mock_logs):
            configure_scheduler_log_delivery(event)

        log_group_arn = (
            f"arn:aws:logs:us-east-1:123456789012"
            f":log-group:{expected_log_group}:*"
        )

        # --- 1. PutDeliverySource called exactly 3 times ---
        assert mock_logs.put_delivery_source.call_count == 3, (
            f"Expected 3 PutDeliverySource calls, "
            f"got {mock_logs.put_delivery_source.call_count}"
        )

        # Verify each PutDeliverySource call has correct arguments
        source_calls = mock_logs.put_delivery_source.call_args_list
        for i, expected in enumerate(_EXPECTED_LOG_TYPES):
            call_kwargs = source_calls[i][1] if source_calls[i][1] else {}
            # If called with positional args, fall back to kwargs from call
            if not call_kwargs:
                call_kwargs = dict(
                    zip(["name", "resourceArn", "logType"], source_calls[i][0])
                )

            expected_source_name = f"{cluster_name}-{expected['suffix']}"
            assert call_kwargs["name"] == expected_source_name, (
                f"PutDeliverySource call {i}: expected name "
                f"'{expected_source_name}', got '{call_kwargs['name']}'"
            )
            assert call_kwargs["resourceArn"] == pcs_cluster_arn, (
                f"PutDeliverySource call {i}: expected resourceArn "
                f"'{pcs_cluster_arn}', got '{call_kwargs['resourceArn']}'"
            )
            assert call_kwargs["logType"] == expected["logType"], (
                f"PutDeliverySource call {i}: expected logType "
                f"'{expected['logType']}', got '{call_kwargs['logType']}'"
            )

        # --- 2. PutDeliveryDestination called exactly 3 times ---
        assert mock_logs.put_delivery_destination.call_count == 3, (
            f"Expected 3 PutDeliveryDestination calls, "
            f"got {mock_logs.put_delivery_destination.call_count}"
        )

        # Verify each PutDeliveryDestination call has correct arguments
        dest_calls = mock_logs.put_delivery_destination.call_args_list
        for i, expected in enumerate(_EXPECTED_LOG_TYPES):
            call_kwargs = dest_calls[i][1] if dest_calls[i][1] else {}
            if not call_kwargs:
                call_kwargs = dict(
                    zip(
                        ["name", "outputFormat", "deliveryDestinationConfiguration"],
                        dest_calls[i][0],
                    )
                )

            expected_dest_name = (
                f"{project_id}-{cluster_name}-{expected['suffix']}"
            )
            assert call_kwargs["name"] == expected_dest_name, (
                f"PutDeliveryDestination call {i}: expected name "
                f"'{expected_dest_name}', got '{call_kwargs['name']}'"
            )
            dest_config = call_kwargs["deliveryDestinationConfiguration"]
            assert dest_config["destinationResourceArn"] == log_group_arn, (
                f"PutDeliveryDestination call {i}: expected "
                f"destinationResourceArn '{log_group_arn}', "
                f"got '{dest_config['destinationResourceArn']}'"
            )

        # --- 3. CreateDelivery called exactly 3 times ---
        assert mock_logs.create_delivery.call_count == 3, (
            f"Expected 3 CreateDelivery calls, "
            f"got {mock_logs.create_delivery.call_count}"
        )

        # Verify each CreateDelivery links the correct source to destination
        delivery_calls = mock_logs.create_delivery.call_args_list
        expected_dest_arn = (
            "arn:aws:logs:us-east-1:123456789012"
            ":delivery-destination:mock-destination"
        )
        for i, expected in enumerate(_EXPECTED_LOG_TYPES):
            call_kwargs = delivery_calls[i][1] if delivery_calls[i][1] else {}
            if not call_kwargs:
                call_kwargs = dict(
                    zip(
                        ["deliverySourceName", "deliveryDestinationArn"],
                        delivery_calls[i][0],
                    )
                )

            expected_source_name = f"{cluster_name}-{expected['suffix']}"
            assert call_kwargs["deliverySourceName"] == expected_source_name, (
                f"CreateDelivery call {i}: expected deliverySourceName "
                f"'{expected_source_name}', "
                f"got '{call_kwargs['deliverySourceName']}'"
            )
            assert call_kwargs["deliveryDestinationArn"] == expected_dest_arn, (
                f"CreateDelivery call {i}: expected deliveryDestinationArn "
                f"'{expected_dest_arn}', "
                f"got '{call_kwargs['deliveryDestinationArn']}'"
            )


# ===================================================================
# [PBT: Property 4] Successful configuration logging
# Feature: pcs-scheduler-log-delivery, Property 4: Successful configuration logging
# ===================================================================


class _LogCapture(logging.Handler):
    """Lightweight log handler that captures LogRecords for assertion."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)


class TestSuccessfulConfigurationLogging:
    """[PBT: Property 4] For any successful delivery configuration,
    configure_scheduler_log_delivery emits an INFO log entry for each
    log type containing the log type name, delivery source name, and
    delivery ID, plus a summary INFO entry with the cluster name and
    the count of deliveries configured (3).

    **Validates: Requirements 6.1, 6.4**
    """

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_info_logs_emitted_for_each_log_type_and_summary(
        self, project_id: str, cluster_name: str
    ):
        """Each of the 3 log types produces an INFO entry containing
        the log type name, source name, and delivery ID.  A final
        summary INFO entry contains the cluster name and count 3.

        **Validates: Requirements 6.1, 6.4**
        """
        expected_log_group = (
            f"/hpc-platform/clusters/{project_id}"
            f"/scheduler-logs/{cluster_name}"
        )
        mock_logs = _build_mock_logs_client(expected_log_group)
        event = _build_event(project_id, cluster_name)

        handler = _LogCapture()
        handler.setLevel(logging.INFO)
        cluster_creation.logger.addHandler(handler)
        try:
            with patch.object(cluster_creation, "logs_client", mock_logs):
                configure_scheduler_log_delivery(event)
        finally:
            cluster_creation.logger.removeHandler(handler)

        info_messages = [
            r.getMessage()
            for r in handler.records
            if r.levelno == logging.INFO
        ]

        # --- Per-log-type entries (Requirement 6.1) ---
        expected_suffixes = [
            ("PCS_SCHEDULER_LOGS", f"{cluster_name}-scheduler-logs"),
            ("PCS_SCHEDULER_AUDIT_LOGS", f"{cluster_name}-scheduler-audit-logs"),
            ("PCS_JOBCOMP_LOGS", f"{cluster_name}-jobcomp-logs"),
        ]

        for log_type, source_name in expected_suffixes:
            matching = [
                m for m in info_messages
                if log_type in m and source_name in m and "delivery" in m.lower()
            ]
            assert len(matching) >= 1, (
                f"Expected at least 1 INFO message containing log type "
                f"'{log_type}', source name '{source_name}', and a "
                f"delivery reference.  INFO messages: {info_messages}"
            )

        # --- Summary entry (Requirement 6.4) ---
        summary_matches = [
            m for m in info_messages
            if cluster_name in m and "3" in m
        ]
        assert len(summary_matches) >= 1, (
            f"Expected at least 1 summary INFO message containing "
            f"cluster name '{cluster_name}' and count '3'.  "
            f"INFO messages: {info_messages}"
        )


# ===================================================================
# [PBT: Property 3] Cleanup ordering and completeness
# Feature: pcs-scheduler-log-delivery, Property 3: Cleanup ordering and completeness
# ===================================================================

_LOG_SUFFIXES = ["scheduler-logs", "scheduler-audit-logs", "jobcomp-logs"]


def _build_mock_cleanup_logs_client(cluster_name: str) -> MagicMock:
    """Create a mock CloudWatch Logs client for cleanup tests.

    ``describe_deliveries`` returns three deliveries whose source names
    match the cluster's expected naming pattern.  All delete calls
    return empty dicts (success).
    """
    mock_logs = MagicMock()

    deliveries = [
        {"id": f"del-{i}", "deliverySourceName": f"{cluster_name}-{suffix}"}
        for i, suffix in enumerate(_LOG_SUFFIXES)
    ]
    mock_logs.describe_deliveries.return_value = {
        "deliveries": deliveries,
    }
    mock_logs.delete_delivery.return_value = {}
    mock_logs.delete_delivery_destination.return_value = {}
    mock_logs.delete_delivery_source.return_value = {}
    mock_logs.delete_log_group.return_value = {}
    return mock_logs


class TestCleanupOrderingAndCompleteness:
    """[PBT: Property 3] For any cluster with delivery resources,
    cleanup_scheduler_log_delivery deletes all deliveries before any
    destinations, all destinations before any sources, and the log
    group only after all sources are deleted.

    **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6**
    """

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_cleanup_ordering_and_completeness(
        self, project_id: str, cluster_name: str
    ):
        """All delete_delivery calls precede delete_delivery_destination
        calls, which precede delete_delivery_source calls, which precede
        delete_log_group.  Counts: 3 deliveries, 3 destinations,
        3 sources, 1 log group.

        **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6**
        """
        mock_logs = _build_mock_cleanup_logs_client(cluster_name)

        # Track call order via side effects
        call_order: list[tuple[str, dict]] = []
        mock_logs.delete_delivery.side_effect = (
            lambda **kwargs: call_order.append(("delete_delivery", kwargs))
        )
        mock_logs.delete_delivery_destination.side_effect = (
            lambda **kwargs: call_order.append(("delete_delivery_destination", kwargs))
        )
        mock_logs.delete_delivery_source.side_effect = (
            lambda **kwargs: call_order.append(("delete_delivery_source", kwargs))
        )
        mock_logs.delete_log_group.side_effect = (
            lambda **kwargs: call_order.append(("delete_log_group", kwargs))
        )

        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
        }

        with patch.object(cluster_destruction, "logs_client", mock_logs):
            cleanup_scheduler_log_delivery(event)

        # Extract operation names in order
        ops = [op for op, _ in call_order]

        # --- Completeness: correct number of each call type ---
        assert ops.count("delete_delivery") == 3, (
            f"Expected 3 delete_delivery calls, got {ops.count('delete_delivery')}"
        )
        assert ops.count("delete_delivery_destination") == 3, (
            f"Expected 3 delete_delivery_destination calls, "
            f"got {ops.count('delete_delivery_destination')}"
        )
        assert ops.count("delete_delivery_source") == 3, (
            f"Expected 3 delete_delivery_source calls, "
            f"got {ops.count('delete_delivery_source')}"
        )
        assert ops.count("delete_log_group") == 1, (
            f"Expected 1 delete_log_group call, "
            f"got {ops.count('delete_log_group')}"
        )

        # --- Ordering: all of type A before any of type B ---
        # Find the index ranges for each operation type
        delivery_indices = [i for i, op in enumerate(ops) if op == "delete_delivery"]
        dest_indices = [i for i, op in enumerate(ops) if op == "delete_delivery_destination"]
        source_indices = [i for i, op in enumerate(ops) if op == "delete_delivery_source"]
        log_group_indices = [i for i, op in enumerate(ops) if op == "delete_log_group"]

        # All delete_delivery calls before any delete_delivery_destination
        assert max(delivery_indices) < min(dest_indices), (
            f"delete_delivery calls must all precede "
            f"delete_delivery_destination calls. Order: {ops}"
        )

        # All delete_delivery_destination calls before any delete_delivery_source
        assert max(dest_indices) < min(source_indices), (
            f"delete_delivery_destination calls must all precede "
            f"delete_delivery_source calls. Order: {ops}"
        )

        # All delete_delivery_source calls before delete_log_group
        assert max(source_indices) < min(log_group_indices), (
            f"delete_delivery_source calls must all precede "
            f"delete_log_group. Order: {ops}"
        )
