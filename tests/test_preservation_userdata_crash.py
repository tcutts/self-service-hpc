"""Preservation property tests — User Data Script Commands and Section Order.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

These tests capture the EXISTING (correct) behavior of the unfixed code.
They are designed to PASS on unfixed code, confirming baseline behavior
that must be preserved after the fix is applied.

Observed behaviors on unfixed code:
- generate_user_data_script() with EFS filesystem ID produces EFS mount commands
- generate_user_data_script() with storage_mode="mountpoint" produces S3 mount commands
- generate_user_data_script() with storage_mode="lustre" produces FSx mount commands
- Script always contains SSM Agent commands as the first section
- Script always contains user creation commands with correct UID/GID for each user
- Script always contains generic account disabling for ec2-user, centos, ubuntu
- Script always contains PAM exec logging commands
- Script always contains CloudWatch agent commands
- Section order: SSM Agent → EFS → Users → Generic accounts → Access logging → CloudWatch → Storage
- wrap_user_data_mime() produces valid MIME multipart output containing the script
- Helper functions return identical command lists regardless of fix
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

for _d in [_SHARED_DIR, _CLUSTER_OPS_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

project_id_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,19}", fullmatch=True)

user_id_strategy = st.from_regex(r"[a-z][a-z0-9]{2,14}", fullmatch=True)

posix_uid_strategy = st.integers(min_value=1000, max_value=65534)

posix_user_strategy = st.fixed_dictionaries({
    "userId": user_id_strategy,
    "posixUid": posix_uid_strategy,
    "posixGid": posix_uid_strategy,
})

posix_users_list_strategy = st.lists(posix_user_strategy, min_size=0, max_size=5)

s3_bucket_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)

efs_filesystem_id_strategy = st.from_regex(
    r"fs-[0-9a-f]{8,17}", fullmatch=True
)

fsx_dns_strategy = st.from_regex(
    r"fs-[0-9a-f]{8}\.fsx\.[a-z]{2}-[a-z]+-[0-9]\.amazonaws\.com",
    fullmatch=True,
)

fsx_mount_strategy = st.from_regex(r"[a-z]{5,8}", fullmatch=True)


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

    member_items = [{"userId": u["userId"]} for u in users]

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


# ---------------------------------------------------------------------------
# Section comment markers observed in unfixed code
# ---------------------------------------------------------------------------

# The unfixed generate_user_data_script() emits these comment markers in order.
# We use them to verify section ordering is preserved.
SECTION_MARKERS = [
    "Ensure SSM Agent",           # SSM Agent section
    "Mount EFS filesystem",       # EFS mount section (conditional)
    "Create project user",        # User creation section
    "Disable generic accounts",   # Generic account disabling
    "Configure access logging",   # PAM exec logging
    "Configure CloudWatch",       # CloudWatch agent
    "Mount project S3 bucket",    # S3 storage (conditional)
    "Mount FSx for Lustre",       # FSx storage (conditional)
]


def _find_marker_position(script: str, marker: str) -> int:
    """Return the character position of a marker in the script, or -1."""
    return script.find(marker)


# ===========================================================================
# Property tests: operational commands preservation
# ===========================================================================

class TestOperationalCommandsPreservation:
    """Verify that generate_user_data_script() produces the correct
    operational commands for all random valid configurations.

    **Validates: Requirements 3.1, 3.5, 3.6, 3.7, 3.8**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=st.lists(posix_user_strategy, min_size=1, max_size=5).filter(
            lambda us: len({u["userId"] for u in us}) == len(us)
        ),
        project_id=project_id_strategy,
    )
    def test_user_creation_commands_present_and_correct(self, users, project_id):
        """For all random valid configurations with at least one user,
        verify useradd, groupadd, and chown commands are present with
        correct UID/GID for each user.

        **Validates: Requirements 3.5**
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

        for user in users:
            uid = user["posixUid"]
            gid = user["posixGid"]
            user_id = user["userId"]
            assert f"groupadd -g {gid} {user_id}" in script, (
                f"Missing groupadd for user '{user_id}' with GID {gid}"
            )
            assert f"useradd -u {uid} -g {gid}" in script, (
                f"Missing useradd for user '{user_id}' with UID {uid}"
            )
            assert f"chown {uid}:{gid} /home/{user_id}" in script, (
                f"Missing chown for /home/{user_id}"
            )

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_generic_account_disabling_always_present(self, users, project_id):
        """For all random valid configurations, verify generic account
        disabling commands are present for ec2-user, centos, ubuntu.

        **Validates: Requirements 3.6**
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

        for account in ["ec2-user", "centos", "ubuntu"]:
            assert account in script, (
                f"Script must reference generic account '{account}'"
            )
        assert "nologin" in script, (
            "Script must disable generic accounts with nologin"
        )

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_ssm_agent_pam_cloudwatch_always_present(self, users, project_id):
        """For all random valid configurations, verify SSM Agent, PAM exec
        logging, and CloudWatch agent commands are always present.

        **Validates: Requirements 3.1, 3.8**
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

        # SSM Agent
        assert "amazon-ssm-agent" in script, (
            "Script must contain SSM Agent commands"
        )
        assert "systemctl enable amazon-ssm-agent" in script, (
            "Script must enable SSM Agent service"
        )

        # PAM exec logging
        assert "pam_exec" in script, (
            "Script must configure pam_exec logging"
        )
        assert "hpc-access-log" in script, (
            "Script must reference hpc-access-log"
        )

        # CloudWatch agent
        assert "amazon-cloudwatch-agent" in script, (
            "Script must configure CloudWatch agent"
        )


# ===========================================================================
# Property tests: section ordering preservation
# ===========================================================================

class TestSectionOrderPreservation:
    """Verify that section comment markers appear in the expected order
    for all random valid configurations.

    Observed order: SSM Agent → EFS → Users → Generic accounts →
    Access logging → CloudWatch → Storage

    **Validates: Requirements 3.1**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_section_order_no_storage(self, users, project_id):
        """With no storage mode, sections appear in order:
        SSM Agent → Users → Generic accounts → Access logging → CloudWatch.

        **Validates: Requirements 3.1**
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

        expected_markers = [
            "Ensure SSM Agent",
            "Create project user",
            "Disable generic accounts",
            "Configure access logging",
            "Configure CloudWatch",
        ]

        positions = []
        for marker in expected_markers:
            pos = _find_marker_position(script, marker)
            assert pos >= 0, (
                f"Section marker '{marker}' not found in script"
            )
            positions.append(pos)

        for i in range(len(positions) - 1):
            assert positions[i] < positions[i + 1], (
                f"Section '{expected_markers[i]}' (pos {positions[i]}) "
                f"must appear before '{expected_markers[i + 1]}' "
                f"(pos {positions[i + 1]})"
            )

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        efs_id=efs_filesystem_id_strategy,
    )
    def test_section_order_with_efs(self, users, project_id, efs_id):
        """With EFS, the EFS section appears after SSM Agent and before
        user creation.

        **Validates: Requirements 3.1, 3.2**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="",
                efs_filesystem_id=efs_id,
            )

        ssm_pos = _find_marker_position(script, "Ensure SSM Agent")
        efs_pos = _find_marker_position(script, "Mount EFS filesystem")
        users_pos = _find_marker_position(script, "Create project user")

        assert ssm_pos >= 0, "SSM Agent section not found"
        assert efs_pos >= 0, "EFS mount section not found"
        assert users_pos >= 0, "User creation section not found"

        assert ssm_pos < efs_pos < users_pos, (
            f"Section order must be SSM ({ssm_pos}) → EFS ({efs_pos}) "
            f"→ Users ({users_pos})"
        )

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        s3_bucket=s3_bucket_strategy,
    )
    def test_section_order_with_s3_storage(self, users, project_id, s3_bucket):
        """With S3 storage, the storage section appears after CloudWatch.

        **Validates: Requirements 3.1, 3.3**
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

        cw_pos = _find_marker_position(script, "Configure CloudWatch")
        s3_pos = _find_marker_position(script, "Mount project S3 bucket")

        assert cw_pos >= 0, "CloudWatch section not found"
        assert s3_pos >= 0, "S3 storage section not found"

        assert cw_pos < s3_pos, (
            f"CloudWatch ({cw_pos}) must appear before S3 ({s3_pos})"
        )

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        fsx_dns=fsx_dns_strategy,
        fsx_mount=fsx_mount_strategy,
    )
    def test_section_order_with_fsx_storage(
        self, users, project_id, fsx_dns, fsx_mount
    ):
        """With FSx storage, the storage section appears after CloudWatch.

        **Validates: Requirements 3.1, 3.4**
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

        cw_pos = _find_marker_position(script, "Configure CloudWatch")
        fsx_pos = _find_marker_position(script, "Mount FSx for Lustre")

        assert cw_pos >= 0, "CloudWatch section not found"
        assert fsx_pos >= 0, "FSx storage section not found"

        assert cw_pos < fsx_pos, (
            f"CloudWatch ({cw_pos}) must appear before FSx ({fsx_pos})"
        )


