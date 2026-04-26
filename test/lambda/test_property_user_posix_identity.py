# Feature: self-service-hpc, Property 1: User creation assigns globally unique POSIX identity
"""Property-based test verifying that every user created via create_user()
receives a globally unique POSIX UID and GID.

**Validates: Requirements 1.1, 17.1**
"""

import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    USERS_TABLE_NAME,
    create_users_table,
    create_cognito_pool,
    reload_user_mgmt_modules,
)

# Strategy: generate lists of distinct user identifiers (1–20 users)
user_id_lists = st.lists(
    st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N"))),
    min_size=1,
    max_size=20,
    unique=True,
)


@given(user_ids=user_id_lists)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@mock_aws
def test_user_creation_assigns_unique_posix_identity(user_ids):
    """For any sequence of distinct user identifiers, all assigned UIDs and
    GIDs must be unique, and each response must include the user identifier.

    **Validates: Requirements 1.1, 17.1**
    """
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"

    create_users_table()
    pool_id = create_cognito_pool()

    handler_mod, users_mod, _ = reload_user_mgmt_modules()

    collected_uids = []
    collected_gids = []

    for uid in user_ids:
        result = users_mod.create_user(
            table_name=USERS_TABLE_NAME,
            user_pool_id=pool_id,
            user_id=uid,
            display_name=f"Display {uid}",
            email=f"{uid}@example.com",
        )
        collected_uids.append(result["posixUid"])
        collected_gids.append(result["posixGid"])
        assert result["userId"] == uid

    assert len(set(collected_uids)) == len(collected_uids), f"Duplicate UIDs: {collected_uids}"
    assert len(set(collected_gids)) == len(collected_gids), f"Duplicate GIDs: {collected_gids}"
