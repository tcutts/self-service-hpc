"""Unit tests for _validate_template_fields() scheduler version validation.

**Validates: Requirements 2.6**

Tests that _validate_template_fields() correctly validates the schedulerVersion
field within the software_stack parameter, accepting supported versions and
rejecting unsupported ones.
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
load_lambda_module("template_management", "errors")
templates = load_lambda_module("template_management", "templates")
_validate_template_fields = templates._validate_template_fields
ValidationError = templates.ValidationError

# Common valid arguments shared across all tests
_VALID_KWARGS = {
    "template_id": "test-tpl",
    "template_name": "Test Template",
    "instance_types": ["c7g.medium"],
    "login_instance_type": "c7g.medium",
    "min_nodes": 1,
    "max_nodes": 10,
    "ami_id": "ami-12345678",
}


class TestValidateTemplateFieldsSchedulerVersion:
    """Scheduler version validation in _validate_template_fields()."""

    @pytest.fixture(autouse=True)
    def _mock_ami_validation(self):
        """Mock AMI validation so these tests focus on scheduler version logic."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }
        with patch("templates.ec2_client", mock_ec2):
            yield

    def test_accepts_version_25_11(self):
        """Accepts software_stack with schedulerVersion '25.11'."""
        _validate_template_fields(
            **_VALID_KWARGS,
            software_stack={"schedulerVersion": "25.11"},
        )

    def test_accepts_version_24_11(self):
        """Accepts software_stack with schedulerVersion '24.11'."""
        _validate_template_fields(
            **_VALID_KWARGS,
            software_stack={"schedulerVersion": "24.11"},
        )

    def test_accepts_version_25_05(self):
        """Accepts software_stack with schedulerVersion '25.05'."""
        _validate_template_fields(
            **_VALID_KWARGS,
            software_stack={"schedulerVersion": "25.05"},
        )

    def test_rejects_unsupported_version(self):
        """Rejects software_stack with schedulerVersion '99.99'."""
        with pytest.raises(ValidationError, match="99.99"):
            _validate_template_fields(
                **_VALID_KWARGS,
                software_stack={"schedulerVersion": "99.99"},
            )

    def test_rejects_empty_string_version(self):
        """Rejects software_stack with schedulerVersion ''."""
        with pytest.raises(ValidationError):
            _validate_template_fields(
                **_VALID_KWARGS,
                software_stack={"schedulerVersion": ""},
            )

    def test_accepts_empty_software_stack(self):
        """Accepts software_stack={} — schedulerVersion is optional."""
        _validate_template_fields(
            **_VALID_KWARGS,
            software_stack={},
        )

    def test_accepts_none_software_stack(self):
        """Accepts software_stack=None for backward compatibility."""
        _validate_template_fields(
            **_VALID_KWARGS,
            software_stack=None,
        )
