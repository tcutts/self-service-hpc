"""Unit tests for the POSIX Reconciliation Lambda (task 8.2).

Covers:
- Handler returns correct summary structure
- No active clusters → summary shows 0 clusters audited
- Active cluster with all members present → no accounts created or disabled
- Active cluster with missing member → account created (Req 10.4)
- Active cluster with stale account → account disabled (Req 10.5)
- Mixed scenario: some missing, some stale
- PENDING_PROPAGATION records are retried (Req 10.6)
- PENDING_RESTORATION records are retried
- SSM failure during audit → error counted, continues processing
- Cluster without loginNodeInstanceId → error counted, skipped

Requirements: 10.2, 10.3, 10.4, 10.5, 10.6, 10.7

Infrastructure is set up once per test class via class-scoped mock_aws
fixtures, avoiding repeated DynamoDB table creation.
"""

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
    _load_module_from,
    create_clusters_table,
    create_projects_table,
    create_users_table,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_user_profile(users_table, user_id, posix_uid, posix_gid):
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


def _seed_project_member(projects_table, project_id, user_id, role="PROJECT_USER"):
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "projectId": project_id,
        "role": role,
        "addedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster(clusters_table, project_id, cluster_name, status="ACTIVE", **extra):
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


def _seed_pending_member(projects_table, project_id, user_id, status="PENDING_PROPAGATION"):
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"MEMBER#{user_id}",
        "userId": user_id,
        "projectId": project_id,
        "role": "PROJECT_USER",
        "addedAt": "2024-01-01T00:00:00+00:00",
        "propagationStatus": status,
    })


def _mock_ssm_list_accounts(accounts_on_node):
    """Return a mock SSM client that returns the given accounts from get_command_invocation."""
    mock_ssm = MagicMock()
    mock_ssm.send_command.return_value = {
        "Command": {"CommandId": "cmd-test"},
    }
    mock_ssm.get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": "\n".join(accounts_on_node) + "\n" if accounts_on_node else "",
    }
    return mock_ssm


# ---------------------------------------------------------------------------
# 1. Handler returns correct summary structure
# 2. No active clusters → 0 clusters audited
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestReconciliationSummaryStructure:
    """Validates: Requirements 10.2, 10.7"""

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

            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            recon_mod = _load_module_from(_CLUSTER_OPS_DIR, "posix_reconciliation")

            yield {
                "recon_mod": recon_mod,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "users_table": users_table,
            }

    def test_handler_returns_summary_keys(self, _env):
        """Handler should return a dict with all expected summary keys."""
        with patch.object(_env["recon_mod"], "ssm_client", _mock_ssm_list_accounts([])):
            result = _env["recon_mod"].handler({}, None)

        assert "clusters_audited" in result
        assert "accounts_created" in result
        assert "accounts_disabled" in result
        assert "pending_resolved" in result
        assert "errors" in result

    def test_no_active_clusters_zero_audited(self, _env):
        """With no active clusters, clusters_audited should be 0."""
        with patch.object(_env["recon_mod"], "ssm_client", _mock_ssm_list_accounts([])):
            result = _env["recon_mod"].handler({}, None)

        assert result["clusters_audited"] == 0
        assert result["accounts_created"] == 0
        assert result["accounts_disabled"] == 0


# ---------------------------------------------------------------------------
# 3. All members present → no drift
# 4. Missing member → account created (Req 10.4)
# 5. Stale account → account disabled (Req 10.5)
# 6. Mixed scenario
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestReconciliationDriftDetection:
    """Validates: Requirements 10.3, 10.4, 10.5"""

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

            # Seed project members: alice and bob
            _seed_user_profile(users_table, "alice", 10001, 10001)
            _seed_user_profile(users_table, "bob", 10002, 10002)
            _seed_project_member(projects_table, "proj-drift", "alice")
            _seed_project_member(projects_table, "proj-drift", "bob")

            # Seed an active cluster
            _seed_cluster(
                clusters_table, "proj-drift", "drift-cluster",
                status="ACTIVE",
                loginNodeInstanceId="i-drift001",
            )

            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            recon_mod = _load_module_from(_CLUSTER_OPS_DIR, "posix_reconciliation")

            yield {
                "recon_mod": recon_mod,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "users_table": users_table,
            }

    def test_all_members_present_no_drift(self, _env):
        """When all members have accounts on the node, no creates or disables."""
        mock_ssm = _mock_ssm_list_accounts(["alice", "bob"])
        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                result = _env["recon_mod"].handler({}, None)

        assert result["clusters_audited"] == 1
        assert result["accounts_created"] == 0
        assert result["accounts_disabled"] == 0

    def test_missing_member_creates_account(self, _env):
        """A member without a Linux account should trigger account creation (Req 10.4)."""
        # Node only has alice — bob is missing
        mock_ssm = _mock_ssm_list_accounts(["alice"])
        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                result = _env["recon_mod"].handler({}, None)

        assert result["clusters_audited"] == 1
        assert result["accounts_created"] == 1
        assert result["accounts_disabled"] == 0

        # Verify SSM send_command was called for account creation
        # First call: list accounts, second call: create missing account
        assert mock_ssm.send_command.call_count >= 2

    def test_stale_account_disabled(self, _env):
        """A Linux account for a non-member should be disabled (Req 10.5)."""
        # Node has alice, bob, and stale-user (not a member)
        mock_ssm = _mock_ssm_list_accounts(["alice", "bob", "stale-user"])
        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                result = _env["recon_mod"].handler({}, None)

        assert result["clusters_audited"] == 1
        assert result["accounts_created"] == 0
        assert result["accounts_disabled"] == 1

    def test_mixed_missing_and_stale(self, _env):
        """Mixed scenario: bob missing, stale-user present."""
        # Node has alice and stale-user — bob is missing, stale-user is stale
        mock_ssm = _mock_ssm_list_accounts(["alice", "stale-user"])
        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                result = _env["recon_mod"].handler({}, None)

        assert result["clusters_audited"] == 1
        assert result["accounts_created"] == 1
        assert result["accounts_disabled"] == 1


