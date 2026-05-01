"""Preservation property tests — individual test file behavior unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

Property 2: Preservation — Individual Test File Behavior Unchanged

For any test file in ``tests/`` that passes when run in isolation on UNFIXED
code, the file SHALL continue to pass when run in isolation.  This establishes
the baseline behavior that the fix must preserve.

For any test file in ``test/lambda/``, running it individually SHALL produce
exit code 0, confirming the existing ``test/lambda/conftest.py`` isolation
infrastructure is unaffected.

On UNFIXED code these tests are EXPECTED TO PASS — each file passes
individually, confirming the baseline behavior we need to preserve.
"""

import os
import subprocess
import sys

from hypothesis import given, settings, HealthCheck, Phase
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.join(os.path.dirname(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)

# ---------------------------------------------------------------------------
# Test files in tests/ that pass individually on UNFIXED code.
#
# Excludes:
# - test_bug_condition_module_isolation.py: designed to FAIL on unfixed code
#   (it runs file *pairs*, not individual files)
# - test_preservation_module_isolation.py: this file itself
# - test_sfn_error_propagation_properties.py: pre-existing failure unrelated
#   to module isolation (moto does not support DescribeDeliveries, causing
#   UnrecognizedClientException in cleanup_scheduler_log_delivery)
# ---------------------------------------------------------------------------
TESTS_DIR_FILES = [
    "tests/test_ami_validation_cluster_creation.py",
    "tests/test_bug_condition_cluster_destruction_hangs.py",
    "tests/test_bug_condition_launch_template.py",
    "tests/test_bug_condition_userdata_crash.py",
    "tests/test_cluster_destruction_properties.py",
    "tests/test_connection_info_properties.py",
    "tests/test_deletion_progress_properties.py",
    "tests/test_deregister_cluster_name_properties.py",
    "tests/test_login_node_event_properties.py",
    "tests/test_notification_properties.py",
    "tests/test_pcs_sizing.py",
    "tests/test_pcs_sizing_integration.py",
    "tests/test_pcs_sizing_properties.py",
    "tests/test_posix_username_validation.py",
    "tests/test_posix_username_validation_properties.py",
    "tests/test_preservation_cluster_destruction_hangs.py",
    "tests/test_preservation_launch_template.py",
    "tests/test_preservation_userdata_crash.py",
    "tests/test_scheduler_log_delivery_properties.py",
    "tests/test_sfn_consolidation_properties.py",
    "tests/test_sfn_destruction_consolidation_properties.py",
    "tests/test_sfn_project_consolidation_properties.py",
    "tests/test_validate_ami_available.py",
    "tests/unit/test_ami_lookup_version.py",
    "tests/unit/test_authorization.py",
    "tests/unit/test_bug_condition_slurm_version.py",
    "tests/unit/test_cleanup_scheduler_log_delivery.py",
    "tests/unit/test_cloudwatch_agent_commands.py",
    "tests/unit/test_cluster_destruction.py",
    "tests/unit/test_configure_scheduler_log_delivery.py",
    "tests/unit/test_create_pcs_cluster_version.py",
    "tests/unit/test_deregister_cluster_name.py",
    "tests/unit/test_get_cluster_connection_info.py",
    "tests/unit/test_handle_default_ami_version.py",
    "tests/unit/test_login_node_event.py",
    "tests/unit/test_login_node_refresh.py",
    "tests/unit/test_pcs_versions.py",
    "tests/unit/test_posix_provisioning_validation.py",
    "tests/unit/test_preservation_properties.py",
    "tests/unit/test_record_cluster_connection.py",
    "tests/unit/test_resolve_login_node_details.py",
    "tests/unit/test_resolve_template_version.py",
    "tests/unit/test_user_creation_validation.py",
    "tests/unit/test_validate_template_version.py",
    "tests/integration/test_destruction_workflow.py",
]

# ---------------------------------------------------------------------------
# Representative test files in test/lambda/ that confirm the existing
# conftest.py isolation infrastructure is unaffected.
#
# Includes a mix of unit tests, smoke tests, and property tests to cover
# the breadth of test/lambda/ without excessive runtime.  Excludes the
# known-failing test_storage_config_properties.py (pre-existing issue
# unrelated to module isolation).
# ---------------------------------------------------------------------------
TEST_LAMBDA_FILES = [
    "test/lambda/test_smoke_docs.py",
    "test/lambda/test_smoke_makefile.py",
    "test/lambda/test_unit_api_logging.py",
    "test/lambda/test_unit_authorization_enforcement.py",
    "test/lambda/test_foundation_timestamp.py",
    "test/lambda/test_batch_error_isolation.py",
    "test/lambda/test_batch_response_format.py",
    "test/lambda/test_unit_fsx_cleanup.py",
    "test/lambda/test_unit_teardown.py",
]


def _run_single_test_file(filepath: str) -> subprocess.CompletedProcess:
    """Run a single test file in its own pytest subprocess."""
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            filepath,
            "-v", "--tb=short", "--no-header", "-q",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_PROJECT_ROOT,
    )
    return result


# ---------------------------------------------------------------------------
# Property: every test file in tests/ passes when run individually
# ---------------------------------------------------------------------------
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    phases=[Phase.generate, Phase.target],
)
@given(test_file=st.sampled_from(TESTS_DIR_FILES))
def test_individual_tests_dir_files_pass(test_file):
    """Preservation: each test file in tests/ passes when run in isolation.

    **Validates: Requirements 3.2, 3.4, 3.5**

    For all test files in tests/ (sampled via st.sampled_from), running the
    file individually via subprocess produces exit code 0.
    """
    result = _run_single_test_file(test_file)
    assert result.returncode == 0, (
        f"{test_file} failed individually with exit code "
        f"{result.returncode}.\n"
        f"--- stdout (last 2000 chars) ---\n"
        f"{result.stdout[-2000:]}\n"
        f"--- stderr (last 1000 chars) ---\n"
        f"{result.stderr[-1000:]}"
    )


# ---------------------------------------------------------------------------
# Property: representative test/lambda/ files pass individually
# ---------------------------------------------------------------------------
@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    phases=[Phase.generate, Phase.target],
)
@given(test_file=st.sampled_from(TEST_LAMBDA_FILES))
def test_individual_test_lambda_files_pass(test_file):
    """Preservation: test/lambda/ files pass, confirming conftest.py is unaffected.

    **Validates: Requirements 3.1, 3.3**

    For all test files in test/lambda/ (sampled via st.sampled_from), running
    them individually produces exit code 0.  This confirms the existing
    test/lambda/conftest.py isolation infrastructure remains functional.
    """
    result = _run_single_test_file(test_file)
    assert result.returncode == 0, (
        f"{test_file} failed individually with exit code "
        f"{result.returncode}.\n"
        f"--- stdout (last 2000 chars) ---\n"
        f"{result.stdout[-2000:]}\n"
        f"--- stderr (last 1000 chars) ---\n"
        f"{result.stderr[-1000:]}"
    )
