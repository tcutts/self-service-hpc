"""Unit tests for _validate_template_fields() scheduler version validation.

**Validates: Requirements 2.6**

Tests that _validate_template_fields() correctly validates the schedulerVersion
field within the software_stack parameter, accepting supported versions and
rejecting unsupported ones.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — same pattern as test_bug_condition_slurm_version.py
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_TEMPLATE_MGMT_DIR = os.path.join(_LAMBDA_DIR, "template_management")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

for _d in [_SHARED_DIR, _TEMPLATE_MGMT_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from templates import _validate_template_fields

# Get ValidationError from the same module that templates.py actually uses.
# Due to sys.modules caching across test files, the bare `errors` module may
# resolve to cluster_operations/errors.py instead of template_management/errors.py.
# We import it from templates' own globals to guarantee class identity match.
import templates as _templates_mod
ValidationError = _templates_mod.ValidationError

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
