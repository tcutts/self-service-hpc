"""Property tests for connectionInfo field formatting.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Property 1: Connection info fields are correctly formatted for any valid inputs.

For any valid combination of login node IP, instance ID, SSH port, and DCV port,
the constructed connectionInfo object satisfies the expected format. When source
fields are empty, the corresponding output fields are empty strings.
"""

import os
import sys

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# cluster_operations must come FIRST so its handler.py is found before
# template_management's handler.py.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

# Remove any other lambda handler directories that might shadow our import
_TEMPLATE_MGMT_DIR = os.path.join(_LAMBDA_DIR, "template_management")
if _TEMPLATE_MGMT_DIR in sys.path:
    sys.path.remove(_TEMPLATE_MGMT_DIR)

for _d in [_CLUSTER_OPS_DIR, _SHARED_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

# Force reimport of handler from cluster_operations if already cached
if "handler" in sys.modules:
    del sys.modules["handler"]

from handler import build_connection_info  # noqa: E402

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


class TestConnectionInfoFormatting:
    """Property 1: Connection info fields are correctly formatted for any valid inputs.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
    """

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        login_ip=ip_or_empty_strategy,
        instance_id=instance_id_or_empty_strategy,
        ssh_port=port_strategy,
        dcv_port=port_strategy,
    )
    def test_connection_info_fields_correctly_formatted(
        self, login_ip, instance_id, ssh_port, dcv_port
    ):
        """For any valid inputs, build_connection_info returns correctly formatted fields.

        - ssh equals 'ssh -p {port} <username>@{ip}' when IP is non-empty, empty otherwise
        - dcv equals 'https://{ip}:{port}' when IP is non-empty, empty otherwise
        - ssm equals 'aws ssm start-session --target {id}' when instance ID is non-empty, empty otherwise

        **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
        """
        result = build_connection_info(login_ip, instance_id, ssh_port, dcv_port)

        # Verify ssh field
        if login_ip:
            assert result["ssh"] == f"ssh -p {ssh_port} <username>@{login_ip}", (
                f"Expected ssh='ssh -p {ssh_port} <username>@{login_ip}', got '{result['ssh']}'"
            )
        else:
            assert result["ssh"] == "", (
                f"Expected empty ssh when login_ip is empty, got '{result['ssh']}'"
            )

        # Verify dcv field
        if login_ip:
            assert result["dcv"] == f"https://{login_ip}:{dcv_port}", (
                f"Expected dcv='https://{login_ip}:{dcv_port}', got '{result['dcv']}'"
            )
        else:
            assert result["dcv"] == "", (
                f"Expected empty dcv when login_ip is empty, got '{result['dcv']}'"
            )

        # Verify ssm field
        if instance_id:
            assert result["ssm"] == f"aws ssm start-session --target {instance_id}", (
                f"Expected ssm='aws ssm start-session --target {instance_id}', got '{result['ssm']}'"
            )
        else:
            assert result["ssm"] == "", (
                f"Expected empty ssm when instance_id is empty, got '{result['ssm']}'"
            )