# ---------------------------------------------------------------------------
# 7. PENDING_PROPAGATION records retried (Req 10.6)
# 8. PENDING_RESTORATION records retried
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestReconciliationPendingRetry:
    """Validates: Requirements 10.6"""

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

            # Seed users with POSIX identities
            _seed_user_profile(users_table, "pending-alice", 10020, 10020)
            _seed_user_profile(users_table, "pending-bob", 10021, 10021)
            _seed_user_profile(users_table, "restore-carol", 10022, 10022)

            # Seed pending propagation records
            _seed_pending_member(projects_table, "proj-pending", "pending-alice", "PENDING_PROPAGATION")
            _seed_pending_member(projects_table, "proj-pending", "pending-bob", "PENDING_PROPAGATION")

            # Seed pending restoration record
            _seed_pending_member(projects_table, "proj-pending", "restore-carol", "PENDING_RESTORATION")

            # Seed an active cluster for propagation
            _seed_cluster(
                clusters_table, "proj-pending", "pending-cluster",
                status="ACTIVE",
                loginNodeInstanceId="i-pending001",
            )

            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            recon_mod = _load_module_from(_CLUSTER_OPS_DIR, "posix_reconciliation")

            yield {
                "recon_mod": recon_mod,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "users_table": users_table,
            }

    def test_pending_propagation_retried_and_resolved(self, _env):
        """PENDING_PROPAGATION records should be retried and resolved on success (Req 10.6)."""
        mock_ssm = _mock_ssm_list_accounts(["pending-alice", "pending-bob", "restore-carol"])

        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                with patch.object(
                    _env["recon_mod"],
                    "propagate_user_to_clusters",
                    return_value="SUCCESS",
                ):
                    result = _env["recon_mod"].handler({}, None)

        assert result["pending_resolved"] >= 2

        # Verify propagationStatus was cleared
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE_NAME)
        item = table.get_item(
            Key={"PK": "PROJECT#proj-pending", "SK": "MEMBER#pending-alice"},
        )["Item"]
        assert "propagationStatus" not in item

    def test_pending_restoration_retried(self, _env):
        """PENDING_RESTORATION records should also be retried."""
        # Re-seed the restoration record
        _seed_pending_member(
            _env["projects_table"], "proj-pending", "restore-carol", "PENDING_RESTORATION",
        )

        mock_ssm = _mock_ssm_list_accounts(["pending-alice", "pending-bob", "restore-carol"])

        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                with patch.object(
                    _env["recon_mod"],
                    "propagate_user_to_clusters",
                    return_value="SUCCESS",
                ):
                    result = _env["recon_mod"].handler({}, None)

        # restore-carol should be resolved
        assert result["pending_resolved"] >= 1

        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE_NAME)
        item = table.get_item(
            Key={"PK": "PROJECT#proj-pending", "SK": "MEMBER#restore-carol"},
        )["Item"]
        assert "propagationStatus" not in item


# ---------------------------------------------------------------------------
# 9. SSM failure during audit → error counted
# 10. Cluster without loginNodeInstanceId → error counted, skipped
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestReconciliationErrorHandling:
    """Validates: Requirements 10.2, 10.3, 10.7"""

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

            # Seed a member
            _seed_user_profile(users_table, "err-user", 10030, 10030)
            _seed_project_member(projects_table, "proj-err", "err-user")

            # Cluster with instance ID (for SSM failure test)
            _seed_cluster(
                clusters_table, "proj-err", "err-cluster",
                status="ACTIVE",
                loginNodeInstanceId="i-err001",
            )

            # Cluster WITHOUT loginNodeInstanceId
            _seed_cluster(
                clusters_table, "proj-err", "no-instance-cluster",
                status="ACTIVE",
                # no loginNodeInstanceId
            )

            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            recon_mod = _load_module_from(_CLUSTER_OPS_DIR, "posix_reconciliation")

            yield {
                "recon_mod": recon_mod,
                "projects_table": projects_table,
                "clusters_table": clusters_table,
                "users_table": users_table,
            }

    def test_ssm_failure_counts_error_and_continues(self, _env):
        """SSM failure when listing accounts should count as error, not crash."""
        mock_ssm = MagicMock()
        error_response = {"Error": {"Code": "InvalidInstanceId", "Message": "Not found"}}
        mock_ssm.send_command.side_effect = ClientError(error_response, "SendCommand")

        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                result = _env["recon_mod"].handler({}, None)

        # Both clusters should produce errors (one SSM failure, one no instance ID)
        assert result["errors"] >= 1
        # Handler should still return (not crash)
        assert "clusters_audited" in result

    def test_cluster_without_instance_id_skipped_with_error(self, _env):
        """Cluster without loginNodeInstanceId should be skipped and counted as error."""
        # Use a mock SSM that succeeds for the cluster that has an instance ID
        mock_ssm = _mock_ssm_list_accounts(["err-user"])

        with patch.object(_env["recon_mod"], "ssm_client", mock_ssm):
            with patch.object(_env["recon_mod"], "time"):
                result = _env["recon_mod"].handler({}, None)

        # The no-instance cluster should produce an error
        assert result["errors"] >= 1
        # The cluster with an instance ID should be audited
        assert result["clusters_audited"] >= 1
