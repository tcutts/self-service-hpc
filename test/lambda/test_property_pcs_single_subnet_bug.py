# Feature: pcs-single-subnet-fix, Property 1: Bug Condition
"""Property-based test for PCS single-subnet bug condition.

Demonstrates that ``create_pcs_cluster`` passes the FULL
``private_subnet_ids`` list to the PCS ``CreateCluster`` API instead of
slicing it to a single element.  The test encodes the EXPECTED (correct)
behaviour — it will FAIL on unfixed code (confirming the bug) and PASS
after the fix is applied.

**Validates: Requirements 1.1, 1.2, 2.1, 2.2**
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

# Subnet IDs: realistic format "subnet-<hex>"
_subnet_id = st.from_regex(r"subnet-[0-9a-f]{8,17}", fullmatch=True)

# Lists of 2–4 subnets (bug condition: more than one subnet)
_multi_subnet_list = st.lists(_subnet_id, min_size=2, max_size=4, unique=True)

# Minimal valid event fields needed by create_pcs_cluster
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)
_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_sg_id = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 1: Bug Condition — CreateCluster receives exactly one subnet
# ---------------------------------------------------------------------------


@given(
    subnets=_multi_subnet_list,
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
)
@settings(max_examples=10, deadline=None)
def test_create_pcs_cluster_passes_single_subnet(
    subnets, cluster_name, project_id, sg_id
):
    """For any event where privateSubnetIds has >1 subnet,
    create_pcs_cluster SHALL pass exactly one subnet (the first) to the
    PCS CreateCluster API's networking.subnetIds parameter.

    Bug condition: LENGTH(event["privateSubnetIds"]) > 1
    Expected: networking.subnetIds == private_subnet_ids[:1]

    **Validates: Requirements 2.1, 2.2**
    """
    # Build a minimal event that satisfies create_pcs_cluster
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

    # Load the module with all its intra-package dependencies
    mod = _load_cluster_creation_module()

    # Patch the module-level PCS client and progress helper
    mock_pcs = MagicMock()
    mock_pcs.create_cluster.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_pcs_cluster(event)

    # Verify the mock was called exactly once
    mock_pcs.create_cluster.assert_called_once()

    # Extract the networking.subnetIds that was passed
    call_kwargs = mock_pcs.create_cluster.call_args
    actual_subnet_ids = call_kwargs.kwargs["networking"]["subnetIds"]

    # The EXPECTED behaviour: only the first subnet is passed
    assert len(actual_subnet_ids) == 1, (
        f"Expected exactly 1 subnet in networking.subnetIds, "
        f"got {len(actual_subnet_ids)}: {actual_subnet_ids}"
    )
    assert actual_subnet_ids[0] == subnets[0], (
        f"Expected first subnet {subnets[0]}, got {actual_subnet_ids[0]}"
    )

    # Also verify the function returned the expected keys
    assert result["pcsClusterId"] == "pcs-123"
    assert result["pcsClusterArn"] == "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123"