# ===========================================================================
# Property tests: EFS mount commands preservation
# ===========================================================================

class TestEfsMountPreservation:
    """Verify EFS mount commands are present with correct filesystem ID
    for all random valid configurations with EFS.

    **Validates: Requirements 3.2**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        efs_id=efs_filesystem_id_strategy,
    )
    def test_efs_mount_commands_present(self, users, project_id, efs_id):
        """When efs_filesystem_id is provided, the script contains EFS
        mount commands with the correct filesystem ID.

        **Validates: Requirements 3.2**
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="",
                efs_filesystem_id=efs_id,
            )

        assert "amazon-efs-utils" in script, (
            "Script must install amazon-efs-utils for EFS mount"
        )
        assert "mount -a -t efs" in script, (
            "Script must contain 'mount -a -t efs' command"
        )
        assert efs_id in script, (
            f"Script must reference EFS filesystem ID '{efs_id}'"
        )
        assert f"{efs_id}:/ /home efs _netdev,tls 0 0" in script, (
            f"Script must contain fstab entry for EFS '{efs_id}'"
        )


# ===========================================================================
# Property tests: S3 mount commands preservation
# ===========================================================================

class TestS3MountPreservation:
    """Verify S3 mount commands are present with correct bucket name
    for all random valid configurations with S3 storage.

    **Validates: Requirements 3.3**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        s3_bucket=s3_bucket_strategy,
    )
    def test_s3_mount_commands_present(self, users, project_id, s3_bucket):
        """When storage_mode="mountpoint" and s3_bucket_name is provided,
        the script contains S3 mount commands with the correct bucket.

        **Validates: Requirements 3.3**
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
            "Script must install mount-s3"
        )
        assert f"mount-s3 {s3_bucket}" in script, (
            f"Script must mount S3 bucket '{s3_bucket}'"
        )
        assert "rc.local" in script, (
            "Script must add mount-s3 to rc.local for persistence"
        )


