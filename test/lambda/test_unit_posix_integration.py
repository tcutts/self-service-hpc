"""Unit tests for POSIX provisioning integration (task 10.2).

Covers:
- Cluster creation includes user data script in launch template config
- Membership add triggers SSM propagation to active clusters
- PENDING_PROPAGATION status is set on membership record when propagation fails
- Reconciliation Lambda retries pending propagations

Requirements: 17.2, 17.5

Infrastructure is set up once per test class via class-scoped mock_aws
fixtures, avoiding repeated DynamoDB table creation.
"""

import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from conftest import (
    AWS_REGION,
    CLUSTERS_TABLE_NAME,
    PROJECTS_TABLE_NAME,
    USERS_TABLE_NAME,
    _CLUSTER_OPS_DIR,
    _PROJECT_MGMT_DIR,
    _load_module_from,
    create_clusters_table,
    create_cognito_pool,
    create_projects_table,
    create_users_table,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_user_profile(users_table, user_id, posix_uid, posix_gid):
    """Insert a user profile with POSIX identity into the PlatformUsers table."""
    users_table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "displayName": f"User {user_id}",
        "posixUid": posix_uid,
        "posixGid": posix_gid,
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_project(projects_table, project_id):
    """Insert a minimal project record."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "budgetBreached": False,
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_project_member(projects_table, project_id, user_id, role="PROJECT_USER"):
    """Insert a membership record into the Projects table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "projectId": project_id,
        "role": role,
        "addedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster(clusters_table, project_id, cluster_name, status="ACTIVE", **extra):
    """Insert a cluster record into the Clusters table."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": status,
        "createdAt": "2024-01-01T00:00:00+00:00",
    }
    item.update(extra)
    clusters_table.put_item(Item=item)


def _seed_pending_member(projects_table, project_id, user_id):
    """Insert a membership record with PENDING_PROPAGATION status."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "projectId": project_id,
        "role": "PROJECT_USER",
        "addedAt": "2024-01-01T00:00:00+00:00",
        "propagationStatus": "PENDING_PROPAGATION",
    })


