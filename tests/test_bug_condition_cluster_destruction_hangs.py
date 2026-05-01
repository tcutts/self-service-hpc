"""Bug condition exploration test — Cluster Destruction Hangs.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**

This test encodes the EXPECTED (correct) behavior. It is designed to FAIL
on unfixed code, proving the bugs exist. After the fix is applied, the
same test should PASS, confirming the bugs are resolved.

Bug conditions:
- Bug Condition 1: delete_pcs_resources swallows sub-resource deletion failures
- Bug Condition 2: check_pcs_deletion_status has no bounded retry count
- Bug Condition 3: check_fsx_export_status has no bounded retry count
- Bug Condition 4: _is_pcs_resource_deleted masks non-ResourceNotFoundException errors
"""

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from botocore.exceptions import ClientError

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
errors = load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
cluster_destruction = load_lambda_module("cluster_operations", "cluster_destruction")

delete_pcs_resources = cluster_destruction.delete_pcs_resources
check_pcs_deletion_status = cluster_destruction.check_pcs_deletion_status
check_fsx_export_status = cluster_destruction.check_fsx_export_status
_is_pcs_resource_deleted = cluster_destruction._is_pcs_resource_deleted
InternalError = errors.InternalError

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

pcs_cluster_id_strategy = st.from_regex(r"pcs_[a-z0-9]{5,10}", fullmatch=True)
node_group_id_strategy = st.from_regex(r"[a-z]{3}-[0-9]{3}", fullmatch=True)
queue_id_strategy = st.from_regex(r"q-[0-9]{3}", fullmatch=True)
cluster_name_strategy = st.from_regex(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}", fullmatch=True)
project_id_strategy = st.from_regex(r"proj-[a-z0-9]{3,6}", fullmatch=True)

# Events with non-empty pcsClusterId and at least one non-empty sub-resource ID
destruction_event_with_sub_resources = st.fixed_dictionaries({
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

# Non-ResourceNotFoundException error codes
non_rnf_error_codes = st.sampled_from([
    "ThrottlingException",
    "AccessDeniedException",
    "ConflictException",
    "InternalServerError",
    "ServiceException",
])


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"Simulated {code}"}},
        "SomeOperation",
    )


# ===================================================================
# Bug Condition 1 — Swallowed deletion failure
# ===================================================================

class TestBugCondition1SwallowedDeletionFailure:
    """delete_pcs_resources must raise InternalError when any sub-resource
    deletion returns a ':failed' result.

    On UNFIXED code this FAILS because delete_pcs_resources uses best-effort
    deletion and always returns successfully, even when _delete_pcs_node_group
    returns a ':failed' result string.

    **Validates: Requirements 1.1, 1.2**
    """

    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(event=destruction_event_with_sub_resources)
    def test_delete_pcs_resources_raises_on_failed_deletion(self, event):
        """When _delete_pcs_node_group returns a ':failed' result,
        delete_pcs_resources must raise InternalError instead of
        returning successfully.

        **Validates: Requirements 1.1, 1.2**
        """
        mock_pcs = MagicMock()

        # Make all PCS delete calls raise a non-ResourceNotFoundException error
        # so _delete_pcs_node_group / _delete_pcs_queue return ':failed' results
        mock_pcs.delete_compute_node_group.side_effect = _client_error("ConflictException")
        mock_pcs.delete_queue.side_effect = _client_error("ConflictException")

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            with pytest.raises(InternalError):
                delete_pcs_resources(event)


# ===================================================================
# Bug Condition 2 — Unbounded PCS polling
# ===================================================================

