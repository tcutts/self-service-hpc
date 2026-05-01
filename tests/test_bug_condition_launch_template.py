"""Bug condition exploration test — Launch Template UserData, EFS Mount, AMI Validation.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8**

This test encodes the EXPECTED (correct) behavior. It is designed to FAIL
on unfixed code, proving the three bugs exist. After the fix is applied,
the same test should PASS, confirming the bugs are resolved.

Bug conditions:
- Defect 1: create_launch_templates() creates LaunchTemplateData with no UserData field
- Defect 2: generate_user_data_script() has no efs_filesystem_id parameter
- Defect 3: _validate_template_fields() does not validate AMI via EC2 DescribeImages
"""

import base64
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "posix_provisioning")
load_lambda_module("cluster_operations", "tagging")
load_lambda_module("cluster_operations", "cluster_creation")
load_lambda_module("template_management", "errors")
load_lambda_module("template_management", "templates")


class TestDefect1LaunchTemplateMissingUserData:
    """create_launch_templates() should include UserData in LaunchTemplateData.

    On UNFIXED code, LaunchTemplateData contains only SecurityGroupIds and
    ImageId — no UserData field. This test asserts UserData is present and
    contains a non-empty base64-encoded string.
    """

    def test_launch_template_contains_userdata(self):
        """create_launch_templates() must include a UserData field in
        LaunchTemplateData with a non-empty base64-encoded provisioning script.

        **Validates: Requirements 1.1, 1.2, 1.5**

        On UNFIXED code this FAILS because create_launch_templates() builds
        lt_data with only SecurityGroupIds and ImageId — no UserData.
        """
        from cluster_creation import create_launch_templates

        event = {
            "projectId": "test-project",
            "clusterName": "test-cluster",
            "securityGroupIds": {
                "headNode": "sg-head123",
                "computeNode": "sg-compute456",
                "efs": "sg-efs789",
                "fsx": "sg-fsx012",
            },
            "amiId": "ami-test12345",
            "loginAmiId": "ami-login12345",
            "efsFileSystemId": "fs-abc123",
            "storageMode": "",
            "s3BucketName": "",
            "fsxDnsName": "",
            "fsxMountName": "",
        }

        mock_ec2 = MagicMock()
        mock_ec2.create_launch_template.return_value = {
            "LaunchTemplate": {"LaunchTemplateId": "lt-mock123"},
        }
        # Mock describe_images for AMI validation (added by Task 3.4 fix)
        mock_ec2.describe_images.return_value = {
            "Images": [{"State": "available"}],
        }

        # Mock DynamoDB for generate_user_data_script (project members lookup)
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": None}
        mock_dynamodb.Table.return_value = mock_table

        with (
            patch("cluster_creation.ec2_client", mock_ec2),
            patch("cluster_creation.dynamodb", mock_dynamodb),
        ):
            create_launch_templates(event)

        # Inspect the LaunchTemplateData passed to ec2_client.create_launch_template
        assert mock_ec2.create_launch_template.call_count >= 1, (
            "ec2_client.create_launch_template was never called"
        )

        for call in mock_ec2.create_launch_template.call_args_list:
            lt_data = call.kwargs.get("LaunchTemplateData", {})
            assert "UserData" in lt_data, (
                f"LaunchTemplateData has no 'UserData' key. "
                f"Keys present: {list(lt_data.keys())}. "
                f"create_launch_templates() creates templates without user data."
            )
            user_data = lt_data["UserData"]
            assert user_data, "UserData field is empty"
            # Verify it's valid base64
            decoded = base64.b64decode(user_data).decode("utf-8")
            assert len(decoded) > 0, "Decoded UserData is empty"
            # Verify MIME multipart format (required by PCS)
            assert "Content-Type: multipart/mixed" in decoded, (
                "UserData is not in MIME multipart format. "
                "PCS requires user data in MIME multipart format."
            )
            assert "Content-Type: text/x-shellscript" in decoded, (
                "UserData does not contain a text/x-shellscript MIME part."
            )


class TestDefect2EfsMountMissing:
    """generate_user_data_script() should accept efs_filesystem_id and produce
    EFS mount commands.

    On UNFIXED code, the function does not accept efs_filesystem_id as a
    parameter — calling it with that keyword argument raises TypeError.
    """

    def test_generate_user_data_script_accepts_efs_filesystem_id(self):
        """generate_user_data_script() must accept efs_filesystem_id parameter
        and produce a script containing EFS mount commands.

        **Validates: Requirements 1.4, 1.6**

        On UNFIXED code this FAILS because generate_user_data_script() does
        not accept efs_filesystem_id — it raises TypeError (unexpected keyword
        argument).
        """
        from posix_provisioning import generate_user_data_script

        # Mock DynamoDB for project members lookup
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": None}
        mock_dynamodb.Table.return_value = mock_table

        with patch("posix_provisioning.dynamodb", mock_dynamodb):
            # This call should NOT raise TypeError
            script = generate_user_data_script(
                project_id="test-project",
                users_table_name="PlatformUsers",
                projects_table_name="Projects",
                efs_filesystem_id="fs-abc123",
            )

        # The script should contain EFS mount commands
        assert "mount -a -t efs" in script, (
            f"Script does not contain 'mount -a -t efs'. "
            f"generate_user_data_script() has no EFS mount logic."
        )
        assert "amazon-efs-utils" in script, (
            f"Script does not contain 'amazon-efs-utils'. "
            f"generate_user_data_script() does not install EFS utilities."
        )


class TestDefect3AmiNotValidated:
    """_validate_template_fields() should validate AMI via EC2 DescribeImages.

    On UNFIXED code, the function only checks that ami_id is a non-empty
    string — it never calls EC2 to verify the AMI exists and is available.
    """

    def test_validate_template_fields_rejects_nonexistent_ami(self):
        """_validate_template_fields() must reject an AMI that does not exist
        in EC2 by raising ValidationError.

        **Validates: Requirements 1.7, 1.8**

        On UNFIXED code this FAILS because _validate_template_fields() only
        checks that ami_id is a non-empty string — "ami-doesnotexist" passes
        validation without any EC2 API call.
        """
        from templates import _validate_template_fields
        import templates as _templates_mod

        ValidationError = _templates_mod.ValidationError

        # Mock EC2 client to return no images for the given AMI
        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {"Images": []}

        with patch("templates.ec2_client", mock_ec2, create=True):
            with pytest.raises(ValidationError):
                _validate_template_fields(
                    template_id="test-tpl",
                    template_name="Test Template",
                    instance_types=["c7g.medium"],
                    login_instance_type="c7g.medium",
                    min_nodes=1,
                    max_nodes=10,
                    ami_id="ami-doesnotexist",
                    software_stack={"schedulerVersion": "24.11"},
                )
