# Feature: orphaned-fsx-cleanup, Property 4: Summary counts consistency
"""Property-based test for FSx cleanup summary counts.

**Validates: Requirements 5.3**
"""

import sys
import os

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Ensure lambda source is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda"))

from fsx_cleanup.cleanup import build_cleanup_summary

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A minimal filesystem record dict — only needs to be a dict for counting
filesystem_record_strategy = st.fixed_dictionaries({
    "filesystem_id": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
    "project_id": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
    "cluster_name": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
})


@st.composite
def cleanup_inputs(draw):
    """Generate valid cleanup inputs where deleted and failed are subsets of orphaned.

    Strategy:
    1. Generate a list of orphaned filesystem records.
    2. Split orphaned into deleted and failed sublists (every orphaned record
       ends up in exactly one of the two).
    3. Generate total_scanned >= len(orphaned) so the scanned invariant holds.
    """
    orphaned = draw(st.lists(filesystem_record_strategy, min_size=0, max_size=10))
    n = len(orphaned)

    # Split orphaned into deleted and failed: each record gets a boolean mask
    mask = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    deleted = [fs for fs, is_deleted in zip(orphaned, mask) if is_deleted]
    failed = [fs for fs, is_deleted in zip(orphaned, mask) if not is_deleted]

    # total_scanned must be >= len(orphaned)
    extra = draw(st.integers(min_value=0, max_value=50))
    total_scanned = n + extra

    return total_scanned, orphaned, deleted, failed


# ---------------------------------------------------------------------------
# Property 4: Summary counts consistency
# ---------------------------------------------------------------------------


@given(inputs=cleanup_inputs())
@settings(max_examples=10, deadline=None)
def test_build_cleanup_summary_counts_are_consistent(inputs):
    """build_cleanup_summary returns counts where:
    - total_orphaned == total_deleted + total_failed
    - total_tagged >= total_orphaned
    - total_scanned >= total_tagged

    **Validates: Requirements 5.3**
    """
    total_scanned, orphaned, deleted, failed = inputs

    result = build_cleanup_summary(total_scanned, orphaned, deleted, failed)

    # Core invariant: orphaned == deleted + failed
    assert result["total_orphaned"] == result["total_deleted"] + result["total_failed"]

    # total_tagged >= total_orphaned (in current impl total_tagged == total_orphaned)
    assert result["total_tagged"] >= result["total_orphaned"]

    # total_scanned >= total_tagged
    assert result["total_scanned"] >= result["total_tagged"]

    # Verify counts match the input lengths
    assert result["total_scanned"] == total_scanned
    assert result["total_orphaned"] == len(orphaned)
    assert result["total_deleted"] == len(deleted)
    assert result["total_failed"] == len(failed)
