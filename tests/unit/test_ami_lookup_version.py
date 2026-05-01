"""Unit tests for get_latest_pcs_ami() version parameter handling.

**Validates: Requirements 2.3, 2.4**

Tests that get_latest_pcs_ami() correctly maps each supported Slurm version
to its OS prefix in the AMI name filter, rejects unsupported versions,
and defaults to DEFAULT_SLURM_VERSION.
"""

from unittest.mock import MagicMock, patch

import pytest

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(os.path.dirname(__file__), "..", "conftest.py"))
_tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_tc)
load_lambda_module = _tc.load_lambda_module
_ensure_shared_modules = _tc._ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
errors = load_lambda_module("template_management", "errors")
ValidationError = errors.ValidationError

ami_lookup = load_lambda_module("template_management", "ami_lookup")
get_latest_pcs_ami = ami_lookup.get_latest_pcs_ami

pcs_versions = load_lambda_module("shared", "pcs_versions")
DEFAULT_SLURM_VERSION = pcs_versions.DEFAULT_SLURM_VERSION


def _make_ec2_mock(os_prefix: str, arch: str, version: str) -> MagicMock:
    """Return a mock EC2 client that returns one matching AMI."""
    mock = MagicMock()
    mock.describe_images.return_value = {
        "Images": [
            {
                "ImageId": "ami-abc123",
                "Name": f"aws-pcs-sample_ami-{os_prefix}-{arch}-slurm-{version}-20250601",
                "Architecture": arch,
                "CreationDate": "2025-06-01T00:00:00Z",
            },
        ],
    }
    return mock


class TestGetLatestPcsAmiVersionParameter:
    """get_latest_pcs_ami() uses the correct OS prefix per Slurm version."""

    def test_version_24_11_uses_amzn2(self):
        """slurm_version='24.11' should filter with OS prefix 'amzn2'."""
        mock_ec2 = _make_ec2_mock("amzn2", "x86_64", "24.11")

        with patch("ami_lookup.ec2_client", mock_ec2):
            get_latest_pcs_ami(arch="x86_64", slurm_version="24.11")

        filters = mock_ec2.describe_images.call_args.kwargs["Filters"]
        name_pattern = [f for f in filters if f["Name"] == "name"][0]["Values"][0]
        assert "amzn2" in name_pattern
        assert "24.11" in name_pattern

    def test_version_25_05_uses_amzn2(self):
        """slurm_version='25.05' should filter with OS prefix 'amzn2'."""
        mock_ec2 = _make_ec2_mock("amzn2", "x86_64", "25.05")

        with patch("ami_lookup.ec2_client", mock_ec2):
            get_latest_pcs_ami(arch="x86_64", slurm_version="25.05")

        filters = mock_ec2.describe_images.call_args.kwargs["Filters"]
        name_pattern = [f for f in filters if f["Name"] == "name"][0]["Values"][0]
        assert "amzn2" in name_pattern
        assert "25.05" in name_pattern

    def test_version_25_11_uses_al2023(self):
        """slurm_version='25.11' should filter with OS prefix 'al2023'."""
        mock_ec2 = _make_ec2_mock("al2023", "x86_64", "25.11")

        with patch("ami_lookup.ec2_client", mock_ec2):
            get_latest_pcs_ami(arch="x86_64", slurm_version="25.11")

        filters = mock_ec2.describe_images.call_args.kwargs["Filters"]
        name_pattern = [f for f in filters if f["Name"] == "name"][0]["Values"][0]
        assert "al2023" in name_pattern
        assert "25.11" in name_pattern

    def test_unsupported_version_raises_validation_error(self):
        """slurm_version='99.99' should raise ValidationError."""
        with pytest.raises(ValidationError):
            get_latest_pcs_ami(arch="x86_64", slurm_version="99.99")

    def test_default_slurm_version_is_used(self):
        """Omitting slurm_version should default to DEFAULT_SLURM_VERSION."""
        mock_ec2 = _make_ec2_mock("al2023", "x86_64", DEFAULT_SLURM_VERSION)

        with patch("ami_lookup.ec2_client", mock_ec2):
            get_latest_pcs_ami(arch="x86_64")

        filters = mock_ec2.describe_images.call_args.kwargs["Filters"]
        name_pattern = [f for f in filters if f["Name"] == "name"][0]["Values"][0]
        assert DEFAULT_SLURM_VERSION in name_pattern
