"""Property-based tests for SFN transition optimization — cluster destruction consolidated handlers.

# Feature: sfn-transition-optimization, Property 3: Cluster destruction consolidated delete output equivalence

For any valid cluster destruction event payload with storageMode in {"lustre", "mountpoint"},
calling consolidated_delete_resources(event) produces the same output dict as calling
delete_pcs_cluster_step, delete_fsx_filesystem, and conditionally remove_mountpoint_s3_policy
(when storageMode == "mountpoint") sequentially, where each step receives the output of the
previous step.

**Validates: Requirements 3.1, 3.4, 14.5**

# Feature: sfn-transition-optimization, Property 4: Cluster destruction consolidated cleanup output equivalence

For any valid cluster destruction event payload, calling consolidated_cleanup(event) produces
the same output dict as calling delete_iam_resources, delete_launch_templates,
deregister_cluster_name_step, and record_cluster_destroyed sequentially, where each step
receives the output of the previous step.

**Validates: Requirements 3.2, 14.5**
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
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
    consolidated_delete_resources,
    consolidated_cleanup,
    delete_pcs_cluster_step,
    delete_fsx_filesystem,
    remove_mountpoint_s3_policy,
    delete_iam_resources,
    delete_launch_templates,
    deregister_cluster_name_step,
    record_cluster_destroyed,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

project_id_strategy = st.from_regex(r"proj-[a-z0-9]{3,8}", fullmatch=True)
cluster_name_strategy = st.from_regex(
    r"[a-zA-Z][a-zA-Z0-9_-]{2,12}", fullmatch=True,
)
pcs_cluster_id_strategy = st.from_regex(r"pcs-[a-z0-9]{6,12}", fullmatch=True)
fsx_id_strategy = st.from_regex(r"fs-[a-f0-9]{8,17}", fullmatch=True)
storage_mode_strategy = st.sampled_from(["lustre", "mountpoint"])


@st.composite
def destruction_event(draw):
    """Generate a valid cluster destruction event payload.

    Produces the fields needed by consolidated_delete_resources:
    delete_pcs_cluster_step, delete_fsx_filesystem, and conditionally
    remove_mountpoint_s3_policy.
    """
    project_id = draw(project_id_strategy)
    cluster_name = draw(cluster_name_strategy)
    pcs_cluster_id = draw(st.one_of(pcs_cluster_id_strategy, st.just("")))
    fsx_filesystem_id = draw(st.one_of(fsx_id_strategy, st.just("")))
    storage_mode = draw(storage_mode_strategy)

    return {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": pcs_cluster_id,
        "fsxFilesystemId": fsx_filesystem_id,
        "storageMode": storage_mode,
    }


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------

def _build_mock_pcs():
    """Build a mock PCS client for deletion steps."""
    mock_pcs = MagicMock()
    mock_pcs.delete_cluster.return_value = {}
    return mock_pcs


def _build_mock_fsx():
    """Build a mock FSx client for filesystem deletion."""
    mock_fsx = MagicMock()
    mock_fsx.delete_file_system.return_value = {}
    return mock_fsx


def _build_mock_iam():
    """Build a mock IAM client for mountpoint S3 policy removal."""
    mock_iam = MagicMock()
    mock_iam.delete_role_policy.return_value = {}
    return mock_iam


def _build_mock_dynamodb():
    """Build a mock DynamoDB resource for step progress tracking."""
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name
        mock_table.update_item.return_value = {}
        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_mock_ec2():
    """Build a mock EC2 client for launch template deletion."""
    mock_ec2 = MagicMock()
    mock_ec2.describe_launch_templates.return_value = {
        "LaunchTemplates": [
            {"LaunchTemplateId": "lt-mock12345678"},
        ],
    }
    mock_ec2.delete_launch_template.return_value = {}
    return mock_ec2


# ===================================================================
# [PBT: Property 3] Cluster destruction consolidated delete output
#                    equivalence
# ===================================================================

class TestClusterDestructionConsolidatedDeleteEquivalence:
    """[PBT: Property 3] For any valid cluster destruction event payload
    with storageMode in {"lustre", "mountpoint"}, consolidated_delete_resources(event)
    produces the same output dict as calling the constituent steps sequentially.

    # Feature: sfn-transition-optimization, Property 3: Cluster destruction consolidated delete output equivalence

    **Validates: Requirements 3.1, 3.4, 14.5**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(event=destruction_event())
    def test_consolidated_matches_sequential(self, event):
        """consolidated_delete_resources(event) == sequential execution of
        delete_pcs_cluster_step → delete_fsx_filesystem →
        (conditionally) remove_mountpoint_s3_policy.

        **Validates: Requirements 3.1, 3.4, 14.5**
        """
        mock_pcs = _build_mock_pcs()
        mock_fsx = _build_mock_fsx()
        mock_iam = _build_mock_iam()
        mock_dynamodb = _build_mock_dynamodb()

        patches = [
            patch.object(cluster_destruction, "pcs_client", mock_pcs),
            patch.object(cluster_destruction, "fsx_client", mock_fsx),
            patch.object(cluster_destruction, "iam_client", mock_iam),
            patch.object(cluster_destruction, "dynamodb", mock_dynamodb),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = delete_pcs_cluster_step(event)
            r2 = delete_fsx_filesystem({**event, **r1})

            if event.get("storageMode") == "mountpoint":
                r3 = remove_mountpoint_s3_policy({**event, **r1, **r2})
                sequential_result = {}
                for r in [r1, r2, r3]:
                    sequential_result = {**sequential_result, **r}
            else:
                sequential_result = {}
                for r in [r1, r2]:
                    sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = consolidated_delete_resources(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )



# ---------------------------------------------------------------------------
# Strategies for cleanup event payloads (Property 4)
# ---------------------------------------------------------------------------

@st.composite
def cleanup_event(draw):
    """Generate a valid cluster destruction event payload for cleanup steps.

    Produces the fields needed by consolidated_cleanup:
    delete_iam_resources, delete_launch_templates,
    deregister_cluster_name_step, and record_cluster_destroyed.
    """
    project_id = draw(project_id_strategy)
    cluster_name = draw(cluster_name_strategy)

    return {
        "projectId": project_id,
        "clusterName": cluster_name,
    }


# ===================================================================
# [PBT: Property 4] Cluster destruction consolidated cleanup output
#                    equivalence
# ===================================================================

class TestClusterDestructionConsolidatedCleanupEquivalence:
    """[PBT: Property 4] For any valid cluster destruction event payload,
    consolidated_cleanup(event) produces the same output dict as calling
    the four cleanup steps sequentially.

    # Feature: sfn-transition-optimization, Property 4: Cluster destruction consolidated cleanup output equivalence

    **Validates: Requirements 3.2, 14.5**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(event=cleanup_event())
    def test_consolidated_matches_sequential(self, event):
        """consolidated_cleanup(event) == sequential execution of
        delete_iam_resources → delete_launch_templates →
        deregister_cluster_name_step → record_cluster_destroyed.

        **Validates: Requirements 3.2, 14.5**
        """
        mock_iam = _build_mock_iam()
        mock_ec2 = _build_mock_ec2()
        mock_dynamodb = _build_mock_dynamodb()

        # Fix datetime.now so both runs produce the same timestamp
        fixed_now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        patches = [
            patch.object(cluster_destruction, "iam_client", mock_iam),
            patch.object(cluster_destruction, "ec2_client", mock_ec2),
            patch.object(cluster_destruction, "dynamodb", mock_dynamodb),
            patch.object(
                cluster_destruction, "cluster_names",
                MagicMock(**{"deregister_cluster_name.return_value": True}),
            ),
            patch.dict(os.environ, {
                "CLUSTER_NAME_REGISTRY_TABLE_NAME": "ClusterNameRegistry",
            }),
        ]

        for p in patches:
            p.start()

        with patch("cluster_destruction.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            try:
                # --- Sequential execution ---
                r1 = delete_iam_resources(event)
                r2 = delete_launch_templates({**event, **r1})
                r3 = deregister_cluster_name_step(
                    {**event, **r1, **r2},
                )
                r4 = record_cluster_destroyed(
                    {**event, **r1, **r2, **r3},
                )
                sequential_result = {}
                for r in [r1, r2, r3, r4]:
                    sequential_result = {**sequential_result, **r}

                # --- Consolidated execution ---
                consolidated_result = consolidated_cleanup(event)
            finally:
                for p in patches:
                    p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )
