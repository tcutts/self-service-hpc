# Feature: pcs-single-subnet-fix, Property 2: Preservation
"""Property-based tests for PCS single-subnet fix — preservation checks.

These tests verify behaviour that must remain UNCHANGED after the fix.
They all PASS on the current unfixed code, establishing a baseline.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**
"""

import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Environment variables required by the cluster_creation module at import
# ---------------------------------------------------------------------------
os.environ.setdefault("CLUSTERS_TABLE_NAME", "Clusters")
os.environ.setdefault("CLUSTER_NAME_REGISTRY_TABLE_NAME", "ClusterNameRegistry")
os.environ.setdefault("PROJECTS_TABLE_NAME", "Projects")
os.environ.setdefault("USERS_TABLE_NAME", "PlatformUsers")

# ---------------------------------------------------------------------------
# Module loading — reuse conftest helpers
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import _CLUSTER_OPS_DIR, _load_module_from, _ensure_shared_modules  # noqa: E402


def _load_cluster_creation_module():
    """Load cluster_creation and all its intra-package dependencies."""
    _ensure_shared_modules()
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    return _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_subnet_id = st.from_regex(r"subnet-[0-9a-f]{8,17}", fullmatch=True)
_single_subnet_list = st.lists(_subnet_id, min_size=1, max_size=1)
_multi_subnet_list = st.lists(_subnet_id, min_size=2, max_size=4, unique=True)
_any_subnet_list = st.lists(_subnet_id, min_size=1, max_size=4, unique=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)
_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_sg_id = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)


# ---------------------------------------------------------------------------
# Test A: Single-subnet events — create_pcs_cluster passes subnet correctly
# ---------------------------------------------------------------------------


@given(
    subnets=_single_subnet_list,
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
)
@settings(max_examples=10, deadline=None)
def test_create_pcs_cluster_single_subnet_passthrough(
    subnets, cluster_name, project_id, sg_id
):
    """For single-subnet events (non-bug-condition), create_pcs_cluster
    passes the single subnet correctly to networking.subnetIds.

    **Validates: Requirements 3.1**
    """
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "privateSubnetIds": subnets,
        "securityGroupIds": {"computeNode": sg_id},
    }

    fake_response = {
        "cluster": {
            "id": "pcs-123",
            "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123",
        }
    }

    mod = _load_cluster_creation_module()

    mock_pcs = MagicMock()
    mock_pcs.create_cluster.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_pcs_cluster(event)

    mock_pcs.create_cluster.assert_called_once()
    call_kwargs = mock_pcs.create_cluster.call_args
    actual_subnet_ids = call_kwargs.kwargs["networking"]["subnetIds"]

    # Single-subnet event: the single subnet is passed through
    assert actual_subnet_ids == subnets, (
        f"Expected {subnets}, got {actual_subnet_ids}"
    )

    assert result["pcsClusterId"] == "pcs-123"
    assert result["pcsClusterArn"] == "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123"


# ---------------------------------------------------------------------------
# Test B: Non-subnet parameters passed identically to create_cluster
# ---------------------------------------------------------------------------


