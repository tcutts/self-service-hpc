"""Property-based tests for login_node_event Lambda.

[PBT: Property 1] Login-only filtering — update occurs iff tag matches
loginNodeGroupId of an ACTIVE cluster; never when tag matches only
computeNodeGroupId.

Feature: event-driven-node-relaunch, Property 1: Login-only filtering
"""

import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty, printable IDs without null bytes for node group IDs
_id_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        min_codepoint=48,
        max_codepoint=122,
    ),
    min_size=1,
    max_size=20,
)

# Strategy for a single cluster record with distinct login and compute
# node group IDs.
_cluster_record_strategy = st.fixed_dictionaries({
    "projectId": _id_strategy,
    "clusterName": _id_strategy,
    "loginNodeGroupId": _id_strategy,
    "computeNodeGroupId": _id_strategy,
    "loginNodeInstanceId": st.just("i-old000000000000000"),
    "loginNodeIp": st.just("1.2.3.4"),
    "status": st.just("ACTIVE"),
}).filter(
    # login and compute IDs must be distinct within a single cluster
    lambda c: c["loginNodeGroupId"] != c["computeNodeGroupId"]
)

# List of 0-5 cluster records
_clusters_strategy = st.lists(
    _cluster_record_strategy,
    min_size=0,
    max_size=5,
)


def _add_pk_sk(cluster: dict) -> dict:
    """Add DynamoDB key fields to a cluster record."""
    return {
        **cluster,
        "PK": f"PROJECT#{cluster['projectId']}",
        "SK": f"CLUSTER#{cluster['clusterName']}",
    }


def _state_change_event(instance_id: str = "i-0abc123def456789a") -> dict:
    """Build a minimal EC2 Instance State-change Notification event."""
    return {
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "detail": {
            "instance-id": instance_id,
            "state": "running",
        },
    }


def _describe_tags_response(tag_value: str) -> dict:
    """Build a mock EC2 describe_tags response."""
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


def _ec2_response(
    instance_id: str = "i-0abc123def456789a",
    public_ip: str = "9.9.9.9",
) -> dict:
    """Build a mock EC2 describe_instances response."""
    return {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": instance_id,
                        "PublicIpAddress": public_ip,
                    }
                ]
            }
        ],
    }


# ===================================================================
# [PBT: Property 1] Login-only filtering
# ===================================================================


