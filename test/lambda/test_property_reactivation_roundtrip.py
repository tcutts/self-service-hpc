# Feature: user-reactivation, Property 1: Reactivation round-trip restores user to ACTIVE with correct profile
# Feature: user-reactivation, Property 2: Reactivation preserves POSIX identity
"""Property-based tests verifying that the create → deactivate → reactivate
round-trip restores a user to ACTIVE status with the correct profile fields,
and that the POSIX UID/GID assigned at creation time are preserved through
the deactivation/reactivation cycle.

**Validates: Requirements 1.1, 1.2, 1.3, 3.2**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from hypothesis.strategies import characters, text
from moto import mock_aws

from conftest import (
    AWS_REGION,
    USERS_TABLE_NAME,
    create_users_table,
    create_cognito_pool,
    reload_user_mgmt_modules,
    build_admin_event,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

user_id_strategy = text(
    alphabet=characters(whitelist_categories=("Ll", "N")),
    min_size=1,
    max_size=20,
)

display_name_strategy = text(
    min_size=1,
    max_size=50,
    alphabet=characters(whitelist_categories=("L", "N"), whitelist_characters=" "),
).filter(lambda s: s.strip()).map(str.strip)

email_strategy = user_id_strategy.map(lambda uid: f"{uid}@example.com")


# ---------------------------------------------------------------------------
# Properties 1 & 2: Reactivation round-trip restores ACTIVE profile and
#                    preserves POSIX identity (combined — same setup)
# ---------------------------------------------------------------------------

@given(
    user_id=user_id_strategy,
    display_name=display_name_strategy,
    email=email_strategy,
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_reactivation_roundtrip_restores_profile_and_preserves_posix(
    user_id, display_name, email
):
    """For any user created with a valid userId, displayName, and email, if
    that user is deactivated and then reactivated:

    - Property 1: the reactivation response SHALL have HTTP status 200,
      contain userId, displayName, email, posixUid, posixGid, and status,
      and the status SHALL be ACTIVE.
    - Property 2: the posixUid and posixGid SHALL be identical to the values
      assigned at creation time.

    **Validates: Requirements 1.1, 1.2, 1.3, 3.2**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
    })

    create_users_table()
    pool_id = create_cognito_pool()
    os.environ["USER_POOL_ID"] = pool_id

    handler_mod, users_mod, _ = reload_user_mgmt_modules()

    # Step 1: Create user
    create_event = build_admin_event(
        "POST", "/users",
        body={"userId": user_id, "displayName": display_name, "email": email},
    )
    create_resp = handler_mod.handler(create_event, {})
    assert create_resp["statusCode"] == 201, (
        f"Create failed: {create_resp['body']}"
    )
    created_body = json.loads(create_resp["body"])
    original_posix_uid = created_body["posixUid"]
    original_posix_gid = created_body["posixGid"]

    # Step 2: Deactivate user
    deactivate_event = build_admin_event(
        "DELETE", "/users/{userId}",
        path_parameters={"userId": user_id},
    )
    deactivate_resp = handler_mod.handler(deactivate_event, {})
    assert deactivate_resp["statusCode"] == 200, (
        f"Deactivate failed: {deactivate_resp['body']}"
    )

    # Step 3: Reactivate user
    reactivate_event = build_admin_event(
        "POST", "/users/{userId}/reactivate",
        path_parameters={"userId": user_id},
    )
    reactivate_resp = handler_mod.handler(reactivate_event, {})

    assert reactivate_resp["statusCode"] == 200, (
        f"Reactivate failed: {reactivate_resp['body']}"
    )

    body = json.loads(reactivate_resp["body"])

    # Property 1: correct profile fields and ACTIVE status
    assert body["userId"] == user_id
    assert body["displayName"] == display_name
    assert body["email"] == email
    assert body["status"] == "ACTIVE"
    assert "posixUid" in body
    assert "posixGid" in body

    # Property 2: POSIX identity preserved
    # DynamoDB returns Decimal types which serialise as strings via json.dumps,
    # so compare as int to handle the Decimal/int mismatch.
    assert int(body["posixUid"]) == int(original_posix_uid), (
        f"posixUid changed: {original_posix_uid} → {body['posixUid']}"
    )
    assert int(body["posixGid"]) == int(original_posix_gid), (
        f"posixGid changed: {original_posix_gid} → {body['posixGid']}"
    )


