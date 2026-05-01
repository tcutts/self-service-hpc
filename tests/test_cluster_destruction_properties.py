"""Property-based tests for cluster_destruction.py step handlers.

[PBT: Property 1] check_pcs_deletion_status returns False when any sub-resource
is still in DELETING state.

[PBT: Property 2] delete_pcs_cluster_step raises an error when PCS cluster
deletion fails (does not return successfully).

[PBT: Property 4] When all PCS deletions succeed, the workflow steps produce
the same event structure fields as the original delete_pcs_resources.
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

check_pcs_deletion_status = cluster_destruction.check_pcs_deletion_status
delete_pcs_cluster_step = cluster_destruction.delete_pcs_cluster_step
delete_pcs_resources = cluster_destruction.delete_pcs_resources
InternalError = errors.InternalError

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty PCS resource IDs
pcs_cluster_id_strategy = st.from_regex(r"pcs_[a-z0-9]{5,10}", fullmatch=True)
node_group_id_strategy = st.from_regex(r"[a-z]{3}-[0-9]{3}", fullmatch=True)
queue_id_strategy = st.from_regex(r"q-[0-9]{3}", fullmatch=True)
cluster_name_strategy = st.from_regex(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}", fullmatch=True)
project_id_strategy = st.from_regex(r"proj-[a-z0-9]{3,6}", fullmatch=True)

# Strategy for a destruction event with non-empty pcsClusterId and at least
# one non-empty sub-resource ID.
destruction_event_with_sub_resources = st.fixed_dictionaries({
    "projectId": project_id_strategy,
    "clusterName": cluster_name_strategy,
    "pcsClusterId": pcs_cluster_id_strategy,
    "fsxFilesystemId": st.just(""),
}).flatmap(lambda base: st.tuples(
    st.just(base),
    # At least one sub-resource must be non-empty
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

# Strategy for PCS error codes that are NOT ResourceNotFoundException
non_rnf_error_codes = st.sampled_from([
    "ConflictException",
    "InternalServerError",
    "ServiceException",
    "ThrottlingException",
    "AccessDeniedException",
])


def _resource_not_found_error():
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "DescribeResource",
    )


def _client_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "error"}},
        "SomeOperation",
    )


# ===================================================================
# [PBT: Property 1] check_pcs_deletion_status returns False when
# any sub-resource is still in DELETING state
# ===================================================================

class TestCheckPcsDeletionStatusProperty:
    """[PBT: Property 1] For any destruction event with non-empty pcsClusterId
    and at least one non-empty sub-resource ID, check_pcs_deletion_status
    returns pcsSubResourcesDeleted: false when any sub-resource is still
    in DELETING state.

    **Validates: Requirements 2.1**
    """

    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(event=destruction_event_with_sub_resources)
    def test_returns_false_when_any_sub_resource_still_deleting(self, event):
        """When at least one sub-resource describe call succeeds (resource
        still exists / DELETING), pcsSubResourcesDeleted must be False.

        **Validates: Requirements 2.1**
        """
        mock_pcs = MagicMock()

        # All describe calls return success (resource still exists)
        mock_pcs.get_compute_node_group.return_value = {
            "computeNodeGroup": {"status": "DELETING"}
        }
        mock_pcs.get_queue.return_value = {
            "queue": {"status": "DELETING"}
        }

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            result = check_pcs_deletion_status(event)

        assert result["pcsSubResourcesDeleted"] is False, (
            f"Expected pcsSubResourcesDeleted=False for event with "
            f"sub-resources still DELETING. Event: {event}"
        )


# ===================================================================
# [PBT: Property 2] delete_pcs_cluster_step raises an error when
# PCS cluster deletion fails
# ===================================================================

class TestDeletePcsClusterStepFailureProperty:
    """[PBT: Property 2] For any destruction event where PCS cluster deletion
    fails, delete_pcs_cluster_step raises an error (does not return
    successfully).

    **Validates: Requirements 2.2, 2.3**
    """

    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        event=st.fixed_dictionaries({
            "projectId": project_id_strategy,
            "clusterName": cluster_name_strategy,
            "pcsClusterId": pcs_cluster_id_strategy,
            "computeNodeGroupId": st.just(""),
            "loginNodeGroupId": st.just(""),
            "queueId": st.just(""),
            "fsxFilesystemId": st.just(""),
        }),
        error_code=non_rnf_error_codes,
    )
    def test_raises_error_on_deletion_failure(self, event, error_code):
        """When delete_cluster raises a non-ResourceNotFoundException error,
        delete_pcs_cluster_step must raise InternalError.

        **Validates: Requirements 2.2, 2.3**
        """
        mock_pcs = MagicMock()
        mock_pcs.delete_cluster.side_effect = _client_error(error_code)

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            with pytest.raises(InternalError):
                delete_pcs_cluster_step(event)


# ===================================================================
# [PBT: Property 4] Successful-path output shape preservation
# ===================================================================

class TestPreservationSuccessfulPathProperty:
    """[PBT: Property 4] For any destruction event where all PCS deletions
    succeed (sub-resources not found, cluster deletion succeeds), the
    workflow steps produce the same event structure fields as the original
    delete_pcs_resources.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**
    """

    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        event=st.fixed_dictionaries({
            "projectId": project_id_strategy,
            "clusterName": cluster_name_strategy,
            "pcsClusterId": st.one_of(pcs_cluster_id_strategy, st.just("")),
            "computeNodeGroupId": st.one_of(node_group_id_strategy, st.just("")),
            "loginNodeGroupId": st.one_of(node_group_id_strategy, st.just("")),
            "queueId": st.one_of(queue_id_strategy, st.just("")),
            "fsxFilesystemId": st.just(""),
        }),
    )
    def test_successful_path_preserves_output_shape(self, event):
        """When all PCS API calls succeed, the combined output of
        delete_pcs_resources + check_pcs_deletion_status + delete_pcs_cluster_step
        should contain the same fields that the original delete_pcs_resources
        would have produced (pcsCleanupResults), plus the new fields
        (pcsSubResourcesDeleted, pcsClusterDeleted).

        The original event fields must be preserved through all steps.

        **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**
        """
        mock_pcs = MagicMock()
        # All deletion calls succeed
        mock_pcs.delete_compute_node_group.return_value = {}
        mock_pcs.delete_queue.return_value = {}
        mock_pcs.delete_cluster.return_value = {}
        # All describe calls raise ResourceNotFoundException (deleted)
        mock_pcs.get_compute_node_group.side_effect = _resource_not_found_error()
        mock_pcs.get_queue.side_effect = _resource_not_found_error()

        with patch.object(cluster_destruction, "pcs_client", mock_pcs):
            # Step 1: Initiate sub-resource deletions
            result1 = delete_pcs_resources(event)

            # Step 2: Check deletion status (all deleted)
            result2 = check_pcs_deletion_status(result1)

            # Step 3: Delete cluster
            result3 = delete_pcs_cluster_step(result2)

        # Original event fields must be preserved
        for key in event:
            assert key in result3, f"Original event key '{key}' missing from final result"
            assert result3[key] == event[key], (
                f"Original event key '{key}' changed: {event[key]} → {result3[key]}"
            )

        # The original delete_pcs_resources output field must be present
        assert "pcsCleanupResults" in result3, "pcsCleanupResults missing from output"
        assert isinstance(result3["pcsCleanupResults"], list)

        # New fields from the split workflow
        assert result3["pcsSubResourcesDeleted"] is True
        assert result3["pcsClusterDeleted"] is True