class TestLoginOnlyFiltering:
    """[PBT: Property 1] Login-only filtering.

    For any EC2 instance with a ``aws:pcs:compute-node-group-id`` tag
    value, the Login Node Event Handler SHALL update DynamoDB only when
    that tag value matches an ACTIVE cluster's ``loginNodeGroupId``
    field, and SHALL never update DynamoDB when the tag value matches
    only a cluster's ``computeNodeGroupId`` field.

    **Validates: Requirements 2.2, 2.3, 5.1**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        clusters=_clusters_strategy,
        tag_value=_id_strategy,
    )
    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_update_iff_tag_matches_login_node_group(
        self,
        mock_dynamodb,
        mock_ec2,
        clusters,
        tag_value,
    ):
        """DynamoDB update_item is called iff the tag value matches at
        least one ACTIVE cluster's loginNodeGroupId. It is never called
        when the tag matches only computeNodeGroupId.

        Feature: event-driven-node-relaunch, Property 1: Login-only filtering

        **Validates: Requirements 2.2, 2.3, 5.1**
        """
        # --- Determine expected behaviour from the generated data --------
        login_matches = [
            c for c in clusters
            if c["loginNodeGroupId"] == tag_value
        ]
        compute_only_matches = [
            c for c in clusters
            if c["computeNodeGroupId"] == tag_value
            and c["loginNodeGroupId"] != tag_value
        ]
        should_update = len(login_matches) > 0

        # --- Configure EC2 mock ------------------------------------------
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            tag_value,
        )
        mock_ec2.describe_instances.return_value = _ec2_response()

        # --- Configure DynamoDB mock -------------------------------------
        mock_table = MagicMock()

        # Build the full cluster records with PK/SK
        full_clusters = [_add_pk_sk(c) for c in clusters]

        # The handler calls scan twice when no login match is found:
        #   1) scan by loginNodeGroupId  -> login_matches
        #   2) scan by computeNodeGroupId (via _is_compute_node_group_only)
        login_scan_results = [
            _add_pk_sk(c) for c in clusters
            if c["loginNodeGroupId"] == tag_value
        ]
        compute_scan_results = [
            _add_pk_sk(c) for c in clusters
            if c["computeNodeGroupId"] == tag_value
        ]

        if login_scan_results:
            # Only the first scan is called (login match found)
            mock_table.scan.return_value = {"Items": login_scan_results}
        else:
            # First scan returns empty, second scan checks compute
            mock_table.scan.side_effect = [
                {"Items": []},
                {"Items": compute_scan_results},
            ]

        mock_dynamodb.Table.return_value = mock_table

        # --- Invoke the handler ------------------------------------------
        from login_node_event import handler

        result = handler(_state_change_event(), None)

        # --- Assert: update_item called iff login match ------------------
        if should_update:
            assert mock_table.update_item.call_count >= 1, (
                f"Expected update_item to be called for tag_value="
                f"'{tag_value}' matching loginNodeGroupId of "
                f"{len(login_matches)} cluster(s), but it was not called. "
                f"Result: {result}"
            )
            # Verify each update targets a login-matched cluster
            for call in mock_table.update_item.call_args_list:
                key = call[1]["Key"] if "Key" in call[1] else call[0][0]
                pk = key["PK"]
                sk = key["SK"]
                matched_project = pk.replace("PROJECT#", "")
                matched_cluster = sk.replace("CLUSTER#", "")
                # The updated cluster must be one whose
                # loginNodeGroupId == tag_value
                matching = [
                    c for c in clusters
                    if c["projectId"] == matched_project
                    and c["clusterName"] == matched_cluster
                    and c["loginNodeGroupId"] == tag_value
                ]
                assert len(matching) > 0, (
                    f"update_item called for project='{matched_project}', "
                    f"cluster='{matched_cluster}' but this cluster's "
                    f"loginNodeGroupId does not match tag_value="
                    f"'{tag_value}'"
                )
        else:
            # No login match — update_item must NOT be called
            assert mock_table.update_item.call_count == 0, (
                f"Expected NO update_item calls when tag_value="
                f"'{tag_value}' does not match any loginNodeGroupId, "
                f"but update_item was called "
                f"{mock_table.update_item.call_count} time(s). "
                f"Compute-only matches: {len(compute_only_matches)}. "
                f"Result: {result}"
            )


# ---------------------------------------------------------------------------
# Strategies for Property 2
# ---------------------------------------------------------------------------

# Instance IDs matching the AWS format: i- followed by 17 hex chars
_instance_id_strategy = st.from_regex(r"i-[a-f0-9]{17}", fullmatch=True)

# IPv4 addresses via Hypothesis built-in, converted to string
_ip_strategy = st.ip_addresses(v=4).map(str)


# ===================================================================
# [PBT: Property 2] Update correctness
# ===================================================================


class TestUpdateCorrectness:
    """[PBT: Property 2] Update correctness.

    For any EC2 state-change event where the instance's node group ID
    matches an ACTIVE cluster's ``loginNodeGroupId`` and the instance ID
    or public IP differs from the stored values, the Login Node Event
    Handler SHALL update the cluster record's ``loginNodeInstanceId``
    and ``loginNodeIp`` fields to the new instance's actual values.

    Feature: event-driven-node-relaunch, Property 2: Update correctness

    **Validates: Requirements 2.4, 2.5**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        new_instance_id=_instance_id_strategy,
        new_ip=_ip_strategy,
        old_instance_id=_instance_id_strategy,
        old_ip=_ip_strategy,
        project_id=_id_strategy,
        cluster_name=_id_strategy,
        node_group_id=_id_strategy,
    )
    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_update_called_with_new_instance_id_and_ip(
        self,
        mock_dynamodb,
        mock_ec2,
        new_instance_id,
        new_ip,
        old_instance_id,
        old_ip,
        project_id,
        cluster_name,
        node_group_id,
    ):
        """DynamoDB update_item is called with the new instance ID and
        IP from the mocked EC2 response when values differ from the
        stored cluster record.

        Feature: event-driven-node-relaunch, Property 2: Update correctness

        **Validates: Requirements 2.4, 2.5**
        """
        from hypothesis import assume

        # Ensure the new values differ from old so an update is expected
        assume(new_instance_id != old_instance_id or new_ip != old_ip)

        # --- Build cluster record with OLD values -----------------------
        cluster_record = {
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
            "projectId": project_id,
            "clusterName": cluster_name,
            "loginNodeGroupId": node_group_id,
            "computeNodeGroupId": "cng-different",
            "loginNodeInstanceId": old_instance_id,
            "loginNodeIp": old_ip,
            "status": "ACTIVE",
        }

        # --- Configure EC2 mock -----------------------------------------
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            node_group_id,
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id=new_instance_id,
            public_ip=new_ip,
        )

        # --- Configure DynamoDB mock ------------------------------------
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster_record]}
        mock_dynamodb.Table.return_value = mock_table

        # --- Invoke the handler -----------------------------------------
        from login_node_event import handler

        event = _state_change_event(instance_id=new_instance_id)
        result = handler(event, None)

        # --- Assert: update_item called with correct values -------------
        assert mock_table.update_item.call_count == 1, (
            f"Expected exactly 1 update_item call but got "
            f"{mock_table.update_item.call_count}. Result: {result}"
        )

        call_kwargs = mock_table.update_item.call_args[1]

        # Verify the Key targets the correct cluster
        assert call_kwargs["Key"] == {
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        }, (
            f"update_item Key mismatch: {call_kwargs['Key']}"
        )

        # Verify the new instance ID and IP are passed
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert expr_values[":iid"] == new_instance_id, (
            f"Expected instance ID '{new_instance_id}' in update "
            f"but got '{expr_values[':iid']}'"
        )
        assert expr_values[":ip"] == new_ip, (
            f"Expected IP '{new_ip}' in update "
            f"but got '{expr_values[':ip']}'"
        )


