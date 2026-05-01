"""Unit tests for create_pcs_cluster() Slurm version usage.

**Validates: Requirements 2.1**

Verifies that create_pcs_cluster() reads schedulerVersion from the event
payload and passes it to the PCS create_cluster API call, and that it
falls back to DEFAULT_SLURM_VERSION when schedulerVersion is absent.
"""

from unittest.mock import MagicMock, patch

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
pcs_versions = load_lambda_module("shared", "pcs_versions")
DEFAULT_SLURM_VERSION = pcs_versions.DEFAULT_SLURM_VERSION

# Pre-load cluster_creation and its transitive dependencies so that
# ``from cluster_creation import create_pcs_cluster`` inside test
# functions resolves correctly.
load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "posix_provisioning")
load_lambda_module("cluster_operations", "tagging")
load_lambda_module("cluster_operations", "cluster_creation")


def _make_event(scheduler_version=None):
    """Build a minimal event dict for create_pcs_cluster().

    If *scheduler_version* is provided it is included as
    ``schedulerVersion``; otherwise the key is omitted so the
    function must fall back to its default.
    """
    event = {
        "clusterName": "test-cluster",
        "projectId": "proj1",
        "privateSubnetIds": ["subnet-abc"],
        "securityGroupIds": {"computeNode": "sg-123"},
    }
    if scheduler_version is not None:
        event["schedulerVersion"] = scheduler_version
    return event


def _mock_pcs_response():
    """Return a canned PCS create_cluster response."""
    return {
        "cluster": {
            "id": "pcs-123",
            "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123",
        },
    }


class TestCreatePcsClusterVersionUsage:
    """create_pcs_cluster() must honour the event's schedulerVersion."""

    def test_uses_event_scheduler_version(self):
        """When schedulerVersion is present in the event, create_pcs_cluster()
        passes it to the PCS create_cluster scheduler config."""
        from cluster_creation import create_pcs_cluster

        mock_pcs = MagicMock()
        mock_pcs.create_cluster.return_value = _mock_pcs_response()

        event = _make_event(scheduler_version="24.11")

        with (
            patch("cluster_creation.pcs_client", mock_pcs),
            patch("cluster_creation._update_step_progress"),
        ):
            create_pcs_cluster(event)

        call_kwargs = mock_pcs.create_cluster.call_args
        scheduler_arg = call_kwargs.kwargs.get("scheduler") or call_kwargs[1].get("scheduler")
        assert scheduler_arg["version"] == "24.11"

    def test_defaults_to_default_slurm_version(self):
        """When schedulerVersion is absent from the event, create_pcs_cluster()
        falls back to DEFAULT_SLURM_VERSION."""
        from cluster_creation import create_pcs_cluster

        mock_pcs = MagicMock()
        mock_pcs.create_cluster.return_value = _mock_pcs_response()

        event = _make_event()  # no schedulerVersion key

        with (
            patch("cluster_creation.pcs_client", mock_pcs),
            patch("cluster_creation._update_step_progress"),
        ):
            create_pcs_cluster(event)

        call_kwargs = mock_pcs.create_cluster.call_args
        scheduler_arg = call_kwargs.kwargs.get("scheduler") or call_kwargs[1].get("scheduler")
        assert scheduler_arg["version"] == DEFAULT_SLURM_VERSION

    def test_each_supported_version_is_forwarded(self):
        """Each supported version string is forwarded verbatim to PCS."""
        from cluster_creation import create_pcs_cluster

        for version in ["24.11", "25.05", "25.11"]:
            mock_pcs = MagicMock()
            mock_pcs.create_cluster.return_value = _mock_pcs_response()

            event = _make_event(scheduler_version=version)

            with (
                patch("cluster_creation.pcs_client", mock_pcs),
                patch("cluster_creation._update_step_progress"),
            ):
                create_pcs_cluster(event)

            call_kwargs = mock_pcs.create_cluster.call_args
            scheduler_arg = call_kwargs.kwargs.get("scheduler") or call_kwargs[1].get("scheduler")
            assert scheduler_arg["version"] == version, (
                f"Expected version '{version}' but PCS received '{scheduler_arg['version']}'"
            )
