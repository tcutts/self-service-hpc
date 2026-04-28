"""Unit tests for authorization enforcement across all handlers.

Covers cross-cutting authorization scenarios that validate role-based
access control is enforced consistently:

- Scoped project listing for each role tier (Req 12.1, 12.2, 12.3)
- Membership operation authorization across projects (Req 11.2)
- Force-fail cluster authorization (Req 11.3)
- End User cannot manage members (Req 11.2, 5.5)

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 12.1, 12.2, 12.3
"""

import json
import os
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    USERS_TABLE_NAME,
    build_admin_event,
    build_non_admin_event,
    create_projects_table,
    create_clusters_table,
    create_users_table,
    create_cognito_pool,
    reload_project_mgmt_modules,
    reload_cluster_ops_handler_modules,
    create_cluster_name_registry_table,
)


# ---------------------------------------------------------------------------
# Event builder helpers
# ---------------------------------------------------------------------------

def _event_with_groups(method, resource, groups, caller="test-user", body=None, path_parameters=None):
    """Build an API Gateway proxy event with specific Cognito groups."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": groups,
                }
            }
        },
        "body": json.dumps(body) if body is not None else None,
    }


def _seed_project(projects_table, project_id, project_name=None):
    """Insert a minimal ACTIVE project record."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": project_name or f"Project {project_id}",
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


def _seed_platform_user(users_table, pool_id, user_id):
    """Insert a user record into PlatformUsers and Cognito."""
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


