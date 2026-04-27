# Feature: instance-profile-per-cluster, Property 2: Preservation
"""Property-based tests for instance profile per-cluster — preservation checks.

These tests verify behaviour that must remain UNCHANGED after the fix.
They all PASS on the current unfixed code, establishing a baseline.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**
"""

import os
import sys
from unittest.mock import MagicMock, patch, call

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Environment variables required by the cluster modules at import
# ---------------------------------------------------------------------------
os.environ.setdefault("CLUSTERS_TABLE_NAME", "Clusters")
os.environ.setdefault("CLUSTER_NAME_REGISTRY_TABLE_NAME", "ClusterNameRegistry")
os.environ.setdefault("PROJECTS_TABLE_NAME", "Projects")
os.environ.setdefault("USERS_TABLE_NAME", "PlatformUsers")

# ---------------------------------------------------------------------------
# Module loading — reuse conftest helpers
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import _CLUSTER_OPS_DIR, _load_module_from  # noqa: E402


def _load_cluster_creation_module():
    """Load cluster_creation and all its intra-package dependencies."""
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    return _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")


def _load_cluster_destruction_module():
    """Load cluster_destruction and its intra-package dependencies."""
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    return _load_module_from(_CLUSTER_OPS_DIR, "cluster_destruction")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)
_sg_id = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)
_subnet_id = st.from_regex(r"subnet-[0-9a-f]{8,17}", fullmatch=True)


# ---------------------------------------------------------------------------
# Test A — FSx creation preserved
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
    fsx_sg=_sg_id,
    subnet=_subnet_id,
)
@settings(max_examples=5, deadline=None)
def test_fsx_creation_preserved(project_id, cluster_name, fsx_sg, subnet):
    """For any valid event, create_fsx_filesystem passes the same
    FileSystemType, StorageCapacity, SubnetIds, SecurityGroupIds, and
    LustreConfiguration to the FSx API regardless of instance profile changes.

    **Validates: Requirements 3.1**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "privateSubnetIds": [subnet],
        "securityGroupIds": {"fsx": fsx_sg},
    }

    mod = _load_cluster_creation_module()

    mock_fsx = MagicMock()
    mock_fsx.create_file_system.return_value = {
        "FileSystem": {"FileSystemId": "fs-test-001"},
    }

    with patch.object(mod, "fsx_client", mock_fsx), \
         patch.object(mod, "_update_step_progress"):

        mod.create_fsx_filesystem(event)

    mock_fsx.create_file_system.assert_called_once()
    kw = mock_fsx.create_file_system.call_args.kwargs

    assert kw["FileSystemType"] == "LUSTRE"
    assert kw["StorageCapacity"] == 1200
    assert kw["SubnetIds"] == [subnet]
    assert kw["SecurityGroupIds"] == [fsx_sg]
    assert kw["LustreConfiguration"] == {
        "DeploymentType": "SCRATCH_2",
        "DataCompressionType": "LZ4",
    }


# ---------------------------------------------------------------------------
# Test B — PCS cluster creation preserved
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
    compute_sg=_sg_id,
    subnet=_subnet_id,
)
@settings(max_examples=5, deadline=None)
def test_pcs_cluster_creation_preserved(project_id, cluster_name, compute_sg, subnet):
    """For any valid event, create_pcs_cluster passes the same clusterName,
    scheduler, size, networking, slurmConfiguration, and tags to the PCS API.

    **Validates: Requirements 3.2**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "privateSubnetIds": [subnet],
        "securityGroupIds": {"computeNode": compute_sg},
    }

    mod = _load_cluster_creation_module()
    tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    expected_tags = tagging_mod.tags_as_dict(project_id, cluster_name)

    mock_pcs = MagicMock()
    mock_pcs.create_cluster.return_value = {
        "cluster": {
            "id": "pcs-test-001",
            "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-test-001",
        },
    }

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        mod.create_pcs_cluster(event)

    mock_pcs.create_cluster.assert_called_once()
    kw = mock_pcs.create_cluster.call_args.kwargs

    assert kw["clusterName"] == cluster_name
    assert kw["scheduler"] == {"type": "SLURM", "version": "24.11"}
    assert kw["size"] == "SMALL"
    assert kw["networking"]["subnetIds"] == [subnet]
    assert kw["networking"]["securityGroupIds"] == [compute_sg]
    assert kw["slurmConfiguration"] == {
        "slurmCustomSettings": [],
        "scaleDownIdleTimeInSeconds": 600,
    }
    assert kw["tags"] == expected_tags


