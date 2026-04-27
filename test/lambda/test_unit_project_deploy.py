"""Unit tests for the project deploy step handlers.

Covers:
- validate_project_state succeeds for DEPLOYING project, fails for other statuses
- record_infrastructure writes correct fields and transitions to ACTIVE
- handle_deploy_failure transitions back to CREATED and stores error message
- Progress tracking updates DynamoDB with correct step numbers and descriptions

Requirements: 2.1, 2.2, 2.3, 2.5, 2.6

The deploy module calls CodeBuild and CloudFormation which are not fully
supported by moto, so we mock those clients at the module level while
using moto for DynamoDB.
"""

from unittest.mock import MagicMock

import pytest
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    create_projects_table,
    _load_module_from,
    _PROJECT_MGMT_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id, status="DEPLOYING", **overrides):
    """Insert a project record for deploy tests."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "vpcId": "",
        "efsFileSystemId": "",
        "s3BucketName": "",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "budgetType": "MONTHLY",
        "cdkStackName": "",
        "currentStep": 0,
        "totalSteps": 0,
        "stepDescription": "",
        "errorMessage": "",
        "createdAt": "2024-06-15T10:00:00+00:00",
        "updatedAt": "2024-06-15T10:00:00+00:00",
        "statusChangedAt": "2024-06-15T10:00:00+00:00",
    }
    item.update(overrides)
    projects_table.put_item(Item=item)


def _get_project(projects_table, project_id):
    """Retrieve a project record from DynamoDB."""
    response = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"}
    )
    return response.get("Item")


# ---------------------------------------------------------------------------
# Test class — Project Deploy Step Handlers
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestProjectDeployStepHandlers:
    """Validates: Requirements 2.1, 2.2, 2.3, 2.5, 2.6"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set up moto DynamoDB and reload deploy modules with mocked AWS clients."""
        with mock_aws():
            self.projects_table = create_projects_table()

            # Load dependency modules first
            _load_module_from(_PROJECT_MGMT_DIR, "errors")
            _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")

            # Load the deploy module
            self.deploy_mod = _load_module_from(_PROJECT_MGMT_DIR, "project_deploy")

            # Mock CodeBuild and CloudFormation clients (not supported by moto)
            self.mock_codebuild = MagicMock()
            self.mock_cfn = MagicMock()
            self.deploy_mod.codebuild_client = self.mock_codebuild
            self.deploy_mod.cfn_client = self.mock_cfn

            yield

    # -- validate_project_state: success ------------------------------------

    def test_validate_project_state_succeeds_for_deploying(self):
        """Validates: Requirement 2.1 — DEPLOYING project passes validation."""
        _seed_project(self.projects_table, "proj-deploy-ok", status="DEPLOYING")

        result = self.deploy_mod.validate_project_state({"projectId": "proj-deploy-ok"})

        assert result["projectId"] == "proj-deploy-ok"

    # -- validate_project_state: failure for non-DEPLOYING statuses ---------

    def test_validate_project_state_fails_for_created(self):
        """Validates: Requirement 2.1 — CREATED project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-created", status="CREATED")

        with pytest.raises(ValidationError) as exc_info:
            self.deploy_mod.validate_project_state({"projectId": "proj-created"})
        assert "CREATED" in str(exc_info.value)

    def test_validate_project_state_fails_for_active(self):
        """Validates: Requirement 2.1 — ACTIVE project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-active", status="ACTIVE")

        with pytest.raises(ValidationError) as exc_info:
            self.deploy_mod.validate_project_state({"projectId": "proj-active"})
        assert "ACTIVE" in str(exc_info.value)

    def test_validate_project_state_fails_for_destroying(self):
        """Validates: Requirement 2.1 — DESTROYING project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-destroying", status="DESTROYING")

        with pytest.raises(ValidationError) as exc_info:
            self.deploy_mod.validate_project_state({"projectId": "proj-destroying"})
        assert "DESTROYING" in str(exc_info.value)

    def test_validate_project_state_fails_for_archived(self):
        """Validates: Requirement 2.1 — ARCHIVED project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-archived", status="ARCHIVED")

        with pytest.raises(ValidationError) as exc_info:
            self.deploy_mod.validate_project_state({"projectId": "proj-archived"})
        assert "ARCHIVED" in str(exc_info.value)

    def test_validate_project_state_fails_for_nonexistent_project(self):
        """Validates: Requirement 2.1 — missing project raises an error.

        Note: _update_project_progress runs before the get_item check and
        creates a skeleton record via update_item (DynamoDB upsert), so the
        subsequent get_item finds a record with no status field.  The code
        raises ValidationError (status '' != 'DEPLOYING') rather than
        NotFoundError.
        """
        from errors import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            self.deploy_mod.validate_project_state({"projectId": "proj-ghost"})
        assert "proj-ghost" in str(exc_info.value)

    def test_validate_project_state_fails_for_missing_project_id(self):
        """Validates: Requirement 2.1 — missing projectId raises ValidationError."""
        from errors import ValidationError

        with pytest.raises(ValidationError):
            self.deploy_mod.validate_project_state({})

    # -- record_infrastructure: writes fields and transitions to ACTIVE -----

    def test_record_infrastructure_writes_fields_and_transitions_to_active(self):
        """Validates: Requirement 2.2 — infrastructure IDs stored, status → ACTIVE."""
        _seed_project(self.projects_table, "proj-record", status="DEPLOYING")

        event = {
            "projectId": "proj-record",
            "vpcId": "vpc-abc123",
            "efsFileSystemId": "fs-def456",
            "s3BucketName": "my-bucket-789",
            "cdkStackName": "HpcProject-proj-record",
            "publicSubnetIds": ["subnet-pub-1", "subnet-pub-2"],
            "privateSubnetIds": ["subnet-priv-1", "subnet-priv-2"],
            "securityGroupIds": {
                "headNode": "sg-head",
                "computeNode": "sg-compute",
                "efs": "sg-efs",
                "fsx": "sg-fsx",
            },
            "instanceProfileArn": "arn:aws:iam::123456789012:instance-profile/AWSPCS-proj-record-node",
            "loginLaunchTemplateId": "lt-login-proj-record",
            "computeLaunchTemplateId": "lt-compute-proj-record",
        }

        result = self.deploy_mod.record_infrastructure(event)

        assert result["status"] == "ACTIVE"

        # Verify DynamoDB record
        item = _get_project(self.projects_table, "proj-record")
        assert item["vpcId"] == "vpc-abc123"
        assert item["efsFileSystemId"] == "fs-def456"
        assert item["s3BucketName"] == "my-bucket-789"
        assert item["cdkStackName"] == "HpcProject-proj-record"
        assert item["publicSubnetIds"] == ["subnet-pub-1", "subnet-pub-2"]
        assert item["privateSubnetIds"] == ["subnet-priv-1", "subnet-priv-2"]
        assert item["securityGroupIds"]["headNode"] == "sg-head"
        assert item["securityGroupIds"]["fsx"] == "sg-fsx"
        assert item["status"] == "ACTIVE"
        assert item["instanceProfileArn"] == "arn:aws:iam::123456789012:instance-profile/AWSPCS-proj-record-node"
        assert item["loginLaunchTemplateId"] == "lt-login-proj-record"
        assert item["computeLaunchTemplateId"] == "lt-compute-proj-record"

    def test_record_infrastructure_handles_empty_fields(self):
        """Validates: Requirement 2.2 — empty infrastructure fields are stored."""
        _seed_project(self.projects_table, "proj-empty-infra", status="DEPLOYING")

        event = {
            "projectId": "proj-empty-infra",
            "vpcId": "",
            "efsFileSystemId": "",
            "s3BucketName": "",
            "cdkStackName": "",
            "publicSubnetIds": [],
            "privateSubnetIds": [],
            "securityGroupIds": {},
        }

        result = self.deploy_mod.record_infrastructure(event)

        assert result["status"] == "ACTIVE"

        item = _get_project(self.projects_table, "proj-empty-infra")
        assert item["status"] == "ACTIVE"
        assert item["vpcId"] == ""
        assert item["publicSubnetIds"] == []
        assert item["privateSubnetIds"] == []
        assert item["securityGroupIds"] == {}

    # -- handle_deploy_failure: transitions back to CREATED -----------------

    def test_handle_deploy_failure_transitions_to_created(self):
        """Validates: Requirement 2.3 — failure transitions project back to CREATED."""
        _seed_project(self.projects_table, "proj-fail", status="DEPLOYING")

        event = {
            "projectId": "proj-fail",
            "error": {"Cause": "CDK deploy timed out"},
        }

        result = self.deploy_mod.handle_deploy_failure(event)

        assert result["status"] == "CREATED"
        assert result["errorMessage"] == "CDK deploy timed out"

        item = _get_project(self.projects_table, "proj-fail")
        assert item["status"] == "CREATED"
        assert item["errorMessage"] == "CDK deploy timed out"

    def test_handle_deploy_failure_uses_error_message_field(self):
        """Validates: Requirement 2.3 — fallback to errorMessage when error.Cause missing."""
        _seed_project(self.projects_table, "proj-fail-msg", status="DEPLOYING")

        event = {
            "projectId": "proj-fail-msg",
            "errorMessage": "Build failed with status FAILED",
        }

        result = self.deploy_mod.handle_deploy_failure(event)

        assert result["status"] == "CREATED"
        assert result["errorMessage"] == "Build failed with status FAILED"

        item = _get_project(self.projects_table, "proj-fail-msg")
        assert item["status"] == "CREATED"
        assert item["errorMessage"] == "Build failed with status FAILED"

    def test_handle_deploy_failure_defaults_to_unknown_error(self):
        """Validates: Requirement 2.3 — defaults to 'Unknown error' when no error info."""
        _seed_project(self.projects_table, "proj-fail-unknown", status="DEPLOYING")

        event = {"projectId": "proj-fail-unknown"}

        result = self.deploy_mod.handle_deploy_failure(event)

        assert result["errorMessage"] == "Unknown error"

    def test_handle_deploy_failure_survives_missing_project_id(self):
        """Validates: Requirement 2.3 — gracefully handles missing projectId."""
        event = {"error": {"Cause": "Something broke"}}

        # Should not raise — failure handler is best-effort
        result = self.deploy_mod.handle_deploy_failure(event)

        assert result["status"] == "CREATED"
        assert result["errorMessage"] == "Something broke"

    # -- Progress tracking --------------------------------------------------

    def test_validate_project_state_updates_progress_to_step_1(self):
        """Validates: Requirements 2.5, 2.6 — step 1 progress written."""
        _seed_project(self.projects_table, "proj-prog-1", status="DEPLOYING")

        self.deploy_mod.validate_project_state({"projectId": "proj-prog-1"})

        item = _get_project(self.projects_table, "proj-prog-1")
        assert item["currentStep"] == 1
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Validating project state"

    def test_start_cdk_deploy_updates_progress_to_step_2(self):
        """Validates: Requirements 2.5, 2.6 — step 2 progress written."""
        _seed_project(self.projects_table, "proj-prog-2", status="DEPLOYING")

        self.mock_codebuild.start_build.return_value = {
            "build": {"id": "build-123"}
        }
        self.deploy_mod.CODEBUILD_PROJECT_NAME = "test-codebuild-project"

        self.deploy_mod.start_cdk_deploy({"projectId": "proj-prog-2"})

        item = _get_project(self.projects_table, "proj-prog-2")
        assert item["currentStep"] == 2
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Starting CDK deployment"

    def test_check_deploy_status_updates_progress_to_step_3(self):
        """Validates: Requirements 2.5, 2.6 — step 3 progress written."""
        _seed_project(self.projects_table, "proj-prog-3", status="DEPLOYING")

        self.mock_codebuild.batch_get_builds.return_value = {
            "builds": [{"buildStatus": "IN_PROGRESS"}]
        }

        self.deploy_mod.check_deploy_status({
            "projectId": "proj-prog-3",
            "buildId": "build-456",
        })

        item = _get_project(self.projects_table, "proj-prog-3")
        assert item["currentStep"] == 3
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Deploying infrastructure"

    def test_extract_stack_outputs_updates_progress_to_step_4(self):
        """Validates: Requirements 2.5, 2.6 — step 4 progress written."""
        _seed_project(self.projects_table, "proj-prog-4", status="DEPLOYING")

        self.mock_cfn.describe_stacks.return_value = {
            "Stacks": [{
                "Outputs": [
                    {"OutputKey": "VpcId", "OutputValue": "vpc-test"},
                    {"OutputKey": "EfsFileSystemId", "OutputValue": "fs-test"},
                    {"OutputKey": "S3BucketName", "OutputValue": "bucket-test"},
                    {"OutputKey": "PublicSubnetIds", "OutputValue": "subnet-pub-1,subnet-pub-2"},
                    {"OutputKey": "PrivateSubnetIds", "OutputValue": "subnet-priv-1,subnet-priv-2"},
                    {"OutputKey": "HeadNodeSecurityGroupId", "OutputValue": "sg-head"},
                    {"OutputKey": "ComputeNodeSecurityGroupId", "OutputValue": "sg-compute"},
                    {"OutputKey": "EfsSecurityGroupId", "OutputValue": "sg-efs"},
                    {"OutputKey": "FsxSecurityGroupId", "OutputValue": "sg-fsx"},
                    {"OutputKey": "InstanceProfileArn", "OutputValue": "arn:aws:iam::123456789012:instance-profile/AWSPCS-test-node"},
                    {"OutputKey": "LoginLaunchTemplateId", "OutputValue": "lt-login-test"},
                    {"OutputKey": "ComputeLaunchTemplateId", "OutputValue": "lt-compute-test"},
                ],
            }]
        }

        result = self.deploy_mod.extract_stack_outputs({"projectId": "proj-prog-4"})

        item = _get_project(self.projects_table, "proj-prog-4")
        assert item["currentStep"] == 4
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Extracting stack outputs"

        # Verify subnet IDs and security groups are extracted
        assert result["publicSubnetIds"] == ["subnet-pub-1", "subnet-pub-2"]
        assert result["privateSubnetIds"] == ["subnet-priv-1", "subnet-priv-2"]
        assert result["securityGroupIds"]["headNode"] == "sg-head"
        assert result["securityGroupIds"]["fsx"] == "sg-fsx"
        # Verify PCS compute node group fields are extracted
        assert result["instanceProfileArn"] == "arn:aws:iam::123456789012:instance-profile/AWSPCS-test-node"
        assert result["loginLaunchTemplateId"] == "lt-login-test"
        assert result["computeLaunchTemplateId"] == "lt-compute-test"

    def test_record_infrastructure_updates_progress_to_step_5(self):
        """Validates: Requirements 2.5, 2.6 — step 5 progress written."""
        _seed_project(self.projects_table, "proj-prog-5", status="DEPLOYING")

        self.deploy_mod.record_infrastructure({
            "projectId": "proj-prog-5",
            "vpcId": "vpc-x",
            "efsFileSystemId": "fs-x",
            "s3BucketName": "bucket-x",
            "cdkStackName": "stack-x",
            "publicSubnetIds": ["subnet-pub-1"],
            "privateSubnetIds": ["subnet-priv-1"],
            "securityGroupIds": {"headNode": "sg-h", "computeNode": "sg-c", "efs": "sg-e", "fsx": "sg-f"},
        })

        item = _get_project(self.projects_table, "proj-prog-5")
        assert item["currentStep"] == 5
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Recording infrastructure"
