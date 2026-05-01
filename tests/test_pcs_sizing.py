"""Boundary value unit tests for determine_controller_size."""

import pytest

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
errors = load_lambda_module("cluster_operations", "errors")
pcs_sizing = load_lambda_module("cluster_operations", "pcs_sizing")

ValidationError = errors.ValidationError
determine_controller_size = pcs_sizing.determine_controller_size


class TestDetermineControllerSizeBoundaries:
    """Boundary value tests for PCS controller tier selection."""

    def test_small_upper_boundary(self):
        """maxNodes=31 → total_managed=32 → SMALL (upper boundary)."""
        assert determine_controller_size(31) == "SMALL"

    def test_medium_lower_boundary(self):
        """maxNodes=32 → total_managed=33 → MEDIUM (lower boundary)."""
        assert determine_controller_size(32) == "MEDIUM"

    def test_medium_upper_boundary(self):
        """maxNodes=511 → total_managed=512 → MEDIUM (upper boundary)."""
        assert determine_controller_size(511) == "MEDIUM"

    def test_large_lower_boundary(self):
        """maxNodes=512 → total_managed=513 → LARGE (lower boundary)."""
        assert determine_controller_size(512) == "LARGE"

    def test_large_upper_boundary(self):
        """maxNodes=2047 → total_managed=2048 → LARGE (upper boundary)."""
        assert determine_controller_size(2047) == "LARGE"

    def test_default_equivalent(self):
        """maxNodes=10 (default value) → SMALL."""
        assert determine_controller_size(10) == "SMALL"

    def test_minimum_valid(self):
        """maxNodes=1 → total_managed=2 → SMALL."""
        assert determine_controller_size(1) == "SMALL"


class TestDetermineControllerSizeErrors:
    """Error case tests for determine_controller_size."""

    def test_over_capacity(self):
        """maxNodes=2048 → total_managed=2049 → ValidationError."""
        with pytest.raises(ValidationError):
            determine_controller_size(2048)

    def test_non_positive(self):
        """maxNodes=0 → ValidationError."""
        with pytest.raises(ValidationError):
            determine_controller_size(0)

    def test_non_integer(self):
        """maxNodes="10" (string) → ValidationError."""
        with pytest.raises(ValidationError):
            determine_controller_size("10")
