# Feature: instance-profile-per-cluster, Property 1: Bug Condition
"""Property-based test for instance profile per-cluster bug condition.

Demonstrates that ``create_login_node_group`` and ``create_compute_node_group``
both use the same project-level ``instanceProfileArn`` instead of distinct,
cluster-scoped profiles.  The test encodes the EXPECTED (correct) behaviour —
it will FAIL on unfixed code (confirming the bug) and PASS after the fix.

**Validates: Requirements 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4**
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

_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_cluster_creation_event(project_id: str, cluster_name: str) -> dict:
    """Build a minimal event dict that satisfies create_login_node_group
    and create_compute_node_group.

    Simulates the event AFTER create_iam_resources has run, which adds
    loginInstanceProfileArn and computeInstanceProfileArn to the event.
    The old project-level instanceProfileArn is kept to verify the fixed
    code ignores it.
    """
    return {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": "pcs-test-123",
        "publicSubnetIds": ["subnet-pub-001"],
        "privateSubnetIds": ["subnet-priv-001"],
        "securityGroupIds": {
            "headNode": "sg-head-001",
            "computeNode": "sg-compute-001",
        },
        # The old project-level instance profile ARN (the bug) — should be ignored by fixed code
        "instanceProfileArn": f"arn:aws:iam::123456789012:instance-profile/AWSPCS-{project_id}-node",
        # Per-cluster instance profile ARNs added by create_iam_resources
        "loginInstanceProfileArn": f"arn:aws:iam::123456789012:instance-profile/AWSPCS-{project_id}-{cluster_name}-login",
        "computeInstanceProfileArn": f"arn:aws:iam::123456789012:instance-profile/AWSPCS-{project_id}-{cluster_name}-compute",
        "loginLaunchTemplateId": "lt-login-001",
        "loginLaunchTemplateVersion": "$Default",
        "computeLaunchTemplateId": "lt-compute-001",
        "computeLaunchTemplateVersion": "$Default",
        "instanceTypes": ["c7g.medium"],
        "maxNodes": 10,
        "minNodes": 0,
        "purchaseOption": "ONDEMAND",
    }


# ---------------------------------------------------------------------------
# Test A — Login and compute get distinct profiles
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=5, deadline=None)
def test_login_and_compute_get_distinct_profiles(project_id, cluster_name):
    """For any cluster creation event, create_login_node_group SHALL pass
    a different iamInstanceProfileArn than create_compute_node_group.

    On unfixed code both read event.get("instanceProfileArn", "") so they
    receive the same value — test FAILS.

    **Validates: Requirements 1.2, 1.3, 2.3, 2.4**
    """
    event = _build_cluster_creation_event(project_id, cluster_name)

    mod = _load_cluster_creation_module()

    mock_pcs = MagicMock()
    mock_pcs.create_compute_node_group.return_value = {
        "computeNodeGroup": {"id": "cng-login-001"},
    }

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"), \
         patch.object(mod, "generate_user_data_script", return_value="#!/bin/bash\n"):

        # Call create_login_node_group
        login_result = mod.create_login_node_group(event)

        # Reset mock to capture compute call separately
        login_call_kwargs = mock_pcs.create_compute_node_group.call_args
        login_profile_arn = login_call_kwargs.kwargs["iamInstanceProfileArn"]

        mock_pcs.reset_mock()
        mock_pcs.create_compute_node_group.return_value = {
            "computeNodeGroup": {"id": "cng-compute-001"},
        }

        # Call create_compute_node_group
        compute_result = mod.create_compute_node_group(login_result)

        compute_call_kwargs = mock_pcs.create_compute_node_group.call_args
        compute_profile_arn = compute_call_kwargs.kwargs["iamInstanceProfileArn"]

    # The EXPECTED behaviour: login and compute get DIFFERENT profiles
    assert login_profile_arn != compute_profile_arn, (
        f"Login and compute node groups received the SAME instance profile ARN: "
        f"login='{login_profile_arn}', compute='{compute_profile_arn}'. "
        f"Each should have a distinct, cluster-scoped profile."
    )


# ---------------------------------------------------------------------------
# Test B — Profile ARNs contain cluster name
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=5, deadline=None)
def test_profile_arns_contain_cluster_name(project_id, cluster_name):
    """For any cluster creation event, the iamInstanceProfileArn passed to
    create_login_node_group SHALL contain the cluster name and end with
    '-login', and the one passed to create_compute_node_group SHALL contain
    the cluster name and end with '-compute'.

    On unfixed code the ARN is AWSPCS-{projectId}-node — test FAILS.

    **Validates: Requirements 2.1, 2.2**
    """
    event = _build_cluster_creation_event(project_id, cluster_name)

    mod = _load_cluster_creation_module()

    mock_pcs = MagicMock()
    mock_pcs.create_compute_node_group.return_value = {
        "computeNodeGroup": {"id": "cng-login-001"},
    }

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"), \
         patch.object(mod, "generate_user_data_script", return_value="#!/bin/bash\n"):

        # Call create_login_node_group
        login_result = mod.create_login_node_group(event)

        login_call_kwargs = mock_pcs.create_compute_node_group.call_args
        login_profile_arn = login_call_kwargs.kwargs["iamInstanceProfileArn"]

        mock_pcs.reset_mock()
        mock_pcs.create_compute_node_group.return_value = {
            "computeNodeGroup": {"id": "cng-compute-001"},
        }

        # Call create_compute_node_group
        compute_result = mod.create_compute_node_group(login_result)

        compute_call_kwargs = mock_pcs.create_compute_node_group.call_args
        compute_profile_arn = compute_call_kwargs.kwargs["iamInstanceProfileArn"]

    # Login profile ARN should contain cluster name and end with -login
    assert cluster_name in login_profile_arn, (
        f"Login profile ARN '{login_profile_arn}' does not contain "
        f"cluster name '{cluster_name}'"
    )
    assert login_profile_arn.endswith("-login"), (
        f"Login profile ARN '{login_profile_arn}' does not end with '-login'"
    )

    # Compute profile ARN should contain cluster name and end with -compute
    assert cluster_name in compute_profile_arn, (
        f"Compute profile ARN '{compute_profile_arn}' does not contain "
        f"cluster name '{cluster_name}'"
    )
    assert compute_profile_arn.endswith("-compute"), (
        f"Compute profile ARN '{compute_profile_arn}' does not end with '-compute'"
    )
