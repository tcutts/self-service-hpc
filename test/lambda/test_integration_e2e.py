"""End-to-end integration tests for the Self-Service HPC Platform.

Exercises the full workflow across multiple Lambda handlers using a single
moto mock_aws context so that DynamoDB state is shared:

  1. User creation via user management handler
  2. Project creation via project management handler
  3. Member addition to the project
  4. Cluster creation (Step Functions start_execution mocked)
  5. Cluster appears in list
  6. Cluster name uniqueness across projects
  7. Budget breach blocks cluster creation and access
  8. Cluster destruction (Step Functions start_execution mocked)

Requirements validated: 1.1, 2.1, 4.1, 6.2, 6.7, 6.9, 7.1, 8.1
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from conftest import (
    AWS_REGION,
    CLUSTERS_TABLE_NAME,
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    PROJECTS_TABLE_NAME,
    USERS_TABLE_NAME,
    create_clusters_table,
    create_cluster_name_registry_table,
    create_cognito_pool,
    create_projects_table,
    create_templates_table,
    create_users_table,
    reload_cluster_ops_handler_modules,
    reload_project_mgmt_modules,
    reload_user_mgmt_modules,
)


# ---------------------------------------------------------------------------
# Event builder helpers
# ---------------------------------------------------------------------------

def _admin_event(method, resource, body=None, path_parameters=None):
    """Build an API Gateway proxy event with Administrator claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "admin-user",
                    "sub": "sub-admin-user",
                    "cognito:groups": "Administrators",
                }
            }
        },
        "body": json.dumps(body) if body else None,
    }


def _project_admin_event(method, resource, project_id, body=None, path_parameters=None, caller="admin-user"):
    """Build an API Gateway proxy event with ProjectAdmin claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": f"Administrators,ProjectAdmin-{project_id}",
                }
            }
        },
        "body": json.dumps(body) if body else None,
    }


def _project_user_event(method, resource, project_id, body=None, path_parameters=None, caller="test-user"):
    """Build an API Gateway proxy event with ProjectUser claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": f"ProjectUser-{project_id}",
                }
            }
        },
        "body": json.dumps(body) if body else None,
    }


