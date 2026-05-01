"""Bug condition exploration test — sys.modules cross-contamination.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

Property 1: Bug Condition — Module Cross-Contamination Under Multi-File Collection

For any pair of test files that import identically-named modules from different
Lambda packages, running them together in a single pytest subprocess SHALL
produce exit code 0 (all tests pass).

On UNFIXED code this test is EXPECTED TO FAIL, confirming the bug exists.
The bug condition is:
    isBugCondition(input) where
        input.testFiles.count > 1
        AND EXISTS file_a, file_b importing same module name from different packages
        AND file_a collected before file_b
"""

import os
import subprocess
import sys

from hypothesis import given, settings, HealthCheck, Phase
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Known-conflicting test file pairs.
#
# Each tuple is (file_a, file_b) where both import an identically-named
# module (e.g. "errors", "handler", "templates") from different Lambda
# packages.  Running them together in one pytest process triggers
# sys.modules cache collisions on unfixed code.
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.join(os.path.dirname(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)

CONFLICTING_PAIRS = [
    # cluster_operations/errors vs project_management/errors
    ("test_pcs_sizing.py", "test_sfn_project_consolidation_properties.py"),
    # cluster_operations/handler vs template_management/templates
    ("test_connection_info_properties.py", "test_validate_ami_available.py"),
    # cluster_operations/errors vs template_management/errors
    ("test_pcs_sizing.py", "test_validate_ami_available.py"),
    # cluster_operations/errors vs template_management/errors (different pair)
    ("test_cluster_destruction_properties.py", "test_bug_condition_launch_template.py"),
]


def _run_pytest_pair(file_a: str, file_b: str) -> subprocess.CompletedProcess:
    """Run two test files together in a single pytest subprocess."""
    path_a = os.path.join(_TESTS_DIR, file_a)
    path_b = os.path.join(_TESTS_DIR, file_b)
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            path_a, path_b,
            "-v", "--tb=short", "--no-header", "-q",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_PROJECT_ROOT,
    )
    return result


@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    phases=[Phase.generate, Phase.target],
)
@given(pair=st.sampled_from(CONFLICTING_PAIRS))
def test_conflicting_pairs_pass_together(pair):
    """Bug condition: conflicting file pairs must all pass when run together.

    On unfixed code this FAILS — pytest returns non-zero for at least one
    pair, proving sys.modules cross-contamination exists.
    """
    file_a, file_b = pair
    result = _run_pytest_pair(file_a, file_b)
    assert result.returncode == 0, (
        f"Pair ({file_a}, {file_b}) failed with exit code "
        f"{result.returncode}.\n"
        f"--- stdout ---\n{result.stdout[-2000:]}\n"
        f"--- stderr ---\n{result.stderr[-2000:]}"
    )


@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    phases=[Phase.generate, Phase.target],
)
@given(pair=st.sampled_from(CONFLICTING_PAIRS))
def test_conflicting_pairs_reversed_pass_together(pair):
    """Bug condition: reversed ordering must also pass.

    Tests that the bug is order-dependent — swapping collection order
    may trigger different contamination paths.
    """
    file_a, file_b = pair
    # Run in reversed order
    result = _run_pytest_pair(file_b, file_a)
    assert result.returncode == 0, (
        f"Reversed pair ({file_b}, {file_a}) failed with exit code "
        f"{result.returncode}.\n"
        f"--- stdout ---\n{result.stdout[-2000:]}\n"
        f"--- stderr ---\n{result.stderr[-2000:]}"
    )