# ===========================================================================
# Property tests: FSx mount commands preservation
# ===========================================================================

class TestFsxMountPreservation:
    """Verify FSx mount commands are present with correct DNS and mount
    names for all random valid configurations with FSx storage.

    **Validates: Requirements 3.4**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
        fsx_dns=fsx_dns_strategy,
        fsx_mount=fsx_mount_strategy,
    )
    def test_fsx_mount_commands_present(
        self, users, project_id, fsx_dns, fsx_mount
    ):
        """When storage_mode="lustre" and FSx parameters are provided,
        the script contains FSx mount commands with correct DNS and
        mount names.

        **Validates: Requirements 3.4**
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

        assert "mount -t lustre" in script, (
            "Script must contain 'mount -t lustre' command"
        )
        assert fsx_dns in script, (
            f"Script must reference FSx DNS name '{fsx_dns}'"
        )
        assert fsx_mount in script, (
            f"Script must reference FSx mount name '{fsx_mount}'"
        )
        assert f"{fsx_dns}@tcp:/{fsx_mount}" in script, (
            f"Script must contain FSx mount spec "
            f"'{fsx_dns}@tcp:/{fsx_mount}'"
        )


# ===========================================================================
# Property tests: MIME wrapping preservation
# ===========================================================================

class TestMimeWrappingPreservation:
    """Verify wrap_user_data_mime() produces valid MIME multipart output
    containing the generated script for all random valid configurations.

    **Validates: Requirements 3.7**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        users=posix_users_list_strategy,
        project_id=project_id_strategy,
    )
    def test_mime_wrapping_produces_valid_output(self, users, project_id):
        """For all random valid configurations, wrap_user_data_mime()
        produces valid MIME multipart output containing the script.

        **Validates: Requirements 3.7**
        """
        import base64
        from email import message_from_string

        from posix_provisioning import (
            generate_user_data_script,
            wrap_user_data_mime,
        )

        mock_dynamodb = _build_mock_dynamodb(users)

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=project_id,
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode="",
            )

        mime_output = wrap_user_data_mime(script)

        assert "Content-Type: multipart/mixed" in mime_output, (
            "MIME output must contain multipart/mixed Content-Type"
        )
        assert "Content-Type: text/x-shellscript" in mime_output, (
            "MIME output must contain text/x-shellscript part"
        )

        # Parse the MIME message and decode the payload
        msg = message_from_string(mime_output)
        assert msg.is_multipart(), "MIME message must be multipart"

        parts = msg.get_payload()
        assert len(parts) >= 1, "MIME message must have at least one part"

        shell_part = parts[0]
        payload = shell_part.get_payload(decode=True).decode("utf-8")
        assert "#!/bin/bash" in payload, (
            "Decoded MIME payload must contain the bash shebang"
        )
        assert project_id in payload, (
            f"Decoded MIME payload must contain project ID '{project_id}'"
        )


# ===========================================================================
# Property tests: helper function preservation
# ===========================================================================

class TestHelperFunctionPreservation:
    """Verify helper functions return identical command lists.
    These functions must not be modified by the fix.

    **Validates: Requirements 3.2, 3.3, 3.4**
    """

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(efs_id=efs_filesystem_id_strategy)
    def test_generate_efs_mount_commands_stable(self, efs_id):
        """generate_efs_mount_commands() returns a consistent list of
        commands for any valid EFS filesystem ID.

        **Validates: Requirements 3.2**
        """
        from posix_provisioning import generate_efs_mount_commands

        commands = generate_efs_mount_commands(efs_id)

        assert isinstance(commands, list), "Must return a list"
        assert len(commands) == 5, (
            f"Expected 5 commands, got {len(commands)}"
        )
        assert any("amazon-efs-utils" in c for c in commands), (
            "Must include amazon-efs-utils install"
        )
        assert any("mkdir -p /home" in c for c in commands), (
            "Must include mkdir for mount point"
        )
        assert any(f"{efs_id}:/" in c for c in commands), (
            f"Must reference EFS ID '{efs_id}'"
        )
        assert any("mount -a -t efs" in c for c in commands), (
            "Must include mount command"
        )

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(s3_bucket=s3_bucket_strategy)
    def test_generate_mountpoint_s3_commands_stable(self, s3_bucket):
        """generate_mountpoint_s3_commands() returns a consistent list
        of commands for any valid S3 bucket name.

        **Validates: Requirements 3.3**
        """
        from posix_provisioning import generate_mountpoint_s3_commands

        commands = generate_mountpoint_s3_commands(s3_bucket)

        assert isinstance(commands, list), "Must return a list"
        assert len(commands) == 6, (
            f"Expected 6 commands, got {len(commands)}"
        )
        assert any("mount-s3" in c for c in commands), (
            "Must include mount-s3 install"
        )
        assert any(f"mount-s3 {s3_bucket}" in c for c in commands), (
            f"Must include mount-s3 for bucket '{s3_bucket}'"
        )
        assert any("rc.local" in c for c in commands), (
            "Must include rc.local persistence"
        )

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        fsx_dns=fsx_dns_strategy,
        fsx_mount=fsx_mount_strategy,
    )
    def test_generate_fsx_lustre_mount_commands_stable(
        self, fsx_dns, fsx_mount
    ):
        """generate_fsx_lustre_mount_commands() returns a consistent
        list of commands for any valid FSx DNS and mount names.

        **Validates: Requirements 3.4**
        """
        from posix_provisioning import generate_fsx_lustre_mount_commands

        commands = generate_fsx_lustre_mount_commands(fsx_dns, fsx_mount)

        assert isinstance(commands, list), "Must return a list"
        assert len(commands) == 5, (
            f"Expected 5 commands, got {len(commands)}"
        )
        assert any("lustre" in c.lower() for c in commands), (
            "Must include lustre client install"
        )
        assert any(f"mount -t lustre" in c for c in commands), (
            "Must include mount -t lustre command"
        )
        assert any(f"{fsx_dns}@tcp:/{fsx_mount}" in c for c in commands), (
            f"Must reference '{fsx_dns}@tcp:/{fsx_mount}'"
        )
        assert any("fstab" in c for c in commands), (
            "Must include fstab entry"
        )