# ---------------------------------------------------------------------------
# Integration test class — single mock_aws context for all tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestEndToEndWorkflow:
    """Full lifecycle integration test across all Lambda handlers.

    Validates: Requirements 1.1, 2.1, 4.1, 6.2, 6.7, 6.9, 7.1, 8.1
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            # Create all DynamoDB tables
            users_table = create_users_table()
            projects_table = create_projects_table()
            clusters_table = create_clusters_table()
            templates_table = create_templates_table()
            registry_table = create_cluster_name_registry_table()
            pool_id = create_cognito_pool()

            # Set environment variables for all handlers
            os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["USER_POOL_ID"] = pool_id
            os.environ["BUDGET_SNS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123456789012:budget-topic"
            os.environ["CREATION_STATE_MACHINE_ARN"] = (
                "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            )
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = (
                "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"
            )

            # Load all handler modules within the mock context
            user_handler, users_mod, user_errors = reload_user_mgmt_modules()
            proj_handler, projects_mod, members_mod, proj_errors = reload_project_mgmt_modules()
            cluster_handler, clusters_mod, auth_mod, cluster_errors, tagging_mod = (
                reload_cluster_ops_handler_modules()
            )

            yield {
                "users_table": users_table,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "registry_table": registry_table,
                "pool_id": pool_id,
                "user_handler": user_handler,
                "proj_handler": proj_handler,
                "cluster_handler": cluster_handler,
                "clusters_mod": clusters_mod,
            }

    # ------------------------------------------------------------------
    # 1. User creation (Requirement 1.1)
    # ------------------------------------------------------------------

    def test_01_create_user(self, _env):
        """Admin creates a platform user and receives confirmation."""
        event = _admin_event("POST", "/users", body={
            "userId": "test-user",
            "displayName": "Test User",
            "email": "test@example.com",
        })
        response = _env["user_handler"].handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["userId"] == "test-user"
        assert body["posixUid"] >= 10000
        assert body["status"] == "ACTIVE"

    # ------------------------------------------------------------------
    # 2. Project creation (Requirement 2.1)
    # ------------------------------------------------------------------

    def test_02_create_project(self, _env):
        """Admin creates a project with a cost allocation tag."""
        event = _admin_event("POST", "/projects", body={
            "projectId": "proj-alpha",
            "projectName": "Alpha Project",
            "costAllocationTag": "proj-alpha",
        })
        response = _env["proj_handler"].handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-alpha"
        assert body["costAllocationTag"] == "proj-alpha"
        assert body["budgetBreached"] is False

    # ------------------------------------------------------------------
    # 3. Member addition (Requirement 4.1)
    # ------------------------------------------------------------------

    def test_03_add_member_to_project(self, _env):
        """Project admin adds the user as a member of the project."""
        event = _project_admin_event(
            "POST",
            "/projects/{projectId}/members",
            "proj-alpha",
            body={"userId": "test-user", "role": "PROJECT_USER"},
            path_parameters={"projectId": "proj-alpha"},
        )
        response = _env["proj_handler"].handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["userId"] == "test-user"
        assert body["projectId"] == "proj-alpha"
        assert body["role"] == "PROJECT_USER"

    # ------------------------------------------------------------------
    # 4. Cluster creation (Requirement 6.2)
    # ------------------------------------------------------------------

    def test_04_create_cluster(self, _env):
        """Project user creates a cluster (Step Functions mocked)."""
        # Simulate project deployment by adding infrastructure fields
        # that would normally be set by the deploy Step Functions workflow.
        _env["projects_table"].update_item(
            Key={"PK": "PROJECT#proj-alpha", "SK": "METADATA"},
            UpdateExpression=(
                "SET s3BucketName = :s3, vpcId = :vpc, "
                "efsFileSystemId = :efs, "
                "publicSubnetIds = :pubsubs, privateSubnetIds = :privsubs, "
                "securityGroupIds = :sgs, #st = :status"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s3": "hpc-proj-alpha-storage",
                ":vpc": "vpc-alpha",
                ":efs": "fs-alpha",
                ":pubsubs": ["subnet-pub-1", "subnet-pub-2"],
                ":privsubs": ["subnet-priv-1", "subnet-priv-2"],
                ":sgs": {
                    "headNode": "sg-head",
                    "computeNode": "sg-compute",
                    "efs": "sg-efs",
                    "fsx": "sg-fsx",
                },
                ":status": "ACTIVE",
            },
        )

        with patch.object(_env["cluster_handler"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters",
                "proj-alpha",
                body={"clusterName": "my-cluster", "templateId": "cpu-general"},
                path_parameters={"projectId": "proj-alpha"},
            )
            response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["clusterName"] == "my-cluster"
        assert body["projectId"] == "proj-alpha"

    # ------------------------------------------------------------------
    # 5. Cluster appears in list — seed an ACTIVE record first
    # ------------------------------------------------------------------

    def test_05_cluster_appears_in_list(self, _env):
        """After creation workflow completes, cluster is visible in list."""
        # Simulate the Step Functions workflow completing by inserting
        # an ACTIVE cluster record directly into DynamoDB.
        _env["clusters_table"].put_item(Item={
            "PK": "PROJECT#proj-alpha",
            "SK": "CLUSTER#my-cluster",
            "clusterName": "my-cluster",
            "projectId": "proj-alpha",
            "templateId": "cpu-general",
            "status": "ACTIVE",
            "loginNodeIp": "10.0.1.100",
            "sshPort": 22,
            "dcvPort": 8443,
            "createdBy": "test-user",
            "createdAt": "2024-01-01T00:00:00+00:00",
        })

        event = _project_user_event(
            "GET",
            "/projects/{projectId}/clusters",
            "proj-alpha",
            path_parameters={"projectId": "proj-alpha"},
        )
        response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        cluster_names = [c["clusterName"] for c in body["clusters"]]
        assert "my-cluster" in cluster_names

    # ------------------------------------------------------------------
    # 5b. Cluster access — SSH/DCV info for ACTIVE cluster (Req 8.1)
    # ------------------------------------------------------------------

    def test_05b_active_cluster_exposes_connection_info(self, _env):
        """ACTIVE cluster returns SSH and DCV connection details."""
        event = _project_user_event(
            "GET",
            "/projects/{projectId}/clusters/{clusterName}",
            "proj-alpha",
            path_parameters={"projectId": "proj-alpha", "clusterName": "my-cluster"},
        )
        response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "connectionInfo" in body
        assert "10.0.1.100" in body["connectionInfo"]["ssh"]
        assert "10.0.1.100" in body["connectionInfo"]["dcv"]

    # ------------------------------------------------------------------
    # 6. Cluster name uniqueness across projects (Requirement 6.7)
    # ------------------------------------------------------------------

    def test_06_cluster_name_uniqueness_across_projects(self, _env):
        """A cluster name used by proj-alpha is rejected for proj-beta."""
        from conftest import _CLUSTER_OPS_DIR, _load_module_from
        # Load errors first, then cluster_names so it imports the same errors
        errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
        cluster_names_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")

        # The cluster creation handler delegates name registration to the
        # Step Functions workflow (which we mock). Simulate the workflow's
        # first step by registering the name for proj-alpha explicitly.
        cluster_names_mod.register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "my-cluster", "proj-alpha",
        )

        # Create a second project
        event = _admin_event("POST", "/projects", body={
            "projectId": "proj-beta",
            "projectName": "Beta Project",
        })
        response = _env["proj_handler"].handler(event, None)
        assert response["statusCode"] == 201

        # Create a second user and add to proj-beta
        event = _admin_event("POST", "/users", body={
            "userId": "beta-user",
            "displayName": "Beta User",
            "email": "beta@example.com",
        })
        response = _env["user_handler"].handler(event, None)
        assert response["statusCode"] == 201

        event = _project_admin_event(
            "POST",
            "/projects/{projectId}/members",
            "proj-beta",
            body={"userId": "beta-user", "role": "PROJECT_USER"},
            path_parameters={"projectId": "proj-beta"},
        )
        response = _env["proj_handler"].handler(event, None)
        assert response["statusCode"] == 201

        # Verify the name is registered to proj-alpha
        record = cluster_names_mod.lookup_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "my-cluster",
        )
        assert record is not None
        assert record["projectId"] == "proj-alpha"

        # Attempting to register the same name for proj-beta should fail
        # Use Exception base class to avoid module identity issues with
        # dynamically loaded error classes, then verify the error type.
        with pytest.raises(Exception, match="reserved.*different project|already reserved"):
            cluster_names_mod.register_cluster_name(
                CLUSTER_NAME_REGISTRY_TABLE_NAME, "my-cluster", "proj-beta",
            )

    def test_06b_same_project_reuse_allowed(self, _env):
        """The same cluster name can be reused within the same project."""
        from conftest import _CLUSTER_OPS_DIR, _load_module_from
        cluster_names_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")

        # Re-registering "my-cluster" for proj-alpha should succeed
        result = cluster_names_mod.register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "my-cluster", "proj-alpha",
        )
        assert result["clusterName"] == "my-cluster"
        assert result["projectId"] == "proj-alpha"

    # ------------------------------------------------------------------
    # 7. Budget breach blocks cluster creation (Requirement 6.9)
    # ------------------------------------------------------------------

    def test_07_budget_breach_blocks_creation(self, _env):
        """Cluster creation is rejected when the project budget is breached."""
        # Set budgetBreached flag on proj-alpha
        _env["projects_table"].update_item(
            Key={"PK": "PROJECT#proj-alpha", "SK": "METADATA"},
            UpdateExpression="SET budgetBreached = :val",
            ExpressionAttributeValues={":val": True},
        )

        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters",
            "proj-alpha",
            body={"clusterName": "blocked-cluster", "templateId": "cpu-general"},
            path_parameters={"projectId": "proj-alpha"},
        )
        response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "BUDGET_EXCEEDED"
        assert "budget" in body["error"]["message"].lower()

    def test_07b_budget_breach_blocks_cluster_access(self, _env):
        """Cluster detail access is denied when the project budget is breached."""
        # budgetBreached is still True from test_07
        event = _project_user_event(
            "GET",
            "/projects/{projectId}/clusters/{clusterName}",
            "proj-alpha",
            path_parameters={"projectId": "proj-alpha", "clusterName": "my-cluster"},
        )
        response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "BUDGET_EXCEEDED"

    def test_07c_budget_breach_via_notification_handler(self, _env):
        """Budget notification handler sets the budgetBreached flag."""
        # Reset the breach flag first
        _env["projects_table"].update_item(
            Key={"PK": "PROJECT#proj-alpha", "SK": "METADATA"},
            UpdateExpression="SET budgetBreached = :val",
            ExpressionAttributeValues={":val": False},
        )

        # Verify it's cleared
        assert _env["clusters_mod"].check_budget_breach(
            PROJECTS_TABLE_NAME, "proj-alpha"
        ) is False

        # Simulate a 100% budget notification by directly updating the flag
        # (the budget notification handler would do this via SNS)
        from conftest import _load_module_from
        _BUDGET_DIR = os.path.join(
            os.path.dirname(__file__), "..", "..", "lambda", "budget_notification"
        )
        budget_handler_mod = _load_module_from(_BUDGET_DIR, "handler")

        sns_event = {
            "Records": [{
                "Sns": {
                    "Message": json.dumps({
                        "budgetName": "hpc-project-proj-alpha",
                        "threshold": 100.0,
                    })
                }
            }]
        }
        result = budget_handler_mod.handler(sns_event, None)
        assert result["processed"] == 1

        # Verify the breach flag is now set
        assert _env["clusters_mod"].check_budget_breach(
            PROJECTS_TABLE_NAME, "proj-alpha"
        ) is True

        # Reset for subsequent tests
        _env["projects_table"].update_item(
            Key={"PK": "PROJECT#proj-alpha", "SK": "METADATA"},
            UpdateExpression="SET budgetBreached = :val",
            ExpressionAttributeValues={":val": False},
        )

    # ------------------------------------------------------------------
    # 8. Cluster destruction (Requirement 7.1)
    # ------------------------------------------------------------------

    def test_08_destroy_cluster(self, _env):
        """Project user destroys a cluster (Step Functions mocked)."""
        with patch.object(_env["cluster_handler"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "DELETE",
                "/projects/{projectId}/clusters/{clusterName}",
                "proj-alpha",
                path_parameters={
                    "projectId": "proj-alpha",
                    "clusterName": "my-cluster",
                },
            )
            response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["clusterName"] == "my-cluster"
        assert "destruction started" in body["message"].lower()

    def test_08b_destroyed_cluster_no_connection_info(self, _env):
        """After destruction, cluster does not expose connection info."""
        # Simulate the destruction workflow completing
        _env["clusters_table"].update_item(
            Key={"PK": "PROJECT#proj-alpha", "SK": "CLUSTER#my-cluster"},
            UpdateExpression="SET #s = :status, destroyedAt = :ts",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "DESTROYED",
                ":ts": "2024-06-01T00:00:00+00:00",
            },
        )

        event = _project_user_event(
            "GET",
            "/projects/{projectId}/clusters/{clusterName}",
            "proj-alpha",
            path_parameters={"projectId": "proj-alpha", "clusterName": "my-cluster"},
        )
        response = _env["cluster_handler"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "DESTROYED"
        assert "connectionInfo" not in body
