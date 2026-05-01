"""Unit tests for lambda/shared/pcs_versions.py constants.

**Validates: Requirements 2.7**
"""

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(os.path.dirname(__file__), "..", "conftest.py"))
_tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_tc)
load_lambda_module = _tc.load_lambda_module

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
pcs_versions = load_lambda_module("shared", "pcs_versions")
DEFAULT_SLURM_VERSION = pcs_versions.DEFAULT_SLURM_VERSION
SUPPORTED_SLURM_VERSIONS = pcs_versions.SUPPORTED_SLURM_VERSIONS


class TestSupportedSlurmVersions:
    """SUPPORTED_SLURM_VERSIONS contains the expected version-to-OS mappings."""

    def test_contains_expected_mappings(self):
        expected = {"24.11": "amzn2", "25.05": "amzn2", "25.11": "al2023"}
        assert SUPPORTED_SLURM_VERSIONS == expected

    def test_no_extra_versions(self):
        assert set(SUPPORTED_SLURM_VERSIONS.keys()) == {"24.11", "25.05", "25.11"}


class TestDefaultSlurmVersion:
    """DEFAULT_SLURM_VERSION is set to the latest supported version."""

    def test_default_is_25_11(self):
        assert DEFAULT_SLURM_VERSION == "25.11"

    def test_default_is_in_supported(self):
        assert DEFAULT_SLURM_VERSION in SUPPORTED_SLURM_VERSIONS
