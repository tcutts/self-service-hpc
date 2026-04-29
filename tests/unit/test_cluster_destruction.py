"""Unit tests for cluster_destruction.py step handlers.

Tests cover:
- check_pcs_deletion_status (2.6)
- delete_pcs_cluster_step (2.7)
- refactored delete_pcs_resources (2.8)
- deregister_cluster_name_step (2.9)
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
for _mod in ["errors", "cluster_names", "cluster_destruction"]:
    if _mod in sys.modules:
        del sys.modules[_mod]

import cluster_destruction  # noqa: E402
from cluster_destruction import (  # noqa: E402
    check_pcs_deletion_status,
    delete_pcs_cluster_step,
    delete_pcs_resources,
    deregister_cluster_name_step,
)
from errors import InternalError  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resource_not_found_error():
    """Create a ResourceNotFoundException ClientError."""
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "DescribeResource",
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
        "pcsClusterId": "pcs_12345",
        "computeNodeGroupId": "cng-001",
        "loginNodeGroupId": "lng-001",
        "queueId": "q-001",
        "fsxFilesystemId": "fs-001",
    }
    event.update(overrides)
    return event


# ===================================================================
# 2.6 — check_pcs_deletion_status tests
# ===================================================================

class TestCheckPcsDeletionStatusAllDeleted:
    """Test that all-deleted (ResourceNotFoundException) returns True."""

    def test_all_resources_deleted(self):
        """All sub-resources raise ResourceNotFoundException → pcsSubResourcesDeleted=True."""
        mock_pcs = MagicMock()
        mock_pcs.get_compute_node_group.side_effect = _resource_not_found_error()
        mock_pcs.get_queue.side_effect = _resource_not_found_error()

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is True
        # Two node groups + one queue = 3 calls total
        assert mock_pcs.get_compute_node_group.call_count == 2
        assert mock_pcs.get_queue.call_count == 1


class TestCheckPcsDeletionStatusStillDeleting:
    """Test that still-deleting resources return False."""

    def test_all_resources_still_deleting(self):
        """All sub-resources still exist → pcsSubResourcesDeleted=False."""
        mock_pcs = MagicMock()
        mock_pcs.get_compute_node_group.return_value = {"computeNodeGroup": {"status": "DELETING"}}
        mock_pcs.get_queue.return_value = {"queue": {"status": "DELETING"}}

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is False


class TestCheckPcsDeletionStatusMixed:
    """Test mixed states — some deleted, some still deleting."""

    def test_mixed_deletion_states(self):
        """Compute deleted, login still deleting, queue deleted → False."""
        mock_pcs = MagicMock()

        def get_node_group_side_effect(**kwargs):
            ng_id = kwargs["computeNodeGroupIdentifier"]
            if ng_id == "cng-001":
                raise _resource_not_found_error()
            return {"computeNodeGroup": {"status": "DELETING"}}

        mock_pcs.get_compute_node_group.side_effect = get_node_group_side_effect
        mock_pcs.get_queue.side_effect = _resource_not_found_error()

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is False


class TestCheckPcsDeletionStatusEmptyIds:
    """Test that empty IDs skip polling."""

    def test_no_pcs_resources(self):
        """Empty pcsClusterId → skip polling, return True."""
        mock_pcs = MagicMock()
        event = _base_event(pcsClusterId="")

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is True
        mock_pcs.get_compute_node_group.assert_not_called()
        mock_pcs.get_queue.assert_not_called()

    def test_empty_sub_resource_ids(self):
        """Non-empty cluster ID but empty sub-resource IDs → skip polling, return True."""
        mock_pcs = MagicMock()
        event = _base_event(computeNodeGroupId="", loginNodeGroupId="", queueId="")

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is True
        mock_pcs.get_compute_node_group.assert_not_called()
        mock_pcs.get_queue.assert_not_called()


# ===================================================================
# 2.7 — delete_pcs_cluster_step tests
# ===================================================================

class TestDeletePcsClusterStepSuccess:
    """Test successful PCS cluster deletion."""

    def test_successful_deletion(self):
        """delete_cluster succeeds → pcsClusterDeleted=True."""
        mock_pcs = MagicMock()
        mock_pcs.delete_cluster.return_value = {}

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_cluster_step(event)

        assert result["pcsClusterDeleted"] is True
        mock_pcs.delete_cluster.assert_called_once_with(clusterIdentifier="pcs_12345")

    def test_already_deleted(self):
        """ResourceNotFoundException → treat as success."""
        mock_pcs = MagicMock()
        mock_pcs.delete_cluster.side_effect = _resource_not_found_error()

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_cluster_step(event)

        assert result["pcsClusterDeleted"] is True

    def test_empty_cluster_id_skips(self):
        """Empty pcsClusterId → skip, return success."""
        mock_pcs = MagicMock()
        event = _base_event(pcsClusterId="")

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_cluster_step(event)

        assert result["pcsClusterDeleted"] is True
        mock_pcs.delete_cluster.assert_not_called()


class TestDeletePcsClusterStepFailure:
    """Test failure propagation (raises InternalError)."""

    def test_failure_raises_internal_error(self):
        """Non-ResourceNotFoundException error → raises InternalError."""
        mock_pcs = MagicMock()
        mock_pcs.delete_cluster.side_effect = _generic_client_error("ConflictException")

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            with pytest.raises(InternalError) as exc_info:
                delete_pcs_cluster_step(event)

        assert "pcs_12345" in str(exc_info.value)


# ===================================================================
# 2.8 — refactored delete_pcs_resources tests
# ===================================================================

class TestDeletePcsResourcesRefactored:
    """Test that delete_pcs_resources no longer attempts cluster deletion."""

    def test_no_cluster_deletion_call(self):
        """delete_pcs_resources should NOT call _delete_pcs_cluster."""
        mock_pcs = MagicMock()
        mock_pcs.delete_compute_node_group.return_value = {}
        mock_pcs.delete_queue.return_value = {}

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_resources(event)

        # Should have called delete for node groups and queue
        assert mock_pcs.delete_compute_node_group.call_count == 2
        assert mock_pcs.delete_queue.call_count == 1
        # Should NOT have called delete_cluster
        mock_pcs.delete_cluster.assert_not_called()
        # Should have pcsCleanupResults
        assert "pcsCleanupResults" in result

    def test_initiates_node_group_and_queue_deletions(self):
        """Verify correct sub-resource deletion calls are made."""
        mock_pcs = MagicMock()
        mock_pcs.delete_compute_node_group.return_value = {}
        mock_pcs.delete_queue.return_value = {}

        event = _base_event()
        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_resources(event)

        # Verify cleanup results contain node group and queue entries
        results = result["pcsCleanupResults"]
        assert len(results) == 3  # compute ng, login ng, queue
        assert any("compute_node_group" in r for r in results)
        assert any("login_node_group" in r for r in results)
        assert any("queue" in r for r in results)
        # No cluster entry
        assert not any("cluster:" in r for r in results)

    def test_empty_ids_skip_deletions(self):
        """Empty sub-resource IDs → no deletion calls."""
        mock_pcs = MagicMock()
        event = _base_event(
            computeNodeGroupId="", loginNodeGroupId="", queueId="", pcsClusterId=""
        )

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_resources(event)

        mock_pcs.delete_compute_node_group.assert_not_called()
        mock_pcs.delete_queue.assert_not_called()
        mock_pcs.delete_cluster.assert_not_called()
        assert result["pcsCleanupResults"] == []


# ===================================================================
# 2.9 — deregister_cluster_name_step tests
# ===================================================================

class TestDeregisterClusterNameStepSuccess:
    """Test successful deregistration via the step handler."""

    def test_successful_deregistration(self):
        """cluster_names.deregister_cluster_name returns True → clusterNameDeregistered=True."""
        event = _base_event()
        env = {"CLUSTER_NAME_REGISTRY_TABLE_NAME": "MyRegistryTable"}

        with patch.dict(os.environ, env):
            with patch.object(cluster_destruction.cluster_names, "deregister_cluster_name", return_value=True) as mock_dereg:
                result = deregister_cluster_name_step(event)

        assert result["clusterNameDeregistered"] is True
        mock_dereg.assert_called_once_with("MyRegistryTable", "my-cluster")


class TestDeregisterClusterNameStepNotFound:
    """Test graceful handling when registry entry doesn't exist."""

    def test_not_found_returns_false(self):
        """cluster_names.deregister_cluster_name returns False → clusterNameDeregistered=False."""
        event = _base_event()
        env = {"CLUSTER_NAME_REGISTRY_TABLE_NAME": "MyRegistryTable"}

        with patch.dict(os.environ, env):
            with patch.object(cluster_destruction.cluster_names, "deregister_cluster_name", return_value=False):
                result = deregister_cluster_name_step(event)

        assert result["clusterNameDeregistered"] is False

    def test_missing_table_name_skips(self):
        """No CLUSTER_NAME_REGISTRY_TABLE_NAME env var → skip, return False."""
        event = _base_event()

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLUSTER_NAME_REGISTRY_TABLE_NAME", None)
            with patch.object(cluster_destruction.cluster_names, "deregister_cluster_name") as mock_dereg:
                result = deregister_cluster_name_step(event)

        assert result["clusterNameDeregistered"] is False
        mock_dereg.assert_not_called()

    def test_empty_cluster_name_skips(self):
        """Empty clusterName → skip, return False."""
        event = _base_event(clusterName="")
        env = {"CLUSTER_NAME_REGISTRY_TABLE_NAME": "MyRegistryTable"}

        with patch.dict(os.environ, env):
            with patch.object(cluster_destruction.cluster_names, "deregister_cluster_name") as mock_dereg:
                result = deregister_cluster_name_step(event)

        assert result["clusterNameDeregistered"] is False
        mock_dereg.assert_not_called()
