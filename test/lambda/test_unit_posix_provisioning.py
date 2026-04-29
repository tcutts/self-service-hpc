"""Unit tests for the POSIX User Provisioning module.

Covers:
- User data script generation with multiple users
- Generic account disabling logic
- SSM propagation retry logic
- PENDING_PROPAGATION fallback

Requirements: 8.3, 8.4, 17.2, 17.3, 17.4, 17.5

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
    _load_module_from,
    create_clusters_table,
    create_projects_table,
    create_users_table,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _load_posix_module():
    """Load the posix_provisioning module inside a moto mock context."""
    return _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")


# ---------------------------------------------------------------------------
# generate_user_creation_commands
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestGenerateUserCreationCommands:
    """Validates: Requirements 17.2, 17.3"""

    @pytest.fixture(autouse=True, scope="class")
    def _setup(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            mod = _load_posix_module()
            yield {"mod": mod}

    def test_creates_group_user_and_chown(self, _setup):
        cmds = _setup["mod"].generate_user_creation_commands("alice", 10001, 10001)
        assert len(cmds) == 3
        assert "groupadd -g 10001 alice" in cmds[0]
        assert "useradd -u 10001 -g 10001 -m -d /home/alice alice" in cmds[1]
        assert "chown 10001:10001 /home/alice" in cmds[2]

    def test_different_uid_gid(self, _setup):
        cmds = _setup["mod"].generate_user_creation_commands("bob", 20000, 30000)
        assert "groupadd -g 30000 bob" in cmds[0]
        assert "useradd -u 20000 -g 30000" in cmds[1]
        assert "chown 20000:30000 /home/bob" in cmds[2]

    def test_empty_user_id_returns_empty(self, _setup):
        cmds = _setup["mod"].generate_user_creation_commands("", 10001, 10001)
        assert cmds == []

    def test_commands_are_idempotent(self, _setup):
        """Commands use '2>/dev/null || true' to handle existing users."""
        cmds = _setup["mod"].generate_user_creation_commands("carol", 10002, 10002)
        assert "2>/dev/null || true" in cmds[0]
        assert "2>/dev/null || true" in cmds[1]


# ---------------------------------------------------------------------------
# generate_disable_generic_accounts_commands
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestDisableGenericAccounts:
    """Validates: Requirements 8.3, 8.4"""

    @pytest.fixture(autouse=True, scope="class")
    def _setup(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            mod = _load_posix_module()
            yield {"mod": mod}

    def test_disables_all_generic_accounts(self, _setup):
        cmds = _setup["mod"].generate_disable_generic_accounts_commands()
        assert len(cmds) == 3
        for account in ["ec2-user", "centos", "ubuntu"]:
            matching = [c for c in cmds if account in c]
            assert len(matching) == 1, f"Expected command for {account}"

    def test_uses_nologin_shell(self, _setup):
        cmds = _setup["mod"].generate_disable_generic_accounts_commands()
        for cmd in cmds:
            assert "/sbin/nologin" in cmd

    def test_locks_password(self, _setup):
        cmds = _setup["mod"].generate_disable_generic_accounts_commands()
        for cmd in cmds:
            assert "usermod -L" in cmd

    def test_checks_account_exists_before_disabling(self, _setup):
        cmds = _setup["mod"].generate_disable_generic_accounts_commands()
        for cmd in cmds:
            assert cmd.startswith("if id ")


# ---------------------------------------------------------------------------
# generate_ssm_agent_commands
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestGenerateSsmAgentCommands:
    """Validates: SSM Agent verification on login nodes."""

    @pytest.fixture(autouse=True, scope="class")
    def _setup(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            mod = _load_posix_module()
            yield {"mod": mod}

    def test_enables_ssm_agent_service(self, _setup):
        cmds = _setup["mod"].generate_ssm_agent_commands()
        enable_cmds = [c for c in cmds if "systemctl enable amazon-ssm-agent" in c]
        assert len(enable_cmds) == 1

    def test_starts_ssm_agent_service(self, _setup):
        cmds = _setup["mod"].generate_ssm_agent_commands()
        start_cmds = [c for c in cmds if "systemctl start amazon-ssm-agent" in c]
        assert len(start_cmds) == 1

    def test_installs_agent_if_missing(self, _setup):
        cmds = _setup["mod"].generate_ssm_agent_commands()
        joined = "\n".join(cmds)
        assert "yum install -y amazon-ssm-agent" in joined
        assert "apt-get install -y amazon-ssm-agent" in joined

    def test_verifies_agent_is_running(self, _setup):
        cmds = _setup["mod"].generate_ssm_agent_commands()
        joined = "\n".join(cmds)
        assert "systemctl is-active --quiet amazon-ssm-agent" in joined

    def test_commands_are_idempotent(self, _setup):
        """Enable and start use '|| true' to avoid failing if already active."""
        cmds = _setup["mod"].generate_ssm_agent_commands()
        enable_cmd = [c for c in cmds if "systemctl enable" in c][0]
        start_cmd = [c for c in cmds if "systemctl start" in c][0]
        assert "|| true" in enable_cmd
        assert "|| true" in start_cmd

    def test_logs_warning_on_failure(self, _setup):
        cmds = _setup["mod"].generate_ssm_agent_commands()
        joined = "\n".join(cmds)
        assert "WARNING: SSM Agent failed to start" in joined


# ---------------------------------------------------------------------------
# generate_user_data_script
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestGenerateUserDataScript:
    """Validates: Requirements 17.2, 17.3, 17.4"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME

            users_table = create_users_table()
            projects_table = create_projects_table()

            # Seed users with POSIX identities
            _seed_user_profile(users_table, "alice", 10001, 10001)
            _seed_user_profile(users_table, "bob", 10002, 10002)
            _seed_user_profile(users_table, "carol", 10003, 10003)

            # Seed project membership
            _seed_project_member(projects_table, "proj-alpha", "alice")
            _seed_project_member(projects_table, "proj-alpha", "bob")

            # A project with no members
            projects_table.put_item(Item={
                "PK": "PROJECT#proj-empty",
                "SK": "METADATA",
                "projectId": "proj-empty",
                "status": "ACTIVE",
            })

            mod = _load_posix_module()

            yield {
                "mod": mod,
                "users_table": users_table,
                "projects_table": projects_table,
            }

    def test_script_starts_with_shebang(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert script.startswith("#!/bin/bash")

    def test_script_contains_user_creation_commands(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "useradd" in script
        assert "alice" in script
        assert "bob" in script
        assert "10001" in script
        assert "10002" in script

    def test_script_contains_generic_account_disabling(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "ec2-user" in script
        assert "centos" in script
        assert "ubuntu" in script
        assert "/sbin/nologin" in script

    def test_script_contains_chown_commands(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "chown 10001:10001 /home/alice" in script
        assert "chown 10002:10002 /home/bob" in script

    def test_empty_project_produces_minimal_script(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-empty", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "#!/bin/bash" in script
        assert "0 user(s)" in script
        # Should still disable generic accounts
        assert "ec2-user" in script

    def test_script_includes_project_id_comment(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "proj-alpha" in script

    def test_script_includes_ssm_agent_verification(self, _env):
        """SSM Agent commands should appear early in the user data script."""
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "amazon-ssm-agent" in script
        assert "systemctl enable amazon-ssm-agent" in script
        # SSM commands should appear before user creation
        ssm_pos = script.index("amazon-ssm-agent")
        useradd_pos = script.index("useradd")
        assert ssm_pos < useradd_pos, "SSM agent setup should run before user creation"

    def test_script_contains_groupadd(self, _env):
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "groupadd" in script

    def test_non_members_excluded_from_script(self, _env):
        """Validates: Requirements 9.1, 9.2

        carol exists in PlatformUsers with a POSIX identity but is NOT
        a member of proj-alpha.  She must NOT appear in the user data script.
        """
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "carol" not in script
        assert "10003" not in script

    def test_only_member_count_in_header(self, _env):
        """Validates: Requirements 9.1, 9.2

        The script header should report only the 2 members (alice, bob),
        not all 3 platform users.
        """
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "2 user(s)" in script

    def test_mountpoint_storage_includes_s3_mount_commands(self, _env):
        """When storage_mode is 'mountpoint', the script includes S3 mount commands."""
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
            storage_mode="mountpoint",
            s3_bucket_name="hpc-proj-alpha-storage",
        )
        assert "mount-s3 hpc-proj-alpha-storage /data" in script
        assert "mountpoint-s3" in script
        # Should NOT contain lustre commands
        assert "lustre" not in script.lower() or "lustre" not in script.split("mount-s3")[0].lower()

    def test_lustre_storage_includes_fsx_mount_commands(self, _env):
        """When storage_mode is 'lustre', the script includes FSx mount commands."""
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
            storage_mode="lustre",
            fsx_dns_name="fs-abc123.fsx.eu-west-1.amazonaws.com",
            fsx_mount_name="abcdef",
        )
        assert "mount -t lustre" in script
        assert "fs-abc123.fsx.eu-west-1.amazonaws.com" in script
        assert "abcdef" in script
        assert "/data" in script
        # Should NOT contain mountpoint-s3 commands
        assert "mount-s3" not in script

    def test_no_storage_mode_omits_mount_commands(self, _env):
        """When storage_mode is empty, no mount commands are generated."""
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
        )
        assert "mount-s3" not in script
        assert "mount -t lustre" not in script

    def test_mountpoint_without_bucket_omits_mount_commands(self, _env):
        """When storage_mode is 'mountpoint' but bucket is empty, no mount commands."""
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
            storage_mode="mountpoint",
            s3_bucket_name="",
        )
        assert "mount-s3" not in script

    def test_lustre_without_dns_omits_mount_commands(self, _env):
        """When storage_mode is 'lustre' but FSx fields are empty, no mount commands."""
        script = _env["mod"].generate_user_data_script(
            "proj-alpha", USERS_TABLE_NAME, PROJECTS_TABLE_NAME,
            storage_mode="lustre",
            fsx_dns_name="",
            fsx_mount_name="",
        )
        assert "mount -t lustre" not in script


# ---------------------------------------------------------------------------
# SSM propagation with retry logic
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestPropagateUserToClusters:
    """Validates: Requirements 9.3, 17.5"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()

            # Seed active cluster with loginNodeInstanceId
            _seed_cluster(
                clusters_table, "proj-ssm", "cluster-a",
                status="ACTIVE",
                loginNodeInstanceId="i-abc123",
            )
            # Seed a destroyed cluster (should be skipped)
            _seed_cluster(
                clusters_table, "proj-ssm", "cluster-b",
                status="DESTROYED",
                loginNodeInstanceId="i-def456",
            )

            # Seed membership records so the membership check passes
            _seed_project_member(projects_table, "proj-ssm", "alice")
            _seed_project_member(projects_table, "proj-ssm", "bob")

            mod = _load_posix_module()

            yield {
                "mod": mod,
                "clusters_table": clusters_table,
                "projects_table": projects_table,
            }

    def test_no_active_clusters_returns_success(self, _env):
        status = _env["mod"].propagate_user_to_clusters(
            "alice", 10001, 10001, "proj-no-clusters", CLUSTERS_TABLE_NAME,
        )
        assert status == _env["mod"].PROPAGATION_SUCCESS

    def test_successful_propagation(self, _env):
        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(return_value={"Command": {"CommandId": "cmd-1"}})

            status = _env["mod"].propagate_user_to_clusters(
                "alice", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
            )

        assert status == _env["mod"].PROPAGATION_SUCCESS
        mock_ssm.send_command.assert_called_once()
        call_args = mock_ssm.send_command.call_args
        assert call_args[1]["InstanceIds"] == ["i-abc123"]
        assert call_args[1]["DocumentName"] == "AWS-RunShellScript"

    def test_ssm_failure_retries_and_returns_pending(self, _env):
        error_response = {"Error": {"Code": "InvalidInstanceId", "Message": "Instance not found"}}
        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(
                side_effect=ClientError(error_response, "SendCommand"),
            )
            # Patch sleep to avoid actual delays
            with patch.object(_env["mod"].time, "sleep"):
                status = _env["mod"].propagate_user_to_clusters(
                    "alice", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
                )

        assert status == _env["mod"].PROPAGATION_PENDING
        assert mock_ssm.send_command.call_count == 3  # 3 retries

    def test_ssm_retry_uses_exponential_backoff(self, _env):
        error_response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}
        sleep_calls = []

        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(
                side_effect=ClientError(error_response, "SendCommand"),
            )
            with patch.object(_env["mod"].time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
                _env["mod"].propagate_user_to_clusters(
                    "alice", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
                )

        # Exponential backoff: 1*2^0=1, 1*2^1=2 (no sleep after last attempt)
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == 1   # _SSM_BASE_DELAY_SECONDS * 2^0
        assert sleep_calls[1] == 2   # _SSM_BASE_DELAY_SECONDS * 2^1

    def test_ssm_succeeds_on_second_attempt(self, _env):
        error_response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}
        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ClientError(error_response, "SendCommand")
            return {"Command": {"CommandId": "cmd-2"}}

        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(side_effect=side_effect)
            with patch.object(_env["mod"].time, "sleep"):
                status = _env["mod"].propagate_user_to_clusters(
                    "alice", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
                )

        assert status == _env["mod"].PROPAGATION_SUCCESS
        assert call_count == 2

    def test_cluster_without_instance_id_returns_pending(self, _env):
        # Seed a cluster without loginNodeInstanceId
        _seed_cluster(
            _env["clusters_table"], "proj-ssm", "cluster-no-id",
            status="ACTIVE",
        )

        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(return_value={"Command": {"CommandId": "cmd-3"}})

            status = _env["mod"].propagate_user_to_clusters(
                "alice", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
            )

        # One cluster has no instance ID, so overall status is PENDING
        assert status == _env["mod"].PROPAGATION_PENDING

    def test_only_active_clusters_are_targeted(self, _env):
        """Destroyed clusters should not receive SSM commands."""
        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(return_value={"Command": {"CommandId": "cmd-4"}})

            _env["mod"].propagate_user_to_clusters(
                "bob", 10002, 10002, "proj-ssm", CLUSTERS_TABLE_NAME,
            )

        # Only active clusters should be targeted (cluster-a and cluster-no-id)
        # cluster-b is DESTROYED and should be skipped
        for call in mock_ssm.send_command.call_args_list:
            assert "i-def456" not in call[1]["InstanceIds"]

    def test_empty_user_id_returns_success(self, _env):
        """Empty user_id generates no commands, so propagation succeeds trivially."""
        # Seed a fresh active cluster for this test
        _seed_cluster(
            _env["clusters_table"], "proj-ssm", "cluster-empty-user",
            status="ACTIVE",
            loginNodeInstanceId="i-empty123",
        )

        status = _env["mod"].propagate_user_to_clusters(
            "", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
        )
        assert status == _env["mod"].PROPAGATION_SUCCESS

    def test_non_member_skips_propagation(self, _env):
        """Validates: Requirement 9.3

        A user who has no Membership_Record for the project should NOT
        have SSM commands sent.  propagate_user_to_clusters returns
        PROPAGATION_SUCCESS (no work needed) without calling SSM.
        """
        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(return_value={"Command": {"CommandId": "cmd-skip"}})

            status = _env["mod"].propagate_user_to_clusters(
                "non-member-user", 99999, 99999, "proj-ssm", CLUSTERS_TABLE_NAME,
            )

        assert status == _env["mod"].PROPAGATION_SUCCESS
        mock_ssm.send_command.assert_not_called()

    def test_member_proceeds_with_propagation(self, _env):
        """Validates: Requirement 9.3

        A user who holds a Membership_Record for the project should
        have SSM commands sent to active clusters.
        """
        with patch.object(_env["mod"], "ssm_client") as mock_ssm:
            mock_ssm.send_command = MagicMock(return_value={"Command": {"CommandId": "cmd-member"}})

            status = _env["mod"].propagate_user_to_clusters(
                "alice", 10001, 10001, "proj-ssm", CLUSTERS_TABLE_NAME,
            )

        assert status in (_env["mod"].PROPAGATION_SUCCESS, _env["mod"].PROPAGATION_PENDING)
        # SSM should have been called at least once for the active cluster(s)
        assert mock_ssm.send_command.call_count >= 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestFetchProjectMembers:
    """Test the internal _fetch_project_members helper."""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME

            projects_table = create_projects_table()

            _seed_project_member(projects_table, "proj-fetch", "user-a")
            _seed_project_member(projects_table, "proj-fetch", "user-b")
            _seed_project_member(projects_table, "proj-fetch", "user-c")

            mod = _load_posix_module()

            yield {"mod": mod}

    def test_returns_all_members(self, _env):
        members = _env["mod"]._fetch_project_members(PROJECTS_TABLE_NAME, "proj-fetch")
        assert sorted(members) == ["user-a", "user-b", "user-c"]

    def test_empty_project_returns_empty_list(self, _env):
        members = _env["mod"]._fetch_project_members(PROJECTS_TABLE_NAME, "proj-nonexistent")
        assert members == []


@pytest.mark.usefixtures("_aws_env_vars")
class TestFetchUserPosixIdentities:
    """Test the internal _fetch_user_posix_identities helper."""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME

            users_table = create_users_table()

            _seed_user_profile(users_table, "posix-user-a", 10001, 10001)
            _seed_user_profile(users_table, "posix-user-b", 10002, 10002)
            # User without POSIX identity
            users_table.put_item(Item={
                "PK": "USER#no-posix",
                "SK": "PROFILE",
                "userId": "no-posix",
                "status": "ACTIVE",
            })

            mod = _load_posix_module()

            yield {"mod": mod}

    def test_returns_posix_identities(self, _env):
        users = _env["mod"]._fetch_user_posix_identities(
            USERS_TABLE_NAME, ["posix-user-a", "posix-user-b"],
        )
        assert len(users) == 2
        uid_map = {u["userId"]: u for u in users}
        assert uid_map["posix-user-a"]["posixUid"] == 10001
        assert uid_map["posix-user-b"]["posixUid"] == 10002

    def test_skips_users_without_posix_identity(self, _env):
        users = _env["mod"]._fetch_user_posix_identities(
            USERS_TABLE_NAME, ["posix-user-a", "no-posix"],
        )
        assert len(users) == 1
        assert users[0]["userId"] == "posix-user-a"

    def test_skips_nonexistent_users(self, _env):
        users = _env["mod"]._fetch_user_posix_identities(
            USERS_TABLE_NAME, ["ghost-user"],
        )
        assert users == []

    def test_empty_list_returns_empty(self, _env):
        users = _env["mod"]._fetch_user_posix_identities(
            USERS_TABLE_NAME, [],
        )
        assert users == []


# ---------------------------------------------------------------------------
# generate_mountpoint_s3_commands
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestGenerateMountpointS3Commands:
    """Validates: Requirements 3.4"""

    @pytest.fixture(autouse=True, scope="class")
    def _setup(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            mod = _load_posix_module()
            yield {"mod": mod}

    def test_contains_mount_s3_command_with_bucket_name(self, _setup):
        cmds = _setup["mod"].generate_mountpoint_s3_commands("my-project-bucket")
        mount_cmds = [c for c in cmds if "mount-s3" in c and "my-project-bucket" in c]
        assert len(mount_cmds) >= 1

    def test_default_mount_path_is_data(self, _setup):
        cmds = _setup["mod"].generate_mountpoint_s3_commands("my-bucket")
        joined = "\n".join(cmds)
        assert "mkdir -p /data" in joined
        assert "mount-s3 my-bucket /data" in joined

    def test_custom_mount_path(self, _setup):
        cmds = _setup["mod"].generate_mountpoint_s3_commands("my-bucket", mount_path="/mnt/s3")
        joined = "\n".join(cmds)
        assert "mkdir -p /mnt/s3" in joined
        assert "mount-s3 my-bucket /mnt/s3" in joined

    def test_installs_mountpoint_package(self, _setup):
        cmds = _setup["mod"].generate_mountpoint_s3_commands("my-bucket")
        install_cmds = [c for c in cmds if "install" in c and "mountpoint-s3" in c]
        assert len(install_cmds) == 1

    def test_persists_mount_in_rc_local(self, _setup):
        cmds = _setup["mod"].generate_mountpoint_s3_commands("my-bucket")
        rc_cmds = [c for c in cmds if "/etc/rc.local" in c and "mount-s3" in c]
        assert len(rc_cmds) == 1


# ---------------------------------------------------------------------------
# generate_fsx_lustre_mount_commands
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestGenerateFsxLustreMountCommands:
    """Validates: Requirements 3.5"""

    @pytest.fixture(autouse=True, scope="class")
    def _setup(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            mod = _load_posix_module()
            yield {"mod": mod}

    def test_contains_lustre_mount_command(self, _setup):
        cmds = _setup["mod"].generate_fsx_lustre_mount_commands(
            "fs-abc123.fsx.us-east-1.amazonaws.com", "abcdef",
        )
        mount_cmds = [c for c in cmds if "mount -t lustre" in c]
        assert len(mount_cmds) == 1
        assert "fs-abc123.fsx.us-east-1.amazonaws.com@tcp:/abcdef" in mount_cmds[0]

    def test_default_mount_path_is_data(self, _setup):
        cmds = _setup["mod"].generate_fsx_lustre_mount_commands("fs-dns", "mntname")
        joined = "\n".join(cmds)
        assert "mkdir -p /data" in joined
        assert "mount -t lustre fs-dns@tcp:/mntname /data" in joined

    def test_custom_mount_path(self, _setup):
        cmds = _setup["mod"].generate_fsx_lustre_mount_commands(
            "fs-dns", "mntname", mount_path="/mnt/lustre",
        )
        joined = "\n".join(cmds)
        assert "mkdir -p /mnt/lustre" in joined
        assert "mount -t lustre fs-dns@tcp:/mntname /mnt/lustre" in joined

    def test_installs_lustre_client(self, _setup):
        cmds = _setup["mod"].generate_fsx_lustre_mount_commands("fs-dns", "mntname")
        install_cmds = [c for c in cmds if "lustre" in c and "install" in c]
        assert len(install_cmds) == 1

    def test_adds_fstab_entry(self, _setup):
        cmds = _setup["mod"].generate_fsx_lustre_mount_commands("fs-dns", "mntname")
        fstab_cmds = [c for c in cmds if "/etc/fstab" in c]
        assert len(fstab_cmds) == 1
        assert "fs-dns@tcp:/mntname" in fstab_cmds[0]
