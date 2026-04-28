# Feature: cluster-scoped-launch-templates, Property 2: Launch template destruction naming
"""Property-based tests for launch template destruction naming.

For any valid projectId and clusterName, verify that ``delete_launch_templates``
attempts to delete templates named ``hpc-{projectId}-{clusterName}-login`` and
``hpc-{projectId}-{clusterName}-compute``.

**Validates: Requirements 5.1, 5.2**
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


def _load_cluster_destruction_module():
    """Load cluster_destruction and all its intra-package dependencies."""
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    return _load_module_from(_CLUSTER_OPS_DIR, "cluster_destruction")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 2 — Launch template destruction targets correctly named templates
# ---------------------------------------------------------------------------


@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=100, deadline=None)
def test_launch_template_destruction_naming(project_id, cluster_name):
    """For any valid projectId and clusterName, delete_launch_templates
    attempts to delete templates named hpc-{projectId}-{clusterName}-login
    and hpc-{projectId}-{clusterName}-compute.

    **Validates: Requirements 5.1, 5.2**
    """
    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
    }

    mod = _load_cluster_destruction_module()

    mock_ec2 = MagicMock()
    mock_ec2.describe_launch_templates.return_value = {
        "LaunchTemplates": [{"LaunchTemplateId": "lt-mock-001"}],
    }

    with patch.object(mod, "ec2_client", mock_ec2):
        result = mod.delete_launch_templates(event)

    # Must have called describe_launch_templates exactly twice
    assert mock_ec2.describe_launch_templates.call_count == 2

    expected_login = f"hpc-{project_id}-{cluster_name}-login"
    expected_compute = f"hpc-{project_id}-{cluster_name}-compute"

    # Verify the describe calls used the correct template names
    describe_calls = mock_ec2.describe_launch_templates.call_args_list
    assert describe_calls[0].kwargs["LaunchTemplateNames"] == [expected_login]
    assert describe_calls[1].kwargs["LaunchTemplateNames"] == [expected_compute]

    # Verify delete was called for each resolved template
    assert mock_ec2.delete_launch_template.call_count == 2
    delete_calls = mock_ec2.delete_launch_template.call_args_list
    assert delete_calls[0].kwargs["LaunchTemplateId"] == "lt-mock-001"
    assert delete_calls[1].kwargs["LaunchTemplateId"] == "lt-mock-001"

    # Result includes cleanup results
    assert "launchTemplateCleanupResults" in result
