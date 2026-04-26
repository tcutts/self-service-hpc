"""Unit tests for the User Management Lambda.

Covers:
- User creation happy path (DynamoDB record, Cognito user, POSIX UID/GID)
- User creation with duplicate identifier (DUPLICATE_ERROR)
- User creation with missing fields (VALIDATION_ERROR)
- User deactivation (status INACTIVE, Cognito disabled)
- POSIX UID/GID counter increments correctly across multiple creations
- GET /users returns list of users (admin only)
- GET /users/{userId} returns specific user
- Non-admin callers rejected with AUTHORISATION_ERROR

Requirements: 1.1, 1.2, 1.3, 1.4, 17.1

Infrastructure is set up once per test class via the ``user_mgmt_env``
fixture from conftest.py, avoiding repeated DynamoDB table and Cognito
pool creation.
"""

import json

import boto3
import pytest

from conftest import (
    AWS_REGION,
    USERS_TABLE_NAME,
    build_admin_event,
    build_non_admin_event,
)


# ---------------------------------------------------------------------------
# User creation – happy path
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserCreationHappyPath:
    """Validates: Requirements 1.1, 17.1"""

    def test_create_user_returns_201_with_user_record(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "alice",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["userId"] == "alice"
        assert body["displayName"] == "Alice Smith"
        assert body["email"] == "alice@example.com"
        assert body["status"] == "ACTIVE"
        assert "posixUid" in body
        assert "posixGid" in body
        assert "cognitoSub" in body
        assert "createdAt" in body

    def test_create_user_stores_record_in_dynamodb(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        table = user_mgmt_env["table"]

        event = build_admin_event("POST", "/users", body={
            "userId": "bob",
            "displayName": "Bob Jones",
            "email": "bob@example.com",
        })
        handler_mod.handler(event, None)

        item = table.get_item(Key={"PK": "USER#bob", "SK": "PROFILE"})
        assert "Item" in item
        stored = item["Item"]
        assert stored["userId"] == "bob"
        assert stored["displayName"] == "Bob Jones"
        assert stored["status"] == "ACTIVE"

    def test_create_user_creates_cognito_user(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        pool_id = user_mgmt_env["pool_id"]

        event = build_admin_event("POST", "/users", body={
            "userId": "carol",
            "displayName": "Carol Lee",
            "email": "carol@example.com",
        })
        handler_mod.handler(event, None)

        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
        resp = cognito_client.admin_get_user(UserPoolId=pool_id, Username="carol")
        assert resp["Username"] == "carol"
        assert resp["Enabled"] is True

    def test_create_user_assigns_posix_uid_and_gid(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "dave",
            "displayName": "Dave",
            "email": "dave@example.com",
        })
        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        # UID/GID are assigned and match each other
        assert body["posixUid"] == body["posixGid"]
        assert body["posixUid"] >= 10001


# ---------------------------------------------------------------------------
# User creation – duplicate identifier
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserCreationDuplicate:
    """Validates: Requirement 1.3"""

    def test_duplicate_user_returns_409(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        create_body = {
            "userId": "dup-user",
            "displayName": "First",
            "email": "dup@example.com",
        }
        event = build_admin_event("POST", "/users", body=create_body)
        handler_mod.handler(event, None)

        # Second creation with same userId
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "DUPLICATE_ERROR"
        assert "dup-user" in body["error"]["message"]


# ---------------------------------------------------------------------------
# User creation – missing fields
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserCreationValidation:
    """Validates: Requirement 1.1 (input validation)"""

    def test_missing_user_id_returns_400(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "displayName": "No ID",
            "email": "noid@example.com",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_display_name_returns_400(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "noname",
            "email": "noname@example.com",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_email_returns_400(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "noemail",
            "displayName": "No Email",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_body_returns_400(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body=None)
        event["body"] = None
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# User deactivation and session revocation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserDeactivation:
    """Validates: Requirement 1.2"""

    def test_deactivate_user_sets_status_inactive(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        table = user_mgmt_env["table"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "deact-user",
                "displayName": "Deactivate Me",
                "email": "deact@example.com",
            }),
            None,
        )

        response = handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "deact-user"}),
            None,
        )

        assert response["statusCode"] == 200
        item = table.get_item(Key={"PK": "USER#deact-user", "SK": "PROFILE"})
        assert item["Item"]["status"] == "INACTIVE"

    def test_deactivate_user_disables_cognito_user(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        pool_id = user_mgmt_env["pool_id"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "cog-deact",
                "displayName": "Cognito Deact",
                "email": "cogdeact@example.com",
            }),
            None,
        )
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "cog-deact"}),
            None,
        )

        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
        resp = cognito_client.admin_get_user(UserPoolId=pool_id, Username="cog-deact")
        assert resp["Enabled"] is False

    def test_deactivate_nonexistent_user_returns_404(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "ghost"}),
            None,
        )

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# POSIX UID/GID atomic counter increment
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestPosixCounter:
    """Validates: Requirements 1.1, 17.1"""

    def test_posix_uid_increments_across_multiple_users(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        uids = []
        for i in range(3):
            event = build_admin_event("POST", "/users", body={
                "userId": f"counter-user-{i}",
                "displayName": f"Counter User {i}",
                "email": f"counter{i}@example.com",
            })
            response = handler_mod.handler(event, None)
            body = json.loads(response["body"])
            uids.append(body["posixUid"])

        # UIDs should be strictly increasing
        assert uids == sorted(uids)
        assert len(set(uids)) == len(uids), "UIDs must be unique"

    def test_posix_gid_matches_uid(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "gid-check",
            "displayName": "GID Check",
            "email": "gid@example.com",
        })
        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert body["posixUid"] == body["posixGid"]


