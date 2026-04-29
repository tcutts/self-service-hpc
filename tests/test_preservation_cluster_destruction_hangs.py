"""Preservation property tests — Cluster Destruction Hangs.

**Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.8, 3.9**

These tests capture the CURRENT baseline behavior of the unfixed code for
normal (non-buggy) paths.  They must PASS on the unfixed code and continue
to pass after the fix is applied, confirming no regressions.

Preservation properties:
- delete_pcs_resources returns successfully when all deletions succeed
- check_pcs_deletion_status returns pcsSubResourcesDeleted=True when all
  sub-resources raise ResourceNotFoundException
- check_pcs_deletion_status skips polling when pcsClusterId is empty
- check_fsx_export_status handles exportSkipped, SUCCEEDED, FAILED/CANCELED,
  and PENDING/EXECUTING lifecycles correctly
- _is_pcs_resource_deleted returns True for ResourceNotFoundException and
  False for successful describe calls
"""

import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)

# Clear cached modules to ensure correct imports
_cached_errors = sys.modules.get("errors")
if _cached_errors is not None:
    _errors_file = getattr(_cached_errors, "__file__", "") or ""
    if "cluster_operations" not in _errors_file:
        del sys.modules["errors"]

for _mod in ["cluster_names", "cluster_destruction"]:
    if _mod in sys.modules:
        del sys.modules[_mod]

import cluster_destruction  # noqa: E402
from cluster_destruction import (  # noqa: E402
    delete_pcs_resources,
    check_pcs_deletion_status,
    check_fsx_export_status,
    _is_pcs_resource_deleted,
)

# ---------------------------------------------------------------------------
# Strategies (reused from bug condition test)
# ---------------------------------------------------------------------------

pcs_cluster_id_strategy = st.from_regex(r"pcs_[a-z0-9]{5,10}", fullmatch=True)
node_group_id_strategy = st.from_regex(r"[a-z]{3}-[0-9]{3}", fullmatch=True)
queue_id_strategy = st.from_regex(r"q-[0-9]{3}", fullmatch=True)
cluster_name_strategy = st.from_regex(
    r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}", fullmatch=True
)
project_id_strategy = st.from_regex(r"proj-[a-z0-9]{3,6}", fullmatch=True)

# Extra event keys that may appear in real payloads
extra_event_keys = st.fixed_dictionaries(
    {},
    optional={
        "extraKey1": st.text(min_size=1, max_size=10),
        "extraKey2": st.integers(min_value=0, max_value=999),
    },
)


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"Simulated {code}"}},
        "SomeOperation",
    )


def _resource_not_found_error() -> ClientError:
    return _client_error("ResourceNotFoundException")


# ---------------------------------------------------------------------------
# Events with all three sub-resource IDs populated (non-empty)
# ---------------------------------------------------------------------------
full_event_strategy = st.fixed_dictionaries({
    "projectId": project_id_strategy,
    "clusterName": cluster_name_strategy,
    "pcsClusterId": pcs_cluster_id_strategy,
    "computeNodeGroupId": node_group_id_strategy,
    "loginNodeGroupId": node_group_id_strategy,
    "queueId": queue_id_strategy,
    "fsxFilesystemId": st.just(""),
})

# Events with at least one non-empty sub-resource ID
event_with_some_resources = st.fixed_dictionaries({
    "projectId": project_id_strategy,
    "clusterName": cluster_name_strategy,
    "pcsClusterId": pcs_cluster_id_strategy,
    "fsxFilesystemId": st.just(""),
}).flatmap(lambda base: st.tuples(
    st.just(base),
    st.tuples(
        st.one_of(node_group_id_strategy, st.just("")),
        st.one_of(node_group_id_strategy, st.just("")),
        st.one_of(queue_id_strategy, st.just("")),
    ).filter(lambda ids: any(ids)),
).map(lambda pair: {
    **pair[0],
    "computeNodeGroupId": pair[1][0],
    "loginNodeGroupId": pair[1][1],
    "queueId": pair[1][2],
}))


# ===================================================================
# Preservation 1 — delete_pcs_resources succeeds when all deletions
#                  succeed, returning pcsCleanupResults with :deleted
# ===================================================================

