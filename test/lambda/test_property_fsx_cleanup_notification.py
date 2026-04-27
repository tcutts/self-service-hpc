# Feature: orphaned-fsx-cleanup, Property 5: Notification message completeness
"""Property-based test for FSx cleanup notification message.

**Validates: Requirements 6.2**
"""

import sys
import os

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Ensure lambda source is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda"))

from fsx_cleanup.cleanup import build_notification_message

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate realistic but arbitrary identifiers
filesystem_id_strategy = st.text(
    min_size=1, max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda s: s.strip() == s and len(s.strip()) > 0)

project_id_strategy = st.text(
    min_size=1, max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda s: s.strip() == s and len(s.strip()) > 0)

cluster_name_strategy = st.text(
    min_size=1, max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda s: s.strip() == s and len(s.strip()) > 0)

reason_strategy = st.sampled_from([
    "cluster_not_found",
    "terminal_status:FAILED",
    "terminal_status:DESTROYED",
])

error_strategy = st.text(
    min_size=1, max_size=60,
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
)

# A single deleted filesystem record
deleted_record_strategy = st.fixed_dictionaries({
    "filesystem_id": filesystem_id_strategy,
    "project_id": project_id_strategy,
    "cluster_name": cluster_name_strategy,
    "reason": reason_strategy,
})

# A single failed filesystem record
failed_record_strategy = st.fixed_dictionaries({
    "filesystem_id": filesystem_id_strategy,
    "project_id": project_id_strategy,
    "cluster_name": cluster_name_strategy,
    "error": error_strategy,
})

deleted_list_strategy = st.lists(deleted_record_strategy, min_size=0, max_size=5)
failed_list_strategy = st.lists(failed_record_strategy, min_size=0, max_size=5)


# ---------------------------------------------------------------------------
# Property 5: Notification message completeness
# ---------------------------------------------------------------------------


@given(deleted=deleted_list_strategy, failed=failed_list_strategy)
@settings(max_examples=10, deadline=None)
def test_notification_message_contains_all_identifiers(deleted, failed):
    """build_notification_message produces a message body that contains the
    filesystem ID, project ID, and cluster name of every deleted and failed
    filesystem, and the subject includes the deleted count.

    **Validates: Requirements 6.2**
    """
    subject, message_body = build_notification_message(deleted, failed)

    # Subject must contain the count of deleted filesystems
    assert str(len(deleted)) in subject

    # Every deleted record's identifiers must appear in the message body
    for record in deleted:
        assert record["filesystem_id"] in message_body, (
            f"Deleted filesystem_id {record['filesystem_id']!r} not found in message"
        )
        assert record["project_id"] in message_body, (
            f"Deleted project_id {record['project_id']!r} not found in message"
        )
        assert record["cluster_name"] in message_body, (
            f"Deleted cluster_name {record['cluster_name']!r} not found in message"
        )

    # Every failed record's identifiers must appear in the message body
    for record in failed:
        assert record["filesystem_id"] in message_body, (
            f"Failed filesystem_id {record['filesystem_id']!r} not found in message"
        )
        assert record["project_id"] in message_body, (
            f"Failed project_id {record['project_id']!r} not found in message"
        )
        assert record["cluster_name"] in message_body, (
            f"Failed cluster_name {record['cluster_name']!r} not found in message"
        )
