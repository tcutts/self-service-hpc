"""Unit tests for CloudWatch Agent command generation.

Feature: event-driven-node-relaunch

Tests the ``generate_cloudwatch_agent_commands()`` function in
``lambda/cluster_operations/posix_provisioning.py``.  Verifies that:
- The existing access log configuration is preserved (backward compatibility).
- The node diagnostics config file is written with correct paths and log group.
- The ``append-config`` mode is used for the diagnostics config.

Validates: Requirements 8.1, 8.2, 8.5, 8.6
"""

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
posix_provisioning = load_lambda_module("cluster_operations", "posix_provisioning")
generate_cloudwatch_agent_commands = posix_provisioning.generate_cloudwatch_agent_commands


# ── Backward compatibility: access log config preserved ────────────────────


class TestAccessLogConfigPreserved:
    """Verify the existing access log configuration is unchanged.

    Validates: Requirement 8.6 (append-config coexistence)
    """

    def test_access_log_config_file_written(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-abc")
        joined = "\n".join(cmds)
        assert "hpc-access-log.json" in joined

    def test_access_log_group_name(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-abc")
        joined = "\n".join(cmds)
        assert "/hpc-platform/clusters/proj-abc/access-logs" in joined

    def test_access_log_file_path(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-abc")
        joined = "\n".join(cmds)
        assert "/var/log/hpc-access.log" in joined

    def test_access_log_stream_name(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-abc")
        joined = "\n".join(cmds)
        assert "{instance_id}/access-log" in joined

    def test_access_log_uses_append_config(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-abc")
        access_ctl = [
            c for c in cmds
            if "append-config" in c and "hpc-access-log.json" in c
        ]
        assert len(access_ctl) == 1


# ── Node diagnostics config: correct paths and log group ──────────────────


class TestNodeDiagnosticsConfig:
    """Verify the node diagnostics config is written with correct values.

    Validates: Requirements 8.1, 8.2, 8.5
    """

    def test_diagnostics_config_file_written(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "hpc-node-diagnostics.json" in joined

    def test_syslog_file_path(self) -> None:
        """Requirement 8.1: collect /var/log/messages."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "/var/log/messages" in joined

    def test_cloud_init_output_file_path(self) -> None:
        """Requirement 8.2: collect /var/log/cloud-init-output.log."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "/var/log/cloud-init-output.log" in joined

    def test_pcs_bootstrap_file_path(self) -> None:
        """Collect /var/log/amazon/pcs/bootstrap.log (PCS bootstrap log)."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "/var/log/amazon/pcs/bootstrap.log" in joined

    def test_diagnostics_log_group_name(self) -> None:
        """Requirement 8.5: log group uses project_id."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "/hpc-platform/clusters/proj-xyz/node-diagnostics" in joined

    def test_syslog_stream_name(self) -> None:
        """Requirement 8.1: syslog stream is {instance_id}/syslog."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "{instance_id}/syslog" in joined

    def test_cloud_init_stream_name(self) -> None:
        """Requirement 8.2: cloud-init stream is {instance_id}/cloud-init-output."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "{instance_id}/cloud-init-output" in joined

    def test_pcs_bootstrap_stream_name(self) -> None:
        """PCS bootstrap stream is {instance_id}/pcs-bootstrap."""
        cmds = generate_cloudwatch_agent_commands("proj-xyz")
        joined = "\n".join(cmds)
        assert "{instance_id}/pcs-bootstrap" in joined

    def test_project_id_embedded_in_log_group(self) -> None:
        """Different project IDs produce different log group names."""
        cmds_a = generate_cloudwatch_agent_commands("alpha")
        cmds_b = generate_cloudwatch_agent_commands("beta")
        joined_a = "\n".join(cmds_a)
        joined_b = "\n".join(cmds_b)
        assert "/hpc-platform/clusters/alpha/node-diagnostics" in joined_a
        assert "/hpc-platform/clusters/beta/node-diagnostics" in joined_b


# ── append-config mode for diagnostics ─────────────────────────────────────


class TestDiagnosticsAppendConfigMode:
    """Verify the diagnostics config uses append-config mode.

    Validates: Requirement 8.6
    """

    def test_diagnostics_uses_append_config(self) -> None:
        cmds = generate_cloudwatch_agent_commands("proj-123")
        diag_ctl = [
            c for c in cmds
            if "append-config" in c and "hpc-node-diagnostics.json" in c
        ]
        assert len(diag_ctl) == 1

    def test_both_configs_use_append_config(self) -> None:
        """Both access log and diagnostics use append-config mode."""
        cmds = generate_cloudwatch_agent_commands("proj-123")
        append_cmds = [c for c in cmds if "append-config" in c]
        assert len(append_cmds) == 2

    def test_diagnostics_config_path(self) -> None:
        """Config is written to the standard CloudWatch agent etc directory."""
        cmds = generate_cloudwatch_agent_commands("proj-123")
        joined = "\n".join(cmds)
        expected = "/opt/aws/amazon-cloudwatch-agent/etc/hpc-node-diagnostics.json"
        assert expected in joined
