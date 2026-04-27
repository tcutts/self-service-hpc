# Feature: bulk-actions-ui, Property 8: Batch user eligibility
"""Property-based tests verifying batch user eligibility.

Property 8: Batch user eligibility — only users in the required status succeed.
For any batch of user identifiers where each user has a random status (ACTIVE or
INACTIVE), the batch deactivate endpoint returns "success" only for users with
status ACTIVE, and the batch reactivate endpoint returns "success" only for users
with status INACTIVE. All other users receive "error" entries.

**Validates: Requirements 6.3, 6.5, 6.8, 6.10**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    USERS_TABLE_NAME,
    _USER_MGMT_DIR,
    create_users_table,
    create_cognito_pool,
    reload_user_mgmt_modules,
    build_admin_event,
    _load_module_from,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_STATUSES = ["ACTIVE", "INACTIVE"]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

user_status_strategy = st.sampled_from(USER_STATUSES)

# Generate 1-5 users, each with a unique ID and random status
user_batch_strategy = st.lists(
    st.tuples(
        st.text(
            min_size=3,
            max_size=12,
            alphabet=st.characters(whitelist_categories=("L", "N")),
        ),
        user_status_strategy,
    ),
    min_size=1,
    max_size=5,
    unique_by=lambda t: t[0],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_user(users_table, cognito_pool_id, user_id, status):
    """Insert a user with the given status into the mocked DynamoDB table and Cognito."""
    import boto3 as _boto3

    users_table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "displayName": f"User {user_id}",
        "email": f"{user_id}@example.com",
        "role": "User",
        "posixUid": 10001,
        "posixGid": 10001,
        "status": status,
        "cognitoSub": f"sub-{user_id}",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })

    # Create the Cognito user so deactivate/reactivate can disable/enable them
    client = _boto3.client("cognito-idp", region_name=AWS_REGION)
    client.admin_create_user(
        UserPoolId=cognito_pool_id,
        Username=user_id,
        UserAttributes=[
            {"Name": "email", "Value": f"{user_id}@example.com"},
        ],
        MessageAction="SUPPRESS",
    )
    # If user should be INACTIVE, disable them in Cognito to match DynamoDB state
    if status == "INACTIVE":
        client.admin_disable_user(
            UserPoolId=cognito_pool_id,
            Username=user_id,
        )


def _setup_env(cognito_pool_id):
    """Set environment variables for mocked AWS."""
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
        "USER_POOL_ID": cognito_pool_id,
    })


def _parse_batch_response(response):
    """Parse the batch response body and return (results, summary)."""
    body = json.loads(response["body"])
    return body["results"], body["summary"]


# ---------------------------------------------------------------------------
# Property 8a: Batch deactivate — only ACTIVE users succeed
# ---------------------------------------------------------------------------

@given(users=user_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_deactivate_only_active_succeed(users):
    """For any batch of users with random statuses, batch deactivate returns
    "success" only for users with status ACTIVE. INACTIVE users get "error".

    **Validates: Requirements 6.3, 6.5**
    """
    users_table = create_users_table()
    cognito_pool_id = create_cognito_pool()
    _setup_env(cognito_pool_id)
    reload_user_mgmt_modules()

    # Seed users with their assigned statuses
    for uid, status in users:
        _seed_user(users_table, cognito_pool_id, uid, status)

    # Re-import handler after reload
    handler_mod = _load_module_from(_USER_MGMT_DIR, "handler")

    user_ids = [uid for uid, _ in users]
    event = build_admin_event(
        "POST",
        "/users/batch/deactivate",
        body={"userIds": user_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Verify each result matches eligibility
    status_map = {uid: status for uid, status in users}
    for result in results:
        uid = result["id"]
        if status_map[uid] == "ACTIVE":
            assert result["status"] == "success", (
                f"User '{uid}' with status ACTIVE should succeed for deactivate, "
                f"got: {result}"
            )
        else:
            assert result["status"] == "error", (
                f"User '{uid}' with status {status_map[uid]} should fail for deactivate, "
                f"got: {result}"
            )

    # Verify summary counts
    expected_success = sum(1 for _, s in users if s == "ACTIVE")
    assert summary["total"] == len(users)
    assert summary["succeeded"] == expected_success
    assert summary["failed"] == len(users) - expected_success


# ---------------------------------------------------------------------------
# Property 8b: Batch reactivate — only INACTIVE users succeed
# ---------------------------------------------------------------------------

@given(users=user_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_reactivate_only_inactive_succeed(users):
    """For any batch of users with random statuses, batch reactivate returns
    "success" only for users with status INACTIVE. ACTIVE users get "error"
    (ValidationError: user is already active).

    **Validates: Requirements 6.8, 6.10**
    """
    users_table = create_users_table()
    cognito_pool_id = create_cognito_pool()
    _setup_env(cognito_pool_id)
    reload_user_mgmt_modules()

    # Seed users with their assigned statuses
    for uid, status in users:
        _seed_user(users_table, cognito_pool_id, uid, status)

    # Re-import handler after reload
    handler_mod = _load_module_from(_USER_MGMT_DIR, "handler")

    user_ids = [uid for uid, _ in users]
    event = build_admin_event(
        "POST",
        "/users/batch/reactivate",
        body={"userIds": user_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Verify each result matches eligibility
    status_map = {uid: status for uid, status in users}
    for result in results:
        uid = result["id"]
        if status_map[uid] == "INACTIVE":
            assert result["status"] == "success", (
                f"User '{uid}' with status INACTIVE should succeed for reactivate, "
                f"got: {result}"
            )
        else:
            assert result["status"] == "error", (
                f"User '{uid}' with status {status_map[uid]} should fail for reactivate, "
                f"got: {result}"
            )

    # Verify summary counts
    expected_success = sum(1 for _, s in users if s == "INACTIVE")
    assert summary["total"] == len(users)
    assert summary["succeeded"] == expected_success
    assert summary["failed"] == len(users) - expected_success