@given(
    subnets=_any_subnet_list,
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
)
@settings(max_examples=10, deadline=None)
def test_create_pcs_cluster_non_subnet_params_unchanged(
    subnets, cluster_name, project_id, sg_id
):
    """For all events, non-subnet parameters (clusterName, scheduler, size,
    securityGroupIds, slurmConfiguration, tags) are passed identically
    to create_cluster.

    **Validates: Requirements 3.5, 3.6**
    """
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "privateSubnetIds": subnets,
        "securityGroupIds": {"computeNode": sg_id},
    }

    fake_response = {
        "cluster": {
            "id": "pcs-456",
            "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-456",
        }
    }

    mod = _load_cluster_creation_module()

    # Load tagging module to compute expected tags
    tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    expected_tags = tagging_mod.tags_as_dict(project_id, cluster_name)

    mock_pcs = MagicMock()
    mock_pcs.create_cluster.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        mod.create_pcs_cluster(event)

    mock_pcs.create_cluster.assert_called_once()
    call_kwargs = mock_pcs.create_cluster.call_args.kwargs

    # clusterName
    assert call_kwargs["clusterName"] == cluster_name, (
        f"Expected clusterName={cluster_name}, got {call_kwargs['clusterName']}"
    )

    # scheduler
    assert call_kwargs["scheduler"] == {"type": "SLURM", "version": "24.11"}, (
        f"Unexpected scheduler: {call_kwargs['scheduler']}"
    )

    # size
    assert call_kwargs["size"] == "SMALL", (
        f"Expected size='SMALL', got {call_kwargs['size']}"
    )

    # securityGroupIds inside networking
    assert call_kwargs["networking"]["securityGroupIds"] == [sg_id], (
        f"Expected securityGroupIds=[{sg_id}], "
        f"got {call_kwargs['networking']['securityGroupIds']}"
    )

    # slurmConfiguration
    expected_slurm = {
        "slurmCustomSettings": [],
        "scaleDownIdleTimeInSeconds": 600,
    }
    assert call_kwargs["slurmConfiguration"] == expected_slurm, (
        f"Unexpected slurmConfiguration: {call_kwargs['slurmConfiguration']}"
    )

    # tags
    assert call_kwargs["tags"] == expected_tags, (
        f"Expected tags={expected_tags}, got {call_kwargs['tags']}"
    )


# ---------------------------------------------------------------------------
# Test C: create_compute_node_group passes full subnet list
# ---------------------------------------------------------------------------


@given(
    subnets=_multi_subnet_list,
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
)
@settings(max_examples=10, deadline=None)
def test_create_compute_node_group_passes_all_subnets(
    subnets, cluster_name, project_id, sg_id
):
    """For all events, create_compute_node_group passes the full
    private_subnet_ids list to subnetIds.

    **Validates: Requirements 3.2**
    """
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "pcsClusterId": "pcs-existing-123",
        "privateSubnetIds": subnets,
        "securityGroupIds": {"computeNode": sg_id},
        "instanceTypes": ["c7g.medium"],
        "maxNodes": 10,
        "minNodes": 0,
        "purchaseOption": "ONDEMAND",
        "computeLaunchTemplateId": "lt-abc123",
        "computeLaunchTemplateVersion": "$Default",
        "instanceProfileArn": "arn:aws:iam::123456789012:instance-profile/test",
    }

    fake_response = {
        "computeNodeGroup": {"id": "cng-123"},
    }

    mod = _load_cluster_creation_module()

    mock_pcs = MagicMock()
    mock_pcs.create_compute_node_group.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_compute_node_group(event)

    mock_pcs.create_compute_node_group.assert_called_once()
    call_kwargs = mock_pcs.create_compute_node_group.call_args.kwargs

    assert call_kwargs["subnetIds"] == subnets, (
        f"Expected all subnets {subnets}, got {call_kwargs['subnetIds']}"
    )

    assert result["computeNodeGroupId"] == "cng-123"


# ---------------------------------------------------------------------------
# Test D: create_fsx_filesystem passes only first subnet
# ---------------------------------------------------------------------------


@given(
    subnets=_multi_subnet_list,
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
)
@settings(max_examples=10, deadline=None)
def test_create_fsx_filesystem_passes_first_subnet_only(
    subnets, cluster_name, project_id, sg_id
):
    """For all events, create_fsx_filesystem passes [private_subnet_ids[0]]
    to SubnetIds.

    **Validates: Requirements 3.4**
    """
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "privateSubnetIds": subnets,
        "securityGroupIds": {"fsx": sg_id},
    }

    fake_response = {
        "FileSystem": {"FileSystemId": "fs-123"},
    }

    mod = _load_cluster_creation_module()

    mock_fsx = MagicMock()
    mock_fsx.create_file_system.return_value = fake_response

    with patch.object(mod, "fsx_client", mock_fsx), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_fsx_filesystem(event)

    mock_fsx.create_file_system.assert_called_once()
    call_kwargs = mock_fsx.create_file_system.call_args.kwargs

    # FSx receives only the first subnet as a single-element list
    assert call_kwargs["SubnetIds"] == [subnets[0]], (
        f"Expected SubnetIds=[{subnets[0]}], got {call_kwargs['SubnetIds']}"
    )

    assert result["fsxFilesystemId"] == "fs-123"