def _create_cognito_user(pool_id, user_id):
    """Create a Cognito user for membership tests."""
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
# Cluster creation includes user data script
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterCreationUserDataScript:
    """Validates: Requirements 17.2

    Verifies that create_login_node_group and create_compute_node_group
    generate POSIX user data scripts and include them in the event.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = "ClusterNameRegistry"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            users_table = create_users_table()

            # Seed project with members
            _seed_project(projects_table, "proj-ud")
            _seed_user_profile(users_table, "alice", 10001, 10001)
            _seed_user_profile(users_table, "bob", 10002, 10002)
            _seed_project_member(projects_table, "proj-ud", "alice")
            _seed_project_member(projects_table, "proj-ud", "bob")

            # Load modules inside mock context
            _load_module_from(_CLUSTER_OPS_DIR, "errors")
            _load_module_from(_CLUSTER_OPS_DIR, "auth")
            _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
            _load_module_from(_CLUSTER_OPS_DIR, "clusters")
            _load_module_from(_CLUSTER_OPS_DIR, "tagging")
            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

            yield {
                "creation_mod": creation_mod,
                "clusters_table": clusters_table,
            }

    def test_login_node_group_generates_user_data(self, _env):
        """create_login_node_group should include a userDataScript in the result."""
        base_event = {
            "projectId": "proj-ud",
            "clusterName": "ud-cluster",
            "pcsClusterId": "pcs-123",
            "publicSubnetIds": ["subnet-pub1"],
            "securityGroupIds": {"headNode": "sg-head", "computeNode": "sg-comp"},
            "loginInstanceType": "c7g.medium",
            "loginLaunchTemplateId": "lt-login",
            "loginLaunchTemplateVersion": "$Default",
            "instanceProfileArn": "arn:aws:iam::123:instance-profile/test",
        }

        with patch.object(_env["creation_mod"], "pcs_client") as mock_pcs:
            mock_pcs.create_compute_node_group = MagicMock(return_value={
                "computeNodeGroup": {"id": "lng-1"},
            })
            result = _env["creation_mod"].create_login_node_group(base_event)

        assert "userDataScript" in result
        script = result["userDataScript"]
        assert "#!/bin/bash" in script
        assert "alice" in script
        assert "bob" in script
        assert "10001" in script
        assert "10002" in script

    def test_compute_node_group_generates_user_data(self, _env):
        """create_compute_node_group should generate a user data script (logged)."""
        base_event = {
            "projectId": "proj-ud",
            "clusterName": "ud-cluster",
            "pcsClusterId": "pcs-123",
            "privateSubnetIds": ["subnet-priv1"],
            "securityGroupIds": {"headNode": "sg-head", "computeNode": "sg-comp"},
            "instanceTypes": ["c7g.medium"],
            "maxNodes": 10,
            "minNodes": 0,
            "purchaseOption": "ONDEMAND",
            "computeLaunchTemplateId": "lt-compute",
            "computeLaunchTemplateVersion": "$Default",
            "instanceProfileArn": "arn:aws:iam::123:instance-profile/test",
        }

        with patch.object(_env["creation_mod"], "pcs_client") as mock_pcs:
            mock_pcs.create_compute_node_group = MagicMock(return_value={
                "computeNodeGroup": {"id": "cng-1"},
            })
            result = _env["creation_mod"].create_compute_node_group(base_event)

        assert "computeNodeGroupId" in result
        assert result["computeNodeGroupId"] == "cng-1"


# ---------------------------------------------------------------------------
# Membership add triggers POSIX propagation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestMembershipPosixPropagation:
    """Validates: Requirements 17.5

    Verifies that add_member triggers POSIX user propagation to active
    clusters and sets propagationStatus on failure.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME

            projects_table = create_projects_table()
            clusters_table = create_clusters_table()
            users_table = create_users_table()
            pool_id = create_cognito_pool()

            os.environ["USER_POOL_ID"] = pool_id

            # Seed project
            _seed_project(projects_table, "proj-prop")

            # Seed users with POSIX identities
            _seed_user_profile(users_table, "new-user", 10010, 10010)
            _seed_user_profile(users_table, "fail-user", 10011, 10011)
            _seed_user_profile(users_table, "no-cluster-user", 10012, 10012)

            # Create Cognito users
            _create_cognito_user(pool_id, "new-user")
            _create_cognito_user(pool_id, "fail-user")
            _create_cognito_user(pool_id, "no-cluster-user")

            # Seed an active cluster
            _seed_cluster(
                clusters_table, "proj-prop", "active-cl",
                status="ACTIVE",
                loginNodeInstanceId="i-active123",
            )

            # Load members module
            _load_module_from(_PROJECT_MGMT_DIR, "errors")
            _load_module_from(_PROJECT_MGMT_DIR, "auth")
            # Load posix_provisioning so the import inside members works
            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            members_mod = _load_module_from(_PROJECT_MGMT_DIR, "members")

            yield {
                "members_mod": members_mod,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "users_table": users_table,
                "pool_id": pool_id,
            }

    def test_add_member_triggers_propagation(self, _env):
        """Adding a member should call propagate_user_to_clusters."""
        with patch(
            "posix_provisioning.propagate_user_to_clusters",
            return_value="SUCCESS",
        ) as mock_prop:
            result = _env["members_mod"].add_member(
                projects_table_name=PROJECTS_TABLE_NAME,
                users_table_name=USERS_TABLE_NAME,
                user_pool_id=_env["pool_id"],
                project_id="proj-prop",
                user_id="new-user",
                role="PROJECT_USER",
            )

        assert result["userId"] == "new-user"
        assert "propagationStatus" not in result
        mock_prop.assert_called_once()

    def test_add_member_sets_pending_on_propagation_failure(self, _env):
        """If propagation fails, the membership record should have propagationStatus."""
        with patch(
            "posix_provisioning.propagate_user_to_clusters",
            return_value="PENDING_PROPAGATION",
        ):
            result = _env["members_mod"].add_member(
                projects_table_name=PROJECTS_TABLE_NAME,
                users_table_name=USERS_TABLE_NAME,
                user_pool_id=_env["pool_id"],
                project_id="proj-prop",
                user_id="fail-user",
                role="PROJECT_USER",
            )

        assert result["propagationStatus"] == "PENDING_PROPAGATION"

        # Verify the DynamoDB record has the propagationStatus
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE_NAME)
        item = table.get_item(
            Key={"PK": "PROJECT#proj-prop", "SK": "MEMBER#fail-user"},
        )["Item"]
        assert item["propagationStatus"] == "PENDING_PROPAGATION"

    def test_add_member_succeeds_even_if_propagation_errors(self, _env):
        """Membership should succeed even if propagation throws an exception."""
        with patch(
            "posix_provisioning.propagate_user_to_clusters",
            side_effect=Exception("SSM unavailable"),
        ):
            result = _env["members_mod"].add_member(
                projects_table_name=PROJECTS_TABLE_NAME,
                users_table_name=USERS_TABLE_NAME,
                user_pool_id=_env["pool_id"],
                project_id="proj-prop",
                user_id="no-cluster-user",
                role="PROJECT_USER",
            )

        # Membership should still succeed
        assert result["userId"] == "no-cluster-user"
        assert result["propagationStatus"] == "PENDING_PROPAGATION"


