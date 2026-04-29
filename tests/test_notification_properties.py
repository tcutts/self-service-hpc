"""Property tests for lifecycle notification content.

**Validates: Requirements 6.1, 6.2**

Property 2: Lifecycle notification contains all applicable connection strings.

For any non-empty login node IP and non-empty instance ID, the cluster-ready
notification message contains the SSH command, DCV URL, and SSM command.
When the IP is empty, the message omits SSH and DCV strings. When the
instance ID is empty, the message omits the SSM command.
"""

import os
import sys

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

for _d in [_SHARED_DIR, _CLUSTER_OPS_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from cluster_creation import build_notification_message  # noqa: E402

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# IPv4 addresses: four octets separated by dots
ipv4_strategy = st.tuples(
    st.integers(min_value=1, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=1, max_value=255),
).map(lambda t: f"{t[0]}.{t[1]}.{t[2]}.{t[3]}")

# Instance IDs: i- followed by 17 hex chars
instance_id_strategy = st.from_regex(r"i-[a-f0-9]{17}", fullmatch=True)

# Port numbers: valid TCP port range
port_strategy = st.integers(min_value=1, max_value=65535)

# IP that is either a valid IPv4 or empty string
ip_or_empty_strategy = st.one_of(ipv4_strategy, st.just(""))

# Instance ID that is either valid or empty string
instance_id_or_empty_strategy = st.one_of(instance_id_strategy, st.just(""))

# Cluster name: alphanumeric with hyphens/underscores
cluster_name_strategy = st.from_regex(r"[a-z][a-z0-9\-_]{2,19}", fullmatch=True)

# Project ID: alphanumeric with hyphens
project_id_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,19}", fullmatch=True)


class TestNotificationContent:
    """Property 2: Lifecycle notification contains all applicable connection strings.

    **Validates: Requirements 6.1, 6.2**
    """

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        cluster_name=cluster_name_strategy,
        project_id=project_id_strategy,
        login_ip=ip_or_empty_strategy,
        instance_id=instance_id_or_empty_strategy,
        ssh_port=port_strategy,
        dcv_port=port_strategy,
    )
    def test_notification_contains_applicable_connection_strings(
        self, cluster_name, project_id, login_ip, instance_id, ssh_port, dcv_port
    ):
        """For any combination of connection details, the notification message
        contains the expected connection strings when present and omits them
        when the source fields are empty.

        **Validates: Requirements 6.1, 6.2**
        """
        message = build_notification_message(
            cluster_name=cluster_name,
            project_id=project_id,
            login_ip=login_ip,
            instance_id=instance_id,
            ssh_port=ssh_port,
            dcv_port=dcv_port,
        )

        # Message always contains the cluster name and project ID
        assert cluster_name in message, (
            f"Message should contain cluster name '{cluster_name}'"
        )
        assert project_id in message, (
            f"Message should contain project ID '{project_id}'"
        )

        ssh_command = f"ssh -p {ssh_port} <username>@{login_ip}"
        dcv_url = f"https://{login_ip}:{dcv_port}"
        ssm_command = f"aws ssm start-session --target {instance_id}"

        # SSH command: present when IP is non-empty, absent when empty
        if login_ip:
            assert ssh_command in message, (
                f"Message should contain SSH command '{ssh_command}' when IP is non-empty"
            )
        else:
            assert "ssh -p" not in message, (
                "Message should not contain SSH command when login_ip is empty"
            )

        # DCV URL: present when IP is non-empty, absent when empty
        if login_ip:
            assert dcv_url in message, (
                f"Message should contain DCV URL '{dcv_url}' when IP is non-empty"
            )
        else:
            assert "https://" not in message, (
                "Message should not contain DCV URL when login_ip is empty"
            )

        # SSM command: present when instance ID is non-empty, absent when empty
        if instance_id:
            assert ssm_command in message, (
                f"Message should contain SSM command '{ssm_command}' when instance_id is non-empty"
            )
        else:
            assert "aws ssm start-session" not in message, (
                "Message should not contain SSM command when instance_id is empty"
            )
