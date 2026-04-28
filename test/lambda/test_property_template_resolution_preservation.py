# Feature: cluster-template-resolution, Property 2: Preservation
"""Property-based tests for cluster template resolution — preservation checks.

These tests verify behaviour that must remain UNCHANGED after the fix.
They all PASS on the current unfixed code, establishing a baseline.

Observations on UNFIXED code:
- ``create_pcs_cluster`` adds only ``pcsClusterId`` and ``pcsClusterArn``
  to the event, preserving all other keys.
- ``create_compute_node_group`` uses ``event.get("instanceTypes", ["c7g.medium"])``,
  ``event.get("maxNodes", 10)``, ``event.get("minNodes", 0)``,
  ``event.get("purchaseOption", "ONDEMAND")`` defaults when fields are absent.
- ``create_login_node_group`` uses ``event.get("loginInstanceType", "c7g.medium")``
  default when field is absent.
- ``_STEP_DISPATCH`` contains all 12 existing step names.

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
os.environ.setdefault("TEMPLATES_TABLE_NAME", "ClusterTemplates")

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

_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)
_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_sg_id = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)
_subnet_id = st.from_regex(r"subnet-[0-9a-f]{8,17}", fullmatch=True)

# Extra arbitrary event keys to verify preservation
_extra_key = st.from_regex(r"extra[A-Z][a-zA-Z]{2,10}", fullmatch=True)
_extra_value = st.text(min_size=1, max_size=20)


# ---------------------------------------------------------------------------
# The 12 existing step names that must always be present in _STEP_DISPATCH
# ---------------------------------------------------------------------------
_EXPECTED_STEP_NAMES = {
    "validate_and_register_name",
    "check_budget_breach",
    "create_fsx_filesystem",
    "check_fsx_status",
    "create_fsx_dra",
    "create_pcs_cluster",
    "create_login_node_group",
    "create_compute_node_group",
    "create_pcs_queue",
    "tag_resources",
    "record_cluster",
    "handle_creation_failure",
}


# ---------------------------------------------------------------------------
# Test A: create_pcs_cluster preserves all original event keys and only
#         adds pcsClusterId and pcsClusterArn
# ---------------------------------------------------------------------------


@given(
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
    subnet_id=_subnet_id,
    extra_key=_extra_key,
    extra_value=_extra_value,
)
@settings(max_examples=10, deadline=None)
def test_create_pcs_cluster_preserves_event_keys(
    cluster_name, project_id, sg_id, subnet_id, extra_key, extra_value
):
    """For any valid event, create_pcs_cluster preserves all original event
    keys and only adds pcsClusterId and pcsClusterArn.

    **Validates: Requirements 3.1, 3.4**
    """
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "privateSubnetIds": [subnet_id],
        "securityGroupIds": {"computeNode": sg_id},
        extra_key: extra_value,
    }
    original_keys = set(event.keys())

    fake_response = {
        "cluster": {
            "id": "pcs-preserve-001",
            "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-preserve-001",
        }
    }

    mod = _load_cluster_creation_module()

    mock_pcs = MagicMock()
    mock_pcs.create_cluster.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_pcs_cluster(event)

    # All original keys must still be present
    for key in original_keys:
        assert key in result, (
            f"Original event key '{key}' was lost after create_pcs_cluster — "
            f"result keys: {sorted(result.keys())}"
        )

    # Only pcsClusterId and pcsClusterArn should be added
    new_keys = set(result.keys()) - original_keys
    assert new_keys == {"pcsClusterId", "pcsClusterArn"}, (
        f"Expected only pcsClusterId and pcsClusterArn to be added, "
        f"but got new keys: {new_keys}"
    )

    # Values must match the fake response
    assert result["pcsClusterId"] == "pcs-preserve-001"
    assert result["pcsClusterArn"] == "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-preserve-001"


# ---------------------------------------------------------------------------
# Test B: create_compute_node_group uses expected defaults when template
#         fields are absent from the event
# ---------------------------------------------------------------------------


@given(
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
    subnet_id=_subnet_id,
)
@settings(max_examples=10, deadline=None)
def test_create_compute_node_group_uses_defaults_when_template_fields_absent(
    cluster_name, project_id, sg_id, subnet_id
):
    """For any event without template fields, create_compute_node_group uses
    the expected defaults: instanceTypes=["c7g.medium"], maxNodes=10,
    minNodes=0, purchaseOption="ONDEMAND".

    **Validates: Requirements 3.6**
    """
    # Event WITHOUT template-driven fields — should trigger defaults
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "pcsClusterId": "pcs-existing-001",
        "privateSubnetIds": [subnet_id],
        "securityGroupIds": {"computeNode": sg_id},
    }

    fake_response = {
        "computeNodeGroup": {"id": "cng-default-001"},
    }

    mod = _load_cluster_creation_module()

    mock_pcs = MagicMock()
    mock_pcs.create_compute_node_group.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_compute_node_group(event)

    mock_pcs.create_compute_node_group.assert_called_once()
    call_kwargs = mock_pcs.create_compute_node_group.call_args.kwargs

    # Verify default instanceTypes → instanceConfigs
    assert call_kwargs["instanceConfigs"] == [{"instanceType": "c7g.medium"}], (
        f"Expected default instanceConfigs=[{{'instanceType': 'c7g.medium'}}], "
        f"got {call_kwargs['instanceConfigs']}"
    )

    # Verify default purchaseOption
    assert call_kwargs["purchaseOption"] == "ONDEMAND", (
        f"Expected default purchaseOption='ONDEMAND', "
        f"got {call_kwargs['purchaseOption']}"
    )

    # Verify default scaling configuration (maxNodes=10, minNodes=0)
    assert call_kwargs["scalingConfiguration"] == {
        "minInstanceCount": 0,
        "maxInstanceCount": 10,
    }, (
        f"Expected default scaling {{minInstanceCount: 0, maxInstanceCount: 10}}, "
        f"got {call_kwargs['scalingConfiguration']}"
    )

    # Verify the function still returns the expected key
    assert result["computeNodeGroupId"] == "cng-default-001"


# ---------------------------------------------------------------------------
# Test C: _STEP_DISPATCH contains all 12 existing step names
# ---------------------------------------------------------------------------


def test_step_dispatch_contains_all_existing_steps():
    """_STEP_DISPATCH must contain all 12 existing step names.

    This is a non-property assertion that verifies the dispatch table
    has not lost any entries after the fix.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.5**
    """
    mod = _load_cluster_creation_module()

    actual_steps = set(mod._STEP_DISPATCH.keys())

    for step_name in _EXPECTED_STEP_NAMES:
        assert step_name in actual_steps, (
            f"_STEP_DISPATCH is missing existing step '{step_name}' — "
            f"actual keys: {sorted(actual_steps)}"
        )
