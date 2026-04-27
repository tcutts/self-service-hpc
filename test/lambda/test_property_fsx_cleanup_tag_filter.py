# Feature: orphaned-fsx-cleanup, Property 1: Tag filtering correctness
"""Property-based test for FSx cleanup tag filtering.

**Validates: Requirements 2.1**
"""

import sys
import os

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Ensure lambda source is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda"))

from fsx_cleanup.cleanup import filter_tagged_filesystems

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary tag key — mix of the two required keys and random extras
REQUIRED_KEYS = ["Project", "ClusterName"]

tag_key_strategy = st.sampled_from(
    REQUIRED_KEYS + ["Environment", "Owner", "CostCenter", "Name", "Team"]
) | st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))

tag_value_strategy = st.text(min_size=0, max_size=30)

tag_strategy = st.fixed_dictionaries({
    "Key": tag_key_strategy,
    "Value": tag_value_strategy,
})

# A single filesystem dict with a random set of tags (may or may not include
# the required keys).  We use unique_by on Key so each tag key appears at most
# once, matching real AWS behaviour.
filesystem_strategy = st.fixed_dictionaries({
    "FileSystemId": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
    "Tags": st.lists(tag_strategy, min_size=0, max_size=8).map(
        lambda tags: list({t["Key"]: t for t in tags}.values())
    ),
})

filesystems_strategy = st.lists(filesystem_strategy, min_size=0, max_size=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_both_required_tags(fs: dict) -> bool:
    """Reference implementation: True iff the filesystem has both required tags."""
    tag_keys = {tag.get("Key") for tag in fs.get("Tags", [])}
    return "Project" in tag_keys and "ClusterName" in tag_keys


# ---------------------------------------------------------------------------
# Property 1: Tag filtering correctness
# ---------------------------------------------------------------------------


@given(filesystems=filesystems_strategy)
@settings(max_examples=10, deadline=None)
def test_filter_tagged_filesystems_returns_exactly_those_with_both_tags(filesystems):
    """filter_tagged_filesystems returns exactly the filesystems that have
    both a ``Project`` tag and a ``ClusterName`` tag.

    **Validates: Requirements 2.1**
    """
    result = filter_tagged_filesystems(filesystems)

    expected = [fs for fs in filesystems if _has_both_required_tags(fs)]

    assert result == expected