# ---------------------------------------------------------------------------
# GET /users – list users (admin only)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestListUsers:
    """Validates: Requirements 1.1, 1.4"""

    def test_list_users_returns_active_users(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        for name in ["list-a", "list-b"]:
            handler_mod.handler(
                build_admin_event("POST", "/users", body={
                    "userId": name,
                    "displayName": f"Display {name}",
                    "email": f"{name}@example.com",
                }),
                None,
            )

        response = handler_mod.handler(build_admin_event("GET", "/users"), None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        user_ids = [u["userId"] for u in body["users"]]
        assert "list-a" in user_ids
        assert "list-b" in user_ids

    def test_list_users_includes_deactivated(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "inactive-u",
                "displayName": "Inactive",
                "email": "inactive-u@example.com",
            }),
            None,
        )
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "inactive-u"}),
            None,
        )

        response = handler_mod.handler(build_admin_event("GET", "/users"), None)
        body = json.loads(response["body"])
        user_ids = [u["userId"] for u in body["users"]]
        assert "inactive-u" in user_ids
        # Verify the inactive user has the correct status
        inactive_user = next(u for u in body["users"] if u["userId"] == "inactive-u")
        assert inactive_user["status"] == "INACTIVE"


# ---------------------------------------------------------------------------
# GET /users/{userId} – get specific user
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestGetUser:
    """Validates: Requirement 1.1"""

    def test_get_user_returns_user_details(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "fetch-me",
                "displayName": "Fetch Me",
                "email": "fetch@example.com",
            }),
            None,
        )

        response = handler_mod.handler(
            build_admin_event("GET", "/users/{userId}", path_parameters={"userId": "fetch-me"}),
            None,
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["userId"] == "fetch-me"
        assert body["displayName"] == "Fetch Me"

    def test_get_nonexistent_user_returns_404(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_admin_event("GET", "/users/{userId}", path_parameters={"userId": "no-such-user"}),
            None,
        )

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    def test_non_admin_can_view_own_profile(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "self-viewer",
                "displayName": "Self Viewer",
                "email": "self@example.com",
            }),
            None,
        )

        response = handler_mod.handler(
            build_non_admin_event("GET", "/users/{userId}", caller="self-viewer", path_parameters={"userId": "self-viewer"}),
            None,
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["userId"] == "self-viewer"

    def test_non_admin_cannot_view_other_profile(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "other-user",
                "displayName": "Other",
                "email": "other@example.com",
            }),
            None,
        )

        response = handler_mod.handler(
            build_non_admin_event("GET", "/users/{userId}", caller="nosy-user", path_parameters={"userId": "other-user"}),
            None,
        )

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# Authorisation – non-admin rejection
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestAuthorisationRejection:
    """Validates: Requirement 1.4"""

    def test_non_admin_cannot_create_user(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_non_admin_event("POST", "/users", body={
                "userId": "sneaky",
                "displayName": "Sneaky",
                "email": "sneaky@example.com",
            }),
            None,
        )

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_admin_cannot_delete_user(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_non_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "someone"}),
            None,
        )

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_admin_cannot_list_users(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_non_admin_event("GET", "/users"),
            None,
        )

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_caller_with_no_groups_is_rejected(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = {
            "httpMethod": "POST",
            "resource": "/users",
            "pathParameters": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "no-groups",
                        "sub": "sub-no-groups",
                        "cognito:groups": "",
                    }
                }
            },
            "body": json.dumps({
                "userId": "attempt",
                "displayName": "Attempt",
                "email": "attempt@example.com",
            }),
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# User reactivation – happy path
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserReactivationHappyPath:
    """Validates: Requirements 1.1, 1.3"""

    def test_reactivate_user_returns_200(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        # Create and deactivate a user
        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "react-happy",
                "displayName": "React Happy",
                "email": "react-happy@example.com",
            }),
            None,
        )
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "react-happy"}),
            None,
        )

        # Reactivate
        response = handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "react-happy"}),
            None,
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["userId"] == "react-happy"
        assert body["displayName"] == "React Happy"
        assert body["email"] == "react-happy@example.com"
        assert body["status"] == "ACTIVE"
        assert "posixUid" in body
        assert "posixGid" in body
        assert "updatedAt" in body

    def test_reactivate_user_sets_status_active_in_dynamodb(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        table = user_mgmt_env["table"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "react-ddb",
                "displayName": "React DDB",
                "email": "react-ddb@example.com",
            }),
            None,
        )
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "react-ddb"}),
            None,
        )

        # Confirm INACTIVE before reactivation
        item = table.get_item(Key={"PK": "USER#react-ddb", "SK": "PROFILE"})
        assert item["Item"]["status"] == "INACTIVE"

        handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "react-ddb"}),
            None,
        )

        item = table.get_item(Key={"PK": "USER#react-ddb", "SK": "PROFILE"})
        assert item["Item"]["status"] == "ACTIVE"

    def test_reactivate_user_reenables_cognito(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        pool_id = user_mgmt_env["pool_id"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "react-cog",
                "displayName": "React Cognito",
                "email": "react-cog@example.com",
            }),
            None,
        )
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "react-cog"}),
            None,
        )

        # Confirm Cognito disabled after deactivation
        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
        resp = cognito_client.admin_get_user(UserPoolId=pool_id, Username="react-cog")
        assert resp["Enabled"] is False

        handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "react-cog"}),
            None,
        )

        resp = cognito_client.admin_get_user(UserPoolId=pool_id, Username="react-cog")
        assert resp["Enabled"] is True