# ---------------------------------------------------------------------------
# Reconciliation Lambda
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestPosixReconciliation:
    """Validates: Requirements 17.5

    Verifies the reconciliation Lambda scans for PENDING_PROPAGATION
    records, retries propagation, and clears the status on success.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME

            projects_table = create_projects_table()
            clusters_table = create_clusters_table()
            users_table = create_users_table()

            # Seed users
            _seed_user_profile(users_table, "pending-alice", 10020, 10020)
            _seed_user_profile(users_table, "pending-bob", 10021, 10021)

            # Seed pending membership records
            _seed_pending_member(projects_table, "proj-recon", "pending-alice")
            _seed_pending_member(projects_table, "proj-recon", "pending-bob")

            # Seed an active cluster
            _seed_cluster(
                clusters_table, "proj-recon", "recon-cluster",
                status="ACTIVE",
                loginNodeInstanceId="i-recon123",
            )

            # Load modules
            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            reconciliation_mod = _load_module_from(
                _CLUSTER_OPS_DIR, "posix_reconciliation",
            )

            yield {
                "reconciliation_mod": reconciliation_mod,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "users_table": users_table,
            }

    def test_reconciliation_finds_pending_records(self, _env):
        """The scan should find membership records with PENDING_PROPAGATION."""
        pending = _env["reconciliation_mod"]._scan_pending_members()
        user_ids = {m["userId"] for m in pending}
        assert "pending-alice" in user_ids
        assert "pending-bob" in user_ids

    def test_reconciliation_clears_status_on_success(self, _env):
        """Successful propagation should remove propagationStatus from the record."""
        with patch.object(
            _env["reconciliation_mod"],
            "propagate_user_to_clusters",
            return_value="SUCCESS",
        ):
            result = _env["reconciliation_mod"].handler({}, None)

        assert result["succeeded"] >= 1

        # Check that at least one record had its status cleared
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE_NAME)
        item = table.get_item(
            Key={"PK": "PROJECT#proj-recon", "SK": "MEMBER#pending-alice"},
        )["Item"]
        assert "propagationStatus" not in item

    def test_reconciliation_keeps_pending_on_failure(self, _env):
        """Failed propagation should leave the record as PENDING_PROPAGATION."""
        # Re-seed a pending record for this test
        _seed_pending_member(
            _env["projects_table"], "proj-recon", "pending-bob",
        )

        with patch.object(
            _env["reconciliation_mod"],
            "propagate_user_to_clusters",
            return_value="PENDING_PROPAGATION",
        ):
            result = _env["reconciliation_mod"].handler({}, None)

        assert result["still_pending"] >= 1

        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE_NAME)
        item = table.get_item(
            Key={"PK": "PROJECT#proj-recon", "SK": "MEMBER#pending-bob"},
        )["Item"]
        assert item.get("propagationStatus") == "PENDING_PROPAGATION"

    def test_reconciliation_handles_empty_scan(self, _env):
        """When no pending records exist, the handler should return zeros."""
        # Clear all pending records first
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE_NAME)
        for user_id in ["pending-alice", "pending-bob"]:
            table.update_item(
                Key={"PK": "PROJECT#proj-recon", "SK": f"MEMBER#{user_id}"},
                UpdateExpression="REMOVE propagationStatus",
            )

        result = _env["reconciliation_mod"].handler({}, None)
        assert result["total"] == 0
        assert result["succeeded"] == 0
        assert result["still_pending"] == 0
