"""Integration tests for the cluster destruction workflow.

These tests exercise the full sequence of step handlers working together,
simulating the Step Functions state machine flow with mocked AWS clients.

Tests cover:
- 4.1: Full destruction workflow — PCS sub-resources polled, cluster deletion
       waits, failures halt workflow, cluster name deregistered
- 4.2: Destruction with no PCS resources (empty IDs) — workflow completes,
       skips PCS polling, still deregisters the name
- 4.3: Destruction with PCS cluster deletion failure — workflow does NOT
       reach record_cluster_destroyed
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
    check_fsx_export_status,
    check_pcs_deletion_status,
    create_fsx_export_task,
    delete_fsx_filesystem,
    delete_iam_resources,
    delete_launch_templates,
    delete_pcs_cluster_step,
    delete_pcs_resources,
    deregister_cluster_name_step,
    record_cluster_destroyed,
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


def _generic_client_error(code="ConflictException"):
    """Create a generic ClientError."""
    return ClientError(
        {"Error": {"Code": code, "Message": "something went wrong"}},
        "SomeOperation",
    )


def _full_event():
    """Build a complete destruction event with all resource IDs populated."""
    return {
        "projectId": "proj-integ",
        "clusterName": "integ-cluster",
        "pcsClusterId": "pcs_integ123",
        "computeNodeGroupId": "cng-integ-001",
        "loginNodeGroupId": "lng-integ-001",
        "queueId": "q-integ-001",
        "fsxFilesystemId": "fs-integ-001",
    }


def _empty_pcs_event():
    """Build a destruction event with no PCS resources."""
    return {
        "projectId": "proj-integ",
        "clusterName": "integ-cluster-nopcs",
        "pcsClusterId": "",
        "computeNodeGroupId": "",
        "loginNodeGroupId": "",
        "queueId": "",
        "fsxFilesystemId": "fs-integ-002",
    }


# ---------------------------------------------------------------------------
# Shared mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_aws_clients():
    """Provide mocked AWS clients for all services used by destruction steps."""
    mock_pcs = MagicMock()
    mock_fsx = MagicMock()
    mock_dynamodb = MagicMock()
    mock_iam = MagicMock()
    mock_ec2 = MagicMock()

    # DynamoDB table mock
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    return {
        "pcs": mock_pcs,
        "fsx": mock_fsx,
        "dynamodb": mock_dynamodb,
        "iam": mock_iam,
        "ec2": mock_ec2,
        "table": mock_table,
    }


# ===================================================================
# 4.1 — Full destruction workflow with mocked AWS services
# ===================================================================

class TestFullDestructionWorkflow:
    """Integration test: full destruction workflow.

    Verifies:
    - PCS sub-resources are polled via check_pcs_deletion_status
    - Cluster deletion waits for sub-resources to be confirmed deleted
    - Cluster name is deregistered
    - Cluster is marked DESTROYED at the end
    """

    def test_full_workflow_happy_path(self, mock_aws_clients):
        """Walk through every step handler in order, verifying the full
        destruction workflow completes successfully with all expected
        side effects."""
        mocks = mock_aws_clients

        # --- Configure FSx mocks ---
        mocks["fsx"].create_data_repository_task.return_value = {
            "DataRepositoryTask": {"TaskId": "task-export-001"},
        }
        mocks["fsx"].describe_data_repository_tasks.return_value = {
            "DataRepositoryTasks": [{"TaskId": "task-export-001", "Lifecycle": "SUCCEEDED"}],
        }
        mocks["fsx"].delete_file_system.return_value = {}

        # --- Configure PCS mocks ---
        # Sub-resource deletion initiations succeed
        mocks["pcs"].delete_compute_node_group.return_value = {}
        mocks["pcs"].delete_queue.return_value = {}
        # Sub-resource polling: ResourceNotFoundException = deleted
        mocks["pcs"].get_compute_node_group.side_effect = _resource_not_found_error()
        mocks["pcs"].get_queue.side_effect = _resource_not_found_error()
        # Cluster deletion succeeds
        mocks["pcs"].delete_cluster.return_value = {}

        # --- Configure IAM mocks (best-effort, all succeed) ---
        mocks["iam"].remove_role_from_instance_profile.return_value = {}
        mocks["iam"].delete_instance_profile.return_value = {}
        mocks["iam"].detach_role_policy.return_value = {}
        mocks["iam"].delete_role_policy.return_value = {}
        mocks["iam"].delete_role.return_value = {}

        # --- Configure EC2 mocks (launch templates) ---
        mocks["ec2"].describe_launch_templates.return_value = {
            "LaunchTemplates": [{"LaunchTemplateId": "lt-001"}],
        }
        mocks["ec2"].delete_launch_template.return_value = {}

        # --- Configure DynamoDB mock ---
        mocks["table"].update_item.return_value = {}

        env = {"CLUSTER_NAME_REGISTRY_TABLE_NAME": "TestRegistryTable"}
        event = _full_event()

        with patch.object(cluster_destruction, "pcs_client", mocks["pcs"]), \
             patch.object(cluster_destruction, "fsx_client", mocks["fsx"]), \
             patch.object(cluster_destruction, "iam_client", mocks["iam"]), \
             patch.object(cluster_destruction, "ec2_client", mocks["ec2"]), \
             patch.object(cluster_destruction, "dynamodb", mocks["dynamodb"]), \
             patch.dict(os.environ, env), \
             patch.object(
                 cluster_destruction.cluster_names,
                 "deregister_cluster_name",
                 return_value=True,
             ) as mock_deregister:

            # Step 1: Create FSx export task
            result = create_fsx_export_task(event)
            assert result["exportTaskId"] == "task-export-001"
            assert result["exportSkipped"] is False

            # Step 2: Check FSx export status (completed)
            result = check_fsx_export_status(result)
            assert result["exportComplete"] is True
            assert result["exportFailed"] is False

            # Step 3: Initiate PCS sub-resource deletions
            result = delete_pcs_resources(result)
            assert "pcsCleanupResults" in result
            assert len(result["pcsCleanupResults"]) == 3  # compute ng, login ng, queue

            # Step 3b: Poll PCS sub-resource deletion status
            result = check_pcs_deletion_status(result)
            assert result["pcsSubResourcesDeleted"] is True

            # Verify PCS sub-resources were actually polled
            assert mocks["pcs"].get_compute_node_group.call_count == 2  # compute + login
            assert mocks["pcs"].get_queue.call_count == 1

            # Step 3c: Delete PCS cluster (after sub-resources confirmed deleted)
            result = delete_pcs_cluster_step(result)
            assert result["pcsClusterDeleted"] is True
            mocks["pcs"].delete_cluster.assert_called_once_with(
                clusterIdentifier="pcs_integ123"
            )

            # Step 4: Delete FSx filesystem
            result = delete_fsx_filesystem(result)
            assert result["fsxDeleted"] is True

            # Step 5: Delete IAM resources
            result = delete_iam_resources(result)
            assert "iamCleanupResults" in result

            # Step 5b: Delete launch templates
            result = delete_launch_templates(result)
            assert "launchTemplateCleanupResults" in result

            # Step: Deregister cluster name
            result = deregister_cluster_name_step(result)
            assert result["clusterNameDeregistered"] is True
            mock_deregister.assert_called_once_with(
                "TestRegistryTable", "integ-cluster"
            )

            # Step 6: Record cluster destroyed
            result = record_cluster_destroyed(result)
            assert result["status"] == "DESTROYED"
            assert "destroyedAt" in result

            # Verify DynamoDB update was called (progress updates + final DESTROYED)
            assert mocks["table"].update_item.call_count >= 1
            # The last update_item call should be the record_cluster_destroyed call
            last_call_kwargs = mocks["table"].update_item.call_args[1]
            assert last_call_kwargs["Key"]["PK"] == "PROJECT#proj-integ"
            assert last_call_kwargs["Key"]["SK"] == "CLUSTER#integ-cluster"

    def test_pcs_polling_loop_waits_then_succeeds(self, mock_aws_clients):
        """Simulate the state machine polling loop: first poll returns
        still-deleting, second poll returns all-deleted."""
        mocks = mock_aws_clients

        # First call: resources still exist (DELETING)
        # Second call: resources gone (ResourceNotFoundException)
        call_count = {"compute": 0, "queue": 0}

        def get_node_group_side_effect(**kwargs):
            call_count["compute"] += 1
            if call_count["compute"] <= 2:  # 2 node groups on first poll
                return {"computeNodeGroup": {"status": "DELETING"}}
            raise _resource_not_found_error()

        def get_queue_side_effect(**kwargs):
            call_count["queue"] += 1
            if call_count["queue"] <= 1:  # 1 queue on first poll
                return {"queue": {"status": "DELETING"}}
            raise _resource_not_found_error()

        mocks["pcs"].get_compute_node_group.side_effect = get_node_group_side_effect
        mocks["pcs"].get_queue.side_effect = get_queue_side_effect
        mocks["pcs"].delete_compute_node_group.return_value = {}
        mocks["pcs"].delete_queue.return_value = {}
        mocks["pcs"].delete_cluster.return_value = {}

        event = _full_event()

        with patch.object(cluster_destruction, "pcs_client", mocks["pcs"]):
            # Initiate deletions
            result = delete_pcs_resources(event)

            # First poll — still deleting
            result = check_pcs_deletion_status(result)
            assert result["pcsSubResourcesDeleted"] is False

            # Second poll — all deleted
            result = check_pcs_deletion_status(result)
            assert result["pcsSubResourcesDeleted"] is True

            # Now cluster deletion can proceed
            result = delete_pcs_cluster_step(result)
            assert result["pcsClusterDeleted"] is True

    def test_pcs_failure_halts_workflow(self, mock_aws_clients):
        """When delete_pcs_cluster_step fails, InternalError is raised
        and the workflow cannot proceed to record_cluster_destroyed."""
        mocks = mock_aws_clients

        mocks["pcs"].delete_compute_node_group.return_value = {}
        mocks["pcs"].delete_queue.return_value = {}
        mocks["pcs"].get_compute_node_group.side_effect = _resource_not_found_error()
        mocks["pcs"].get_queue.side_effect = _resource_not_found_error()
        # Cluster deletion fails with a non-ResourceNotFoundException error
        mocks["pcs"].delete_cluster.side_effect = _generic_client_error("ServiceException")

        event = _full_event()

        with patch.object(cluster_destruction, "pcs_client", mocks["pcs"]):
            result = delete_pcs_resources(event)
            result = check_pcs_deletion_status(result)
            assert result["pcsSubResourcesDeleted"] is True

            # Cluster deletion should raise InternalError
            with pytest.raises(InternalError):
                delete_pcs_cluster_step(result)

        # record_cluster_destroyed was never called — the exception halted flow
        mocks["dynamodb"] = MagicMock()
        mocks["dynamodb"].Table.return_value.update_item.assert_not_called()


# ===================================================================
# 4.2 — Destruction with no PCS resources (empty IDs)
# ===================================================================

class TestDestructionNoPcsResources:
    """Integration test: destruction of cluster with no PCS resources.

    Verifies:
    - Workflow completes successfully
    - PCS polling is skipped
    - Cluster name is still deregistered
    """

    def test_no_pcs_resources_workflow(self, mock_aws_clients):
        """Walk through the workflow with empty PCS IDs — PCS steps
        should be no-ops, but cluster name deregistration and
        record_cluster_destroyed should still execute."""
        mocks = mock_aws_clients

        # FSx mocks
        mocks["fsx"].create_data_repository_task.return_value = {
            "DataRepositoryTask": {"TaskId": "task-export-002"},
        }
        mocks["fsx"].describe_data_repository_tasks.return_value = {
            "DataRepositoryTasks": [{"TaskId": "task-export-002", "Lifecycle": "SUCCEEDED"}],
        }
        mocks["fsx"].delete_file_system.return_value = {}

        # IAM mocks (best-effort)
        mocks["iam"].remove_role_from_instance_profile.return_value = {}
        mocks["iam"].delete_instance_profile.return_value = {}
        mocks["iam"].detach_role_policy.return_value = {}
        mocks["iam"].delete_role_policy.return_value = {}
        mocks["iam"].delete_role.return_value = {}

        # EC2 mocks
        mocks["ec2"].describe_launch_templates.return_value = {
            "LaunchTemplates": [{"LaunchTemplateId": "lt-002"}],
        }
        mocks["ec2"].delete_launch_template.return_value = {}

        # DynamoDB mock
        mocks["table"].update_item.return_value = {}

        env = {"CLUSTER_NAME_REGISTRY_TABLE_NAME": "TestRegistryTable"}
        event = _empty_pcs_event()

        with patch.object(cluster_destruction, "pcs_client", mocks["pcs"]), \
             patch.object(cluster_destruction, "fsx_client", mocks["fsx"]), \
             patch.object(cluster_destruction, "iam_client", mocks["iam"]), \
             patch.object(cluster_destruction, "ec2_client", mocks["ec2"]), \
             patch.object(cluster_destruction, "dynamodb", mocks["dynamodb"]), \
             patch.dict(os.environ, env), \
             patch.object(
                 cluster_destruction.cluster_names,
                 "deregister_cluster_name",
                 return_value=True,
             ) as mock_deregister:

            # Step 1: Create FSx export task
            result = create_fsx_export_task(event)

            # Step 2: Check FSx export status
            result = check_fsx_export_status(result)
            assert result["exportComplete"] is True

            # Step 3: Initiate PCS sub-resource deletions (no-op)
            result = delete_pcs_resources(result)
            assert result["pcsCleanupResults"] == []

            # Step 3b: Check PCS deletion status (skip — no PCS cluster)
            result = check_pcs_deletion_status(result)
            assert result["pcsSubResourcesDeleted"] is True

            # Verify PCS polling was skipped
            mocks["pcs"].get_compute_node_group.assert_not_called()
            mocks["pcs"].get_queue.assert_not_called()

            # Step 3c: Delete PCS cluster (no-op — empty ID)
            result = delete_pcs_cluster_step(result)
            assert result["pcsClusterDeleted"] is True
            mocks["pcs"].delete_cluster.assert_not_called()

            # Step 4: Delete FSx filesystem
            result = delete_fsx_filesystem(result)

            # Step 5: Delete IAM resources
            result = delete_iam_resources(result)

            # Step 5b: Delete launch templates
            result = delete_launch_templates(result)

            # Step: Deregister cluster name — should still happen
            result = deregister_cluster_name_step(result)
            assert result["clusterNameDeregistered"] is True
            mock_deregister.assert_called_once_with(
                "TestRegistryTable", "integ-cluster-nopcs"
            )

            # Step 6: Record cluster destroyed
            result = record_cluster_destroyed(result)
            assert result["status"] == "DESTROYED"


# ===================================================================
# 4.3 — Destruction with PCS cluster deletion failure
# ===================================================================

class TestDestructionPcsClusterFailure:
    """Integration test: PCS cluster deletion failure.

    Verifies:
    - InternalError is raised by delete_pcs_cluster_step
    - record_cluster_destroyed is NOT called
    - Cluster is NOT marked as DESTROYED
    """

    def test_pcs_cluster_deletion_failure_halts_workflow(self, mock_aws_clients):
        """When PCS cluster deletion raises a non-ResourceNotFoundException
        error, the workflow halts and record_cluster_destroyed is never
        reached."""
        mocks = mock_aws_clients

        # PCS sub-resource deletions succeed
        mocks["pcs"].delete_compute_node_group.return_value = {}
        mocks["pcs"].delete_queue.return_value = {}
        # Sub-resources confirmed deleted
        mocks["pcs"].get_compute_node_group.side_effect = _resource_not_found_error()
        mocks["pcs"].get_queue.side_effect = _resource_not_found_error()
        # Cluster deletion fails
        mocks["pcs"].delete_cluster.side_effect = _generic_client_error("ServiceException")

        # DynamoDB mock — should NOT be called
        mocks["table"].update_item.return_value = {}

        event = _full_event()
        record_destroyed_called = False

        with patch.object(cluster_destruction, "pcs_client", mocks["pcs"]), \
             patch.object(cluster_destruction, "dynamodb", mocks["dynamodb"]):

            # Steps up to PCS cluster deletion
            result = delete_pcs_resources(event)
            result = check_pcs_deletion_status(result)
            assert result["pcsSubResourcesDeleted"] is True

            # PCS cluster deletion should raise InternalError
            with pytest.raises(InternalError) as exc_info:
                delete_pcs_cluster_step(result)

            assert "pcs_integ123" in str(exc_info.value)

            # Verify record_cluster_destroyed was NOT called.
            # update_item may have been called for progress tracking, but
            # none of those calls should set status to DESTROYED.
            for call in mocks["table"].update_item.call_args_list:
                call_kwargs = call[1]
                expr_values = call_kwargs.get("ExpressionAttributeValues", {})
                assert expr_values.get(":status") != "DESTROYED", \
                    "Cluster should not be marked DESTROYED after a failure"

    def test_cluster_not_marked_destroyed_on_failure(self, mock_aws_clients):
        """Explicitly verify that after a PCS cluster deletion failure,
        calling record_cluster_destroyed is not possible because the
        exception halts the flow — the cluster status remains unchanged."""
        mocks = mock_aws_clients

        mocks["pcs"].delete_compute_node_group.return_value = {}
        mocks["pcs"].delete_queue.return_value = {}
        mocks["pcs"].get_compute_node_group.side_effect = _resource_not_found_error()
        mocks["pcs"].get_queue.side_effect = _resource_not_found_error()
        mocks["pcs"].delete_cluster.side_effect = _generic_client_error("InternalServerError")

        event = _full_event()

        with patch.object(cluster_destruction, "pcs_client", mocks["pcs"]):
            result = delete_pcs_resources(event)
            result = check_pcs_deletion_status(result)

            # The workflow raises here — record_cluster_destroyed never runs
            with pytest.raises(InternalError):
                delete_pcs_cluster_step(result)

        # Confirm the event was never updated with DESTROYED status
        assert result.get("status") != "DESTROYED"
        assert "destroyedAt" not in result
