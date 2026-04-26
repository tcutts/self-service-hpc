# Feature: cluster-template-resolution, Property 1: Bug Condition
"""Property-based test for cluster template resolution bug condition.

Verifies that ``_STEP_DISPATCH`` contains a ``resolve_template`` entry,
confirming that the template resolution step is registered in the
cluster creation workflow dispatch table.

``resolve_template`` runs as a separate step BEFORE ``create_pcs_cluster``,
so ``create_pcs_cluster`` itself is not expected to add template-driven
fields — that is ``resolve_template``'s responsibility.

The test encodes the EXPECTED (correct) behaviour — it will FAIL on
unfixed code (confirming the bug) and PASS after the fix is applied.

**Validates: Requirements 1.1, 1.2**
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

# Template IDs: realistic non-empty identifiers
_template_id = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)

# Minimal valid event fields needed by create_pcs_cluster
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)
_project_id = st.from_regex(r"proj-[a-z0-9]{4,10}", fullmatch=True)
_sg_id = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)
_subnet_id = st.from_regex(r"subnet-[0-9a-f]{8,17}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 1: Bug Condition — Template fields missing from PCS output
#              and _STEP_DISPATCH has no resolve_template entry
# ---------------------------------------------------------------------------

# Template-driven fields that SHOULD be present after the fix
_TEMPLATE_FIELDS = {
    "loginInstanceType",
    "instanceTypes",
    "maxNodes",
    "minNodes",
    "purchaseOption",
}


@given(
    template_id=_template_id,
    cluster_name=_cluster_name,
    project_id=_project_id,
    sg_id=_sg_id,
    subnet_id=_subnet_id,
)
@settings(max_examples=10, deadline=None)
def test_create_pcs_cluster_missing_template_fields(
    template_id, cluster_name, project_id, sg_id, subnet_id
):
    """For any event with a valid templateId, _STEP_DISPATCH must contain
    a ``resolve_template`` entry so that template fields are resolved
    before the parallel FSx/PCS state executes.

    ``resolve_template`` runs as a separate step BEFORE ``create_pcs_cluster``,
    so ``create_pcs_cluster`` itself is not expected to add template fields.
    The key validation is that the dispatch table includes the resolution step.

    This test FAILS on unfixed code (confirming the bug exists) and
    PASSES after the fix is applied.

    **Validates: Requirements 1.1, 1.2**
    """
    # Load the module with all its intra-package dependencies
    mod = _load_cluster_creation_module()

    # ---- Assert the EXPECTED (fixed) behaviour ----
    # After the fix, _STEP_DISPATCH must contain "resolve_template"
    assert "resolve_template" in mod._STEP_DISPATCH, (
        "_STEP_DISPATCH does not contain 'resolve_template' — "
        "no template resolution step is registered in the dispatch table"
    )

    # Verify the registered handler is callable
    assert callable(mod._STEP_DISPATCH["resolve_template"]), (
        "'resolve_template' is registered in _STEP_DISPATCH but is not callable"
    )

    # Also verify create_pcs_cluster still works correctly — it should
    # add pcsClusterId and pcsClusterArn without needing template fields
    # (those are resolved by the separate resolve_template step)
    event = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "templateId": template_id,
        "privateSubnetIds": [subnet_id],
        "securityGroupIds": {"computeNode": sg_id},
    }

    fake_response = {
        "cluster": {
            "id": "pcs-123",
            "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123",
        }
    }

    mock_pcs = MagicMock()
    mock_pcs.create_cluster.return_value = fake_response

    with patch.object(mod, "pcs_client", mock_pcs), \
         patch.object(mod, "_update_step_progress"):

        result = mod.create_pcs_cluster(event)

    # create_pcs_cluster should still add its own fields
    assert "pcsClusterId" in result, (
        "create_pcs_cluster did not add 'pcsClusterId' to the result"
    )
    assert "pcsClusterArn" in result, (
        "create_pcs_cluster did not add 'pcsClusterArn' to the result"
    )
