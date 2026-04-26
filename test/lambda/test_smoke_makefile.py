"""Smoke tests for Makefile targets.

Verifies that the Makefile at the repository root contains the expected
targets and that key targets can be parsed without syntax errors using
``make --dry-run`` (``make -n``).

Requirements: 20.1, 20.2, 20.3
"""

import os
import re
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MAKEFILE_PATH = os.path.join(_REPO_ROOT, "Makefile")


def _read_makefile() -> str:
    """Return the raw text of the Makefile."""
    with open(_MAKEFILE_PATH, "r") as fh:
        return fh.read()


def _extract_targets(makefile_text: str) -> set[str]:
    """Parse explicit target names from a Makefile.

    Matches lines like ``target_name:`` or ``.PHONY: target_name`` and
    returns the set of all discovered target names.
    """
    targets: set[str] = set()

    # Match ".PHONY: target" declarations
    for m in re.finditer(r"^\.PHONY:\s+(\S+)", makefile_text, re.MULTILINE):
        targets.add(m.group(1))

    # Match "target: [deps]" rule lines (skip variable assignments with :=)
    for m in re.finditer(r"^([a-zA-Z_][a-zA-Z0-9_-]*):\s", makefile_text, re.MULTILINE):
        targets.add(m.group(1))

    return targets


# ---------------------------------------------------------------------------
# Tests — target existence (parse the Makefile)
# ---------------------------------------------------------------------------

class TestMakefileTargetsExist:
    """Verify the Makefile declares the required targets."""

    @pytest.fixture(scope="class")
    def targets(self) -> set[str]:
        return _extract_targets(_read_makefile())

    @pytest.mark.parametrize("target", ["deploy", "teardown", "purge"])
    def test_primary_targets_present(self, targets: set[str], target: str):
        """Primary lifecycle targets (deploy, teardown, purge) must exist.

        Validates: Requirements 20.1, 20.2, 20.3
        """
        assert target in targets, f"Makefile is missing the '{target}' target"

    @pytest.mark.parametrize("target", ["build", "test", "synth"])
    def test_helper_targets_present(self, targets: set[str], target: str):
        """Helper targets (build, test, synth) must exist."""
        assert target in targets, f"Makefile is missing the '{target}' target"


# ---------------------------------------------------------------------------
# Tests — dry-run syntax validation
# ---------------------------------------------------------------------------

class TestMakefileDryRun:
    """Verify key targets parse without syntax errors via ``make -n``."""

    @pytest.mark.parametrize("target", ["deploy", "teardown", "purge", "build", "test", "synth"])
    def test_dry_run_no_syntax_errors(self, target: str):
        """``make -n <target>`` should exit 0, proving the Makefile parses correctly.

        Validates: Requirements 20.1, 20.2, 20.3
        """
        result = subprocess.run(
            ["make", "-n", target],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"'make -n {target}' failed (rc={result.returncode}).\n"
            f"stderr: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )


# ---------------------------------------------------------------------------
# Tests — target dependency structure
# ---------------------------------------------------------------------------

class TestMakefileTargetDependencies:
    """Verify key dependency relationships in the Makefile."""

    @pytest.fixture(scope="class")
    def makefile_text(self) -> str:
        return _read_makefile()

    def test_purge_depends_on_teardown(self, makefile_text: str):
        """The purge target should depend on teardown.

        Validates: Requirements 20.3
        """
        # Match "purge: teardown" or "purge: teardown other_dep"
        assert re.search(
            r"^purge:.*\bteardown\b", makefile_text, re.MULTILINE
        ), "purge target should depend on teardown"

    def test_deploy_depends_on_build(self, makefile_text: str):
        """The deploy target should depend on build.

        Validates: Requirements 20.1
        """
        assert re.search(
            r"^deploy:.*\bbuild\b", makefile_text, re.MULTILINE
        ), "deploy target should depend on build"
