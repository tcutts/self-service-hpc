"""Unit tests for validate_ami_available() in cluster_creation.py and
AMI validation in create_launch_templates().

**Validates: Requirements 2.9**

Tests cover:
- validate_ami_available() in cluster_creation module with valid AMI succeeds
- validate_ami_available() in cluster_creation module with non-existent AMI raises ValidationError
- validate_ami_available() in cluster_creation module with unavailable AMI raises ValidationError
- validate_ami_available() in cluster_creation module handles ClientError
- create_launch_templates() calls validate_ami_available() for ami_id before creating templates
- create_launch_templates() calls validate_ami_available() for login_ami_id when different from ami_id
- create_launch_templates() skips duplicate validation when login_ami_id equals ami_id
"""

import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

for _d in [_SHARED_DIR, _CLUSTER_OPS_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)


from botocore.exceptions import ClientError

# Import ValidationError via cluster_creation to avoid class identity
# mismatches when other test files clear 'errors' from sys.modules.
import cluster_creation  # noqa: E402
ValidationError = cluster_creation.ValidationError


# ---------------------------------------------------------------------------
# validate_ami_available() tests — duplicated function in cluster_creation
# ---------------------------------------------------------------------------

class TestValidateAmiAvailableClusterCreation:
    """Tests for validate_ami_available() in cluster_creation module."""

    def test_valid_ami_available(self):
        """AMI exists and state is 'available' — no error raised."""
        from cluster_creation import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }

        with patch("cluster_creation.ec2_client", mock_ec2):
            validate_ami_available("ami-valid123")

        mock_ec2.describe_images.assert_called_once_with(ImageIds=["ami-valid123"])

    def test_no_images_returned(self):
        """DescribeImages returns empty list — raises ValidationError."""
        from cluster_creation import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {"Images": []}

        with patch("cluster_creation.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not found"):
                validate_ami_available("ami-doesnotexist")

    def test_ami_not_available_state(self):
        """AMI exists but state is 'deregistered' — raises ValidationError."""
        from cluster_creation import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "deregistered"}],
        }

        with patch("cluster_creation.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not available"):
                validate_ami_available("ami-old123")

    def test_client_error_invalid_ami_id(self):
        """EC2 raises ClientError — raises ValidationError."""
        from cluster_creation import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.side_effect = ClientError(
            {"Error": {"Code": "InvalidAMIID.Malformed", "Message": "bad"}},
            "DescribeImages",
        )

        with patch("cluster_creation.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not valid"):
                validate_ami_available("not-an-ami")

    def test_ami_pending_state(self):
        """AMI exists but state is 'pending' — raises ValidationError."""
        from cluster_creation import validate_ami_available

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "pending"}],
        }

        with patch("cluster_creation.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not available"):
                validate_ami_available("ami-pending")


# ---------------------------------------------------------------------------
# create_launch_templates() AMI validation integration tests
# ---------------------------------------------------------------------------

class TestCreateLaunchTemplatesAmiValidation:
    """Tests that create_launch_templates() validates AMIs before creating templates."""

    def _make_event(self, ami_id="ami-compute123", login_ami_id=""):
        """Build a minimal valid event for create_launch_templates()."""
        event = {
            "projectId": "test-project",
            "clusterName": "test-cluster",
            "securityGroupIds": {
                "headNode": "sg-head123",
                "computeNode": "sg-compute456",
                "efs": "sg-efs789",
                "fsx": "sg-fsx012",
            },
            "amiId": ami_id,
            "efsFileSystemId": "",
            "storageMode": "",
            "s3BucketName": "",
            "fsxDnsName": "",
            "fsxMountName": "",
        }
        if login_ami_id:
            event["loginAmiId"] = login_ami_id
        return event

    def test_validates_ami_id_before_creating_templates(self):
        """create_launch_templates() rejects invalid ami_id before any template is created."""
        from cluster_creation import create_launch_templates

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {"Images": []}

        with patch("cluster_creation.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not found"):
                create_launch_templates(self._make_event(ami_id="ami-invalid"))

        # No launch template should have been created
        mock_ec2.create_launch_template.assert_not_called()

    def test_validates_login_ami_id_when_different(self):
        """create_launch_templates() validates login_ami_id when it differs from ami_id."""
        from cluster_creation import create_launch_templates

        mock_ec2 = MagicMock()

        def describe_images_side_effect(ImageIds):
            ami = ImageIds[0]
            if ami == "ami-compute123":
                return {"Images": [{"State": "available"}]}
            elif ami == "ami-loginbad":
                return {"Images": []}
            return {"Images": []}

        mock_ec2.describe_images.side_effect = describe_images_side_effect

        event = self._make_event(ami_id="ami-compute123", login_ami_id="ami-loginbad")

        with patch("cluster_creation.ec2_client", mock_ec2):
            with pytest.raises(ValidationError, match="not found"):
                create_launch_templates(event)

        # No launch template should have been created
        mock_ec2.create_launch_template.assert_not_called()

    def test_skips_duplicate_validation_when_same_ami(self):
        """When login_ami_id equals ami_id, validate_ami_available is called only once."""
        from cluster_creation import create_launch_templates

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }
        mock_ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-mock123"},
        }

        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": None}
        mock_dynamodb.Table.return_value = mock_table

        # login_ami_id not set, so it defaults to ami_id
        event = self._make_event(ami_id="ami-same123")

        with (
            patch("cluster_creation.ec2_client", mock_ec2),
            patch("cluster_creation.dynamodb", mock_dynamodb),
        ):
            create_launch_templates(event)

        # describe_images should be called exactly once (not twice for the same AMI)
        mock_ec2.describe_images.assert_called_once_with(ImageIds=["ami-same123"])

    def test_valid_amis_proceed_to_template_creation(self):
        """When both AMIs are valid, launch templates are created successfully."""
        from cluster_creation import create_launch_templates

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }
        mock_ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-mock123"},
        }

        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": None}
        mock_dynamodb.Table.return_value = mock_table

        event = self._make_event(
            ami_id="ami-compute123",
            login_ami_id="ami-login456",
        )

        with (
            patch("cluster_creation.ec2_client", mock_ec2),
            patch("cluster_creation.dynamodb", mock_dynamodb),
        ):
            result = create_launch_templates(event)

        # Both AMIs validated
        assert mock_ec2.describe_images.call_count == 2
        # Both templates created
        assert mock_ec2.create_launch_template.call_count == 2
        assert "loginLaunchTemplateId" in result
        assert "computeLaunchTemplateId" in result