# ---------------------------------------------------------------------------
# Property 3: Reactivating an already-active user is rejected
# ---------------------------------------------------------------------------

@given(
    user_id=user_id_strategy,
    display_name=display_name_strategy,
    email=email_strategy,
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_reactivating_active_user_is_rejected(user_id, display_name, email):
    """For any user who is currently in ACTIVE status, submitting a
    reactivation request SHALL return an HTTP 400 response with error code
    VALIDATION_ERROR.

    **Validates: Requirements 1.4**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
    })

    create_users_table()
    pool_id = create_cognito_pool()
    os.environ["USER_POOL_ID"] = pool_id

    handler_mod, users_mod, _ = reload_user_mgmt_modules()

    # Step 1: Create user (starts in ACTIVE status)
    create_event = build_admin_event(
        "POST", "/users",
        body={"userId": user_id, "displayName": display_name, "email": email},
    )
    create_resp = handler_mod.handler(create_event, {})
    assert create_resp["statusCode"] == 201, (
        f"Create failed: {create_resp['body']}"
    )

    # Step 2: Attempt to reactivate the already-active user
    reactivate_event = build_admin_event(
        "POST", "/users/{userId}/reactivate",
        path_parameters={"userId": user_id},
    )
    reactivate_resp = handler_mod.handler(reactivate_event, {})

    # Must be rejected with HTTP 400
    assert reactivate_resp["statusCode"] == 400, (
        f"Expected 400 but got {reactivate_resp['statusCode']}: {reactivate_resp['body']}"
    )

    body = json.loads(reactivate_resp["body"])
    assert body["error"]["code"] == "VALIDATION_ERROR", (
        f"Expected VALIDATION_ERROR but got {body['error']['code']}"
    )


# ---------------------------------------------------------------------------
# Property 5: User list returns both ACTIVE and INACTIVE users
# ---------------------------------------------------------------------------

@given(
    data=st.data(),
    num_users=st.integers(min_value=1, max_value=5),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_list_users_returns_both_active_and_inactive(data, num_users):
    """For any set of created users where a random subset has been deactivated,
    the list_users response SHALL contain every user regardless of status, and
    the count SHALL equal the total number of created users.

    **Validates: Requirements 5.1**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
    })

    create_users_table()
    pool_id = create_cognito_pool()
    os.environ["USER_POOL_ID"] = pool_id

    handler_mod, users_mod, _ = reload_user_mgmt_modules()

    # Generate unique user IDs
    user_ids = data.draw(
        st.lists(
            user_id_strategy,
            min_size=num_users,
            max_size=num_users,
            unique=True,
        )
    )

    # Create all users
    for uid in user_ids:
        create_event = build_admin_event(
            "POST", "/users",
            body={"userId": uid, "displayName": f"Name {uid}", "email": f"{uid}@example.com"},
        )
        resp = handler_mod.handler(create_event, {})
        assert resp["statusCode"] == 201, f"Create failed for {uid}: {resp['body']}"

    # Decide which users to deactivate (random subset, possibly empty)
    deactivate_flags = data.draw(
        st.lists(st.booleans(), min_size=num_users, max_size=num_users)
    )
    deactivated_ids = {uid for uid, flag in zip(user_ids, deactivate_flags) if flag}

    for uid in deactivated_ids:
        deactivate_event = build_admin_event(
            "DELETE", "/users/{userId}",
            path_parameters={"userId": uid},
        )
        resp = handler_mod.handler(deactivate_event, {})
        assert resp["statusCode"] == 200, f"Deactivate failed for {uid}: {resp['body']}"

    # List all users via GET /users
    list_event = build_admin_event("GET", "/users")
    list_resp = handler_mod.handler(list_event, {})
    assert list_resp["statusCode"] == 200

    body = json.loads(list_resp["body"])
    returned_ids = {u["userId"] for u in body["users"]}

    # Every created user must appear in the list
    assert returned_ids == set(user_ids), (
        f"Expected {set(user_ids)} but got {returned_ids}"
    )

    # Verify statuses are correct
    returned_map = {u["userId"]: u["status"] for u in body["users"]}
    for uid in user_ids:
        expected_status = "INACTIVE" if uid in deactivated_ids else "ACTIVE"
        assert returned_map[uid] == expected_status, (
            f"User {uid}: expected {expected_status} but got {returned_map[uid]}"
        )
