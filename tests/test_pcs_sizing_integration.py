"""Integration tests for create_pcs_cluster ↔ determine_controller_size wiring.

Validates that the handler reads maxNodes from the event, passes it through
the sizing function, and forwards the result to pcs_client.create_cluster().

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**
"""

from unittest.mock import MagicMock, patch

import pytest

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
errors = load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
pcs_sizing = load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "posix_provisioning")
load_lambda_module("cluster_operations", "tagging")
cluster_creation = load_lambda_module("cluster_operations", "cluster_creation")

ValidationError = errors.ValidationError
determine_controller_size = pcs_sizing.determine_controller_size


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides) -> dict:
    """Return a minimal valid event for create_pcs_cluster."""
    event = {
        "clusterName": "test-cluster",
        "projectId": "proj-123",
        "privateSubnetIds": ["subnet-aaa"],
        "securityGroupIds": {"computeNode": "sg-111"},
    }
    event.update(overrides)
    return event


def _mock_create_cluster_response(cluster_name: str = "test-cluster"):
    """Return a realistic create_cluster API response."""
    return {
        "cluster": {
            "id": "pcs-abc123",
            "arn": f"arn:aws:pcs:us-east-1:123456789012:cluster/{cluster_name}",
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandlerSizingWiring:
    """Integration tests: create_pcs_cluster uses determine_controller_size."""

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "pcs_client")
    def test_medium_size_for_100_nodes(self, mock_pcs, mock_progress):
        """maxNodes=100 → create_cluster called with size='MEDIUM'.

        **Validates: Requirements 2.1, 2.2**
        """
        mock_pcs.create_cluster.return_value = _mock_create_cluster_response()

        event = _make_event(maxNodes=100)
        result = cluster_creation.create_pcs_cluster(event)

        call_kwargs = mock_pcs.create_cluster.call_args
        assert call_kwargs[1]["size"] == "MEDIUM"
        assert "pcsClusterId" in result

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "pcs_client")
    def test_default_max_nodes_uses_small(self, mock_pcs, mock_progress):
        """Missing maxNodes defaults to 10 → size='SMALL'.

        **Validates: Requirements 2.1, 2.2**
        """
        mock_pcs.create_cluster.return_value = _mock_create_cluster_response()

        event = _make_event()  # no maxNodes key
        result = cluster_creation.create_pcs_cluster(event)

        call_kwargs = mock_pcs.create_cluster.call_args
        assert call_kwargs[1]["size"] == "SMALL"
        assert "pcsClusterId" in result

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "pcs_client")
    def test_over_capacity_raises_validation_error(self, mock_pcs, mock_progress):
        """maxNodes=5000 → ValidationError, create_cluster never called.

        **Validates: Requirements 2.3**
        """
        event = _make_event(maxNodes=5000)

        with pytest.raises(ValidationError):
            cluster_creation.create_pcs_cluster(event)

        mock_pcs.create_cluster.assert_not_called()

    @patch.object(cluster_creation, "_update_step_progress")
    @patch.object(cluster_creation, "pcs_client")
    def test_size_matches_sizing_function(self, mock_pcs, mock_progress):
        """size param always matches determine_controller_size output.

        **Validates: Requirements 2.2, 2.4**
        """
        mock_pcs.create_cluster.return_value = _mock_create_cluster_response()

        for max_nodes in [1, 31, 32, 100, 511, 512, 1000, 2047]:
            mock_pcs.reset_mock()
            expected_size = determine_controller_size(max_nodes)

            event = _make_event(maxNodes=max_nodes)
            cluster_creation.create_pcs_cluster(event)

            call_kwargs = mock_pcs.create_cluster.call_args
            actual_size = call_kwargs[1]["size"]
            assert actual_size == expected_size, (
                f"maxNodes={max_nodes}: expected size={expected_size}, "
                f"got size={actual_size}"
            )