# ---------------------------------------------------------------------------
# Test C — PCS queue creation preserved
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
)
@settings(max_examples=5, deadline=None)
def test_pcs_queue_creation_preserved(project_id, cluster_name):
    """For any valid event, create_pcs_queue passes the same clusterIdentifier,
    queueName, computeNodeGroupConfigurations, and tags to the PCS API.

    **Validates: Requirements 3.2**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": "pcs-test-001",
        "computeNodeGroupId": "cng-test-001",
    }

    mod = _load_cluster_creation_module()
    tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    expected_tags = tagging_mod.tags_as_dict(project_id, cluster_name)

    mock_pcs = MagicMock()
    mock_pcs.create_queue.return_value = {
        "queue": {"id": "q-test-001"},
    }

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        mod.create_pcs_queue(event)

    mock_pcs.create_queue.assert_called_once()
    kw = mock_pcs.create_queue.call_args.kwargs

    assert kw["clusterIdentifier"] == "pcs-test-001"
    assert kw["queueName"] == f"{cluster_name}-queue"
    assert kw["computeNodeGroupConfigurations"] == [
        {"computeNodeGroupId": "cng-test-001"},
    ]
    assert kw["tags"] == expected_tags


# ---------------------------------------------------------------------------
# Test D — Non-IAM node group params preserved
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
    pub_subnet=_subnet_id,
    priv_subnet=_subnet_id,
)
@settings(max_examples=5, deadline=None)
def test_non_iam_node_group_params_preserved(
    project_id, cluster_name, pub_subnet, priv_subnet
):
    """For any valid event, create_login_node_group and create_compute_node_group
    pass the same subnetIds, purchaseOption, scalingConfiguration, instanceConfigs,
    customLaunchTemplate, and tags to the PCS API (everything except
    iamInstanceProfileArn).

    **Validates: Requirements 3.4**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": "pcs-test-001",
        "publicSubnetIds": [pub_subnet],
        "privateSubnetIds": [priv_subnet],
        "securityGroupIds": {
            "headNode": "sg-head-001",
            "computeNode": "sg-compute-001",
        },
        "instanceProfileArn": f"arn:aws:iam::123456789012:instance-profile/AWSPCS-{project_id}-node",
        "loginLaunchTemplateId": "lt-login-001",
        "loginLaunchTemplateVersion": "$Default",
        "computeLaunchTemplateId": "lt-compute-001",
        "computeLaunchTemplateVersion": "$Default",
        "instanceTypes": ["c7g.medium"],
        "maxNodes": 10,
        "minNodes": 0,
        "purchaseOption": "ONDEMAND",
    }

    mod = _load_cluster_creation_module()
    tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    expected_tags = tagging_mod.tags_as_dict(project_id, cluster_name)

    mock_pcs = MagicMock()
    mock_pcs.create_compute_node_group.return_value = {
        "computeNodeGroup": {"id": "cng-login-001"},
    }

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"), \
         patch.object(mod, "generate_user_data_script", return_value="#!/bin/bash\n"):

        login_result = mod.create_login_node_group(event)

    # Verify login node group non-IAM params
    login_kw = mock_pcs.create_compute_node_group.call_args.kwargs

    assert login_kw["subnetIds"] == [pub_subnet]
    assert login_kw["purchaseOption"] == "ONDEMAND"
    assert login_kw["scalingConfiguration"] == {
        "minInstanceCount": 1,
        "maxInstanceCount": 1,
    }
    assert login_kw["instanceConfigs"] == [{"instanceType": "c7g.medium"}]
    assert login_kw["customLaunchTemplate"] == {
        "id": "lt-login-001",
        "version": "$Default",
    }
    assert login_kw["tags"] == expected_tags

    # Now test compute node group
    mock_pcs.reset_mock()
    mock_pcs.create_compute_node_group.return_value = {
        "computeNodeGroup": {"id": "cng-compute-001"},
    }

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"), \
         patch.object(mod, "generate_user_data_script", return_value="#!/bin/bash\n"):

        mod.create_compute_node_group(login_result)

    compute_kw = mock_pcs.create_compute_node_group.call_args.kwargs

    assert compute_kw["subnetIds"] == [priv_subnet]
    assert compute_kw["purchaseOption"] == "ONDEMAND"
    assert compute_kw["scalingConfiguration"] == {
        "minInstanceCount": 0,
        "maxInstanceCount": 10,
    }
    assert compute_kw["instanceConfigs"] == [{"instanceType": "c7g.medium"}]
    assert compute_kw["customLaunchTemplate"] == {
        "id": "lt-compute-001",
        "version": "$Default",
    }
    assert compute_kw["tags"] == expected_tags