class TestBugCondition2UnboundedPcsPolling:
    """check_pcs_deletion_status must raise an error when pcsRetryCount
    exceeds MAX_PCS_DELETION_RETRIES.

    On UNFIXED code this FAILS because check_pcs_deletion_status has no
    retry count tracking — it always returns pcsSubResourcesDeleted=False
    for another iteration, with no bound.

    **Validates: Requirements 1.2, 1.5**
    """

    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        event=destruction_event_with_sub_resources,
        retry_count=st.integers(min_value=121, max_value=200),
    )
    def test_check_pcs_deletion_status_raises_when_retries_exceeded(self, event, retry_count):
        """When pcsRetryCount exceeds MAX_PCS_DELETION_RETRIES (120),
        check_pcs_deletion_status must raise an error to halt the loop.

        **Validates: Requirements 1.2, 1.5**
        """
        event_with_retries = {
            **event,
            "pcsRetryCount": retry_count,
            "pcsSubResourcesDeleted": False,
        }

        mock_pcs = MagicMock()
        # Resources still exist (not deleted)
        mock_pcs.get_compute_node_group.return_value = {
            "computeNodeGroup": {"status": "ACTIVE"}
        }
        mock_pcs.get_queue.return_value = {"queue": {"status": "ACTIVE"}}

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            with pytest.raises((InternalError, Exception)):
                check_pcs_deletion_status(event_with_retries)


# ===================================================================
# Bug Condition 3 — Unbounded FSx export polling
# ===================================================================

class TestBugCondition3UnboundedFsxExportPolling:
    """check_fsx_export_status must return exportComplete=True and
    exportFailed=True when exportRetryCount exceeds MAX_EXPORT_RETRIES.

    On UNFIXED code this FAILS because check_fsx_export_status has no
    retry count tracking — it always returns exportComplete=False for
    another iteration, with no bound.

    **Validates: Requirements 1.3, 1.5**
    """

    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(retry_count=st.integers(min_value=61, max_value=200))
    def test_check_fsx_export_status_returns_failed_when_retries_exceeded(self, retry_count):
        """When exportRetryCount exceeds MAX_EXPORT_RETRIES (60),
        check_fsx_export_status must return exportComplete=True and
        exportFailed=True to halt the loop.

        **Validates: Requirements 1.3, 1.5**
        """
        event = {
            "projectId": "proj-test",
            "clusterName": "test-cluster",
            "fsxFilesystemId": "fs-abc123",
            "exportTaskId": "task-xyz789",
            "exportSkipped": False,
            "exportRetryCount": retry_count,
            "exportComplete": False,
        }

        mock_fsx = MagicMock()
        # Export task stuck in PENDING
        mock_fsx.describe_data_repository_tasks.return_value = {
            "DataRepositoryTasks": [{
                "TaskId": "task-xyz789",
                "Lifecycle": "PENDING",
            }],
        }

        with patch.object(cluster_destruction, "fsx_client", mock_fsx):
            result = check_fsx_export_status(event)

        assert result.get("exportComplete") is True, (
            f"Expected exportComplete=True when exportRetryCount={retry_count} "
            f"exceeds MAX_EXPORT_RETRIES, but got exportComplete={result.get('exportComplete')}. "
            f"check_fsx_export_status has no retry bound — it returns exportComplete=False forever."
        )
        assert result.get("exportFailed") is True, (
            f"Expected exportFailed=True when export polling timed out, "
            f"but got exportFailed={result.get('exportFailed')}."
        )


# ===================================================================
# Bug Condition 4 — Masked API errors
# ===================================================================

class TestBugCondition4MaskedApiErrors:
    """_is_pcs_resource_deleted must re-raise non-ResourceNotFoundException
    errors instead of returning False.

    On UNFIXED code this FAILS because _is_pcs_resource_deleted catches
    all ClientError exceptions and returns False for anything that is not
    ResourceNotFoundException, masking real API errors.

    **Validates: Requirements 1.4**
    """

    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(error_code=non_rnf_error_codes)
    def test_is_pcs_resource_deleted_raises_on_unexpected_error(self, error_code):
        """When the PCS describe function raises a non-ResourceNotFoundException
        ClientError, _is_pcs_resource_deleted must re-raise the error instead
        of returning False.

        **Validates: Requirements 1.4**
        """
        def describe_fn():
            raise _client_error(error_code)

        with pytest.raises(ClientError) as exc_info:
            _is_pcs_resource_deleted(describe_fn, f"test resource ({error_code})")

        assert exc_info.value.response["Error"]["Code"] == error_code
