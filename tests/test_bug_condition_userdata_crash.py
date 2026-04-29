"""Bug condition exploration test — User Data Script Crash Due to set -euo pipefail.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.6**

This test encodes the EXPECTED (correct/fixed) behavior. It is designed to
FAIL on unfixed code, proving the bug exists. After the fix is applied, the
same test should PASS, confirming the bug is resolved.

Bug condition:
- generate_user_data_script() produces a bash script with `set -euo pipefail`
  on line 2, causing any single command failure to abort the entire script.
- No error isolation exists around individual sections.
- The script does not exit 0 unconditionally.
- No section tracking or summary output exists.
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
# Hypothesis strategies (reused from test_preservation_launch_template.py)
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

storage_mode_strategy = st.sampled_from(["", "mountpoint", "lustre"])

s3_bucket_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True)

efs_filesystem_id_strategy = st.from_regex(r"fs-[0-9a-f]{8,17}", fullmatch=True)

fsx_dns_strategy = st.from_regex(
    r"fs-[0-9a-f]{8}\.fsx\.[a-z]{2}-[a-z]+-[0-9]\.amazonaws\.com",
    fullmatch=True,
)

fsx_mount_strategy = st.from_regex(r"[a-z]{5,8}", fullmatch=True)


# ---------------------------------------------------------------------------
# Helper: build a mock DynamoDB that returns given users
# ---------------------------------------------------------------------------

def _build_mock_dynamodb(users):
    """Build a mock DynamoDB resource that returns the given user list."""
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
# Composite strategy: generate full input configurations
# ---------------------------------------------------------------------------

@st.composite
def script_config_strategy(draw):
    """Generate a full configuration for generate_user_data_script()."""
    project_id = draw(project_id_strategy)
    users = draw(posix_users_list_strategy)
    storage_mode = draw(storage_mode_strategy)
    efs_filesystem_id = draw(efs_filesystem_id_strategy)

    s3_bucket_name = ""
    fsx_dns_name = ""
    fsx_mount_name = ""

    if storage_mode == "mountpoint":
        s3_bucket_name = draw(s3_bucket_strategy)
    elif storage_mode == "lustre":
        fsx_dns_name = draw(fsx_dns_strategy)
        fsx_mount_name = draw(fsx_mount_strategy)

    return {
        "project_id": project_id,
        "users": users,
        "storage_mode": storage_mode,
        "s3_bucket_name": s3_bucket_name,
        "fsx_dns_name": fsx_dns_name,
        "fsx_mount_name": fsx_mount_name,
        "efs_filesystem_id": efs_filesystem_id,
    }


# ===========================================================================
# Bug condition exploration test
# ===========================================================================

class TestBugConditionUserDataCrash:
    """Verify that generate_user_data_script() produces a resilient script.

    On UNFIXED code, these tests FAIL — proving the bug exists:
    - `set -euo pipefail` is present (should be absent)
    - No error isolation wrappers exist (should be present)
    - Script does not end with `exit 0` (should)
    - No section tracking variables (should be present)
    - No summary section (should be present)

    **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.6**
    """

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(config=script_config_strategy())
    def test_script_does_not_contain_set_euo_pipefail(self, config):
        """Generated script must NOT contain `set -euo pipefail`.

        **Validates: Requirements 1.1, 2.1**

        On UNFIXED code this FAILS because line 2 of every generated
        script is `set -euo pipefail`.
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(config["users"])

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=config["project_id"],
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode=config["storage_mode"],
                s3_bucket_name=config["s3_bucket_name"],
                fsx_dns_name=config["fsx_dns_name"],
                fsx_mount_name=config["fsx_mount_name"],
                efs_filesystem_id=config["efs_filesystem_id"],
            )

        assert "set -euo pipefail" not in script, (
            "Script contains 'set -euo pipefail' which causes the entire "
            "script to abort on any command failure. This is the root cause "
            "of the login node crash loop."
        )

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(config=script_config_strategy())
    def test_script_ends_with_exit_0(self, config):
        """Generated script must end with `exit 0`.

        **Validates: Requirements 2.2**

        On UNFIXED code this FAILS because the script ends with
        `echo 'POSIX user provisioning complete.'` instead of `exit 0`.
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(config["users"])

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=config["project_id"],
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode=config["storage_mode"],
                s3_bucket_name=config["s3_bucket_name"],
                fsx_dns_name=config["fsx_dns_name"],
                fsx_mount_name=config["fsx_mount_name"],
                efs_filesystem_id=config["efs_filesystem_id"],
            )

        # Strip trailing whitespace and get the last non-empty line
        lines = [l for l in script.strip().splitlines() if l.strip()]
        last_line = lines[-1].strip() if lines else ""

        assert last_line == "exit 0", (
            f"Script must end with 'exit 0' so cloud-final reports success "
            f"and PCS does not terminate the node. "
            f"Last line is: '{last_line}'"
        )

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(config=script_config_strategy())
    def test_script_has_error_isolation_subshells(self, config):
        """Each section must be wrapped in error-isolation blocks.

        The pattern uses subshells `(` ... `)` with exit code capture
        to prevent individual section failures from aborting the script.

        **Validates: Requirements 1.3, 1.4, 1.5, 2.1**

        On UNFIXED code this FAILS because no error isolation exists.
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(config["users"])

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=config["project_id"],
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode=config["storage_mode"],
                s3_bucket_name=config["s3_bucket_name"],
                fsx_dns_name=config["fsx_dns_name"],
                fsx_mount_name=config["fsx_mount_name"],
                efs_filesystem_id=config["efs_filesystem_id"],
            )

        # Check for subshell error-isolation pattern: lines starting
        # with `(` (subshell open) and `)` (subshell close) with
        # exit code capture via `$?`
        has_subshell_open = bool(re.search(r"^\($", script, re.MULTILINE))
        has_subshell_close = bool(re.search(r"^\)", script, re.MULTILINE))
        has_exit_code_capture = "$?" in script

        assert has_subshell_open and has_subshell_close and has_exit_code_capture, (
            "Script must wrap sections in error-isolation subshell blocks "
            "using pattern: ( ... ) with exit code capture via $?. "
            f"Found subshell open: {has_subshell_open}, "
            f"subshell close: {has_subshell_close}, "
            f"exit code capture: {has_exit_code_capture}"
        )

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(config=script_config_strategy())
    def test_script_has_section_tracking_variables(self, config):
        """Script must declare FAILED_SECTIONS and SUCCEEDED_SECTIONS
        tracking variables.

        **Validates: Requirements 2.6**

        On UNFIXED code this FAILS because no section tracking exists.
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(config["users"])

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=config["project_id"],
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode=config["storage_mode"],
                s3_bucket_name=config["s3_bucket_name"],
                fsx_dns_name=config["fsx_dns_name"],
                fsx_mount_name=config["fsx_mount_name"],
                efs_filesystem_id=config["efs_filesystem_id"],
            )

        assert "FAILED_SECTIONS" in script, (
            "Script must declare a FAILED_SECTIONS tracking variable "
            "to record which sections failed during execution."
        )
        assert "SUCCEEDED_SECTIONS" in script, (
            "Script must declare a SUCCEEDED_SECTIONS tracking variable "
            "to record which sections succeeded during execution."
        )

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(config=script_config_strategy())
    def test_script_has_summary_section(self, config):
        """Script must contain a summary section at the end listing
        succeeded and failed sections.

        **Validates: Requirements 1.6, 2.6**

        On UNFIXED code this FAILS because no summary output exists.
        """
        from posix_provisioning import generate_user_data_script

        mock_dynamodb = _build_mock_dynamodb(config["users"])

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            script = generate_user_data_script(
                project_id=config["project_id"],
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                storage_mode=config["storage_mode"],
                s3_bucket_name=config["s3_bucket_name"],
                fsx_dns_name=config["fsx_dns_name"],
                fsx_mount_name=config["fsx_mount_name"],
                efs_filesystem_id=config["efs_filesystem_id"],
            )

        # The summary should reference both tracking arrays
        has_summary = (
            "SUCCEEDED_SECTIONS" in script
            and "FAILED_SECTIONS" in script
            # Summary should print/echo results
            and re.search(r"(echo|printf).*[Ss]ummary", script) is not None
        )

        assert has_summary, (
            "Script must contain a summary section at the end that lists "
            "which sections succeeded and which failed, enabling diagnosis "
            "from CloudWatch logs or EC2 console output."
        )