# ---------------------------------------------------------------------------
# User reactivation – POSIX identity preservation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserReactivationPosixPreservation:
    """Validates: Requirement 1.2"""

    def test_posix_uid_gid_unchanged_after_reactivation(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        # Create user and capture POSIX IDs
        create_response = handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "posix-preserve",
                "displayName": "POSIX Preserve",
                "email": "posix@example.com",
            }),
            None,
        )
        create_body = json.loads(create_response["body"])
        original_uid = create_body["posixUid"]
        original_gid = create_body["posixGid"]

        # Deactivate
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "posix-preserve"}),
            None,
        )

        # Reactivate
        react_response = handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "posix-preserve"}),
            None,
        )
        react_body = json.loads(react_response["body"])

        assert int(react_body["posixUid"]) == int(original_uid)
        assert int(react_body["posixGid"]) == int(original_gid)

    def test_posix_uid_gid_unchanged_in_dynamodb_after_reactivation(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        table = user_mgmt_env["table"]

        create_response = handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "posix-ddb",
                "displayName": "POSIX DDB",
                "email": "posix-ddb@example.com",
            }),
            None,
        )
        create_body = json.loads(create_response["body"])
        original_uid = create_body["posixUid"]
        original_gid = create_body["posixGid"]

        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "posix-ddb"}),
            None,
        )
        handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "posix-ddb"}),
            None,
        )

        item = table.get_item(Key={"PK": "USER#posix-ddb", "SK": "PROFILE"})
        assert int(item["Item"]["posixUid"]) == original_uid
        assert int(item["Item"]["posixGid"]) == original_gid