class TestPreservationDeletePcsResourcesSuccess:
    """When all PCS delete calls succeed, delete_pcs_resources returns
    an event with pcsCleanupResults containing only ':deleted' entries
    and all original event keys preserved.

    **Validates: Requirements 3.1, 3.8**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(event=event_with_some_resources)
    def test_successful_deletions_return_cleanup_results(self, event):
        """All PCS delete calls succeed → pcsCleanupResults has only
        ':deleted' entries and original event keys are preserved.

        **Validates: Requirements 3.1, 3.8**
        """
        mock_pcs = MagicMock()
        # All delete calls succeed (no exception)
        mock_pcs.delete_compute_node_group.return_value = {}
        mock_pcs.delete_queue.return_value = {}

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = delete_pcs_resources(event)

        # pcsCleanupResults must be present
        assert "pcsCleanupResults" in result

        # Every result must end with ':deleted'
        for entry in result["pcsCleanupResults"]:
            assert entry.endswith(":deleted"), (
                f"Expected all results to end with ':deleted', got '{entry}'"
            )

        # All original event keys must be preserved
        for key in event:
            assert key in result, (
                f"Original event key '{key}' missing from result"
            )
            assert result[key] == event[key], (
                f"Original event key '{key}' changed: "
                f"{event[key]!r} → {result[key]!r}"
            )


# ===================================================================
# Preservation 2 — check_pcs_deletion_status returns
#                  pcsSubResourcesDeleted=True when all sub-resources
#                  raise ResourceNotFoundException
# ===================================================================

class TestPreservationCheckPcsDeletionAllDeleted:
    """When all sub-resources raise ResourceNotFoundException,
    check_pcs_deletion_status returns pcsSubResourcesDeleted=True
    with all original event keys preserved.

    **Validates: Requirements 3.1, 3.5**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(event=event_with_some_resources)
    def test_all_resources_deleted_returns_true(self, event):
        """All sub-resources raise ResourceNotFoundException →
        pcsSubResourcesDeleted=True and original keys preserved.

        **Validates: Requirements 3.1, 3.5**
        """
        mock_pcs = MagicMock()
        # All describe calls raise ResourceNotFoundException
        mock_pcs.get_compute_node_group.side_effect = _resource_not_found_error()
        mock_pcs.get_queue.side_effect = _resource_not_found_error()

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is True

        # All original event keys must be preserved
        for key in event:
            assert key in result, (
                f"Original event key '{key}' missing from result"
            )
            assert result[key] == event[key], (
                f"Original event key '{key}' changed: "
                f"{event[key]!r} → {result[key]!r}"
            )


# ===================================================================
# Preservation 3 — check_pcs_deletion_status with empty pcsClusterId
#                  returns pcsSubResourcesDeleted=True without calling
#                  PCS APIs
# ===================================================================

class TestPreservationCheckPcsDeletionEmptyClusterId:
    """When pcsClusterId is empty, check_pcs_deletion_status returns
    pcsSubResourcesDeleted=True without calling any PCS APIs.

    **Validates: Requirements 3.7**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_empty_cluster_id_skips_polling(self, project_id, cluster_name):
        """Empty pcsClusterId → pcsSubResourcesDeleted=True, no PCS calls.

        **Validates: Requirements 3.7**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "pcsClusterId": "",
            "computeNodeGroupId": "cng-123",
            "loginNodeGroupId": "lng-456",
            "queueId": "q-789",
        }

        mock_pcs = MagicMock()

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is True

        # No PCS API calls should have been made
        mock_pcs.get_compute_node_group.assert_not_called()
        mock_pcs.get_queue.assert_not_called()


# ===================================================================
# Preservation 4 — check_fsx_export_status with exportSkipped=True
# ===================================================================

class TestPreservationFsxExportSkipped:
    """When exportSkipped=True, check_fsx_export_status returns
    exportComplete=True and exportFailed=False.

    **Validates: Requirements 3.6**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_export_skipped_returns_complete(self, project_id, cluster_name):
        """exportSkipped=True → exportComplete=True, exportFailed=False.

        **Validates: Requirements 3.6**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "exportSkipped": True,
            "fsxFilesystemId": "",
        }

        result = check_fsx_export_status(event)

        assert result["exportComplete"] is True
        assert result["exportFailed"] is False


# ===================================================================
# Preservation 5 — check_fsx_export_status with SUCCEEDED lifecycle
# ===================================================================