# ---------------------------------------------------------------------------
# Test E — Cluster destruction PCS cleanup preserved
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
)
@settings(max_examples=5, deadline=None)
def test_destruction_pcs_cleanup_preserved(project_id, cluster_name):
    """For any valid destruction event, delete_pcs_resources calls
    delete_compute_node_group, delete_queue, and delete_cluster in the
    same order with the same arguments.

    **Validates: Requirements 3.3**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": "pcs-test-001",
        "computeNodeGroupId": "cng-compute-001",
        "loginNodeGroupId": "cng-login-001",
        "queueId": "q-test-001",
    }

    mod = _load_cluster_destruction_module()

    mock_pcs = MagicMock()

    with patch.object(mod, "pcs_client", mock_pcs):
        mod.delete_pcs_resources(event)

    # Verify the order: compute node group, login node group, queue, cluster
    expected_calls = [
        call.delete_compute_node_group(
            clusterIdentifier="pcs-test-001",
            computeNodeGroupIdentifier="cng-compute-001",
        ),
        call.delete_compute_node_group(
            clusterIdentifier="pcs-test-001",
            computeNodeGroupIdentifier="cng-login-001",
        ),
        call.delete_queue(
            clusterIdentifier="pcs-test-001",
            queueIdentifier="q-test-001",
        ),
        call.delete_cluster(
            clusterIdentifier="pcs-test-001",
        ),
    ]

    assert mock_pcs.method_calls == expected_calls, (
        f"Expected PCS cleanup calls in order:\n{expected_calls}\n"
        f"Got:\n{mock_pcs.method_calls}"
    )


# ---------------------------------------------------------------------------
# Test F — DynamoDB record_cluster_destroyed preserved
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
)
@settings(max_examples=5, deadline=None)
def test_record_cluster_destroyed_preserved(project_id, cluster_name):
    """For any valid destruction event, record_cluster_destroyed sets status
    to DESTROYED and writes destroyedAt.

    **Validates: Requirements 3.3**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
    }

    mod = _load_cluster_destruction_module()

    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with patch.object(mod, "dynamodb", mock_dynamodb):
        result = mod.record_cluster_destroyed(event)

    # Verify status is DESTROYED
    assert result["status"] == "DESTROYED"
    # Verify destroyedAt is present and non-empty
    assert "destroyedAt" in result
    assert result["destroyedAt"] != ""

    # Verify DynamoDB update_item was called correctly
    mock_table.update_item.assert_called_once()
    update_kw = mock_table.update_item.call_args.kwargs

    assert update_kw["Key"] == {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
    }
    assert ":status" in update_kw["ExpressionAttributeValues"]
    assert update_kw["ExpressionAttributeValues"][":status"] == "DESTROYED"
    assert ":ts" in update_kw["ExpressionAttributeValues"]
    assert update_kw["ExpressionAttributeValues"][":ts"] != ""
