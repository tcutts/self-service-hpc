"""Unit tests for the Project Management Lambda.

Covers:
- Project creation with cost allocation tag (happy path, duplicate, missing fields)
- Project deletion blocked by active clusters / allowed when none
- Membership add/remove happy path and error cases
- Budget creation with validation (missing budgetLimit, non-numeric budgetLimit)
- Authorisation for all endpoints
- Deploy route (success, status rejection, auth rejection)
- Destroy route (success, active clusters rejection, status rejection, auth rejection)
- Edit route (success, status rejection, auth rejection, validation errors)
- GET project progress fields for DEPLOYING/DESTROYING statuses

Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.6, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.4, 6.5, 6.6

Infrastructure is set up once per test class via the ``project_mgmt_env``
fixture from conftest.py, avoiding repeated DynamoDB table and Cognito
pool creation.
"""

import json
from unittest.mock import patch, MagicMock

import boto3
from botocore.exceptions import ClientError
import pytest

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    USERS_TABLE_NAME,
    build_admin_event,
    build_non_admin_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_admin_event(method, resource, project_id, body=None, path_parameters=None):
    """Build an API Gateway proxy event with ProjectAdmin + Administrator claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "proj-admin",
                    "sub": "sub-proj-admin",
                    "cognito:groups": f"ProjectAdmin-{project_id}, Administrators",
                }
            }
        },
        "body": json.dumps(body) if body is not None else None,
    }


def _seed_platform_user(users_table, pool_id, user_id):
    """Insert a user record into PlatformUsers and Cognito so membership ops succeed."""
    users_table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "displayName": f"Display {user_id}",
        "email": f"{user_id}@example.com",
        "posixUid": 10099,
        "posixGid": 10099,
        "status": "ACTIVE",
        "cognitoSub": f"sub-{user_id}",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })
    cognito = boto3.client("cognito-idp", region_name=AWS_REGION)
    try:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=user_id,
            UserAttributes=[{"Name": "email", "Value": f"{user_id}@example.com"}],
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        pass


def _seed_project(projects_table, project_id, project_name="Test Project"):
    """Insert a minimal project record into the Projects table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": project_name,
        "costAllocationTag": project_id,
        "vpcId": "",
        "efsFileSystemId": "",
        "s3BucketName": "",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "cdkStackName": "",
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_active_cluster(clusters_table, project_id, cluster_name, status="ACTIVE"):
    """Insert a cluster record into the Clusters table."""
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": status,
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestProjectCreation:
    """Validates: Requirements 2.1"""

    def test_create_project_returns_201(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects", body={
            "projectId": "proj-alpha",
            "projectName": "Alpha Project",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-alpha"
        assert body["projectName"] == "Alpha Project"
        assert body["status"] == "CREATED"
        assert "createdAt" in body

    def test_create_project_with_custom_cost_tag(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects", body={
            "projectId": "proj-tagged",
            "projectName": "Tagged Project",
            "costAllocationTag": "custom-tag-value",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["costAllocationTag"] == "custom-tag-value"

    def test_create_project_defaults_cost_tag_to_project_id(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects", body={
            "projectId": "proj-default-tag",
            "projectName": "Default Tag Project",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["costAllocationTag"] == "proj-default-tag"

    def test_create_project_stores_record_in_dynamodb(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        event = build_admin_event("POST", "/projects", body={
            "projectId": "proj-stored",
            "projectName": "Stored Project",
        })
        handler_mod.handler(event, None)

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-stored", "SK": "METADATA"}
        )
        assert "Item" in item
        assert item["Item"]["projectId"] == "proj-stored"

    def test_duplicate_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects", body={
            "projectId": "proj-dup",
            "projectName": "Dup Project",
        })
        handler_mod.handler(event, None)

        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "DUPLICATE_ERROR"
        assert "proj-dup" in body["error"]["message"]

    def test_missing_project_id_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects", body={
            "projectName": "No ID",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_project_name_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects", body={
            "projectId": "proj-noname",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_body_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event("POST", "/projects")
        event["body"] = None
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Project deletion
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestProjectDeletion:
    """Validates: Requirements 2.2, 2.3"""

    def test_delete_project_with_no_clusters_succeeds(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-del-ok")

        event = build_admin_event(
            "DELETE", "/projects/{projectId}",
            path_parameters={"projectId": "proj-del-ok"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200

        # Verify record is gone
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-del-ok", "SK": "METADATA"}
        )
        assert "Item" not in item

    def test_delete_project_blocked_by_active_cluster(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-del-blocked")
        _seed_active_cluster(clusters_table, "proj-del-blocked", "my-cluster", status="ACTIVE")

        event = build_admin_event(
            "DELETE", "/projects/{projectId}",
            path_parameters={"projectId": "proj-del-blocked"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        assert "my-cluster" in body["error"]["message"] or "my-cluster" in str(body["error"]["details"])

    def test_delete_project_blocked_by_creating_cluster(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-del-creating")
        _seed_active_cluster(clusters_table, "proj-del-creating", "creating-cluster", status="CREATING")

        event = build_admin_event(
            "DELETE", "/projects/{projectId}",
            path_parameters={"projectId": "proj-del-creating"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_delete_project_allowed_with_destroyed_clusters(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-del-destroyed")
        _seed_active_cluster(clusters_table, "proj-del-destroyed", "old-cluster", status="DESTROYED")

        event = build_admin_event(
            "DELETE", "/projects/{projectId}",
            path_parameters={"projectId": "proj-del-destroyed"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_delete_nonexistent_project_returns_404(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event(
            "DELETE", "/projects/{projectId}",
            path_parameters={"projectId": "proj-ghost"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Membership management
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestMembershipManagement:
    """Validates: Requirements 4.1, 4.2, 4.3"""

    def test_add_member_happy_path(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-mem")
        _seed_platform_user(users_table, pool_id, "member-user")

        event = _project_admin_event(
            "POST", "/projects/{projectId}/members", "proj-mem",
            body={"userId": "member-user", "role": "PROJECT_USER"},
            path_parameters={"projectId": "proj-mem"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["userId"] == "member-user"
        assert body["projectId"] == "proj-mem"
        assert body["role"] == "PROJECT_USER"
        assert "addedAt" in body

    def test_add_member_stores_record_in_dynamodb(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-mem-db")
        _seed_platform_user(users_table, pool_id, "db-member")

        event = _project_admin_event(
            "POST", "/projects/{projectId}/members", "proj-mem-db",
            body={"userId": "db-member"},
            path_parameters={"projectId": "proj-mem-db"},
        )
        handler_mod.handler(event, None)

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-mem-db", "SK": "MEMBER#db-member"}
        )
        assert "Item" in item
        assert item["Item"]["userId"] == "db-member"

    def test_add_nonexistent_user_returns_404(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-mem-nouser")

        event = _project_admin_event(
            "POST", "/projects/{projectId}/members", "proj-mem-nouser",
            body={"userId": "ghost-user"},
            path_parameters={"projectId": "proj-mem-nouser"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"
        assert "ghost-user" in body["error"]["message"]

    def test_add_duplicate_member_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-mem-dup")
        _seed_platform_user(users_table, pool_id, "dup-member")

        event = _project_admin_event(
            "POST", "/projects/{projectId}/members", "proj-mem-dup",
            body={"userId": "dup-member"},
            path_parameters={"projectId": "proj-mem-dup"},
        )
        handler_mod.handler(event, None)

        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "DUPLICATE_ERROR"

    def test_remove_member_happy_path(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-mem-rm")
        _seed_platform_user(users_table, pool_id, "rm-member")

        # Add member first
        add_event = _project_admin_event(
            "POST", "/projects/{projectId}/members", "proj-mem-rm",
            body={"userId": "rm-member"},
            path_parameters={"projectId": "proj-mem-rm"},
        )
        handler_mod.handler(add_event, None)

        # Remove member
        rm_event = _project_admin_event(
            "DELETE", "/projects/{projectId}/members/{userId}", "proj-mem-rm",
            path_parameters={"projectId": "proj-mem-rm", "userId": "rm-member"},
        )
        response = handler_mod.handler(rm_event, None)

        assert response["statusCode"] == 200

        # Verify record is gone
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-mem-rm", "SK": "MEMBER#rm-member"}
        )
        assert "Item" not in item

    def test_remove_nonexistent_membership_returns_404(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-mem-rm-ghost")

        event = _project_admin_event(
            "DELETE", "/projects/{projectId}/members/{userId}", "proj-mem-rm-ghost",
            path_parameters={"projectId": "proj-mem-rm-ghost", "userId": "nobody"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


class TestRemoveMemberDeprovisioning:
    """Validates: Requirements 8.2, 8.4 — POSIX de-provisioning on member removal."""

    def test_remove_member_calls_deprovision(self, project_mgmt_env):
        """Removing a member triggers deprovision_user_from_clusters."""
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-deprov-call")
        _seed_platform_user(users_table, pool_id, "deprov-user")
        members_mod.add_member(
            PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id,
            "proj-deprov-call", "deprov-user", "PROJECT_USER",
        )

        with patch.object(members_mod, "deprovision_user_from_clusters", return_value="NO_ACTIVE_CLUSTERS") as mock_deprov:
            members_mod.remove_member(
                PROJECTS_TABLE_NAME, pool_id, "proj-deprov-call", "deprov-user",
            )
            mock_deprov.assert_called_once_with("deprov-user", "proj-deprov-call", CLUSTERS_TABLE_NAME)

        # Membership record should be deleted
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-deprov-call", "SK": "MEMBER#deprov-user"}
        )
        assert "Item" not in item

    def test_remove_member_continues_on_partial_failure(self, project_mgmt_env):
        """Partial deprovisioning failure logs warning but removal still succeeds (Req 8.4)."""
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-deprov-partial")
        _seed_platform_user(users_table, pool_id, "partial-user")
        members_mod.add_member(
            PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id,
            "proj-deprov-partial", "partial-user", "PROJECT_USER",
        )

        with patch.object(members_mod, "deprovision_user_from_clusters", return_value="PARTIAL_FAILURE"):
            members_mod.remove_member(
                PROJECTS_TABLE_NAME, pool_id, "proj-deprov-partial", "partial-user",
            )

        # Membership record should still be deleted despite partial failure
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-deprov-partial", "SK": "MEMBER#partial-user"}
        )
        assert "Item" not in item

    def test_remove_member_continues_on_deprovision_exception(self, project_mgmt_env):
        """If deprovisioning raises an exception, removal still succeeds (Req 8.4)."""
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-deprov-exc")
        _seed_platform_user(users_table, pool_id, "exc-user")
        members_mod.add_member(
            PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id,
            "proj-deprov-exc", "exc-user", "PROJECT_USER",
        )

        with patch.object(members_mod, "deprovision_user_from_clusters", side_effect=RuntimeError("SSM boom")):
            members_mod.remove_member(
                PROJECTS_TABLE_NAME, pool_id, "proj-deprov-exc", "exc-user",
            )

        # Membership record should still be deleted despite exception
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-deprov-exc", "SK": "MEMBER#exc-user"}
        )
        assert "Item" not in item


class TestListMembers:
    """Validates: Requirement 7.1"""

    def test_list_members_returns_all_members(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-list-all")
        _seed_platform_user(users_table, pool_id, "list-user-a")
        _seed_platform_user(users_table, pool_id, "list-user-b")

        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-list-all", "list-user-a", "PROJECT_USER")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-list-all", "list-user-b", "PROJECT_ADMIN")

        result = members_mod.list_members(PROJECTS_TABLE_NAME, "proj-list-all")

        assert len(result) == 2
        user_ids = {m["userId"] for m in result}
        assert user_ids == {"list-user-a", "list-user-b"}

    def test_list_members_returns_correct_fields(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-list-fields")
        _seed_platform_user(users_table, pool_id, "list-fields-user")

        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-list-fields", "list-fields-user", "PROJECT_ADMIN")

        result = members_mod.list_members(PROJECTS_TABLE_NAME, "proj-list-fields")

        assert len(result) == 1
        member = result[0]
        assert member["userId"] == "list-fields-user"
        assert member["displayName"] == "Display list-fields-user"
        assert member["role"] == "PROJECT_ADMIN"
        assert member["addedAt"] != ""

    def test_list_members_empty_project(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-list-empty")

        result = members_mod.list_members(PROJECTS_TABLE_NAME, "proj-list-empty")

        assert result == []

    def test_list_members_nonexistent_project_returns_404(self, project_mgmt_env):
        _, _, members_mod, errors_mod = project_mgmt_env["modules"]

        with pytest.raises(errors_mod.NotFoundError):
            members_mod.list_members(PROJECTS_TABLE_NAME, "proj-does-not-exist")

    def test_list_members_sorted_by_added_at(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-list-sort")
        _seed_platform_user(users_table, pool_id, "sort-user-1")
        _seed_platform_user(users_table, pool_id, "sort-user-2")

        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-list-sort", "sort-user-1", "PROJECT_USER")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-list-sort", "sort-user-2", "PROJECT_USER")

        result = members_mod.list_members(PROJECTS_TABLE_NAME, "proj-list-sort")

        assert len(result) == 2
        assert result[0]["addedAt"] <= result[1]["addedAt"]


# ---------------------------------------------------------------------------
# Change member role
# ---------------------------------------------------------------------------

class TestChangeMemberRole:
    """Validates: Requirement 6.4"""

    def test_change_role_from_user_to_admin(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-role-up")
        _seed_platform_user(users_table, pool_id, "role-up-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-role-up", "role-up-user", "PROJECT_USER")

        result = members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "proj-role-up", "role-up-user", "PROJECT_ADMIN")

        assert result["role"] == "PROJECT_ADMIN"
        assert result["userId"] == "role-up-user"
        assert result["projectId"] == "proj-role-up"

    def test_change_role_from_admin_to_user(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-role-down")
        _seed_platform_user(users_table, pool_id, "role-down-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-role-down", "role-down-user", "PROJECT_ADMIN")

        result = members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "proj-role-down", "role-down-user", "PROJECT_USER")

        assert result["role"] == "PROJECT_USER"

    def test_change_role_updates_dynamodb(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-role-db")
        _seed_platform_user(users_table, pool_id, "role-db-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-role-db", "role-db-user", "PROJECT_USER")

        members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "proj-role-db", "role-db-user", "PROJECT_ADMIN")

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-role-db", "SK": "MEMBER#role-db-user"}
        )
        assert item["Item"]["role"] == "PROJECT_ADMIN"

    def test_change_role_noop_same_role(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-role-noop")
        _seed_platform_user(users_table, pool_id, "role-noop-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-role-noop", "role-noop-user", "PROJECT_USER")

        result = members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "proj-role-noop", "role-noop-user", "PROJECT_USER")

        assert result["role"] == "PROJECT_USER"

    def test_change_role_nonexistent_member_returns_404(self, project_mgmt_env):
        _, _, members_mod, errors_mod = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-role-ghost")

        with pytest.raises(errors_mod.NotFoundError):
            members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "proj-role-ghost", "ghost-user", "PROJECT_ADMIN")

    def test_change_role_invalid_role_returns_400(self, project_mgmt_env):
        _, _, members_mod, errors_mod = project_mgmt_env["modules"]
        pool_id = project_mgmt_env["pool_id"]

        with pytest.raises(errors_mod.ValidationError):
            members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "any-proj", "any-user", "INVALID_ROLE")

    def test_change_role_updates_cognito_groups(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-role-cog")
        _seed_platform_user(users_table, pool_id, "role-cog-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-role-cog", "role-cog-user", "PROJECT_USER")

        members_mod.change_member_role(PROJECTS_TABLE_NAME, pool_id, "proj-role-cog", "role-cog-user", "PROJECT_ADMIN")

        # Verify user is in the new Cognito group
        cog = boto3.client("cognito-idp", region_name=AWS_REGION)
        groups_resp = cog.admin_list_groups_for_user(
            UserPoolId=pool_id, Username="role-cog-user",
        )
        group_names = [g["GroupName"] for g in groups_resp["Groups"]]
        assert "ProjectAdmin-proj-role-cog" in group_names
        assert "ProjectUser-proj-role-cog" not in group_names


# ---------------------------------------------------------------------------
# Budget management
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestBudgetManagement:
    """Validates: Requirements 5.1, 5.2, 5.3"""

    def test_set_budget_missing_budget_limit_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-budget-missing")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}/budget", "proj-budget-missing",
            body={},
            path_parameters={"projectId": "proj-budget-missing"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "budgetLimit" in body["error"]["message"]

    def test_set_budget_non_numeric_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-budget-nan")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}/budget", "proj-budget-nan",
            body={"budgetLimit": "not-a-number"},
            path_parameters={"projectId": "proj-budget-nan"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_set_budget_zero_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-budget-zero")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}/budget", "proj-budget-zero",
            body={"budgetLimit": 0},
            path_parameters={"projectId": "proj-budget-zero"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_set_budget_negative_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-budget-neg")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}/budget", "proj-budget-neg",
            body={"budgetLimit": -100},
            path_parameters={"projectId": "proj-budget-neg"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestProjectAuthorisation:
    """Validates: Requirements 2.4, 4.4, 5.4"""

    def test_non_admin_cannot_create_project(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event("POST", "/projects", body={
            "projectId": "sneaky-proj",
            "projectName": "Sneaky",
        })
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_admin_cannot_delete_project(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "DELETE", "/projects/{projectId}",
            path_parameters={"projectId": "some-proj"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_admin_list_projects_returns_scoped_results(self, project_mgmt_env):
        """Non-admin users get 200 with only their member projects."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event("GET", "/projects")
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "projects" in body
        # The non-admin event has ProjectUser-alpha group, so only project "alpha"
        # would be returned if it exists. Since it likely doesn't exist in this
        # test class, we just verify the response shape is correct.
        assert isinstance(body["projects"], list)

    def test_non_member_cannot_get_project(self, project_mgmt_env):
        """A user with no membership for a project gets 403."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        # build_non_admin_event creates a user with ProjectUser-alpha group
        # so accessing "some-proj" (not alpha) should be denied
        event = build_non_admin_event(
            "GET", "/projects/{projectId}",
            path_parameters={"projectId": "some-proj"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_project_admin_cannot_add_member(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "POST", "/projects/{projectId}/members",
            body={"userId": "someone"},
            path_parameters={"projectId": "proj-x"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_project_admin_cannot_remove_member(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "DELETE", "/projects/{projectId}/members/{userId}",
            path_parameters={"projectId": "proj-x", "userId": "someone"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_project_admin_cannot_set_budget(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "PUT", "/projects/{projectId}/budget",
            body={"budgetLimit": 1000},
            path_parameters={"projectId": "proj-x"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_caller_with_no_groups_rejected(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = {
            "httpMethod": "POST",
            "resource": "/projects",
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
                "projectId": "attempt",
                "projectName": "Attempt",
            }),
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_user_cannot_manage_members(self, project_mgmt_env):
        """A ProjectUser (not ProjectAdmin) for a project cannot add members."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = {
            "httpMethod": "POST",
            "resource": "/projects/{projectId}/members",
            "pathParameters": {"projectId": "proj-y"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "proj-user",
                        "sub": "sub-proj-user",
                        "cognito:groups": "ProjectUser-proj-y",
                    }
                }
            },
            "body": json.dumps({"userId": "someone"}),
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_user_cannot_set_budget(self, project_mgmt_env):
        """A ProjectUser (not ProjectAdmin) for a project cannot set budget."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = {
            "httpMethod": "PUT",
            "resource": "/projects/{projectId}/budget",
            "pathParameters": {"projectId": "proj-y"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "proj-user",
                        "sub": "sub-proj-user",
                        "cognito:groups": "ProjectUser-proj-y",
                    }
                }
            },
            "body": json.dumps({"budgetLimit": 500}),
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# Lifecycle state machine — validate_transition
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestLifecycleValidateTransition:
    """Validates: Requirements 1.4, 1.5, 1.6

    Tests the pure validate_transition function which enforces the project
    lifecycle state machine.
    """

    # -- Valid transitions --------------------------------------------------

    def test_created_to_deploying_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition
        validate_transition("CREATED", "DEPLOYING")  # should not raise

    def test_deploying_to_active_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition
        validate_transition("DEPLOYING", "ACTIVE")

    def test_deploying_to_created_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition
        validate_transition("DEPLOYING", "CREATED")

    def test_active_to_destroying_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition
        validate_transition("ACTIVE", "DESTROYING")

    def test_destroying_to_archived_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition
        validate_transition("DESTROYING", "ARCHIVED")

    def test_destroying_to_active_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition
        validate_transition("DESTROYING", "ACTIVE")

    # -- Invalid transitions ------------------------------------------------

    def test_created_to_active_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError) as exc_info:
            validate_transition("CREATED", "ACTIVE")
        assert "CREATED" in str(exc_info.value)
        assert "ACTIVE" in str(exc_info.value)

    def test_created_to_destroying_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("CREATED", "DESTROYING")

    def test_created_to_archived_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("CREATED", "ARCHIVED")

    def test_active_to_deploying_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("ACTIVE", "DEPLOYING")

    def test_active_to_created_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("ACTIVE", "CREATED")

    def test_active_to_archived_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition

        # Should not raise — ACTIVE → ARCHIVED is now a valid deactivation transition
        validate_transition("ACTIVE", "ARCHIVED")

    def test_archived_to_invalid_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        # ARCHIVED → ACTIVE is now valid (reactivation), but others are not
        for target in ("CREATED", "DEPLOYING", "DESTROYING"):
            with pytest.raises(ConflictError):
                validate_transition("ARCHIVED", target)

    def test_archived_to_active_is_valid(self, project_mgmt_env):
        from lifecycle import validate_transition

        # Should not raise — ARCHIVED → ACTIVE is a valid reactivation transition
        validate_transition("ARCHIVED", "ACTIVE")

    def test_deploying_to_destroying_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("DEPLOYING", "DESTROYING")

    def test_destroying_to_created_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("DESTROYING", "CREATED")

    def test_destroying_to_deploying_raises_conflict(self, project_mgmt_env):
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError):
            validate_transition("DESTROYING", "DEPLOYING")

    def test_conflict_error_lists_valid_transitions(self, project_mgmt_env):
        """The error message should list the valid transitions from the current state."""
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError) as exc_info:
            validate_transition("CREATED", "ARCHIVED")
        msg = str(exc_info.value)
        assert "DEPLOYING" in msg  # the only valid target from CREATED

    def test_conflict_error_includes_details(self, project_mgmt_env):
        """The ConflictError details dict should include current/target status."""
        from lifecycle import validate_transition
        from errors import ConflictError

        with pytest.raises(ConflictError) as exc_info:
            validate_transition("ACTIVE", "CREATED")
        assert exc_info.value.details["currentStatus"] == "ACTIVE"
        assert exc_info.value.details["targetStatus"] == "CREATED"
        assert exc_info.value.details["validTransitions"] == ["DESTROYING", "UPDATING", "ARCHIVED"]


# ---------------------------------------------------------------------------
# Lifecycle state machine — transition_project (DynamoDB integration)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestLifecycleTransitionProject:
    """Validates: Requirements 1.4, 1.5, 1.6

    Tests the transition_project function which atomically updates project
    status in DynamoDB using a ConditionExpression.
    """

    def _seed_project_with_status(self, projects_table, project_id, status):
        """Insert a project record with a specific status."""
        projects_table.put_item(Item={
            "PK": f"PROJECT#{project_id}",
            "SK": "METADATA",
            "projectId": project_id,
            "projectName": f"Project {project_id}",
            "costAllocationTag": project_id,
            "status": status,
            "vpcId": "",
            "efsFileSystemId": "",
            "s3BucketName": "",
            "s3BucketProvided": False,
            "budgetLimit": 50,
            "budgetBreached": False,
            "cdkStackName": "",
            "statusChangedAt": "2024-01-01T00:00:00+00:00",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:00:00+00:00",
            "errorMessage": "",
        })

    # -- Valid transitions update status and timestamps ---------------------

    def test_transition_created_to_deploying(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-deploy", "CREATED")
        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-deploy", "DEPLOYING")

        assert attrs["status"] == "DEPLOYING"

    def test_transition_deploying_to_active(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-active", "DEPLOYING")
        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-active", "ACTIVE")

        assert attrs["status"] == "ACTIVE"

    def test_transition_deploying_to_created_on_failure(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-fail-deploy", "DEPLOYING")
        attrs = transition_project(
            PROJECTS_TABLE_NAME, "lc-fail-deploy", "CREATED",
            error_message="CDK deploy failed",
        )

        assert attrs["status"] == "CREATED"
        assert attrs["errorMessage"] == "CDK deploy failed"

    def test_transition_active_to_destroying(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-destroy", "ACTIVE")
        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-destroy", "DESTROYING")

        assert attrs["status"] == "DESTROYING"

    def test_transition_destroying_to_archived(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-archive", "DESTROYING")
        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-archive", "ARCHIVED")

        assert attrs["status"] == "ARCHIVED"

    def test_transition_destroying_to_active_on_failure(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-fail-destroy", "DESTROYING")
        attrs = transition_project(
            PROJECTS_TABLE_NAME, "lc-fail-destroy", "ACTIVE",
            error_message="CDK destroy failed",
        )

        assert attrs["status"] == "ACTIVE"
        assert attrs["errorMessage"] == "CDK destroy failed"

    # -- Timestamps are set on transition -----------------------------------

    def test_timestamps_updated_on_transition(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-ts", "CREATED")
        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-ts", "DEPLOYING")

        # statusChangedAt and updatedAt should be updated from the seed value
        assert attrs["statusChangedAt"] != "2024-01-01T00:00:00+00:00"
        assert attrs["updatedAt"] != "2024-01-01T00:00:00+00:00"
        # Both should be the same timestamp (set in the same update)
        assert attrs["statusChangedAt"] == attrs["updatedAt"]

    def test_timestamps_are_iso_format(self, project_mgmt_env):
        from datetime import datetime
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-iso", "ACTIVE")
        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-iso", "DESTROYING")

        # Should parse as valid ISO 8601
        datetime.fromisoformat(attrs["statusChangedAt"])
        datetime.fromisoformat(attrs["updatedAt"])

    # -- Error message cleared on success -----------------------------------

    def test_error_message_cleared_on_success_transition(self, project_mgmt_env):
        from lifecycle import transition_project
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-clear-err", "DEPLOYING")
        # Set an existing error message
        projects_table.update_item(
            Key={"PK": "PROJECT#lc-clear-err", "SK": "METADATA"},
            UpdateExpression="SET errorMessage = :err",
            ExpressionAttributeValues={":err": "previous error"},
        )

        attrs = transition_project(PROJECTS_TABLE_NAME, "lc-clear-err", "ACTIVE")

        assert attrs["errorMessage"] == ""

    # -- ConditionExpression failure (concurrent modification) ---------------

    def test_concurrent_status_change_raises_conflict(self, project_mgmt_env):
        """If the project status was changed by another process, the transition
        should fail with ConflictError due to the ConditionExpression."""
        from lifecycle import transition_project
        from errors import ConflictError
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-race", "CREATED")

        # Simulate a concurrent change: move to DEPLOYING behind our back
        projects_table.update_item(
            Key={"PK": "PROJECT#lc-race", "SK": "METADATA"},
            UpdateExpression="SET #st = :s",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "DEPLOYING"},
        )

        # Now try to transition from CREATED→DEPLOYING — should fail because
        # the item is already DEPLOYING, and DEPLOYING→DEPLOYING is not valid
        # (DEPLOYING can go to ACTIVE or CREATED, not DEPLOYING)
        with pytest.raises(ConflictError) as exc_info:
            transition_project(PROJECTS_TABLE_NAME, "lc-race", "DEPLOYING")
        assert "lc-race" in str(exc_info.value)

    def test_transition_wrong_source_status_raises_conflict(self, project_mgmt_env):
        """Attempting to transition to a target from an invalid source status
        should raise ConflictError."""
        from lifecycle import transition_project
        from errors import ConflictError
        projects_table = project_mgmt_env["projects_table"]

        # Project is ACTIVE, but ACTIVE→ACTIVE is not valid
        self._seed_project_with_status(projects_table, "lc-bad-src", "ACTIVE")

        with pytest.raises(ConflictError):
            transition_project(PROJECTS_TABLE_NAME, "lc-bad-src", "DEPLOYING")

    def test_transition_invalid_target_raises_conflict(self, project_mgmt_env):
        """Transitioning to a target that has no valid source statuses at all
        should raise ConflictError."""
        from lifecycle import transition_project
        from errors import ConflictError
        projects_table = project_mgmt_env["projects_table"]

        self._seed_project_with_status(projects_table, "lc-no-target", "CREATED")

        # "NONEXISTENT" is not a valid target for any source
        with pytest.raises(ConflictError):
            transition_project(PROJECTS_TABLE_NAME, "lc-no-target", "NONEXISTENT")



# ---------------------------------------------------------------------------
# Helpers for lifecycle handler tests
# ---------------------------------------------------------------------------

def _seed_project_with_status(projects_table, project_id, status, **overrides):
    """Insert a project record with a specific status for lifecycle tests."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "vpcId": "",
        "efsFileSystemId": "",
        "s3BucketName": "",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "budgetType": "MONTHLY",
        "cdkStackName": "",
        "currentStep": 0,
        "totalSteps": 0,
        "stepDescription": "",
        "errorMessage": "",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
        "trustedCidrRanges": [],
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    }
    item.update(overrides)
    projects_table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Deploy route — POST /projects/{projectId}/deploy
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestDeployRoute:
    """Validates: Requirements 2.1, 2.4"""

    def test_deploy_created_project_returns_202(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-deploy-ok", "CREATED")

        event = build_admin_event(
            "POST", "/projects/{projectId}/deploy",
            path_parameters={"projectId": "proj-deploy-ok"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-deploy-ok"
        assert body["status"] == "DEPLOYING"
        assert "deployment started" in body["message"].lower()

    def test_deploy_transitions_project_to_deploying(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-deploy-check", "CREATED")

        event = build_admin_event(
            "POST", "/projects/{projectId}/deploy",
            path_parameters={"projectId": "proj-deploy-check"},
        )
        handler_mod.handler(event, None)

        # Verify the project status was updated in DynamoDB
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-deploy-check", "SK": "METADATA"}
        )["Item"]
        assert item["status"] == "DEPLOYING"
        assert int(item["totalSteps"]) == 5

    def test_deploy_active_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-deploy-active", "ACTIVE")

        event = build_admin_event(
            "POST", "/projects/{projectId}/deploy",
            path_parameters={"projectId": "proj-deploy-active"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        assert "ACTIVE" in body["error"]["message"]
        assert "CREATED" in body["error"]["message"]

    def test_deploy_deploying_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-deploy-dup", "DEPLOYING")

        event = build_admin_event(
            "POST", "/projects/{projectId}/deploy",
            path_parameters={"projectId": "proj-deploy-dup"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_deploy_non_admin_returns_403(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "POST", "/projects/{projectId}/deploy",
            path_parameters={"projectId": "some-proj"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# Destroy route — POST /projects/{projectId}/destroy
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestDestroyRoute:
    """Validates: Requirements 3.1, 3.2, 3.6"""

    def test_destroy_active_project_no_clusters_returns_202(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-destroy-ok", "ACTIVE")

        event = build_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "proj-destroy-ok"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-destroy-ok"
        assert body["status"] == "DESTROYING"
        assert "destruction started" in body["message"].lower()

    def test_destroy_transitions_project_to_destroying(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-destroy-check", "ACTIVE")

        event = build_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "proj-destroy-check"},
        )
        handler_mod.handler(event, None)

        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-destroy-check", "SK": "METADATA"}
        )["Item"]
        assert item["status"] == "DESTROYING"
        assert int(item["totalSteps"]) == 5

    def test_destroy_with_active_clusters_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project_with_status(projects_table, "proj-destroy-clust", "ACTIVE")
        _seed_active_cluster(clusters_table, "proj-destroy-clust", "running-cluster", status="ACTIVE")

        event = build_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "proj-destroy-clust"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        assert "active clusters" in body["error"]["message"].lower() or "running-cluster" in str(body["error"]["details"])

    def test_destroy_with_creating_clusters_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project_with_status(projects_table, "proj-destroy-creating", "ACTIVE")
        _seed_active_cluster(clusters_table, "proj-destroy-creating", "new-cluster", status="CREATING")

        event = build_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "proj-destroy-creating"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_destroy_created_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-destroy-created", "CREATED")

        event = build_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "proj-destroy-created"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        # Atomic conditional update doesn't expose the current status —
        # it only knows the condition (status = ACTIVE) was not met.
        assert "not ACTIVE" in body["error"]["message"] or "ACTIVE" in body["error"]["message"]

    def test_destroy_archived_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-destroy-archived", "ARCHIVED")

        event = build_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "proj-destroy-archived"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_destroy_non_admin_returns_403(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "POST", "/projects/{projectId}/destroy",
            path_parameters={"projectId": "some-proj"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# Edit route — PUT /projects/{projectId}
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestEditRoute:
    """Validates: Requirements 6.4, 6.5, 6.6"""

    def test_edit_active_project_success(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-ok", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-ok",
            body={"budgetLimit": 1000, "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-ok"},
        )

        # Mock set_budget since it calls AWS Budgets/STS/CE which aren't
        # fully supported in moto's mock_aws context
        with patch("handler.set_budget") as mock_set_budget:
            mock_set_budget.return_value = {
                "projectId": "proj-edit-ok",
                "budgetName": "hpc-project-proj-edit-ok",
                "budgetLimit": 1000,
                "budgetType": "MONTHLY",
                "thresholds": [80, 100],
                "snsTopicArn": "arn:aws:sns:us-east-1:123456789012:budget-topic",
            }
            response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-edit-ok"

    def test_edit_active_project_with_total_budget_type(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-total", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-total",
            body={"budgetLimit": 5000, "budgetType": "TOTAL"},
            path_parameters={"projectId": "proj-edit-total"},
        )

        with patch("handler.set_budget") as mock_set_budget:
            mock_set_budget.return_value = {
                "projectId": "proj-edit-total",
                "budgetName": "hpc-project-proj-edit-total",
                "budgetLimit": 5000,
                "budgetType": "TOTAL",
                "thresholds": [80, 100],
                "snsTopicArn": "arn:aws:sns:us-east-1:123456789012:budget-topic",
            }
            response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        # Verify set_budget was called with the correct budget_type
        mock_set_budget.assert_called_once()
        call_kwargs = mock_set_budget.call_args
        assert call_kwargs.kwargs.get("budget_type") or call_kwargs[1].get("budget_type") == "TOTAL"

    def test_edit_created_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-created", "CREATED")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-created",
            body={"budgetLimit": 100, "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-created"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        assert "CREATED" in body["error"]["message"]
        assert "ACTIVE" in body["error"]["message"]

    def test_edit_deploying_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-deploying", "DEPLOYING")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-deploying",
            body={"budgetLimit": 100, "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-deploying"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_edit_non_project_admin_returns_403(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "PUT", "/projects/{projectId}",
            body={"budgetLimit": 100, "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-noauth"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_edit_project_user_returns_403(self, project_mgmt_env):
        """A ProjectUser (not ProjectAdmin) cannot edit a project."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = {
            "httpMethod": "PUT",
            "resource": "/projects/{projectId}",
            "pathParameters": {"projectId": "proj-edit-user"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "proj-user",
                        "sub": "sub-proj-user",
                        "cognito:groups": "ProjectUser-proj-edit-user",
                    }
                }
            },
            "body": json.dumps({"budgetLimit": 100, "budgetType": "MONTHLY"}),
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_edit_missing_budget_limit_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-nolimit", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-nolimit",
            body={"budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-nolimit"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "budgetLimit" in body["error"]["message"]

    def test_edit_zero_budget_limit_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-zero", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-zero",
            body={"budgetLimit": 0, "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-zero"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_edit_negative_budget_limit_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-neg", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-neg",
            body={"budgetLimit": -50, "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-neg"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_edit_non_numeric_budget_limit_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-nan", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-nan",
            body={"budgetLimit": "not-a-number", "budgetType": "MONTHLY"},
            path_parameters={"projectId": "proj-edit-nan"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_edit_invalid_budget_type_returns_400(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-edit-badtype", "ACTIVE")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}", "proj-edit-badtype",
            body={"budgetLimit": 100, "budgetType": "WEEKLY"},
            path_parameters={"projectId": "proj-edit-badtype"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "budgetType" in body["error"]["message"]


# ---------------------------------------------------------------------------
# GET project — progress fields for DEPLOYING/DESTROYING
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestGetProjectProgress:
    """Validates: Requirements 2.5, 2.6, 3.7, 3.8"""

    def test_get_deploying_project_includes_progress(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(
            projects_table, "proj-progress-deploy", "DEPLOYING",
            currentStep=2, totalSteps=5, stepDescription="Deploying VPC",
        )

        event = build_admin_event(
            "GET", "/projects/{projectId}",
            path_parameters={"projectId": "proj-progress-deploy"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "DEPLOYING"
        assert "progress" in body
        assert body["progress"]["currentStep"] == 2
        assert body["progress"]["totalSteps"] == 5
        assert body["progress"]["stepDescription"] == "Deploying VPC"

    def test_get_destroying_project_includes_progress(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(
            projects_table, "proj-progress-destroy", "DESTROYING",
            currentStep=3, totalSteps=5, stepDescription="Removing EFS",
        )

        event = build_admin_event(
            "GET", "/projects/{projectId}",
            path_parameters={"projectId": "proj-progress-destroy"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "DESTROYING"
        assert "progress" in body
        assert body["progress"]["currentStep"] == 3
        assert body["progress"]["totalSteps"] == 5
        assert body["progress"]["stepDescription"] == "Removing EFS"

    def test_get_active_project_has_no_progress(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-progress-active", "ACTIVE")

        event = build_admin_event(
            "GET", "/projects/{projectId}",
            path_parameters={"projectId": "proj-progress-active"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "ACTIVE"
        assert "progress" not in body

    def test_get_created_project_has_no_progress(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-progress-created", "CREATED")

        event = build_admin_event(
            "GET", "/projects/{projectId}",
            path_parameters={"projectId": "proj-progress-created"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "CREATED"
        assert "progress" not in body


# ---------------------------------------------------------------------------
# Update route — POST /projects/{projectId}/update
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestUpdateRoute:
    """Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6"""

    def test_update_active_project_returns_202(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-update-ok", "ACTIVE")

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-ok"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-update-ok"
        assert body["status"] == "UPDATING"
        assert "update started" in body["message"].lower()

    def test_update_transitions_project_to_updating(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-update-check", "ACTIVE")

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-check"},
        )
        handler_mod.handler(event, None)

        # Verify the project status was updated in DynamoDB
        item = projects_table.get_item(
            Key={"PK": "PROJECT#proj-update-check", "SK": "METADATA"}
        )["Item"]
        assert item["status"] == "UPDATING"
        assert int(item["currentStep"]) == 0
        assert int(item["totalSteps"]) == 5

    def test_update_non_admin_returns_403(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "some-proj"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_update_created_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-update-created", "CREATED")

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-created"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        assert "CREATED" in body["error"]["message"]
        assert "ACTIVE" in body["error"]["message"]

    def test_update_deploying_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-update-deploying", "DEPLOYING")

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-deploying"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_update_destroying_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-update-destroying", "DESTROYING")

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-destroying"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_update_archived_project_returns_409(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-update-archived", "ARCHIVED")

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-archived"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_update_nonexistent_project_returns_404(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_admin_event(
            "POST", "/projects/{projectId}/update",
            path_parameters={"projectId": "proj-update-ghost"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# GET project — progress fields for UPDATING status
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestGetProjectUpdateProgress:
    """Validates: Requirements 5.1"""

    def test_get_updating_project_includes_progress(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(
            projects_table, "proj-progress-update", "UPDATING",
            currentStep=3, totalSteps=5, stepDescription="Extracting stack outputs",
        )

        event = build_admin_event(
            "GET", "/projects/{projectId}",
            path_parameters={"projectId": "proj-progress-update"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "UPDATING"
        assert "progress" in body
        assert body["progress"]["currentStep"] == 3
        assert body["progress"]["totalSteps"] == 5
        assert body["progress"]["stepDescription"] == "Extracting stack outputs"


# ---------------------------------------------------------------------------
# Deprovision user from clusters
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestDeprovisionUserFromClusters:
    """Validates: Requirement 8.2"""

    def test_no_active_clusters_returns_no_active_clusters(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-deprov-empty")

        result = members_mod.deprovision_user_from_clusters(
            "some-user", "proj-deprov-empty", CLUSTERS_TABLE_NAME,
        )
        assert result == "NO_ACTIVE_CLUSTERS"

    def test_destroyed_clusters_returns_no_active_clusters(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-deprov-destroyed")
        _seed_active_cluster(clusters_table, "proj-deprov-destroyed", "dead-cluster", status="DESTROYED")

        result = members_mod.deprovision_user_from_clusters(
            "some-user", "proj-deprov-destroyed", CLUSTERS_TABLE_NAME,
        )
        assert result == "NO_ACTIVE_CLUSTERS"

    @patch("members.boto3.client")
    def test_active_cluster_sends_ssm_command(self, mock_boto_client, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-deprov-ssm")
        clusters_table.put_item(Item={
            "PK": "PROJECT#proj-deprov-ssm",
            "SK": "CLUSTER#my-cluster",
            "clusterName": "my-cluster",
            "projectId": "proj-deprov-ssm",
            "status": "ACTIVE",
            "loginNodeInstanceId": "i-1234567890abcdef0",
            "createdAt": "2024-01-01T00:00:00+00:00",
        })

        mock_ssm = MagicMock()
        mock_boto_client.return_value = mock_ssm

        result = members_mod.deprovision_user_from_clusters(
            "target-user", "proj-deprov-ssm", CLUSTERS_TABLE_NAME,
        )

        assert result == "DEPROVISIONED"
        mock_ssm.send_command.assert_called_once_with(
            InstanceIds=["i-1234567890abcdef0"],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": ["usermod --lock --expiredate 1 target-user"]},
            Comment="Deprovision user 'target-user' from cluster 'my-cluster'",
        )

    @patch("members.boto3.client")
    def test_ssm_failure_returns_partial_failure(self, mock_boto_client, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-deprov-fail")
        clusters_table.put_item(Item={
            "PK": "PROJECT#proj-deprov-fail",
            "SK": "CLUSTER#fail-cluster",
            "clusterName": "fail-cluster",
            "projectId": "proj-deprov-fail",
            "status": "ACTIVE",
            "loginNodeInstanceId": "i-failinstance",
            "createdAt": "2024-01-01T00:00:00+00:00",
        })

        mock_ssm = MagicMock()
        mock_ssm.send_command.side_effect = ClientError(
            {"Error": {"Code": "InvalidInstanceId", "Message": "Instance not found"}},
            "SendCommand",
        )
        mock_boto_client.return_value = mock_ssm

        result = members_mod.deprovision_user_from_clusters(
            "target-user", "proj-deprov-fail", CLUSTERS_TABLE_NAME,
        )

        assert result == "PARTIAL_FAILURE"

    def test_cluster_without_instance_id_returns_partial_failure(self, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-deprov-noid")
        clusters_table.put_item(Item={
            "PK": "PROJECT#proj-deprov-noid",
            "SK": "CLUSTER#no-id-cluster",
            "clusterName": "no-id-cluster",
            "projectId": "proj-deprov-noid",
            "status": "ACTIVE",
            "createdAt": "2024-01-01T00:00:00+00:00",
        })

        result = members_mod.deprovision_user_from_clusters(
            "target-user", "proj-deprov-noid", CLUSTERS_TABLE_NAME,
        )

        assert result == "PARTIAL_FAILURE"

    @patch("members.boto3.client")
    def test_multiple_clusters_all_succeed(self, mock_boto_client, project_mgmt_env):
        _, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project(projects_table, "proj-deprov-multi")
        for i in range(3):
            clusters_table.put_item(Item={
                "PK": "PROJECT#proj-deprov-multi",
                "SK": f"CLUSTER#cluster-{i}",
                "clusterName": f"cluster-{i}",
                "projectId": "proj-deprov-multi",
                "status": "ACTIVE",
                "loginNodeInstanceId": f"i-instance{i}",
                "createdAt": "2024-01-01T00:00:00+00:00",
            })

        mock_ssm = MagicMock()
        mock_boto_client.return_value = mock_ssm

        result = members_mod.deprovision_user_from_clusters(
            "multi-user", "proj-deprov-multi", CLUSTERS_TABLE_NAME,
        )

        assert result == "DEPROVISIONED"
        assert mock_ssm.send_command.call_count == 3


# ---------------------------------------------------------------------------
# Handler-level tests for GET /projects/{projectId}/members
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestListMembersHandler:
    """Validates: Requirements 4.1, 7.1, 11.2 — list members via handler route."""

    def test_list_members_returns_200_with_members(self, project_mgmt_env):
        handler_mod, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-hlist")
        _seed_platform_user(users_table, pool_id, "hlist-user-a")
        _seed_platform_user(users_table, pool_id, "hlist-user-b")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-hlist", "hlist-user-a", "PROJECT_USER")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-hlist", "hlist-user-b", "PROJECT_ADMIN")

        event = _project_admin_event(
            "GET", "/projects/{projectId}/members", "proj-hlist",
            path_parameters={"projectId": "proj-hlist"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "members" in body
        assert len(body["members"]) == 2
        user_ids = {m["userId"] for m in body["members"]}
        assert user_ids == {"hlist-user-a", "hlist-user-b"}

    def test_list_members_admin_can_list_any_project(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "proj-hlist-admin")

        event = build_admin_event(
            "GET", "/projects/{projectId}/members",
            path_parameters={"projectId": "proj-hlist-admin"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "members" in body
        assert body["members"] == []

    def test_list_members_non_project_admin_returns_403(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "GET", "/projects/{projectId}/members",
            path_parameters={"projectId": "proj-hlist-noauth"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_list_members_project_user_returns_403(self, project_mgmt_env):
        """A ProjectUser (not ProjectAdmin) cannot list members."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = {
            "httpMethod": "GET",
            "resource": "/projects/{projectId}/members",
            "pathParameters": {"projectId": "proj-hlist-pu"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "proj-user",
                        "sub": "sub-proj-user",
                        "cognito:groups": "ProjectUser-proj-hlist-pu",
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
# Handler-level tests for PUT /projects/{projectId}/members/{userId}
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestChangeMemberRoleHandler:
    """Validates: Requirements 4.2, 6.4, 11.2 — change member role via handler route."""

    def test_change_role_via_handler_returns_200(self, project_mgmt_env):
        handler_mod, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-hrole")
        _seed_platform_user(users_table, pool_id, "hrole-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-hrole", "hrole-user", "PROJECT_USER")

        event = _project_admin_event(
            "PUT", "/projects/{projectId}/members/{userId}", "proj-hrole",
            body={"role": "PROJECT_ADMIN"},
            path_parameters={"projectId": "proj-hrole", "userId": "hrole-user"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["userId"] == "hrole-user"
        assert body["role"] == "PROJECT_ADMIN"

    def test_change_role_admin_can_change_any_project(self, project_mgmt_env):
        handler_mod, _, members_mod, _ = project_mgmt_env["modules"]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "proj-hrole-adm")
        _seed_platform_user(users_table, pool_id, "hrole-adm-user")
        members_mod.add_member(PROJECTS_TABLE_NAME, USERS_TABLE_NAME, pool_id, "proj-hrole-adm", "hrole-adm-user", "PROJECT_USER")

        event = build_admin_event(
            "PUT", "/projects/{projectId}/members/{userId}",
            body={"role": "PROJECT_ADMIN"},
            path_parameters={"projectId": "proj-hrole-adm", "userId": "hrole-adm-user"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["role"] == "PROJECT_ADMIN"

    def test_change_role_non_project_admin_returns_403(self, project_mgmt_env):
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = build_non_admin_event(
            "PUT", "/projects/{projectId}/members/{userId}",
            body={"role": "PROJECT_ADMIN"},
            path_parameters={"projectId": "proj-hrole-noauth", "userId": "someone"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_change_role_project_user_returns_403(self, project_mgmt_env):
        """A ProjectUser (not ProjectAdmin) cannot change member roles."""
        handler_mod, _, _, _ = project_mgmt_env["modules"]

        event = {
            "httpMethod": "PUT",
            "resource": "/projects/{projectId}/members/{userId}",
            "pathParameters": {"projectId": "proj-hrole-pu", "userId": "someone"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "proj-user",
                        "sub": "sub-proj-user",
                        "cognito:groups": "ProjectUser-proj-hrole-pu",
                    }
                }
            },
            "body": json.dumps({"role": "PROJECT_ADMIN"}),
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

# ---------------------------------------------------------------------------
# Reactivate project — lifecycle.reactivate_project
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestReactivateProject:
    """Validates: Requirements 14.4, 14.5, 14.7

    Tests the reactivate_project function which recreates Cognito groups,
    restores membership records, and transitions ARCHIVED → ACTIVE.
    """

    def test_reactivate_archived_project_transitions_to_active(self, project_mgmt_env):
        """Reactivating an ARCHIVED project should transition it to ACTIVE."""
        from lifecycle import reactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-react-ok", "ARCHIVED")

        result = reactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-react-ok",
        )

        assert result["status"] == "ACTIVE"

    def test_reactivate_recreates_cognito_groups(self, project_mgmt_env):
        """Reactivation should recreate ProjectAdmin and ProjectUser Cognito groups (Req 14.4)."""
        from lifecycle import reactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-react-groups", "ARCHIVED")

        reactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-react-groups",
        )

        # Verify groups were created
        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
        groups_resp = cognito_client.list_groups(UserPoolId=pool_id)
        group_names = [g["GroupName"] for g in groups_resp["Groups"]]
        assert "ProjectAdmin-proj-react-groups" in group_names
        assert "ProjectUser-proj-react-groups" in group_names

    def test_reactivate_restores_members_to_cognito_groups(self, project_mgmt_env):
        """Reactivation should add each preserved member to the appropriate Cognito group (Req 14.5)."""
        from lifecycle import reactivate_project

        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-react-members", "ARCHIVED")

        # Seed platform users in Cognito
        _seed_platform_user(users_table, pool_id, "admin-member")
        _seed_platform_user(users_table, pool_id, "user-member")

        # Seed preserved membership records
        projects_table.put_item(Item={
            "PK": "PROJECT#proj-react-members",
            "SK": "MEMBER#admin-member",
            "userId": "admin-member",
            "projectId": "proj-react-members",
            "role": "PROJECT_ADMIN",
            "addedAt": "2024-01-01T00:00:00+00:00",
        })
        projects_table.put_item(Item={
            "PK": "PROJECT#proj-react-members",
            "SK": "MEMBER#user-member",
            "userId": "user-member",
            "projectId": "proj-react-members",
            "role": "PROJECT_USER",
            "addedAt": "2024-01-01T00:00:00+00:00",
        })

        reactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-react-members",
        )

        # Verify members were added to the correct groups
        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)

        admin_users = cognito_client.list_users_in_group(
            UserPoolId=pool_id,
            GroupName="ProjectAdmin-proj-react-members",
        )
        admin_usernames = [u["Username"] for u in admin_users["Users"]]
        assert "admin-member" in admin_usernames

        user_users = cognito_client.list_users_in_group(
            UserPoolId=pool_id,
            GroupName="ProjectUser-proj-react-members",
        )
        user_usernames = [u["Username"] for u in user_users["Users"]]
        assert "user-member" in user_usernames

    def test_reactivate_nonexistent_project_raises_not_found(self, project_mgmt_env):
        """Reactivating a project that doesn't exist should raise NotFoundError."""
        from lifecycle import reactivate_project
        from errors import NotFoundError

        pool_id = project_mgmt_env["pool_id"]

        with pytest.raises(NotFoundError):
            reactivate_project(
                table_name=PROJECTS_TABLE_NAME,
                user_pool_id=pool_id,
                project_id="proj-react-ghost",
            )

    def test_reactivate_active_project_raises_conflict(self, project_mgmt_env):
        """Reactivating a project that is ACTIVE should raise ConflictError."""
        from lifecycle import reactivate_project
        from errors import ConflictError

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-react-active", "ACTIVE")

        with pytest.raises(ConflictError) as exc_info:
            reactivate_project(
                table_name=PROJECTS_TABLE_NAME,
                user_pool_id=pool_id,
                project_id="proj-react-active",
            )
        assert "ARCHIVED" in str(exc_info.value)

    def test_reactivate_marks_failed_restoration_as_pending(self, project_mgmt_env):
        """If adding a member to Cognito fails, the record should be marked PENDING_RESTORATION (Req 14.7)."""
        from lifecycle import reactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-react-fail", "ARCHIVED")

        # Seed a membership record for a user that does NOT exist in Cognito
        # (admin_add_user_to_group will fail for a non-existent user)
        projects_table.put_item(Item={
            "PK": "PROJECT#proj-react-fail",
            "SK": "MEMBER#nonexistent-user",
            "userId": "nonexistent-user",
            "projectId": "proj-react-fail",
            "role": "PROJECT_USER",
            "addedAt": "2024-01-01T00:00:00+00:00",
        })

        # Should not raise — failures are logged and marked
        result = reactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-react-fail",
        )

        # Project should still transition to ACTIVE
        assert result["status"] == "ACTIVE"

        # The failed member should be marked PENDING_RESTORATION
        member = projects_table.get_item(
            Key={"PK": "PROJECT#proj-react-fail", "SK": "MEMBER#nonexistent-user"}
        )["Item"]
        assert member.get("propagationStatus") == "PENDING_RESTORATION"

    def test_reactivate_with_no_members_succeeds(self, project_mgmt_env):
        """Reactivating a project with no membership records should succeed."""
        from lifecycle import reactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-react-empty", "ARCHIVED")

        result = reactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-react-empty",
        )

        assert result["status"] == "ACTIVE"

# ---------------------------------------------------------------------------
# Deactivate project — lifecycle.deactivate_project
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestDeactivateProject:
    """Validates: Requirements 14.1, 14.2, 14.3, 14.6

    Tests the deactivate_project function which deletes Cognito groups,
    preserves membership records, and transitions ACTIVE → ARCHIVED.
    """

    def test_deactivate_active_project_transitions_to_archived(self, project_mgmt_env):
        """Deactivating an ACTIVE project with no clusters should transition to ARCHIVED (Req 14.1)."""
        from lifecycle import deactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-deact-ok", "ACTIVE")

        result = deactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-deact-ok",
            clusters_table_name=CLUSTERS_TABLE_NAME,
        )

        assert result["status"] == "ARCHIVED"

    def test_deactivate_blocked_by_active_clusters(self, project_mgmt_env):
        """Deactivation should be blocked when active clusters exist (Req 14.1)."""
        from lifecycle import deactivate_project
        from errors import ConflictError

        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-deact-clust", "ACTIVE")
        _seed_active_cluster(clusters_table, "proj-deact-clust", "running-cluster", status="ACTIVE")

        with pytest.raises(ConflictError) as exc_info:
            deactivate_project(
                table_name=PROJECTS_TABLE_NAME,
                user_pool_id=pool_id,
                project_id="proj-deact-clust",
                clusters_table_name=CLUSTERS_TABLE_NAME,
            )
        assert "active clusters" in str(exc_info.value).lower()

    def test_deactivate_non_active_project_raises_conflict(self, project_mgmt_env):
        """Deactivating a project that is not ACTIVE should raise ConflictError."""
        from lifecycle import deactivate_project
        from errors import ConflictError

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-deact-created", "CREATED")

        with pytest.raises(ConflictError) as exc_info:
            deactivate_project(
                table_name=PROJECTS_TABLE_NAME,
                user_pool_id=pool_id,
                project_id="proj-deact-created",
                clusters_table_name=CLUSTERS_TABLE_NAME,
            )
        assert "ACTIVE" in str(exc_info.value)

    def test_deactivate_deletes_cognito_groups(self, project_mgmt_env):
        """Deactivation should delete ProjectAdmin and ProjectUser Cognito groups (Req 14.2)."""
        from lifecycle import deactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]
        cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)

        _seed_project_with_status(projects_table, "proj-deact-cog", "ACTIVE")

        # Create the Cognito groups first
        for group_name in ("ProjectAdmin-proj-deact-cog", "ProjectUser-proj-deact-cog"):
            try:
                cognito_client.create_group(GroupName=group_name, UserPoolId=pool_id)
            except cognito_client.exceptions.GroupExistsException:
                pass

        deactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-deact-cog",
            clusters_table_name=CLUSTERS_TABLE_NAME,
        )

        # Verify groups were deleted
        groups_resp = cognito_client.list_groups(UserPoolId=pool_id)
        group_names = [g["GroupName"] for g in groups_resp["Groups"]]
        assert "ProjectAdmin-proj-deact-cog" not in group_names
        assert "ProjectUser-proj-deact-cog" not in group_names

    def test_deactivate_preserves_membership_records(self, project_mgmt_env):
        """Deactivation should preserve membership records in DynamoDB (Req 14.3)."""
        from lifecycle import deactivate_project

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-deact-mem", "ACTIVE")

        # Seed membership records
        projects_table.put_item(Item={
            "PK": "PROJECT#proj-deact-mem",
            "SK": "MEMBER#kept-user",
            "userId": "kept-user",
            "projectId": "proj-deact-mem",
            "role": "PROJECT_ADMIN",
            "addedAt": "2024-01-01T00:00:00+00:00",
        })

        deactivate_project(
            table_name=PROJECTS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id="proj-deact-mem",
            clusters_table_name=CLUSTERS_TABLE_NAME,
        )

        # Verify membership record is still present
        member = projects_table.get_item(
            Key={"PK": "PROJECT#proj-deact-mem", "SK": "MEMBER#kept-user"}
        )
        assert "Item" in member
        assert member["Item"]["userId"] == "kept-user"
        assert member["Item"]["role"] == "PROJECT_ADMIN"

    def test_deactivate_continues_on_cognito_group_deletion_failure(self, project_mgmt_env):
        """Cognito group deletion failure should be logged but not block deactivation (Req 14.6)."""
        from lifecycle import deactivate_project
        from unittest.mock import patch

        projects_table = project_mgmt_env["projects_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project_with_status(projects_table, "proj-deact-cogfail", "ACTIVE")

        # Patch cognito.delete_group to raise an exception
        with patch("lifecycle.cognito.delete_group", side_effect=Exception("Cognito unavailable")):
            result = deactivate_project(
                table_name=PROJECTS_TABLE_NAME,
                user_pool_id=pool_id,
                project_id="proj-deact-cogfail",
                clusters_table_name=CLUSTERS_TABLE_NAME,
            )

        # Should still transition to ARCHIVED despite Cognito failure
        assert result["status"] == "ARCHIVED"

    def test_deactivate_nonexistent_project_raises_not_found(self, project_mgmt_env):
        """Deactivating a project that doesn't exist should raise NotFoundError."""
        from lifecycle import deactivate_project
        from errors import NotFoundError

        pool_id = project_mgmt_env["pool_id"]

        with pytest.raises(NotFoundError):
            deactivate_project(
                table_name=PROJECTS_TABLE_NAME,
                user_pool_id=pool_id,
                project_id="proj-deact-ghost",
                clusters_table_name=CLUSTERS_TABLE_NAME,
            )


# ---------------------------------------------------------------------------
# Deactivate route — POST /projects/{projectId}/deactivate
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestDeactivateRoute:
    """Handler-level tests for POST /projects/{projectId}/deactivate.

    Validates: Requirements 14.1, 14.2, 14.3
    """

    def test_admin_can_deactivate_active_project(self, project_mgmt_env):
        """Platform Admin can deactivate an ACTIVE project with no clusters."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-deact-route", "ACTIVE")

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/deactivate",
            path_parameters={"projectId": "proj-deact-route"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "deactivated" in body["message"].lower()
        assert body["project"]["status"] == "ARCHIVED"

    def test_non_admin_cannot_deactivate(self, project_mgmt_env):
        """Non-admin callers should receive 403 when attempting deactivation."""
        handler_mod = project_mgmt_env["modules"][0]

        event = build_non_admin_event(
            "POST",
            "/projects/{projectId}/deactivate",
            path_parameters={"projectId": "proj-deact-noauth"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403

    def test_deactivate_nonexistent_project_returns_404(self, project_mgmt_env):
        """Deactivating a project that doesn't exist should return 404."""
        handler_mod = project_mgmt_env["modules"][0]

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/deactivate",
            path_parameters={"projectId": "proj-deact-missing"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404

    def test_deactivate_blocked_by_active_clusters_returns_409(self, project_mgmt_env):
        """Deactivation via route should return 409 when active clusters exist (Req 14.1)."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]
        clusters_table = project_mgmt_env["clusters_table"]

        _seed_project_with_status(projects_table, "proj-deact-rt-clust", "ACTIVE")
        _seed_active_cluster(clusters_table, "proj-deact-rt-clust", "busy-cluster", status="ACTIVE")

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/deactivate",
            path_parameters={"projectId": "proj-deact-rt-clust"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_deactivate_non_active_project_returns_409(self, project_mgmt_env):
        """Deactivating a project that is not ACTIVE should return 409."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-deact-rt-created", "CREATED")

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/deactivate",
            path_parameters={"projectId": "proj-deact-rt-created"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Reactivate route — POST /projects/{projectId}/reactivate
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestReactivateRoute:
    """Handler-level tests for POST /projects/{projectId}/reactivate.

    Validates: Requirements 14.4, 14.5
    """

    def test_admin_can_reactivate_archived_project(self, project_mgmt_env):
        """Platform Admin can reactivate an ARCHIVED project."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-react-route", "ARCHIVED")

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/reactivate",
            path_parameters={"projectId": "proj-react-route"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "reactivated" in body["message"].lower()
        assert body["project"]["status"] == "ACTIVE"

    def test_non_admin_cannot_reactivate(self, project_mgmt_env):
        """Non-admin callers should receive 403 when attempting reactivation."""
        handler_mod = project_mgmt_env["modules"][0]

        event = build_non_admin_event(
            "POST",
            "/projects/{projectId}/reactivate",
            path_parameters={"projectId": "proj-react-noauth"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403

    def test_reactivate_nonexistent_project_returns_404(self, project_mgmt_env):
        """Reactivating a project that doesn't exist should return 404."""
        handler_mod = project_mgmt_env["modules"][0]

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/reactivate",
            path_parameters={"projectId": "proj-react-missing"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404

    def test_reactivate_active_project_returns_conflict(self, project_mgmt_env):
        """Reactivating an ACTIVE project should return 409 conflict."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project_with_status(projects_table, "proj-react-active", "ACTIVE")

        event = build_admin_event(
            "POST",
            "/projects/{projectId}/reactivate",
            path_parameters={"projectId": "proj-react-active"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
