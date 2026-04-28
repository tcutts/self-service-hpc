# Feature: cluster-scoped-launch-templates, Property 1: Launch template creation naming and configuration
"""Property-based tests for launch template creation naming and configuration.

For any valid projectId and clusterName, verify that ``create_launch_templates``
calls ``ec2_client.create_launch_template`` with the correct names, security
groups, and Project/ClusterName tags.

**Validates: Requirements 4.1, 4.2, 4.5**
"""

import os
import sys
from unittest.mock import MagicMock, patch

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

_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)
_sg_id = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 1 — Launch template creation naming and configuration
# ---------------------------------------------------------------------------


@given(
    project_id=_project_id,
    cluster_name=_cluster_name,
    head_sg=_sg_id,
    compute_sg=_sg_id,
)
@settings(max_examples=100, deadline=None)
def test_launch_template_creation_naming_and_configuration(
    project_id, cluster_name, head_sg, compute_sg
):
    """For any valid projectId and clusterName, create_launch_templates calls
    ec2_client.create_launch_template with correct names
    (hpc-{projectId}-{clusterName}-login and hpc-{projectId}-{clusterName}-compute),
    correct security groups (headNode and computeNode), and correct
    Project/ClusterName tags.

    **Validates: Requirements 4.1, 4.2, 4.5**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "securityGroupIds": {
            "headNode": head_sg,
            "computeNode": compute_sg,
        },
    }

    mod = _load_cluster_creation_module()
    tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    expected_tags = tagging_mod.build_resource_tags(project_id, cluster_name)

    mock_ec2 = MagicMock()
    mock_ec2.create_launch_template.return_value = {
        "LaunchTemplate": {"LaunchTemplateId": "lt-mock-001"},
    }

    with patch.object(mod, "ec2_client", mock_ec2):
        result = mod.create_launch_templates(event)

    # Must have been called exactly twice (login + compute)
    assert mock_ec2.create_launch_template.call_count == 2

    calls = mock_ec2.create_launch_template.call_args_list

    # --- Login template (first call) ---
    login_kw = calls[0].kwargs
    assert login_kw["LaunchTemplateName"] == f"hpc-{project_id}-{cluster_name}-login"
    assert login_kw["LaunchTemplateData"]["SecurityGroupIds"] == [head_sg]
    assert login_kw["TagSpecifications"] == [
        {"ResourceType": "launch-template", "Tags": expected_tags},
    ]

    # --- Compute template (second call) ---
    compute_kw = calls[1].kwargs
    assert compute_kw["LaunchTemplateName"] == f"hpc-{project_id}-{cluster_name}-compute"
    assert compute_kw["LaunchTemplateData"]["SecurityGroupIds"] == [compute_sg]
    assert compute_kw["TagSpecifications"] == [
        {"ResourceType": "launch-template", "Tags": expected_tags},
    ]

    # --- Result contains both template IDs ---
    assert result["loginLaunchTemplateId"] == "lt-mock-001"
    assert result["computeLaunchTemplateId"] == "lt-mock-001"