# ===================================================================
# [PBT: Property 3] Multi-cluster update
# ===================================================================


class TestMultiClusterUpdate:
    """[PBT: Property 3] Multi-cluster update.

    For any node group ID that matches the ``loginNodeGroupId`` of
    multiple ACTIVE cluster records, the Login Node Event Handler SHALL
    update all matching cluster records with the new instance details.

    Feature: event-driven-node-relaunch, Property 3: Multi-cluster update

    **Validates: Requirements 5.4**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        cluster_count=st.integers(min_value=2, max_value=5),
        new_instance_id=_instance_id_strategy,
        new_ip=_ip_strategy,
        shared_node_group_id=_id_strategy,
    )
    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_update_called_once_per_matching_cluster(
        self,
        mock_dynamodb,
        mock_ec2,
        cluster_count,
        new_instance_id,
        new_ip,
        shared_node_group_id,
    ):
        """DynamoDB update_item is called exactly once per matching
        cluster when multiple ACTIVE clusters share the same
        loginNodeGroupId.

        Feature: event-driven-node-relaunch, Property 3: Multi-cluster update

        **Validates: Requirements 5.4**
        """
        # --- Build cluster records sharing the same loginNodeGroupId ---
        clusters = []
        for i in range(cluster_count):
            clusters.append({
                "PK": f"PROJECT#proj{i}",
                "SK": f"CLUSTER#cluster{i}",
                "projectId": f"proj{i}",
                "clusterName": f"cluster{i}",
                "loginNodeGroupId": shared_node_group_id,
                "computeNodeGroupId": f"cng-different-{i}",
                "loginNodeInstanceId": "i-old000000000000000",
                "loginNodeIp": "1.2.3.4",
                "status": "ACTIVE",
            })

        # --- Configure EC2 mock ----------------------------------------
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            shared_node_group_id,
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id=new_instance_id,
            public_ip=new_ip,
        )

        # --- Configure DynamoDB mock -----------------------------------
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": clusters}
        mock_dynamodb.Table.return_value = mock_table

        # --- Invoke the handler ----------------------------------------
        from login_node_event import handler

        event = _state_change_event(instance_id=new_instance_id)
        result = handler(event, None)

        # --- Assert: update_item called once per cluster ---------------
        assert mock_table.update_item.call_count == cluster_count, (
            f"Expected {cluster_count} update_item calls (one per "
            f"matching cluster) but got "
            f"{mock_table.update_item.call_count}. Result: {result}"
        )

        # Verify each cluster was targeted exactly once
        updated_keys = set()
        for call in mock_table.update_item.call_args_list:
            key = call[1]["Key"]
            pk_sk = (key["PK"], key["SK"])
            assert pk_sk not in updated_keys, (
                f"Cluster {pk_sk} was updated more than once"
            )
            updated_keys.add(pk_sk)

            # Verify correct values in each update
            expr_values = call[1]["ExpressionAttributeValues"]
            assert expr_values[":iid"] == new_instance_id, (
                f"Expected instance ID '{new_instance_id}' but got "
                f"'{expr_values[':iid']}'"
            )
            assert expr_values[":ip"] == new_ip, (
                f"Expected IP '{new_ip}' but got "
                f"'{expr_values[':ip']}'"
            )

        # Verify all expected clusters were updated
        expected_keys = {
            (c["PK"], c["SK"]) for c in clusters
        }
        assert updated_keys == expected_keys, (
            f"Mismatch between updated and expected clusters. "
            f"Updated: {updated_keys}, Expected: {expected_keys}"
        )


# ===================================================================
# [PBT: Property 4] Successful update logging completeness
# ===================================================================


class TestSuccessfulUpdateLoggingCompleteness:
    """[PBT: Property 4] Successful update logging completeness.

    For any successfully processed event that results in a DynamoDB
    update, the Login Node Event Handler's INFO-level log output SHALL
    contain the EC2 instance ID, instance state, cluster name, project
    ID, previous instance ID, new instance ID, previous IP, and new IP.

    Feature: event-driven-node-relaunch, Property 4: Successful update logging completeness

    **Validates: Requirements 2.10, 7.1, 7.2**
    """

    # Composite strategy combining instance, cluster, and IP generators
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        new_instance_id=_instance_id_strategy,
        new_ip=_ip_strategy,
        old_instance_id=_instance_id_strategy,
        old_ip=_ip_strategy,
        project_id=_id_strategy,
        cluster_name=_id_strategy,
        node_group_id=_id_strategy,
    )
    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_info_log_contains_all_required_fields(
        self,
        mock_dynamodb,
        mock_ec2,
        new_instance_id,
        new_ip,
        old_instance_id,
        old_ip,
        project_id,
        cluster_name,
        node_group_id,
    ):
        """INFO log emitted on successful update contains instance_id,
        state, cluster_name, project_id, old_instance_id,
        new_instance_id, old_ip, and new_ip.

        Feature: event-driven-node-relaunch, Property 4: Successful update logging completeness

        **Validates: Requirements 2.10, 7.1, 7.2**
        """
        import logging as stdlib_logging
        from hypothesis import assume

        # Ensure old and new values differ so the update fires
        assume(new_instance_id != old_instance_id or new_ip != old_ip)

        # --- Build cluster record with OLD values -----------------------
        cluster_record = {
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
            "projectId": project_id,
            "clusterName": cluster_name,
            "loginNodeGroupId": node_group_id,
            "computeNodeGroupId": "cng-different",
            "loginNodeInstanceId": old_instance_id,
            "loginNodeIp": old_ip,
            "status": "ACTIVE",
        }

        # --- Configure EC2 mock -----------------------------------------
        mock_ec2.describe_tags.return_value = _describe_tags_response(
            node_group_id,
        )
        mock_ec2.describe_instances.return_value = _ec2_response(
            instance_id=new_instance_id,
            public_ip=new_ip,
        )

        # --- Configure DynamoDB mock ------------------------------------
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [cluster_record]}
        mock_dynamodb.Table.return_value = mock_table

        # --- Capture log output -----------------------------------------
        from login_node_event import handler, logger as event_logger

        # Use a custom handler to capture log records
        captured_records: list[stdlib_logging.LogRecord] = []

        class _CaptureHandler(stdlib_logging.Handler):
            def emit(self, record: stdlib_logging.LogRecord) -> None:
                captured_records.append(record)

        capture_handler = _CaptureHandler()
        capture_handler.setLevel(stdlib_logging.INFO)
        event_logger.addHandler(capture_handler)

        try:
            event = _state_change_event(instance_id=new_instance_id)
            result = handler(event, None)
        finally:
            event_logger.removeHandler(capture_handler)

        # --- Assert: handler reported an update -------------------------
        assert result["action"] == "updated", (
            f"Expected action='updated' but got '{result['action']}'. "
            f"Result: {result}"
        )

        # --- Collect all INFO-level log messages ------------------------
        info_messages = [
            r.getMessage()
            for r in captured_records
            if r.levelno == stdlib_logging.INFO
        ]
        combined_info = "\n".join(info_messages)

        # --- Assert: all required fields present in INFO output ---------
        required_fields = {
            "instance_id (new)": new_instance_id,
            "state": "running",
            "cluster_name": cluster_name,
            "project_id": project_id,
            "old_instance_id": old_instance_id,
            "new_instance_id": new_instance_id,
            "old_ip": old_ip,
            "new_ip": new_ip,
        }

        for field_label, expected_value in required_fields.items():
            assert expected_value in combined_info, (
                f"INFO log missing {field_label}='{expected_value}'. "
                f"Full INFO output:\n{combined_info}"
            )


# ===================================================================
# [PBT: Property 5] Skip reason logging
# ===================================================================

# Strategy that picks one of the three skip scenarios per iteration.
_skip_scenario_strategy = st.sampled_from([
    "no_pcs_tag",
    "no_cluster_match",
    "compute_only_match",
])


class TestSkipReasonLogging:
    """[PBT: Property 5] Skip reason logging.

    For any event that is skipped (no PCS tag, no matching cluster by
    loginNodeGroupId, or instance belongs to a compute node group), the
    Login Node Event Handler SHALL emit a DEBUG-level log entry
    containing the reason for skipping.

    Feature: event-driven-node-relaunch, Property 5: Skip reason logging

    **Validates: Requirements 7.3**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        scenario=_skip_scenario_strategy,
        instance_id=_instance_id_strategy,
        node_group_id=_id_strategy,
    )
    @patch("login_node_event.ec2_client")
    @patch("login_node_event.dynamodb")
    def test_debug_log_contains_skip_reason(
        self,
        mock_dynamodb,
        mock_ec2,
        scenario,
        instance_id,
        node_group_id,
    ):
        """DEBUG log emitted when an event is skipped contains a
        reason string for each skip scenario.

        Feature: event-driven-node-relaunch, Property 5: Skip reason logging

        **Validates: Requirements 7.3**
        """
        import logging as stdlib_logging

        # --- Configure mocks per scenario --------------------------------
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        if scenario == "no_pcs_tag":
            # describe_tags returns empty — no PCS tag on instance
            mock_ec2.describe_tags.return_value = {"Tags": []}

        elif scenario == "no_cluster_match":
            # Instance has a PCS tag but no ACTIVE cluster matches
            mock_ec2.describe_tags.return_value = (
                _describe_tags_response(node_group_id)
            )
            # First scan (loginNodeGroupId) → empty
            # Second scan (computeNodeGroupId) → also empty
            mock_table.scan.side_effect = [
                {"Items": []},
                {"Items": []},
            ]

        elif scenario == "compute_only_match":
            # Instance has a PCS tag, login scan empty, compute scan
            # returns a match — instance belongs to compute node group
            mock_ec2.describe_tags.return_value = (
                _describe_tags_response(node_group_id)
            )
            compute_cluster = {
                "PK": "PROJECT#proj1",
                "SK": "CLUSTER#cluster1",
                "projectId": "proj1",
                "clusterName": "cluster1",
                "loginNodeGroupId": "lng-different",
                "computeNodeGroupId": node_group_id,
                "loginNodeInstanceId": "i-old000000000000000",
                "loginNodeIp": "1.2.3.4",
                "status": "ACTIVE",
            }
            # First scan (loginNodeGroupId) → empty
            # Second scan (computeNodeGroupId) → match
            mock_table.scan.side_effect = [
                {"Items": []},
                {"Items": [compute_cluster]},
            ]

        # --- Capture log output at DEBUG level ---------------------------
        from login_node_event import handler, logger as event_logger

        captured_records: list[stdlib_logging.LogRecord] = []

        class _CaptureHandler(stdlib_logging.Handler):
            def emit(self, record: stdlib_logging.LogRecord) -> None:
                captured_records.append(record)

        capture_handler = _CaptureHandler()
        capture_handler.setLevel(stdlib_logging.DEBUG)

        # Temporarily lower logger level to capture DEBUG messages
        original_level = event_logger.level
        event_logger.setLevel(stdlib_logging.DEBUG)
        event_logger.addHandler(capture_handler)

        try:
            event = _state_change_event(instance_id=instance_id)
            result = handler(event, None)
        finally:
            event_logger.removeHandler(capture_handler)
            event_logger.setLevel(original_level)

        # --- Assert: action is "skipped" ---------------------------------
        assert result["action"] == "skipped", (
            f"Expected action='skipped' for scenario '{scenario}' "
            f"but got '{result['action']}'. Result: {result}"
        )

        # --- Assert: at least one DEBUG log with a reason ----------------
        debug_messages = [
            r.getMessage()
            for r in captured_records
            if r.levelno == stdlib_logging.DEBUG
        ]

        assert len(debug_messages) > 0, (
            f"No DEBUG log emitted for skip scenario '{scenario}'. "
            f"All captured log messages: "
            f"{[r.getMessage() for r in captured_records]}"
        )

        # Map scenario to expected reason substring in the DEBUG log
        expected_reason_fragments = {
            "no_pcs_tag": "no PCS node group tag",
            "no_cluster_match": "No ACTIVE cluster",
            "compute_only_match": "compute node group",
        }
        expected_fragment = expected_reason_fragments[scenario]

        combined_debug = "\n".join(debug_messages)
        assert expected_fragment in combined_debug, (
            f"DEBUG log for scenario '{scenario}' does not contain "
            f"expected reason fragment '{expected_fragment}'. "
            f"DEBUG output:\n{combined_debug}"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 6
# ---------------------------------------------------------------------------

# Project IDs: lowercase alphanumeric, 3-20 chars (realistic project IDs)
_project_id_strategy = st.from_regex(r"[a-z0-9]{3,20}", fullmatch=True)


# ===================================================================
# [PBT: Property 6] CloudWatch Agent diagnostics configuration
# ===================================================================


class TestCloudWatchAgentDiagnosticsConfiguration:
    """[PBT: Property 6] CloudWatch Agent diagnostics configuration.

    For any valid ``project_id``, the ``generate_cloudwatch_agent_commands``
    function SHALL produce commands that configure collection of
    ``/var/log/messages`` (with log stream ``{instance_id}/syslog``),
    ``/var/log/cloud-init-output.log`` (with log stream
    ``{instance_id}/cloud-init-output``), and
    ``/var/log/amazon/pcs/bootstrap.log`` (with log stream
    ``{instance_id}/pcs-bootstrap``), targeting the log group
    ``/hpc-platform/clusters/{project_id}/node-diagnostics``, using
    ``append-config`` mode.

    Feature: event-driven-node-relaunch, Property 6: CloudWatch Agent diagnostics configuration

    **Validates: Requirements 8.1, 8.2, 8.5, 8.6**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(project_id=_project_id_strategy)
    def test_diagnostics_config_contains_required_elements(
        self,
        project_id,
    ):
        """generate_cloudwatch_agent_commands output contains
        /var/log/messages, /var/log/cloud-init-output.log,
        /var/log/amazon/pcs/bootstrap.log, the correct
        node-diagnostics log group, correct stream name patterns, and
        append-config mode.

        Feature: event-driven-node-relaunch, Property 6: CloudWatch Agent diagnostics configuration

        **Validates: Requirements 8.1, 8.2, 8.5, 8.6**
        """
        from posix_provisioning import generate_cloudwatch_agent_commands

        commands = generate_cloudwatch_agent_commands(project_id)
        combined = "\n".join(commands)

        # --- Requirement 8.1: syslog collection -------------------------
        assert "/var/log/messages" in combined, (
            f"Output missing '/var/log/messages' for project_id="
            f"'{project_id}'. Output:\n{combined}"
        )

        # --- Requirement 8.2: cloud-init output collection --------------
        assert "/var/log/cloud-init-output.log" in combined, (
            f"Output missing '/var/log/cloud-init-output.log' for "
            f"project_id='{project_id}'. Output:\n{combined}"
        )

        # --- Requirement 8.5: correct log group name --------------------
        expected_log_group = (
            f"/hpc-platform/clusters/{project_id}/node-diagnostics"
        )
        assert expected_log_group in combined, (
            f"Output missing expected log group "
            f"'{expected_log_group}'. Output:\n{combined}"
        )

        # --- Requirement 8.1: syslog stream name pattern ----------------
        assert "{instance_id}/syslog" in combined, (
            f"Output missing stream name pattern "
            f"'{{instance_id}}/syslog'. Output:\n{combined}"
        )

        # --- Requirement 8.2: cloud-init-output stream name pattern -----
        assert "{instance_id}/cloud-init-output" in combined, (
            f"Output missing stream name pattern "
            f"'{{instance_id}}/cloud-init-output'. Output:\n{combined}"
        )

        # --- PCS bootstrap log collection -------------------------------
        assert "/var/log/amazon/pcs/bootstrap.log" in combined, (
            f"Output missing '/var/log/amazon/pcs/bootstrap.log' for "
            f"project_id='{project_id}'. Output:\n{combined}"
        )

        # --- PCS bootstrap stream name pattern --------------------------
        assert "{instance_id}/pcs-bootstrap" in combined, (
            f"Output missing stream name pattern "
            f"'{{instance_id}}/pcs-bootstrap'. Output:\n{combined}"
        )

        # --- Requirement 8.6: append-config mode ------------------------
        assert "append-config" in combined, (
            f"Output missing 'append-config' mode for project_id="
            f"'{project_id}'. Output:\n{combined}"
        )
