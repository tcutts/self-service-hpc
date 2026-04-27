# Feature: orphaned-fsx-cleanup, Property 2: Orphan classification correctness
"""Property-based test for FSx cleanup orphan classification.

**Validates: Requirements 2.3, 2.4, 2.5**
"""

import sys
import os

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Ensure lambda source is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda"))

from fsx_cleanup.cleanup import classify_filesystem, TERMINAL_STATUSES, ACTIVE_STATUSES

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary tag dict — filesystem_tags is a dict of tag key→value
tag_key_strategy = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
tag_value_strategy = st.text(min_size=0, max_size=30)
filesystem_tags_strategy = st.dictionaries(
    keys=tag_key_strategy,
    values=tag_value_strategy,
    min_size=0,
    max_size=8,
)

# Cluster record states: None (missing), terminal status, or active status
terminal_status_strategy = st.sampled_from(sorted(TERMINAL_STATUSES))
active_status_strategy = st.sampled_from(sorted(ACTIVE_STATUSES))

# A cluster record with a terminal status
terminal_record_strategy = st.fixed_dictionaries({
    "status": terminal_status_strategy,
})

# A cluster record with an active status
active_record_strategy = st.fixed_dictionaries({
    "status": active_status_strategy,
})

# Combined: None | terminal record | active record
cluster_record_strategy = st.one_of(
    st.none(),
    terminal_record_strategy,
    active_record_strategy,
)


# ---------------------------------------------------------------------------
# Property 2: Orphan classification correctness
# ---------------------------------------------------------------------------


@given(
    filesystem_tags=filesystem_tags_strategy,
    cluster_record=cluster_record_strategy,
)
@settings(max_examples=10, deadline=None)
def test_classify_filesystem_orphaned_iff_missing_or_terminal(filesystem_tags, cluster_record):
    """classify_filesystem returns is_orphaned=True if and only if the cluster
    record is missing (None) or has a terminal status (FAILED, DESTROYED).

    **Validates: Requirements 2.3, 2.4, 2.5**
    """
    is_orphaned, reason = classify_filesystem(filesystem_tags, cluster_record)

    if cluster_record is None:
        # Requirement 2.3: missing cluster record → orphaned
        assert is_orphaned is True
        assert reason == "cluster_not_found"
    elif cluster_record.get("status") in TERMINAL_STATUSES:
        # Requirement 2.4: terminal status → orphaned
        assert is_orphaned is True
        assert reason == f"terminal_status:{cluster_record['status']}"
    else:
        # Requirement 2.5: active status → not orphaned
        assert is_orphaned is False
        assert reason == "active"