# ---------------------------------------------------------------------------
# Scoped project listing (Req 12.1, 12.2, 12.3)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestScopedProjectListing:
    """Validates: Requirements 12.1, 12.2, 12.3"""

    def test_platform_admin_sees_all_projects(self, project_mgmt_env):
        """Req 12.2: Platform Admin sees all projects."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "scope-all-a", "Project A")
        _seed_project(projects_table, "scope-all-b", "Project B")

        event = build_admin_event("GET", "/projects")
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        project_ids = [p["projectId"] for p in body["projects"]]
        assert "scope-all-a" in project_ids
        assert "scope-all-b" in project_ids

    def test_project_admin_sees_only_their_projects(self, project_mgmt_env):
        """Req 12.1: Project Admin sees only projects they administer."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "scope-pa-mine", "My Project")
        _seed_project(projects_table, "scope-pa-other", "Other Project")

        event = _event_with_groups(
            "GET", "/projects",
            groups="ProjectAdmin-scope-pa-mine",
            caller="proj-admin-scoped",
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        project_ids = [p["projectId"] for p in body["projects"]]
        assert "scope-pa-mine" in project_ids
        assert "scope-pa-other" not in project_ids

    def test_end_user_sees_only_their_projects(self, project_mgmt_env):
        """Req 12.3: End User sees only projects they are a member of."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "scope-eu-mine", "My EU Project")
        _seed_project(projects_table, "scope-eu-other", "Other EU Project")

        event = _event_with_groups(
            "GET", "/projects",
            groups="ProjectUser-scope-eu-mine",
            caller="end-user-scoped",
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        project_ids = [p["projectId"] for p in body["projects"]]
        assert "scope-eu-mine" in project_ids
        assert "scope-eu-other" not in project_ids

    def test_user_with_no_project_groups_sees_empty_list(self, project_mgmt_env):
        """User with no project group memberships sees an empty project list."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "scope-empty-a")

        event = _event_with_groups(
            "GET", "/projects",
            groups="",
            caller="no-groups-user",
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["projects"] == []

    def test_user_with_multiple_project_memberships(self, project_mgmt_env):
        """User in multiple projects sees all of them."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]

        _seed_project(projects_table, "scope-multi-a", "Multi A")
        _seed_project(projects_table, "scope-multi-b", "Multi B")

        event = _event_with_groups(
            "GET", "/projects",
            groups="ProjectUser-scope-multi-a, ProjectAdmin-scope-multi-b",
            caller="multi-user",
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        project_ids = [p["projectId"] for p in body["projects"]]
        assert "scope-multi-a" in project_ids
        assert "scope-multi-b" in project_ids


# ---------------------------------------------------------------------------
# Membership cross-project authorization (Req 11.2)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("project_mgmt_env")
class TestMembershipCrossProjectAuth:
    """Validates: Requirements 11.2, 4.3, 5.5"""

    def test_project_admin_can_add_member_to_own_project(self, project_mgmt_env):
        """Req 11.2: Project Admin can add members to their project."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "cross-own")
        _seed_platform_user(users_table, pool_id, "cross-new-member")

        event = _event_with_groups(
            "POST", "/projects/{projectId}/members",
            groups="ProjectAdmin-cross-own",
            caller="cross-admin",
            body={"userId": "cross-new-member", "role": "PROJECT_USER"},
            path_parameters={"projectId": "cross-own"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201

    def test_project_admin_cannot_add_member_to_other_project(self, project_mgmt_env):
        """Req 4.3: Project Admin for project A cannot add members to project B."""
        handler_mod = project_mgmt_env["modules"][0]

        event = _event_with_groups(
            "POST", "/projects/{projectId}/members",
            groups="ProjectAdmin-project-a",
            caller="admin-a",
            body={"userId": "someone", "role": "PROJECT_USER"},
            path_parameters={"projectId": "project-b"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_admin_cannot_remove_member_from_other_project(self, project_mgmt_env):
        """Req 4.3: Project Admin for project A cannot remove members from project B."""
        handler_mod = project_mgmt_env["modules"][0]

        event = _event_with_groups(
            "DELETE", "/projects/{projectId}/members/{userId}",
            groups="ProjectAdmin-project-a",
            caller="admin-a",
            path_parameters={"projectId": "project-b", "userId": "someone"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_end_user_cannot_add_member(self, project_mgmt_env):
        """Req 5.5: End User cannot add project members."""
        handler_mod = project_mgmt_env["modules"][0]

        event = _event_with_groups(
            "POST", "/projects/{projectId}/members",
            groups="ProjectUser-proj-eu",
            caller="end-user",
            body={"userId": "someone", "role": "PROJECT_USER"},
            path_parameters={"projectId": "proj-eu"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_end_user_cannot_remove_member(self, project_mgmt_env):
        """Req 5.5: End User cannot remove project members."""
        handler_mod = project_mgmt_env["modules"][0]

        event = _event_with_groups(
            "DELETE", "/projects/{projectId}/members/{userId}",
            groups="ProjectUser-proj-eu",
            caller="end-user",
            path_parameters={"projectId": "proj-eu", "userId": "someone"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_platform_admin_can_add_member_to_any_project(self, project_mgmt_env):
        """Req 3.3: Platform Admin can add members to any project."""
        handler_mod = project_mgmt_env["modules"][0]
        projects_table = project_mgmt_env["projects_table"]
        users_table = project_mgmt_env["users_table"]
        pool_id = project_mgmt_env["pool_id"]

        _seed_project(projects_table, "cross-any")
        _seed_platform_user(users_table, pool_id, "cross-any-member")

        event = build_admin_event(
            "POST", "/projects/{projectId}/members",
            body={"userId": "cross-any-member", "role": "PROJECT_USER"},
            path_parameters={"projectId": "cross-any"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201


# ---------------------------------------------------------------------------
# Cluster operations — force-fail authorization (Req 11.3)
# ---------------------------------------------------------------------------

class TestClusterForceFailAuth:
    """Validates: Requirement 11.3 — force-fail requires project membership."""

    @pytest.fixture(scope="class")
    def cluster_env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = "ClusterNameRegistry"
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

            # Seed a CREATING cluster for force-fail tests
            clusters_table.put_item(Item={
                "PK": "PROJECT#proj-ff",
                "SK": "CLUSTER#stuck-cl",
                "clusterName": "stuck-cl",
                "projectId": "proj-ff",
                "status": "CREATING",
                "createdAt": "2024-01-01T00:00:00+00:00",
            })

            yield {
                "handler_mod": handler_mod,
                "clusters_table": clusters_table,
            }

    def test_unauthorised_user_cannot_force_fail(self, cluster_env):
        """Non-member cannot force-fail a cluster."""
        event = _event_with_groups(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/fail",
            groups="ProjectUser-other-project",
            caller="outsider",
            path_parameters={"projectId": "proj-ff", "clusterName": "stuck-cl"},
        )
        response = cluster_env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_member_can_force_fail(self, cluster_env):
        """Project member can force-fail a CREATING cluster."""
        # Re-seed the cluster to CREATING status
        cluster_env["clusters_table"].put_item(Item={
            "PK": "PROJECT#proj-ff",
            "SK": "CLUSTER#stuck-cl",
            "clusterName": "stuck-cl",
            "projectId": "proj-ff",
            "status": "CREATING",
            "createdAt": "2024-01-01T00:00:00+00:00",
        })

        event = _event_with_groups(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/fail",
            groups="ProjectUser-proj-ff",
            caller="proj-member",
            path_parameters={"projectId": "proj-ff", "clusterName": "stuck-cl"},
        )
        response = cluster_env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "marked as failed" in body["message"]
