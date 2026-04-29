"""Unit tests for validate_ami_available() and AMI validation in _validate_template_fields().

**Validates: Requirements 2.8, 3.10**

Tests cover:
- validate_ami_available() with valid AMI (state=available) succeeds
- validate_ami_available() with non-existent AMI raises ValidationError
- validate_ami_available() with unavailable AMI (state=deregistered) raises ValidationError
- validate_ami_available() handles ClientError (InvalidAMIID.Malformed, InvalidAMIID.NotFound)
- _validate_template_fields() calls AMI validation for ami_id
- _validate_template_fields() calls AMI validation for login_ami_id when provided
- _validate_template_fields() skips login_ami_id validation when empty
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_TEMPLATE_MGMT_DIR = os.path.join(_LAMBDA_DIR, "template_management")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

for _d in [_SHARED_DIR, _TEMPLATE_MGMT_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from botocore.exceptions import ClientError

from errors import ValidationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_TEMPLATE_FIELDS = {
    "template_id": "test-tpl",
    "template_name": "Test Template",
    "instance_types": ["c7g.medium"],
    "login_instance_type": "c7g.medium",
    "min_nodes": 1,
    "max_nodes": 10,
    "ami_id": "ami-valid123",
    "software_stack": {"schedulerVersion": "24.11"},
}


def _make_client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "DescribeImages",
    )


# ---------------------------------------------------------------------------
# validate_ami_available() tests
# ---------------------------------------------------------------------------

class TestValidateAmiAvailable:
    """Tests for the validate_ami_available() module-level function."""

    def test_valid_ami_available(self):
        """AMI exists and state is 'available' — no error raised."""
        from templates import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"ImageId": "ami-valid123", "State": "available"}],
        }

        with patch("templates.ec2_client", mock_ec2):
            validate_ami_available("ami-valid123")

        mock_ec2.describe_images.assert_called_once_with(ImageIds=["ami-valid123"])

    def test_no_images_returned(self):
        """DescribeImages returns empty list — raises ValidationError."""
        from templates import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {"Images": []}

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not found"):
                validate_ami_available("ami-doesnotexist")

    def test_ami_not_available_state(self):
        """AMI exists but state is 'deregistered' — raises ValidationError."""
        from templates import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"ImageId": "ami-old123", "State": "deregistered"}],
        }

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not available"):
                validate_ami_available("ami-old123")

    def test_client_error_invalid_ami_id_malformed(self):
        """EC2 raises InvalidAMIID.Malformed — raises ValidationError."""
        from templates import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.side_effect = _make_client_error(
            "InvalidAMIID.Malformed"
        )

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not valid"):
                validate_ami_available("not-an-ami")

    def test_client_error_invalid_ami_id_not_found(self):
        """EC2 raises InvalidAMIID.NotFound — raises ValidationError."""
        from templates import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.side_effect = _make_client_error(
            "InvalidAMIID.NotFound"
        )

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not valid"):
                validate_ami_available("ami-nonexistent")

    def test_ami_pending_state(self):
        """AMI exists but state is 'pending' — raises ValidationError."""
        from templates import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"ImageId": "ami-pending", "State": "pending"}],
        }

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not available"):
                validate_ami_available("ami-pending")


# ---------------------------------------------------------------------------
# _validate_template_fields() AMI validation integration
# ---------------------------------------------------------------------------

class TestValidateTemplateFieldsAmi:
    """Tests that _validate_template_fields() calls AMI validation."""

    def _mock_ec2_available(self, ami_id: str) -> MagicMock:
        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"ImageId": ami_id, "State": "available"}],
        }
        return mock_ec2

    def test_validates_ami_id(self):
        """_validate_template_fields() calls validate_ami_available for ami_id."""
        from templates import _validate_template_fields

        mock_ec2 = self._mock_ec2_available("ami-valid123")

        with patch("templates.ec2_client", mock_ec2):
            _validate_template_fields(**VALID_TEMPLATE_FIELDS)

        mock_ec2.describe_images.assert_called_with(ImageIds=["ami-valid123"])

    def test_rejects_invalid_ami_id(self):
        """_validate_template_fields() raises ValidationError for invalid ami_id."""
        from templates import _validate_template_fields

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {"Images": []}

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError):
                _validate_template_fields(**VALID_TEMPLATE_FIELDS | {"ami_id": "ami-bad"})

    def test_validates_login_ami_id_when_provided(self):
        """_validate_template_fields() validates login_ami_id when non-empty."""
        from templates import _validate_template_fields

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"ImageId": "ami-any", "State": "available"}],
        }

        with patch("templates.ec2_client", mock_ec2):
            _validate_template_fields(
                **VALID_TEMPLATE_FIELDS,
                login_ami_id="ami-login456",
            )

        # Should have been called for both ami_id and login_ami_id
        assert mock_ec2.describe_images.call_count == 2
        calls = [c.kwargs["ImageIds"][0] for c in mock_ec2.describe_images.call_args_list]
        assert "ami-valid123" in calls
        assert "ami-login456" in calls

    def test_skips_login_ami_id_when_empty(self):
        """_validate_template_fields() skips login_ami_id validation when empty."""
        from templates import _validate_template_fields

        mock_ec2 = self._mock_ec2_available("ami-valid123")

        with patch("templates.ec2_client", mock_ec2):
            _validate_template_fields(**VALID_TEMPLATE_FIELDS, login_ami_id="")

        # Only called once for ami_id
        assert mock_ec2.describe_images.call_count == 1

    def test_rejects_invalid_login_ami_id(self):
        """_validate_template_fields() raises ValidationError for invalid login_ami_id."""
        from templates import _validate_template_fields

        mock_ec2 = MagicMock()
        # First call (ami_id) succeeds, second call (login_ami_id) fails
        mock_ec2.describe_images.side_effect = [
            {"Images": [{"ImageId": "ami-valid123", "State": "available"}]},
            {"Images": []},
        ]

        with patch("templates.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not found"):
                _validate_template_fields(
                    **VALID_TEMPLATE_FIELDS,
                    login_ami_id="ami-badlogin",
                )