class TestPreservationFsxExportSucceeded:
    """When the FSx export task has SUCCEEDED lifecycle,
    check_fsx_export_status returns exportComplete=True, exportFailed=False.

    **Validates: Requirements 3.2**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_succeeded_lifecycle_returns_complete(self, project_id, cluster_name):
        """SUCCEEDED lifecycle → exportComplete=True, exportFailed=False.

        **Validates: Requirements 3.2**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "exportSkipped": False,
            "exportTaskId": "task-abc123",
            "fsxFilesystemId": "fs-xyz789",
        }

        mock_fsx = MagicMock()
        mock_fsx.describe_data_repository_tasks.return_value = {
            "DataRepositoryTasks": [{
                "TaskId": "task-abc123",
                "Lifecycle": "SUCCEEDED",
            }],
        }

        with patch.object(cluster_destruction, "fsx_client", mock_fsx):
            result = check_fsx_export_status(event)

        assert result["exportComplete"] is True
        assert result["exportFailed"] is False


# ===================================================================
# Preservation 6 — check_fsx_export_status with FAILED/CANCELED
# ===================================================================

class TestPreservationFsxExportFailedCanceled:
    """When the FSx export task has FAILED or CANCELED lifecycle,
    check_fsx_export_status returns exportComplete=True, exportFailed=True.

    **Validates: Requirements 3.3**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
        lifecycle=st.sampled_from(["FAILED", "CANCELED"]),
    )
    def test_failed_canceled_lifecycle_returns_export_failed(
        self, project_id, cluster_name, lifecycle
    ):
        """FAILED/CANCELED lifecycle → exportComplete=True, exportFailed=True.

        **Validates: Requirements 3.3**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "exportSkipped": False,
            "exportTaskId": "task-abc123",
            "fsxFilesystemId": "fs-xyz789",
        }

        mock_fsx = MagicMock()
        mock_fsx.describe_data_repository_tasks.return_value = {
            "DataRepositoryTasks": [{
                "TaskId": "task-abc123",
                "Lifecycle": lifecycle,
                "FailureDetails": {"Message": "Simulated failure"},
            }],
        }

        with patch.object(cluster_destruction, "fsx_client", mock_fsx):
            result = check_fsx_export_status(event)

        assert result["exportComplete"] is True
        assert result["exportFailed"] is True


# ===================================================================
# Preservation 7 — check_fsx_export_status with PENDING/EXECUTING
# ===================================================================

class TestPreservationFsxExportInProgress:
    """When the FSx export task has PENDING or EXECUTING lifecycle,
    check_fsx_export_status returns exportComplete=False, exportFailed=False.

    **Validates: Requirements 3.2, 3.3**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
        lifecycle=st.sampled_from(["PENDING", "EXECUTING"]),
    )
    def test_pending_executing_lifecycle_returns_not_complete(
        self, project_id, cluster_name, lifecycle
    ):
        """PENDING/EXECUTING lifecycle → exportComplete=False, exportFailed=False.

        **Validates: Requirements 3.2, 3.3**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "exportSkipped": False,
            "exportTaskId": "task-abc123",
            "fsxFilesystemId": "fs-xyz789",
        }

        mock_fsx = MagicMock()
        mock_fsx.describe_data_repository_tasks.return_value = {
            "DataRepositoryTasks": [{
                "TaskId": "task-abc123",
                "Lifecycle": lifecycle,
            }],
        }

        with patch.object(cluster_destruction, "fsx_client", mock_fsx):
            result = check_fsx_export_status(event)

        assert result["exportComplete"] is False
        assert result["exportFailed"] is False


# ===================================================================
# Preservation 8 — _is_pcs_resource_deleted returns True for
#                  ResourceNotFoundException
# ===================================================================

class TestPreservationIsPcsResourceDeletedRNF:
    """_is_pcs_resource_deleted returns True when the describe function
    raises ResourceNotFoundException.

    **Validates: Requirements 3.9**
    """

    def test_resource_not_found_returns_true(self):
        """ResourceNotFoundException → True.

        **Validates: Requirements 3.9**
        """
        def describe_fn():
            raise _resource_not_found_error()

        result = _is_pcs_resource_deleted(describe_fn, "test resource")
        assert result is True


# ===================================================================
# Preservation 9 — _is_pcs_resource_deleted returns False for
#                  successful describe call
# ===================================================================

class TestPreservationIsPcsResourceDeletedStillExists:
    """_is_pcs_resource_deleted returns False when the describe function
    succeeds (resource still exists).

    **Validates: Requirements 3.9**
    """

    def test_successful_describe_returns_false(self):
        """Successful describe call → False (resource still exists).

        **Validates: Requirements 3.9**
        """
        def describe_fn():
            return {"computeNodeGroup": {"status": "DELETING"}}

        result = _is_pcs_resource_deleted(describe_fn, "test resource")
        assert result is False
