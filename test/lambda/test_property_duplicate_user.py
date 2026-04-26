# Feature: self-service-hpc, Property 2: Duplicate user creation is rejected
"""Property-based test verifying that creating a user with an identifier
that already exists on the platform is rejected with a descriptive error,
and the existing user record remains unchanged.

**Validates: Requirements 1.3**
"""

import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
import pytest

from conftest import (
    AWS_REGION,
    USERS_TABLE_NAME,
    create_users_table,
    create_cognito_pool,
    reload_user_mgmt_modules,
)

user_id_strategy = st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N")))


@given(user_id=user_id_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@mock_aws
def test_duplicate_user_creation_is_rejected(user_id):
    """For any user identifier that already exists on the platform, a
    subsequent request to create a user with the same identifier SHALL be
    rejected with a descriptive error message (DUPLICATE_ERROR), and the
    existing user record SHALL remain unchanged.

    **Validates: Requirements 1.3**
    """
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"

    table = create_users_table()
    pool_id = create_cognito_pool()
    _, users_mod, errors_mod = reload_user_mgmt_modules()

    original = users_mod.create_user(
        table_name=USERS_TABLE_NAME, user_pool_id=pool_id,
        user_id=user_id, display_name=f"Display {user_id}", email=f"{user_id}@example.com",
    )

    with pytest.raises(errors_mod.DuplicateError) as exc_info:
        users_mod.create_user(
            table_name=USERS_TABLE_NAME, user_pool_id=pool_id,
            user_id=user_id, display_name=f"Different {user_id}", email=f"diff-{user_id}@example.com",
        )

    error = exc_info.value
    assert error.code == "DUPLICATE_ERROR"
    assert user_id in error.message

    stored = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"})["Item"]
    assert stored["displayName"] == original["displayName"]
    assert stored["email"] == original["email"]
    assert int(stored["posixUid"]) == original["posixUid"]
    assert stored["status"] == "ACTIVE"
