# Feature: cluster-scoped-launch-templates, Property 3: Rollback cleanup naming
"""Property-based tests for rollback launch template cleanup naming.

For any valid projectId and clusterName, verify that ``handle_creation_failure``
attempts to delete templates named ``hpc-{projectId}-{clusterName}-login`` and
``hpc-{projectId}-{clusterName}-compute``.

**Validates: Requirements 6.1**
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


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 3 — Rollback cleanup targets correctly named launch templates
# ---------------------------------------------------------------------------


@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=100, deadline=None)
def test_rollback_cleanup_targets_correctly_named_launch_templates(
    project_id, cluster_name
):
    """For any valid projectId and clusterName, handle_creation_failure
    attempts to delete templates named hpc-{projectId}-{clusterName}-login
    and hpc-{projectId}-{clusterName}-compute.

    **Validates: Requirements 6.1**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": "",
        "fsxFilesystemId": "",
        "queueId": "",
        "loginNodeGroupId": "",
        "computeNodeGroupId": "",
    }

    mod = _load_cluster_creation_module()

    mock_ec2 = MagicMock()
    mock_ec2.describe_launch_templates.return_value = {
        "LaunchTemplates": [{"LaunchTemplateId": "lt-mock-001"}],
    }

    mock_iam = MagicMock()
    mock_iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    mock_iam.list_instance_profiles_for_role.return_value = {
        "InstanceProfiles": [],
    }

    mock_dynamodb = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with (
        patch.object(mod, "ec2_client", mock_ec2),
        patch.object(mod, "iam_client", mock_iam),
        patch.object(mod, "pcs_client", MagicMock()),
        patch.object(mod, "fsx_client", MagicMock()),
        patch.object(mod, "dynamodb", mock_dynamodb),
        patch.object(mod, "sns_client", MagicMock()),
        patch.object(mod, "_lookup_user_email", return_value=""),
    ):
        result = mod.handle_creation_failure(event)

    # ec2_client.describe_launch_templates must be called exactly twice
    # (once for login, once for compute)
    assert mock_ec2.describe_launch_templates.call_count == 2

    expected_login = f"hpc-{project_id}-{cluster_name}-login"
    expected_compute = f"hpc-{project_id}-{cluster_name}-compute"

    describe_calls = mock_ec2.describe_launch_templates.call_args_list
    assert describe_calls[0].kwargs["LaunchTemplateNames"] == [expected_login]
    assert describe_calls[1].kwargs["LaunchTemplateNames"] == [expected_compute]

    # Verify delete was called for each resolved template
    assert mock_ec2.delete_launch_template.call_count == 2
    delete_calls = mock_ec2.delete_launch_template.call_args_list
    assert delete_calls[0].kwargs["LaunchTemplateId"] == "lt-mock-001"
    assert delete_calls[1].kwargs["LaunchTemplateId"] == "lt-mock-001"

    # Result indicates FAILED status
    assert result["status"] == "FAILED"
