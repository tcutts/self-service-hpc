"""Unit tests for the project update step handlers.

Covers:
- validate_update_state succeeds for UPDATING project, fails for other statuses,
  snapshots previous infrastructure outputs
- start_cdk_update passes correct CDK command to CodeBuild
- check_update_status returns correct completion flag for each build status
- record_updated_infrastructure writes correct fields, detects changed IDs,
  transitions to ACTIVE
- handle_update_failure transitions back to ACTIVE and stores error message
- Progress tracking updates DynamoDB with correct step numbers and descriptions

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9

The update module calls CodeBuild and CloudFormation which are not fully
supported by moto, so we mock those clients at the module level while
using moto for DynamoDB.
"""

import logging
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

def _seed_project(projects_table, project_id, status="UPDATING", **overrides):
    """Insert a project record for update tests."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "vpcId": "vpc-old-111",
        "efsFileSystemId": "fs-old-222",
        "s3BucketName": "old-bucket-333",
        "s3BucketProvided": False,
        "budgetLimit": 50,
        "budgetBreached": False,
        "budgetType": "MONTHLY",
        "cdkStackName": f"HpcProject-{project_id}",
        "publicSubnetIds": ["subnet-pub-old-1"],
        "privateSubnetIds": ["subnet-priv-old-1"],
        "securityGroupIds": {
            "headNode": "sg-head-old",
            "computeNode": "sg-compute-old",
            "efs": "sg-efs-old",
            "fsx": "sg-fsx-old",
        },
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
# Test class — Project Update Step Handlers
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestProjectUpdateStepHandlers:
    """Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set up moto DynamoDB and reload update modules with mocked AWS clients."""
        with mock_aws():
            self.projects_table = create_projects_table()

            # Load dependency modules first
            _load_module_from(_PROJECT_MGMT_DIR, "errors")
            _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")

            # Load the update module
            self.update_mod = _load_module_from(_PROJECT_MGMT_DIR, "project_update")

            # Mock CodeBuild and CloudFormation clients (not supported by moto)
            self.mock_codebuild = MagicMock()
            self.mock_cfn = MagicMock()
            self.update_mod.codebuild_client = self.mock_codebuild
            self.update_mod.cfn_client = self.mock_cfn

            yield

    # ===================================================================
    # validate_update_state: success
    # ===================================================================

    def test_validate_update_state_succeeds_for_updating(self):
        """Validates: Requirement 3.1 — UPDATING project passes validation."""
        _seed_project(self.projects_table, "proj-update-ok", status="UPDATING")

        result = self.update_mod.validate_update_state({"projectId": "proj-update-ok"})

        assert result["projectId"] == "proj-update-ok"

    def test_validate_update_state_snapshots_previous_outputs(self):
        """Validates: Requirement 3.1 — current infrastructure outputs are snapshotted."""
        _seed_project(
            self.projects_table, "proj-snap", status="UPDATING",
            vpcId="vpc-snap-1",
            efsFileSystemId="fs-snap-2",
            s3BucketName="bucket-snap-3",
            publicSubnetIds=["subnet-pub-a", "subnet-pub-b"],
            privateSubnetIds=["subnet-priv-a"],
            securityGroupIds={
                "headNode": "sg-head-snap",
                "computeNode": "sg-compute-snap",
                "efs": "sg-efs-snap",
                "fsx": "sg-fsx-snap",
            },
        )

        result = self.update_mod.validate_update_state({"projectId": "proj-snap"})

        prev = result["previousOutputs"]
        assert prev["vpcId"] == "vpc-snap-1"
        assert prev["efsFileSystemId"] == "fs-snap-2"
        assert prev["s3BucketName"] == "bucket-snap-3"
        assert prev["publicSubnetIds"] == ["subnet-pub-a", "subnet-pub-b"]
        assert prev["privateSubnetIds"] == ["subnet-priv-a"]
        assert prev["securityGroupIds"]["headNode"] == "sg-head-snap"
        assert prev["securityGroupIds"]["fsx"] == "sg-fsx-snap"

    # ===================================================================
    # validate_update_state: failure for non-UPDATING statuses
    # ===================================================================

    def test_validate_update_state_fails_for_active(self):
        """Validates: Requirement 3.1 — ACTIVE project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-active", status="ACTIVE")

        with pytest.raises(ValidationError) as exc_info:
            self.update_mod.validate_update_state({"projectId": "proj-active"})
        assert "ACTIVE" in str(exc_info.value)

    def test_validate_update_state_fails_for_created(self):
        """Validates: Requirement 3.1 — CREATED project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-created", status="CREATED")

        with pytest.raises(ValidationError) as exc_info:
            self.update_mod.validate_update_state({"projectId": "proj-created"})
        assert "CREATED" in str(exc_info.value)

    def test_validate_update_state_fails_for_deploying(self):
        """Validates: Requirement 3.1 — DEPLOYING project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-deploying", status="DEPLOYING")

        with pytest.raises(ValidationError) as exc_info:
            self.update_mod.validate_update_state({"projectId": "proj-deploying"})
        assert "DEPLOYING" in str(exc_info.value)

    def test_validate_update_state_fails_for_destroying(self):
        """Validates: Requirement 3.1 — DESTROYING project is rejected."""
        from errors import ValidationError

        _seed_project(self.projects_table, "proj-destroying", status="DESTROYING")

        with pytest.raises(ValidationError) as exc_info:
            self.update_mod.validate_update_state({"projectId": "proj-destroying"})
        assert "DESTROYING" in str(exc_info.value)

    def test_validate_update_state_fails_for_nonexistent_project(self):
        """Validates: Requirement 3.1 — missing project raises an error.

        Note: _update_project_progress runs before the get_item check and
        creates a skeleton record via update_item (DynamoDB upsert), so the
        subsequent get_item finds a record with no status field.  The code
        raises ValidationError (status '' != 'UPDATING') rather than
        NotFoundError.
        """
        from errors import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            self.update_mod.validate_update_state({"projectId": "proj-ghost"})
        assert "proj-ghost" in str(exc_info.value)

    def test_validate_update_state_fails_for_missing_project_id(self):
        """Validates: Requirement 3.1 — missing projectId raises ValidationError."""
        from errors import ValidationError

        with pytest.raises(ValidationError):
            self.update_mod.validate_update_state({})

    # ===================================================================
    # start_cdk_update: passes correct CDK command
    # ===================================================================

    def test_start_cdk_update_passes_correct_cdk_command(self):
        """Validates: Requirements 3.2, 3.3 — correct CDK command sent to CodeBuild."""
        _seed_project(self.projects_table, "proj-cdk", status="UPDATING")

        self.mock_codebuild.start_build.return_value = {
            "build": {"id": "build-update-123"}
        }
        self.update_mod.CODEBUILD_PROJECT_NAME = "test-codebuild-project"

        result = self.update_mod.start_cdk_update({"projectId": "proj-cdk"})

        assert result["buildId"] == "build-update-123"

        # Verify the CodeBuild call
        call_args = self.mock_codebuild.start_build.call_args
        env_vars = call_args.kwargs["environmentVariablesOverride"]

        project_id_var = next(v for v in env_vars if v["name"] == "PROJECT_ID")
        assert project_id_var["value"] == "proj-cdk"

        cdk_cmd_var = next(v for v in env_vars if v["name"] == "CDK_COMMAND")
        assert cdk_cmd_var["value"] == (
            "npx cdk deploy HpcProject-proj-cdk "
            "--exclusively --require-approval never"
        )

    def test_start_cdk_update_fails_without_codebuild_project_name(self):
        """Validates: Requirement 3.2 — missing CODEBUILD_PROJECT_NAME raises error."""
        from errors import InternalError

        _seed_project(self.projects_table, "proj-no-cb", status="UPDATING")
        self.update_mod.CODEBUILD_PROJECT_NAME = ""

        with pytest.raises(InternalError):
            self.update_mod.start_cdk_update({"projectId": "proj-no-cb"})

    # ===================================================================
    # check_update_status: returns correct completion flag
    # ===================================================================

    def test_check_update_status_returns_true_for_succeeded(self):
        """Validates: Requirement 3.4 — SUCCEEDED build returns updateComplete=True."""
        _seed_project(self.projects_table, "proj-check-ok", status="UPDATING")

        self.mock_codebuild.batch_get_builds.return_value = {
            "builds": [{"buildStatus": "SUCCEEDED"}]
        }

        result = self.update_mod.check_update_status({
            "projectId": "proj-check-ok",
            "buildId": "build-ok",
        })

        assert result["updateComplete"] is True

    def test_check_update_status_returns_false_for_in_progress(self):
        """Validates: Requirement 3.4 — IN_PROGRESS build returns updateComplete=False."""
        _seed_project(self.projects_table, "proj-check-ip", status="UPDATING")

        self.mock_codebuild.batch_get_builds.return_value = {
            "builds": [{"buildStatus": "IN_PROGRESS"}]
        }

        result = self.update_mod.check_update_status({
            "projectId": "proj-check-ip",
            "buildId": "build-ip",
        })

        assert result["updateComplete"] is False

    @pytest.mark.parametrize("build_status", ["FAILED", "FAULT", "TIMED_OUT", "STOPPED"])
    def test_check_update_status_raises_for_terminal_failures(self, build_status):
        """Validates: Requirement 3.4 — terminal build statuses raise InternalError."""
        from errors import InternalError

        _seed_project(self.projects_table, f"proj-check-{build_status.lower()}", status="UPDATING")

        self.mock_codebuild.batch_get_builds.return_value = {
            "builds": [{"buildStatus": build_status}]
        }

        with pytest.raises(InternalError) as exc_info:
            self.update_mod.check_update_status({
                "projectId": f"proj-check-{build_status.lower()}",
                "buildId": f"build-{build_status.lower()}",
            })
        assert build_status in str(exc_info.value)

    def test_check_update_status_raises_for_missing_build_id(self):
        """Validates: Requirement 3.4 — missing buildId raises InternalError."""
        from errors import InternalError

        _seed_project(self.projects_table, "proj-no-build", status="UPDATING")

        with pytest.raises(InternalError):
            self.update_mod.check_update_status({
                "projectId": "proj-no-build",
                "buildId": "",
            })

    # ===================================================================
    # record_updated_infrastructure: writes fields and transitions to ACTIVE
    # ===================================================================

    def test_record_updated_infrastructure_writes_fields_and_transitions_to_active(self):
        """Validates: Requirements 3.5, 3.6, 3.7 — infrastructure IDs stored, status → ACTIVE."""
        _seed_project(self.projects_table, "proj-record", status="UPDATING")

        event = {
            "projectId": "proj-record",
            "vpcId": "vpc-new-abc",
            "efsFileSystemId": "fs-new-def",
            "s3BucketName": "new-bucket-ghi",
            "cdkStackName": "HpcProject-proj-record",
            "publicSubnetIds": ["subnet-pub-new-1", "subnet-pub-new-2"],
            "privateSubnetIds": ["subnet-priv-new-1"],
            "securityGroupIds": {
                "headNode": "sg-head-new",
                "computeNode": "sg-compute-new",
                "efs": "sg-efs-new",
                "fsx": "sg-fsx-new",
            },
            "previousOutputs": {},
        }

        result = self.update_mod.record_updated_infrastructure(event)

        assert result["status"] == "ACTIVE"

        # Verify DynamoDB record
        item = _get_project(self.projects_table, "proj-record")
        assert item["vpcId"] == "vpc-new-abc"
        assert item["efsFileSystemId"] == "fs-new-def"
        assert item["s3BucketName"] == "new-bucket-ghi"
        assert item["cdkStackName"] == "HpcProject-proj-record"
        assert item["publicSubnetIds"] == ["subnet-pub-new-1", "subnet-pub-new-2"]
        assert item["privateSubnetIds"] == ["subnet-priv-new-1"]
        assert item["securityGroupIds"]["headNode"] == "sg-head-new"
        assert item["securityGroupIds"]["fsx"] == "sg-fsx-new"
        assert item["status"] == "ACTIVE"

    def test_record_updated_infrastructure_detects_changed_vpc_id(self, caplog):
        """Validates: Requirement 3.6 — changed vpcId triggers a warning log."""
        _seed_project(self.projects_table, "proj-diff-vpc", status="UPDATING")

        event = {
            "projectId": "proj-diff-vpc",
            "vpcId": "vpc-CHANGED",
            "efsFileSystemId": "fs-old-222",
            "s3BucketName": "old-bucket-333",
            "cdkStackName": "HpcProject-proj-diff-vpc",
            "publicSubnetIds": ["subnet-pub-old-1"],
            "privateSubnetIds": ["subnet-priv-old-1"],
            "securityGroupIds": {
                "headNode": "sg-head-old",
                "computeNode": "sg-compute-old",
                "efs": "sg-efs-old",
                "fsx": "sg-fsx-old",
            },
            "previousOutputs": {
                "vpcId": "vpc-old-111",
                "efsFileSystemId": "fs-old-222",
                "s3BucketName": "old-bucket-333",
                "publicSubnetIds": ["subnet-pub-old-1"],
                "privateSubnetIds": ["subnet-priv-old-1"],
                "securityGroupIds": {
                    "headNode": "sg-head-old",
                    "computeNode": "sg-compute-old",
                    "efs": "sg-efs-old",
                    "fsx": "sg-fsx-old",
                },
            },
        }

        with caplog.at_level(logging.WARNING):
            self.update_mod.record_updated_infrastructure(event)

        assert any("vpcId" in r.message and "vpc-CHANGED" in r.message for r in caplog.records)

    def test_record_updated_infrastructure_detects_changed_security_group(self, caplog):
        """Validates: Requirement 3.6 — changed security group ID triggers a warning log."""
        _seed_project(self.projects_table, "proj-diff-sg", status="UPDATING")

        event = {
            "projectId": "proj-diff-sg",
            "vpcId": "vpc-old-111",
            "efsFileSystemId": "fs-old-222",
            "s3BucketName": "old-bucket-333",
            "cdkStackName": "HpcProject-proj-diff-sg",
            "publicSubnetIds": ["subnet-pub-old-1"],
            "privateSubnetIds": ["subnet-priv-old-1"],
            "securityGroupIds": {
                "headNode": "sg-head-CHANGED",
                "computeNode": "sg-compute-old",
                "efs": "sg-efs-old",
                "fsx": "sg-fsx-old",
            },
            "previousOutputs": {
                "vpcId": "vpc-old-111",
                "efsFileSystemId": "fs-old-222",
                "s3BucketName": "old-bucket-333",
                "publicSubnetIds": ["subnet-pub-old-1"],
                "privateSubnetIds": ["subnet-priv-old-1"],
                "securityGroupIds": {
                    "headNode": "sg-head-old",
                    "computeNode": "sg-compute-old",
                    "efs": "sg-efs-old",
                    "fsx": "sg-fsx-old",
                },
            },
        }

        with caplog.at_level(logging.WARNING):
            self.update_mod.record_updated_infrastructure(event)

        assert any("headNode" in r.message and "sg-head-CHANGED" in r.message for r in caplog.records)

    # ===================================================================
    # handle_update_failure: transitions back to ACTIVE
    # ===================================================================

    def test_handle_update_failure_transitions_to_active(self):
        """Validates: Requirement 3.8 — failure transitions project back to ACTIVE."""
        _seed_project(self.projects_table, "proj-fail", status="UPDATING")

        event = {
            "projectId": "proj-fail",
            "error": {"Cause": "CDK update timed out"},
        }

        result = self.update_mod.handle_update_failure(event)

        assert result["status"] == "ACTIVE"
        assert result["errorMessage"] == "CDK update timed out"

        item = _get_project(self.projects_table, "proj-fail")
        assert item["status"] == "ACTIVE"
        assert item["errorMessage"] == "CDK update timed out"

    def test_handle_update_failure_uses_error_message_field(self):
        """Validates: Requirement 3.8 — fallback to errorMessage when error.Cause missing."""
        _seed_project(self.projects_table, "proj-fail-msg", status="UPDATING")

        event = {
            "projectId": "proj-fail-msg",
            "errorMessage": "Build failed with status FAILED",
        }

        result = self.update_mod.handle_update_failure(event)

        assert result["status"] == "ACTIVE"
        assert result["errorMessage"] == "Build failed with status FAILED"

        item = _get_project(self.projects_table, "proj-fail-msg")
        assert item["status"] == "ACTIVE"
        assert item["errorMessage"] == "Build failed with status FAILED"

    def test_handle_update_failure_defaults_to_unknown_error(self):
        """Validates: Requirement 3.8 — defaults to 'Unknown error' when no error info."""
        _seed_project(self.projects_table, "proj-fail-unknown", status="UPDATING")

        event = {"projectId": "proj-fail-unknown"}

        result = self.update_mod.handle_update_failure(event)

        assert result["errorMessage"] == "Unknown error"

    def test_handle_update_failure_survives_missing_project_id(self):
        """Validates: Requirement 3.8 — gracefully handles missing projectId."""
        event = {"error": {"Cause": "Something broke"}}

        # Should not raise — failure handler is best-effort
        result = self.update_mod.handle_update_failure(event)

        assert result["status"] == "ACTIVE"
        assert result["errorMessage"] == "Something broke"

    # ===================================================================
    # Progress tracking
    # ===================================================================

    def test_validate_update_state_updates_progress_to_step_1(self):
        """Validates: Requirement 3.9 — step 1 progress written."""
        _seed_project(self.projects_table, "proj-prog-1", status="UPDATING")

        self.update_mod.validate_update_state({"projectId": "proj-prog-1"})

        item = _get_project(self.projects_table, "proj-prog-1")
        assert item["currentStep"] == 1
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Validating project state"

    def test_start_cdk_update_updates_progress_to_step_2(self):
        """Validates: Requirement 3.9 — step 2 progress written."""
        _seed_project(self.projects_table, "proj-prog-2", status="UPDATING")

        self.mock_codebuild.start_build.return_value = {
            "build": {"id": "build-123"}
        }
        self.update_mod.CODEBUILD_PROJECT_NAME = "test-codebuild-project"

        self.update_mod.start_cdk_update({"projectId": "proj-prog-2"})

        item = _get_project(self.projects_table, "proj-prog-2")
        assert item["currentStep"] == 2
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Starting CDK update"

    def test_check_update_status_updates_progress_to_step_3(self):
        """Validates: Requirement 3.9 — step 3 progress written."""
        _seed_project(self.projects_table, "proj-prog-3", status="UPDATING")

        self.mock_codebuild.batch_get_builds.return_value = {
            "builds": [{"buildStatus": "IN_PROGRESS"}]
        }

        self.update_mod.check_update_status({
            "projectId": "proj-prog-3",
            "buildId": "build-456",
        })

        item = _get_project(self.projects_table, "proj-prog-3")
        assert item["currentStep"] == 3
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Updating infrastructure"

    def test_extract_stack_outputs_updates_progress_to_step_4(self):
        """Validates: Requirement 3.9 — step 4 progress written."""
        _seed_project(self.projects_table, "proj-prog-4", status="UPDATING")

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
                ],
            }]
        }

        result = self.update_mod.extract_stack_outputs({"projectId": "proj-prog-4"})

        item = _get_project(self.projects_table, "proj-prog-4")
        assert item["currentStep"] == 4
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Extracting stack outputs"

        # Verify outputs are extracted correctly
        assert result["publicSubnetIds"] == ["subnet-pub-1", "subnet-pub-2"]
        assert result["privateSubnetIds"] == ["subnet-priv-1", "subnet-priv-2"]
        assert result["securityGroupIds"]["headNode"] == "sg-head"
        assert result["securityGroupIds"]["fsx"] == "sg-fsx"

    def test_record_updated_infrastructure_updates_progress_to_step_5(self):
        """Validates: Requirement 3.9 — step 5 progress written."""
        _seed_project(self.projects_table, "proj-prog-5", status="UPDATING")

        self.update_mod.record_updated_infrastructure({
            "projectId": "proj-prog-5",
            "vpcId": "vpc-x",
            "efsFileSystemId": "fs-x",
            "s3BucketName": "bucket-x",
            "cdkStackName": "stack-x",
            "publicSubnetIds": ["subnet-pub-1"],
            "privateSubnetIds": ["subnet-priv-1"],
            "securityGroupIds": {"headNode": "sg-h", "computeNode": "sg-c", "efs": "sg-e", "fsx": "sg-f"},
            "previousOutputs": {},
        })

        item = _get_project(self.projects_table, "proj-prog-5")
        assert item["currentStep"] == 5
        assert item["totalSteps"] == 5
        assert item["stepDescription"] == "Recording updated infrastructure"

    # ===================================================================
    # step_handler dispatch
    # ===================================================================

    def test_step_handler_dispatches_to_validate_update_state(self):
        """Validates: Requirement 3.1 — step_handler routes correctly."""
        _seed_project(self.projects_table, "proj-dispatch", status="UPDATING")

        result = self.update_mod.step_handler(
            {"step": "validate_update_state", "payload": {"projectId": "proj-dispatch"}},
            None,
        )

        assert result["projectId"] == "proj-dispatch"
        assert "previousOutputs" in result

    def test_step_handler_raises_for_unknown_step(self):
        """Validates: step_handler rejects unknown step names."""
        with pytest.raises(ValueError, match="Unknown update step"):
            self.update_mod.step_handler(
                {"step": "nonexistent_step", "payload": {}},
                None,
            )