# ---------------------------------------------------------------------------
# User reactivation – validation errors
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserReactivationValidation:
    """Validates: Requirements 1.4, 1.5"""

    def test_reactivate_already_active_user_returns_400(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "already-active",
                "displayName": "Already Active",
                "email": "already-active@example.com",
            }),
            None,
        )

        response = handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "already-active"}),
            None,
        )

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "already active" in body["error"]["message"].lower()

    def test_reactivate_nonexistent_user_returns_404(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "no-such-user-react"}),
            None,
        )

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# User reactivation – authorisation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserReactivationAuthorisation:
    """Validates: Requirement 2.1"""

    def test_non_admin_cannot_reactivate_user(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        response = handler_mod.handler(
            build_non_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "someone"}),
            None,
        )

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_caller_with_no_groups_cannot_reactivate(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = {
            "httpMethod": "POST",
            "resource": "/users/{userId}/reactivate",
            "pathParameters": {"userId": "someone"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "no-groups-react",
                        "sub": "sub-no-groups-react",
                        "cognito:groups": "",
                    }
                }
            },
            "body": None,
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# List users – includes both ACTIVE and INACTIVE users
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestListUsersIncludesInactive:
    """Validates: Requirement 5.1"""

    def test_list_returns_both_active_and_inactive_users(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        # Create two users
        for uid in ["list-active-u", "list-inactive-u"]:
            handler_mod.handler(
                build_admin_event("POST", "/users", body={
                    "userId": uid,
                    "displayName": f"Display {uid}",
                    "email": f"{uid}@example.com",
                }),
                None,
            )

        # Deactivate one
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "list-inactive-u"}),
            None,
        )

        response = handler_mod.handler(build_admin_event("GET", "/users"), None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        user_ids = [u["userId"] for u in body["users"]]
        assert "list-active-u" in user_ids
        assert "list-inactive-u" in user_ids

        # Verify statuses are correct
        active_user = next(u for u in body["users"] if u["userId"] == "list-active-u")
        inactive_user = next(u for u in body["users"] if u["userId"] == "list-inactive-u")
        assert active_user["status"] == "ACTIVE"
        assert inactive_user["status"] == "INACTIVE"

    def test_list_returns_reactivated_user_as_active(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/users", body={
                "userId": "list-reactivated",
                "displayName": "List Reactivated",
                "email": "list-reactivated@example.com",
            }),
            None,
        )
        handler_mod.handler(
            build_admin_event("DELETE", "/users/{userId}", path_parameters={"userId": "list-reactivated"}),
            None,
        )
        handler_mod.handler(
            build_admin_event("POST", "/users/{userId}/reactivate", path_parameters={"userId": "list-reactivated"}),
            None,
        )

        response = handler_mod.handler(build_admin_event("GET", "/users"), None)
        body = json.loads(response["body"])
        reactivated = next(u for u in body["users"] if u["userId"] == "list-reactivated")
        assert reactivated["status"] == "ACTIVE"

# ---------------------------------------------------------------------------
# User creation – role assignment
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("user_mgmt_env")
class TestUserCreationRoleAssignment:
    """Validates role selection during user creation."""

    def test_create_user_default_role_is_user(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-default",
            "displayName": "Role Default",
            "email": "role-default@example.com",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["role"] == "User"

    def test_create_user_with_explicit_user_role(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-explicit-user",
            "displayName": "Explicit User",
            "email": "role-explicit-user@example.com",
            "role": "User",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["role"] == "User"

    def test_create_user_with_administrator_role(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-admin",
            "displayName": "Admin User",
            "email": "role-admin@example.com",
            "role": "Administrator",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["role"] == "Administrator"

    def test_create_administrator_adds_to_cognito_group(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        pool_id = user_mgmt_env["pool_id"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-admin-cog",
            "displayName": "Admin Cognito",
            "email": "role-admin-cog@example.com",
            "role": "Administrator",
        })
        handler_mod.handler(event, None)

        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
        groups_resp = cognito_client.admin_list_groups_for_user(
            UserPoolId=pool_id, Username="role-admin-cog"
        )
        group_names = [g["GroupName"] for g in groups_resp["Groups"]]
        assert "Administrators" in group_names

    def test_create_regular_user_not_in_administrators_group(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        pool_id = user_mgmt_env["pool_id"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-regular-cog",
            "displayName": "Regular Cognito",
            "email": "role-regular-cog@example.com",
            "role": "User",
        })
        handler_mod.handler(event, None)

        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
        groups_resp = cognito_client.admin_list_groups_for_user(
            UserPoolId=pool_id, Username="role-regular-cog"
        )
        group_names = [g["GroupName"] for g in groups_resp["Groups"]]
        assert "Administrators" not in group_names

    def test_create_user_with_invalid_role_returns_400(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-invalid",
            "displayName": "Invalid Role",
            "email": "role-invalid@example.com",
            "role": "SuperAdmin",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "role" in body["error"]["message"].lower()

    def test_create_user_role_stored_in_dynamodb(self, user_mgmt_env):
        handler_mod, _, _ = user_mgmt_env["modules"]
        table = user_mgmt_env["table"]

        event = build_admin_event("POST", "/users", body={
            "userId": "role-ddb",
            "displayName": "Role DDB",
            "email": "role-ddb@example.com",
            "role": "Administrator",
        })
        handler_mod.handler(event, None)

        item = table.get_item(Key={"PK": "USER#role-ddb", "SK": "PROFILE"})
        assert item["Item"]["role"] == "Administrator"
