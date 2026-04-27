# Feature: orphaned-fsx-cleanup, Property 3: Error resilience
"""Property-based test for FSx cleanup error resilience.

**Validates: Requirements 4.2, 8.1**

Verifies that individual filesystem deletion failures do not block
processing of remaining orphaned filesystems. For any set of orphaned
filesystems where some are marked to fail DRA deletion or filesystem
deletion, the handler SHALL still attempt all of them and the count of
attempted deletions plus DRA failures SHALL equal total orphaned.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Ensure lambda source is importable
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_FSX_CLEANUP_DIR = os.path.join(_LAMBDA_ROOT, "fsx_cleanup")
sys.path.insert(0, _LAMBDA_ROOT)


def _load_module(directory: str, module_name: str):
    """Load a module from a specific directory to avoid import collisions."""
    filepath = os.path.join(directory, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate a filesystem ID like "fs-abc123"
fs_id_strategy = st.text(
    min_size=3, max_size=12,
    alphabet=st.characters(whitelist_categories=("L", "N")),
).map(lambda s: f"fs-{s}")

# Each orphaned filesystem record has an ID, project, cluster, and reason,
# plus flags indicating whether DRA deletion or filesystem deletion should fail.
orphaned_fs_strategy = st.fixed_dictionaries({
    "filesystem_id": fs_id_strategy,
    "project_id": st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))),
    "cluster_name": st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))),
    "reason": st.sampled_from(["cluster_not_found", "terminal_status:FAILED", "terminal_status:DESTROYED"]),
    "dra_fails": st.booleans(),
    "fs_delete_fails": st.booleans(),
})

# Generate a non-empty list of orphaned filesystems with unique IDs
orphaned_list_strategy = st.lists(
    orphaned_fs_strategy,
    min_size=1,
    max_size=8,
    unique_by=lambda x: x["filesystem_id"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_tagged_filesystem(fs_id: str, project_id: str, cluster_name: str) -> dict:
    """Build a filesystem dict as returned by the FSx API."""
    return {
        "FileSystemId": fs_id,
        "FileSystemType": "LUSTRE",
        "Tags": [
            {"Key": "Project", "Value": project_id},
            {"Key": "ClusterName", "Value": cluster_name},
        ],
    }


# ---------------------------------------------------------------------------
# Property 3: Error resilience
# ---------------------------------------------------------------------------


@given(orphaned_filesystems=orphaned_list_strategy)
@settings(max_examples=100, deadline=None)
def test_error_resilience_all_orphans_attempted(orphaned_filesystems):
    """For any set of orphaned filesystems with arbitrary failure patterns,
    the handler processes every orphaned filesystem. The count of successful
    deletions plus the count of failures equals the total number of orphaned
    filesystems.

    **Validates: Requirements 4.2, 8.1**
    """
    # Build the FSx API response from the generated orphaned filesystems.
    # All filesystems are tagged and will be classified as orphaned.
    raw_filesystems = [
        _build_tagged_filesystem(fs["filesystem_id"], fs["project_id"], fs["cluster_name"])
        for fs in orphaned_filesystems
    ]

    # Build a lookup of which filesystems should fail at each stage
    dra_fail_ids = {fs["filesystem_id"] for fs in orphaned_filesystems if fs["dra_fails"]}
    fs_delete_fail_ids = {
        fs["filesystem_id"]
        for fs in orphaned_filesystems
        if fs["fs_delete_fails"] and not fs["dra_fails"]
    }

    # Track which filesystem IDs were attempted for DRA deletion
    dra_attempted = []
    fs_delete_attempted = []

    def mock_scan():
        return raw_filesystems

    def mock_lookup(project_id, cluster_name):
        # Return None so every filesystem is classified as orphaned
        return None

    def mock_delete_dras(fs_id):
        dra_attempted.append(fs_id)
        return fs_id not in dra_fail_ids

    def mock_delete_fs(fs_id):
        fs_delete_attempted.append(fs_id)
        return fs_id not in fs_delete_fail_ids

    def mock_publish(subject, message):
        pass  # No-op for testing

    # Load modules fresh so patches apply to the handler's imports
    cleanup_mod = _load_module(_FSX_CLEANUP_DIR, "cleanup")
    handler_mod = _load_module(_FSX_CLEANUP_DIR, "handler")

    # Patch the I/O functions on the handler module (where they are imported)
    with patch.object(handler_mod, "scan_fsx_filesystems", side_effect=mock_scan), \
         patch.object(handler_mod, "lookup_cluster_record", side_effect=mock_lookup), \
         patch.object(handler_mod, "delete_filesystem_dras", side_effect=mock_delete_dras), \
         patch.object(handler_mod, "delete_filesystem", side_effect=mock_delete_fs), \
         patch.object(handler_mod, "publish_notification", side_effect=mock_publish):

        result = handler_mod.handler({}, None)

    total_orphaned = len(orphaned_filesystems)
    total_deleted = result["total_deleted"]
    total_failed = result["total_failed"]

    # Core property: every orphaned filesystem was attempted
    # attempted_deletions + dra_failures == total_orphaned
    assert total_deleted + total_failed == total_orphaned, (
        f"total_deleted ({total_deleted}) + total_failed ({total_failed}) "
        f"!= total_orphaned ({total_orphaned})"
    )

    # Every orphaned filesystem had DRA deletion attempted
    assert len(dra_attempted) == total_orphaned, (
        f"DRA deletion attempted for {len(dra_attempted)} filesystems, "
        f"expected {total_orphaned}"
    )

    # Filesystem deletion was only attempted for those where DRA succeeded
    expected_fs_attempts = total_orphaned - len(dra_fail_ids)
    assert len(fs_delete_attempted) == expected_fs_attempts, (
        f"Filesystem deletion attempted for {len(fs_delete_attempted)}, "
        f"expected {expected_fs_attempts}"
    )
