"""Unit tests for lambda/shared/pcs_versions.py constants.

**Validates: Requirements 2.7**
"""

import os
import sys

# ---------------------------------------------------------------------------
# Path setup — same pattern as test_bug_condition_slurm_version.py
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pcs_versions import DEFAULT_SLURM_VERSION, SUPPORTED_SLURM_VERSIONS


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
