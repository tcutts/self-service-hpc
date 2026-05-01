"""Preservation property tests — Launch Template Fields, POSIX Script Content, Workflow Structure.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10**

These tests capture the EXISTING (correct) behavior of the unfixed code.
They are designed to PASS on unfixed code, confirming baseline behavior
that must be preserved after the fix is applied.

Observed behaviors on unfixed code:
- create_launch_templates() passes correct SecurityGroupIds per template type
- create_launch_templates() passes correct ImageId per template type
- create_launch_templates() passes correct TagSpecifications with project/cluster tags
- create_launch_templates() adopts existing templates on AlreadyExistsException
- generate_user_data_script() with storage_mode="mountpoint" produces S3 mount commands
- generate_user_data_script() with storage_mode="lustre" produces FSx mount commands
- generate_user_data_script() with no storage mode omits storage mount commands
- generate_user_data_script() always produces POSIX, generic account, PAM, CloudWatch commands
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "posix_provisioning")
load_lambda_module("cluster_operations", "tagging")
load_lambda_module("cluster_operations", "cluster_creation")


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Security group IDs: sg- followed by 8-17 hex chars
sg_id_strategy = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)

# AMI IDs: ami- followed by 8-17 hex chars
ami_id_strategy = st.from_regex(r"ami-[0-9a-f]{8,17}", fullmatch=True)

# Project IDs: alphanumeric with hyphens, 3-20 chars
project_id_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,19}", fullmatch=True)

# Cluster names: alphanumeric with hyphens/underscores, 3-20 chars
cluster_name_strategy = st.from_regex(r"[a-z][a-z0-9\-_]{2,19}", fullmatch=True)

# POSIX user IDs: simple alphanumeric usernames
user_id_strategy = st.from_regex(r"[a-z][a-z0-9]{2,14}", fullmatch=True)

# POSIX UID/GID: valid range
posix_uid_strategy = st.integers(min_value=1000, max_value=65534)

# Storage mode strategy
storage_mode_strategy = st.sampled_from(["", "mountpoint", "lustre"])

# S3 bucket name strategy
s3_bucket_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)

# FSx DNS name strategy
fsx_dns_strategy = st.from_regex(r"fs-[0-9a-f]{8}\.fsx\.[a-z]{2}-[a-z]+-[0-9]\.amazonaws\.com", fullmatch=True)

# FSx mount name strategy
fsx_mount_strategy = st.from_regex(r"[a-z]{5,8}", fullmatch=True)

# A single POSIX user record
posix_user_strategy = st.fixed_dictionaries({
    "userId": user_id_strategy,
    "posixUid": posix_uid_strategy,
    "posixGid": posix_uid_strategy,
})

# List of 0 to 5 POSIX users
posix_users_list_strategy = st.lists(posix_user_strategy, min_size=0, max_size=5)


# ---------------------------------------------------------------------------
# Helper: build a mock DynamoDB that returns given users
# ---------------------------------------------------------------------------

def _build_mock_dynamodb(users):
    """Build a mock DynamoDB resource that returns the given user list.

    The mock supports:
    - Table("Projects").query() -> returns MEMBER# items for each user
    - Table("PlatformUsers").get_item() -> returns PROFILE with posixUid/posixGid
    """
    mock_dynamodb = MagicMock()

    # Build member items for the projects table query
    member_items = [{"userId": u["userId"]} for u in users]

    # Build user profile items for the users table get_item
    user_profiles = {}
    for u in users:
        user_profiles[f"USER#{u['userId']}"] = {
            "Item": {
                "PK": f"USER#{u['userId']}",
                "SK": "PROFILE",
                "userId": u["userId"],
                "posixUid": u["posixUid"],
                "posixGid": u["posixGid"],
            }
        }

    def mock_table(table_name):
        table = MagicMock()
        table.query.return_value = {"Items": member_items}

        def mock_get_item(Key=None, **kwargs):
            pk = Key.get("PK", "") if Key else ""
            if pk in user_profiles:
                return user_profiles[pk]
            return {"Item": None}

        table.get_item.side_effect = mock_get_item
        return table

    mock_dynamodb.Table.side_effect = mock_table
    return mock_dynamodb


# ===========================================================================
# Property tests for create_launch_templates() preservation
# ===========================================================================

class TestCreateLaunchTemplatesPreservation:
    """Verify create_launch_templates() preserves SecurityGroupIds, ImageId,
    and TagSpecifications for random valid events.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
    """

    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        head_sg=sg_id_strategy,
        compute_sg=sg_id_strategy,
        ami_id=ami_id_strategy,
        login_ami_id=ami_id_strategy,
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_security_group_ids_and_image_ids_preserved(
        self, head_sg, compute_sg, ami_id, login_ami_id, project_id, cluster_name
    ):
        """For random valid events, create_launch_templates() passes the correct
        SecurityGroupIds and ImageId to ec2_client.create_launch_template.

        **Validates: Requirements 3.1, 3.2**

        Login template gets headNode SG and login AMI.
        Compute template gets computeNode SG and compute AMI.
        """
        from cluster_creation import create_launch_templates

        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "securityGroupIds": {
                "headNode": head_sg,
                "computeNode": compute_sg,
                "efs": "sg-efs000",
                "fsx": "sg-fsx000",
            },
            "amiId": ami_id,
            "loginAmiId": login_ami_id,
        }

        mock_ec2 = MagicMock()
        mock_ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-mock123"},
        }
        # Mock describe_images for AMI validation (added by Task 3.4)
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }

        mock_dynamodb = _build_mock_dynamodb([])

        with patch("cluster_creation.ec2_client", mock_ec2), \
             patch("posix_provisioning.dynamodb", mock_dynamodb):
            create_launch_templates(event)

        assert mock_ec2.create_launch_template.call_count == 2

        # First call = login template
        login_call = mock_ec2.create_launch_template.call_args_list[0]
        login_lt_data = login_call.kwargs["LaunchTemplateData"]
        assert login_lt_data["SecurityGroupIds"] == [head_sg], (
            f"Login template SecurityGroupIds should be [{head_sg}], "
            f"got {login_lt_data['SecurityGroupIds']}"
        )
        assert login_lt_data["ImageId"] == login_ami_id, (
            f"Login template ImageId should be {login_ami_id}, "
            f"got {login_lt_data['ImageId']}"
        )

        # Second call = compute template
        compute_call = mock_ec2.create_launch_template.call_args_list[1]
        compute_lt_data = compute_call.kwargs["LaunchTemplateData"]
        assert compute_lt_data["SecurityGroupIds"] == [compute_sg], (
            f"Compute template SecurityGroupIds should be [{compute_sg}], "
            f"got {compute_lt_data['SecurityGroupIds']}"
        )
        assert compute_lt_data["ImageId"] == ami_id, (
            f"Compute template ImageId should be {ami_id}, "
            f"got {compute_lt_data['ImageId']}"
        )

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
    )
    def test_tag_specifications_preserved(self, project_id, cluster_name):
        """For random valid events, create_launch_templates() passes correct
        TagSpecifications with Project and ClusterName tags.

        **Validates: Requirements 3.3**
        """
        from cluster_creation import create_launch_templates

        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "securityGroupIds": {
                "headNode": "sg-head000",
                "computeNode": "sg-comp000",
                "efs": "sg-efs000",
                "fsx": "sg-fsx000",
            },
            "amiId": "ami-test12345678",
        }

        mock_ec2 = MagicMock()
        mock_ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-mock123"},
        }
        # Mock describe_images for AMI validation (added by Task 3.4)
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }

        mock_dynamodb = _build_mock_dynamodb([])

        with patch("cluster_creation.ec2_client", mock_ec2), \
             patch("posix_provisioning.dynamodb", mock_dynamodb):
            create_launch_templates(event)

        for call in mock_ec2.create_launch_template.call_args_list:
            tag_specs = call.kwargs["TagSpecifications"]
            assert len(tag_specs) == 1
            assert tag_specs[0]["ResourceType"] == "launch-template"

            tags = tag_specs[0]["Tags"]
            tag_dict = {t["Key"]: t["Value"] for t in tags}
            assert tag_dict.get("Project") == project_id, (
                f"Expected Project tag '{project_id}', got '{tag_dict.get('Project')}'"
            )
            assert tag_dict.get("ClusterName") == cluster_name, (
                f"Expected ClusterName tag '{cluster_name}', got '{tag_dict.get('ClusterName')}'"
            )

    def test_adopts_existing_template_on_already_exists(self):
        """When InvalidLaunchTemplateName.AlreadyExistsException is raised,
        create_launch_templates() adopts the existing template.

        **Validates: Requirements 3.4**
        """
        from botocore.exceptions import ClientError
        from cluster_creation import create_launch_templates

        event = {
            "projectId": "test-project",
            "clusterName": "test-cluster",
            "securityGroupIds": {
                "headNode": "sg-head000",
                "computeNode": "sg-comp000",
                "efs": "sg-efs000",
                "fsx": "sg-fsx000",
            },
            "amiId": "ami-test12345678",
        }

        mock_ec2 = MagicMock()

        # Mock describe_images for AMI validation (added by Task 3.4)
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }

        # First call raises AlreadyExistsException, second succeeds
        already_exists_error = ClientError(
            {
                "Error": {
                    "Code": "InvalidLaunchTemplateName.AlreadyExistsException",
                    "Message": "Launch template name already in use",
                }
            },
            "CreateLaunchTemplate",
        )
        mock_ec2.create_launch_template.side_effect = [
            already_exists_error,
            {"LaunchTemplate": {"LaunchTemplateId": "lt-compute123"}},
        ]
        mock_ec2.describe_launch_templates.return_value = {
            "LaunchTemplates": [{"LaunchTemplateId": "lt-existing123"}],
        }

        mock_dynamodb = _build_mock_dynamodb([])

        with patch("cluster_creation.ec2_client", mock_ec2), \
             patch("posix_provisioning.dynamodb", mock_dynamodb):
            result = create_launch_templates(event)

        # The login template should have been adopted via describe
        assert result["loginLaunchTemplateId"] == "lt-existing123"
        # The compute template was created normally
        assert result["computeLaunchTemplateId"] == "lt-compute123"


# ===========================================================================
# Property tests for generate_user_data_script() preservation
# ===========================================================================

class TestGenerateUserDataScriptPreservation:
    """Verify generate_user_data_script() preserves POSIX commands, generic
    account disabling, PAM logging, CloudWatch agent, and storage mount
    commands for random combinations of users and storage modes.

    **Validates: Requirements 3.5, 3.6, 3.7, 3.8**
    """

    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_posix_and_common_commands_always_present(self, users, project_id):
        """For random users and project IDs, the script always contains
        POSIX user creation, generic account disabling, PAM logging,
        and CloudWatch agent commands.

        **Validates: Requirements 3.7**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="",
            )

        # Script always starts with shebang
        assert script.startswith("#!/bin/bash"), "Script must start with shebang"

        # POSIX user creation commands for each user
        for user in users:
            assert f"useradd" in script or len(users) == 0, (
                "Script should contain useradd commands for users"
            )
            if users:
                assert f"groupadd" in script, (
                    "Script should contain groupadd commands"
                )

        # Generic account disabling
        for account in ["ec2-user", "centos", "ubuntu"]:
            assert account in script, (
                f"Script should reference generic account '{account}'"
            )
        assert "nologin" in script, "Script should disable generic accounts with nologin"

        # PAM exec logging
        assert "pam_exec" in script, "Script should configure pam_exec logging"
        assert "hpc-access-log" in script, "Script should reference access log"

        # CloudWatch agent
        assert "cloudwatch" in script.lower() or "amazon-cloudwatch-agent" in script, (
            "Script should configure CloudWatch agent"
        )

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        s3_bucket=s3_bucket_strategy,
    )
    def test_mountpoint_s3_commands_when_storage_mode_mountpoint(
        self, users, project_id, s3_bucket
    ):
        """When storage_mode="mountpoint" and s3_bucket_name is provided,
        the script contains Mountpoint for S3 mount commands.

        **Validates: Requirements 3.5**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="mountpoint",
                s3_bucket_name=s3_bucket,
            )

        assert "mount-s3" in script, (
            "Script should contain mount-s3 install command"
        )
        assert f"mount-s3 {s3_bucket}" in script, (
            f"Script should mount S3 bucket '{s3_bucket}'"
        )

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        fsx_dns=fsx_dns_strategy,
        fsx_mount=fsx_mount_strategy,
    )
    def test_fsx_lustre_commands_when_storage_mode_lustre(
        self, users, project_id, fsx_dns, fsx_mount
    ):
        """When storage_mode="lustre" and FSx parameters are provided,
        the script contains FSx for Lustre mount commands.

        **Validates: Requirements 3.6**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="lustre",
                fsx_dns_name=fsx_dns,
                fsx_mount_name=fsx_mount,
            )

        assert "lustre" in script.lower(), (
            "Script should contain lustre mount commands"
        )
        assert f"mount -t lustre" in script, (
            "Script should contain 'mount -t lustre' command"
        )
        assert fsx_dns in script, (
            f"Script should reference FSx DNS name '{fsx_dns}'"
        )
        assert fsx_mount in script, (
            f"Script should reference FSx mount name '{fsx_mount}'"
        )

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_no_storage_commands_when_no_storage_mode(self, users, project_id):
        """When storage_mode is empty, the script omits storage mount commands.

        **Validates: Requirements 3.8**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="",
            )

        assert "dnf install -y mount-s3" not in script, (
            "Script should NOT contain mount-s3 install when no storage mode"
        )
        assert "mount-s3" not in script, (
            "Script should NOT contain mount-s3 when no storage mode"
        )
        assert "mount -t lustre" not in script, (
            "Script should NOT contain lustre mount when no storage mode"
        )

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_user_creation_commands_match_users(self, users, project_id):
        """For each user in the input, the script contains the correct
        useradd and groupadd commands with matching UID/GID.

        **Validates: Requirements 3.7**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
            )

        for user in users:
            uid = user["posixUid"]
            gid = user["posixGid"]
            user_id = user["userId"]
            assert f"groupadd -g {gid} {user_id}" in script, (
                f"Script should contain groupadd for user '{user_id}' with GID {gid}"
            )
            assert f"useradd -u {uid} -g {gid}" in script, (
                f"Script should contain useradd for user '{user_id}' with UID {uid}"
            )
            assert f"chown {uid}:{gid} /home/{user_id}" in script, (
                f"Script should set ownership on /home/{user_id}"
            )
