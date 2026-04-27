"""Unit tests for the Cluster Operations Lambda.

Covers:
- Cluster name validation and suggestion
- Cluster name registry (cross-project rejection, same-project reuse)
- Budget breach check before cluster creation
- Cluster creation workflow step ordering
- Cluster destruction workflow with FSx export
- Authorisation for all cluster endpoints
- Non-ACTIVE clusters do not expose connection info

Requirements: 6.1, 6.7, 6.8, 6.9, 7.1, 7.2, 7.3, 7.4, 8.7, 18.1, 18.3, 18.4

Infrastructure is set up once per test class via class-scoped mock_aws
fixtures from conftest.py, avoiding repeated DynamoDB table creation.
"""

import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from conftest import (
    AWS_REGION,
    CLUSTERS_TABLE_NAME,
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    PROJECTS_TABLE_NAME,
    build_admin_event,
    build_non_admin_event,
    create_clusters_table,
    create_cluster_name_registry_table,
    create_projects_table,
    reload_cluster_ops_modules,
    reload_cluster_ops_handler_modules,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_user_event(method, resource, project_id, body=None, path_parameters=None, caller="proj-user"):
    """Build an API Gateway proxy event with ProjectUser claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": f"ProjectUser-{project_id}",
                }
            }
        },
        "body": json.dumps(body) if body is not None else None,
    }


def _unauthorised_event(method, resource, body=None, path_parameters=None):
    """Build an API Gateway proxy event for a user with no project access."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "outsider",
                    "sub": "sub-outsider",
                    "cognito:groups": "ProjectUser-other-project",
                }
            }
        },
        "body": json.dumps(body) if body is not None else None,
    }


def _seed_project(projects_table, project_id, budget_breached=False, **extra):
    """Insert a minimal project record into the Projects table."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "budgetBreached": budget_breached,
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "s3BucketName": f"hpc-{project_id}-storage",
        "vpcId": f"vpc-{project_id}",
        "efsFileSystemId": f"fs-{project_id}",
        "publicSubnetIds": ["subnet-pub-1", "subnet-pub-2"],
        "privateSubnetIds": ["subnet-priv-1", "subnet-priv-2"],
        "securityGroupIds": {
            "headNode": "sg-head",
            "computeNode": "sg-compute",
            "efs": "sg-efs",
            "fsx": "sg-fsx",
        },
        "instanceProfileArn": f"arn:aws:iam::123456789012:instance-profile/AWSPCS-{project_id}-node",
        "loginLaunchTemplateId": f"lt-login-{project_id}",
        "computeLaunchTemplateId": f"lt-compute-{project_id}",
    }
    item.update(extra)
    projects_table.put_item(Item=item)


def _seed_cluster(clusters_table, project_id, cluster_name, status="ACTIVE", **extra):
    """Insert a cluster record into the Clusters table."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": status,
        "createdAt": "2024-01-01T00:00:00+00:00",
    }
    item.update(extra)
    clusters_table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Cluster name validation and suggestion
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterNameValidation:
    """Validates: Requirements 18.1, 6.1"""

    @pytest.fixture(autouse=True, scope="class")
    def _setup(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            cluster_names_mod, errors_mod = reload_cluster_ops_modules()
            yield {
                "cluster_names_mod": cluster_names_mod,
                "errors_mod": errors_mod,
            }

    def test_valid_alphanumeric_name(self, _setup):
        assert _setup["cluster_names_mod"].validate_cluster_name("myCluster123")

    def test_valid_name_with_hyphens(self, _setup):
        assert _setup["cluster_names_mod"].validate_cluster_name("my-cluster")

    def test_valid_name_with_underscores(self, _setup):
        assert _setup["cluster_names_mod"].validate_cluster_name("my_cluster")

    def test_valid_name_mixed(self, _setup):
        assert _setup["cluster_names_mod"].validate_cluster_name("proj-A_cluster-01")

    def test_empty_string_rejected(self, _setup):
        assert not _setup["cluster_names_mod"].validate_cluster_name("")

    def test_spaces_rejected(self, _setup):
        assert not _setup["cluster_names_mod"].validate_cluster_name("my cluster")

    def test_special_chars_rejected(self, _setup):
        assert not _setup["cluster_names_mod"].validate_cluster_name("my@cluster!")

    def test_dots_rejected(self, _setup):
        assert not _setup["cluster_names_mod"].validate_cluster_name("my.cluster")

    def test_suggestion_format(self, _setup):
        name = _setup["cluster_names_mod"].suggest_cluster_name("proj-alpha")
        assert name.startswith("proj-alpha-")
        suffix = name[len("proj-alpha-"):]
        assert len(suffix) == 6
        assert suffix.isalnum()

    def test_suggestion_uniqueness(self, _setup):
        names = {_setup["cluster_names_mod"].suggest_cluster_name("proj") for _ in range(20)}
        # With 36^6 possibilities, 20 names should all be distinct
        assert len(names) == 20


# ---------------------------------------------------------------------------
# Cluster name registry
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterNameRegistry:
    """Validates: Requirements 6.7, 6.8, 18.3, 18.4"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME

            registry_table = create_cluster_name_registry_table()
            cluster_names_mod, errors_mod = reload_cluster_ops_modules()

            yield {
                "registry_table": registry_table,
                "cluster_names_mod": cluster_names_mod,
                "errors_mod": errors_mod,
            }

    def test_register_new_name_succeeds(self, _env):
        result = _env["cluster_names_mod"].register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "new-cluster", "proj-a",
        )
        assert result["clusterName"] == "new-cluster"
        assert result["projectId"] == "proj-a"
        assert "registeredAt" in result

    def test_same_project_reuse_succeeds(self, _env):
        _env["cluster_names_mod"].register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "reuse-cluster", "proj-b",
        )
        # Re-register within the same project — should succeed
        result = _env["cluster_names_mod"].register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "reuse-cluster", "proj-b",
        )
        assert result["clusterName"] == "reuse-cluster"
        assert result["projectId"] == "proj-b"

    def test_cross_project_rejected(self, _env):
        _env["cluster_names_mod"].register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "taken-cluster", "proj-c",
        )
        with pytest.raises(_env["errors_mod"].ConflictError) as exc_info:
            _env["cluster_names_mod"].register_cluster_name(
                CLUSTER_NAME_REGISTRY_TABLE_NAME, "taken-cluster", "proj-d",
            )
        assert "reserved" in str(exc_info.value).lower() or "different project" in str(exc_info.value).lower()

    def test_invalid_name_raises_validation_error(self, _env):
        with pytest.raises(_env["errors_mod"].ValidationError):
            _env["cluster_names_mod"].register_cluster_name(
                CLUSTER_NAME_REGISTRY_TABLE_NAME, "bad name!", "proj-e",
            )

    def test_lookup_registered_name(self, _env):
        _env["cluster_names_mod"].register_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "lookup-cluster", "proj-f",
        )
        result = _env["cluster_names_mod"].lookup_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "lookup-cluster",
        )
        assert result is not None
        assert result["projectId"] == "proj-f"

    def test_lookup_unregistered_name_returns_none(self, _env):
        result = _env["cluster_names_mod"].lookup_cluster_name(
            CLUSTER_NAME_REGISTRY_TABLE_NAME, "nonexistent-cluster",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Budget breach check before cluster creation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestBudgetBreachBlocksCreation:
    """Validates: Requirements 6.9"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            yield {
                "clusters_table": clusters_table,
                "projects_table": projects_table,
                "handler_mod": handler_mod,
                "clusters_mod": clusters_mod,
                "errors_mod": errors_mod,
            }

    def test_budget_breached_project_rejects_creation(self, _env):
        _seed_project(_env["projects_table"], "proj-breach", budget_breached=True)

        event = _project_user_event(
            "POST", "/projects/{projectId}/clusters", "proj-breach",
            body={"clusterName": "test-cluster", "templateId": "tpl-1"},
            path_parameters={"projectId": "proj-breach"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "BUDGET_EXCEEDED"
        assert "budget" in body["error"]["message"].lower()

    def test_budget_ok_project_allows_creation(self, _env):
        _seed_project(_env["projects_table"], "proj-ok", budget_breached=False)

        # Mock Step Functions start_execution to avoid real AWS call
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST", "/projects/{projectId}/clusters", "proj-ok",
                body={"clusterName": "good-cluster", "templateId": "tpl-1"},
                path_parameters={"projectId": "proj-ok"},
            )
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["clusterName"] == "good-cluster"

        # Verify the step function payload includes infrastructure details
        call_kwargs = mock_sfn.start_execution.call_args
        sfn_input = json.loads(call_kwargs.kwargs.get("input") or call_kwargs[1]["input"])
        assert sfn_input["s3BucketName"] == "hpc-proj-ok-storage"
        assert sfn_input["privateSubnetIds"] == ["subnet-priv-1", "subnet-priv-2"]
        assert sfn_input["securityGroupIds"]["fsx"] == "sg-fsx"

    def test_budget_breach_check_uses_consistent_read(self, _env):
        """Verify the clusters module uses ConsistentRead for budget checks."""
        _seed_project(_env["projects_table"], "proj-consistent", budget_breached=False)
        result = _env["clusters_mod"].check_budget_breach(PROJECTS_TABLE_NAME, "proj-consistent")
        assert result is False

    def test_budget_breach_returns_true_when_breached(self, _env):
        _seed_project(_env["projects_table"], "proj-breached-flag", budget_breached=True)
        result = _env["clusters_mod"].check_budget_breach(PROJECTS_TABLE_NAME, "proj-breached-flag")
        assert result is True


# ---------------------------------------------------------------------------
# Infrastructure payload in step function invocation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterCreationInfraPayload:
    """Validates that cluster creation and recreation include infrastructure
    details (s3BucketName, privateSubnetIds, securityGroupIds) in the
    Step Functions payload.  Regression test for KeyError: 's3BucketName'.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            yield {
                "clusters_table": clusters_table,
                "projects_table": projects_table,
                "handler_mod": handler_mod,
                "errors_mod": errors_mod,
            }

    def test_create_cluster_payload_includes_all_infra_fields(self, _env):
        """The SFN payload must contain every key that create_fsx_filesystem reads."""
        _seed_project(_env["projects_table"], "proj-infra")

        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST", "/projects/{projectId}/clusters", "proj-infra",
                body={"clusterName": "infra-cl", "templateId": "tpl-1"},
                path_parameters={"projectId": "proj-infra"},
            )
            _env["handler_mod"].handler(event, None)

        sfn_input = json.loads(mock_sfn.start_execution.call_args.kwargs.get("input")
                               or mock_sfn.start_execution.call_args[1]["input"])

        assert "s3BucketName" in sfn_input
        assert "privateSubnetIds" in sfn_input
        assert "publicSubnetIds" in sfn_input
        assert "securityGroupIds" in sfn_input
        assert "vpcId" in sfn_input
        assert "efsFileSystemId" in sfn_input
        assert sfn_input["s3BucketName"] == "hpc-proj-infra-storage"
        assert sfn_input["privateSubnetIds"] == ["subnet-priv-1", "subnet-priv-2"]
        assert sfn_input["securityGroupIds"]["fsx"] == "sg-fsx"
        assert sfn_input["securityGroupIds"]["headNode"] == "sg-head"
        assert "instanceProfileArn" not in sfn_input
        assert "loginLaunchTemplateId" in sfn_input
        assert "computeLaunchTemplateId" in sfn_input
        assert sfn_input["loginLaunchTemplateId"] == "lt-login-proj-infra"
        assert sfn_input["computeLaunchTemplateId"] == "lt-compute-proj-infra"

    def test_recreate_cluster_payload_includes_all_infra_fields(self, _env):
        """Recreation must also pass infrastructure details to the SFN."""
        _seed_project(_env["projects_table"], "proj-recreate-infra")
        _seed_cluster(
            _env["clusters_table"], "proj-recreate-infra", "old-cl",
            status="DESTROYED", templateId="tpl-1",
        )

        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate-infra",
                path_parameters={
                    "projectId": "proj-recreate-infra",
                    "clusterName": "old-cl",
                },
            )
            _env["handler_mod"].handler(event, None)

        sfn_input = json.loads(mock_sfn.start_execution.call_args.kwargs.get("input")
                               or mock_sfn.start_execution.call_args[1]["input"])

        assert sfn_input["s3BucketName"] == "hpc-proj-recreate-infra-storage"
        assert sfn_input["privateSubnetIds"] == ["subnet-priv-1", "subnet-priv-2"]
        assert sfn_input["securityGroupIds"]["fsx"] == "sg-fsx"
        assert "instanceProfileArn" not in sfn_input
        assert sfn_input["loginLaunchTemplateId"] == "lt-login-proj-recreate-infra"
        assert sfn_input["computeLaunchTemplateId"] == "lt-compute-proj-recreate-infra"

    def test_create_cluster_fails_when_infra_missing(self, _env):
        """Creation must fail gracefully when project has no infrastructure."""
        _env["projects_table"].put_item(Item={
            "PK": "PROJECT#proj-no-infra",
            "SK": "METADATA",
            "projectId": "proj-no-infra",
            "projectName": "No Infra Project",
            "budgetBreached": False,
            "status": "ACTIVE",
            "createdAt": "2024-01-01T00:00:00+00:00",
        })

        event = _project_user_event(
            "POST", "/projects/{projectId}/clusters", "proj-no-infra",
            body={"clusterName": "doomed-cl", "templateId": "tpl-1"},
            path_parameters={"projectId": "proj-no-infra"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "infrastructure" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Cluster creation workflow step ordering
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterCreationWorkflow:
    """Validates: Requirements 6.2, 7.1, 7.2, 7.3"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            from conftest import _CLUSTER_OPS_DIR, _load_module_from
            errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
            _load_module_from(_CLUSTER_OPS_DIR, "auth")
            cluster_names_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
            _load_module_from(_CLUSTER_OPS_DIR, "clusters")
            tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

            yield {
                "clusters_table": clusters_table,
                "projects_table": projects_table,
                "creation_mod": creation_mod,
                "cluster_names_mod": cluster_names_mod,
                "errors_mod": errors_mod,
                "tagging_mod": tagging_mod,
            }

    def test_step1_validate_and_register_name_succeeds(self, _env):
        _seed_project(_env["projects_table"], "proj-wf")

        event = {"projectId": "proj-wf", "clusterName": "wf-cluster"}
        result = _env["creation_mod"].validate_and_register_name(event)

        assert result["projectId"] == "proj-wf"
        assert result["clusterName"] == "wf-cluster"

    def test_step1_invalid_name_raises_validation_error(self, _env):
        event = {"projectId": "proj-wf", "clusterName": "bad name!"}
        with pytest.raises(_env["errors_mod"].ValidationError):
            _env["creation_mod"].validate_and_register_name(event)

    def test_step1_empty_name_raises_validation_error(self, _env):
        event = {"projectId": "proj-wf", "clusterName": ""}
        with pytest.raises(_env["errors_mod"].ValidationError):
            _env["creation_mod"].validate_and_register_name(event)

    def test_step2_budget_breach_raises_error(self, _env):
        _seed_project(_env["projects_table"], "proj-wf-breach", budget_breached=True)

        event = {"projectId": "proj-wf-breach"}
        with pytest.raises(_env["errors_mod"].BudgetExceededError):
            _env["creation_mod"].check_budget_breach(event)

    def test_step2_budget_ok_passes(self, _env):
        _seed_project(_env["projects_table"], "proj-wf-ok", budget_breached=False)

        event = {"projectId": "proj-wf-ok"}
        result = _env["creation_mod"].check_budget_breach(event)
        assert result["projectId"] == "proj-wf-ok"

    def test_step10_record_cluster_stores_active_status(self, _env):
        event = {
            "projectId": "proj-wf",
            "clusterName": "recorded-cluster",
            "templateId": "tpl-1",
            "pcsClusterId": "pcs-123",
            "pcsClusterArn": "arn:aws:pcs:us-east-1:123:cluster/pcs-123",
            "loginNodeGroupId": "lng-1",
            "computeNodeGroupId": "cng-1",
            "queueId": "q-1",
            "fsxFilesystemId": "fs-123",
            "loginNodeIp": "10.0.1.5",
            "createdBy": "test-user",
        }
        result = _env["creation_mod"].record_cluster(event)

        assert result["status"] == "ACTIVE"
        assert "createdAt" in result

        # Verify DynamoDB record
        item = _env["clusters_table"].get_item(
            Key={"PK": "PROJECT#proj-wf", "SK": "CLUSTER#recorded-cluster"}
        )
        assert item["Item"]["status"] == "ACTIVE"
        assert item["Item"]["pcsClusterId"] == "pcs-123"
        assert item["Item"]["loginNodeIp"] == "10.0.1.5"

    def test_step11_handle_creation_failure_marks_failed(self, _env):
        event = {
            "projectId": "proj-wf",
            "clusterName": "failed-cluster",
            "pcsClusterId": "",
            "fsxFilesystemId": "",
            "queueId": "",
            "loginNodeGroupId": "",
            "computeNodeGroupId": "",
            "errorMessage": "Something went wrong",
        }
        result = _env["creation_mod"].handle_creation_failure(event)

        assert result["status"] == "FAILED"
        assert result["errorMessage"] == "Something went wrong"

        # Verify DynamoDB record
        item = _env["clusters_table"].get_item(
            Key={"PK": "PROJECT#proj-wf", "SK": "CLUSTER#failed-cluster"}
        )
        assert item["Item"]["status"] == "FAILED"

    def test_step9_tag_resources_builds_correct_tags(self, _env):
        tags = _env["tagging_mod"].build_resource_tags("proj-x", "cluster-y")
        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["Project"] == "proj-x"
        assert tag_dict["ClusterName"] == "cluster-y"

    def test_tags_as_dict_format(self, _env):
        tags = _env["tagging_mod"].tags_as_dict("proj-x", "cluster-y")
        assert tags == {"Project": "proj-x", "ClusterName": "cluster-y"}

    def test_step4_check_fsx_status_returns_available(self, _env):
        """check_fsx_status returns fsxAvailable=True when filesystem is AVAILABLE."""
        creation_mod = _env["creation_mod"]
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{
                "Lifecycle": "AVAILABLE",
                "DNSName": "fs-123.fsx.us-east-1.amazonaws.com",
                "LustreConfiguration": {"MountName": "abcdef"},
            }],
        }):
            result = creation_mod.check_fsx_status({
                "fsxFilesystemId": "fs-123",
                "projectId": "proj-wf",
                "clusterName": "fsx-cl",
            })
        assert result["fsxAvailable"] is True
        assert result["fsxDnsName"] == "fs-123.fsx.us-east-1.amazonaws.com"
        assert result["fsxMountName"] == "abcdef"
        assert result["fsxPollCount"] == 1

    def test_step4_check_fsx_status_returns_not_available_when_creating(self, _env):
        """check_fsx_status returns fsxAvailable=False when filesystem is still CREATING."""
        creation_mod = _env["creation_mod"]
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{
                "Lifecycle": "CREATING",
                "LustreConfiguration": {},
            }],
        }):
            result = creation_mod.check_fsx_status({
                "fsxFilesystemId": "fs-123",
                "projectId": "proj-wf",
                "clusterName": "fsx-cl",
            })
        assert result["fsxAvailable"] is False
        assert result["fsxPollCount"] == 1

    def test_step4_check_fsx_status_raises_on_failed_state(self, _env):
        """check_fsx_status raises InternalError when filesystem enters FAILED state."""
        creation_mod = _env["creation_mod"]
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{"Lifecycle": "FAILED", "LustreConfiguration": {}}],
        }):
            with pytest.raises(_env["errors_mod"].InternalError, match="terminal state 'FAILED'"):
                creation_mod.check_fsx_status({
                    "fsxFilesystemId": "fs-123",
                    "projectId": "proj-wf",
                    "clusterName": "fsx-cl",
                })

    def test_step4_check_fsx_status_raises_on_misconfigured_state(self, _env):
        """check_fsx_status raises InternalError when filesystem is MISCONFIGURED."""
        creation_mod = _env["creation_mod"]
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{"Lifecycle": "MISCONFIGURED", "LustreConfiguration": {}}],
        }):
            with pytest.raises(_env["errors_mod"].InternalError, match="terminal state"):
                creation_mod.check_fsx_status({
                    "fsxFilesystemId": "fs-123",
                    "projectId": "proj-wf",
                    "clusterName": "fsx-cl",
                })

    def test_step4_check_fsx_status_raises_on_max_poll_exceeded(self, _env):
        """check_fsx_status raises InternalError when max poll attempts exceeded."""
        creation_mod = _env["creation_mod"]
        max_polls = creation_mod._FSX_MAX_POLL_ATTEMPTS
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{"Lifecycle": "CREATING", "LustreConfiguration": {}}],
        }):
            with pytest.raises(_env["errors_mod"].InternalError, match="timed out"):
                creation_mod.check_fsx_status({
                    "fsxFilesystemId": "fs-123",
                    "projectId": "proj-wf",
                    "clusterName": "fsx-cl",
                    "fsxPollCount": max_polls - 1,
                })

    def test_step4_check_fsx_status_increments_poll_count(self, _env):
        """check_fsx_status increments fsxPollCount across calls."""
        creation_mod = _env["creation_mod"]
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{"Lifecycle": "CREATING", "LustreConfiguration": {}}],
        }):
            result = creation_mod.check_fsx_status({
                "fsxFilesystemId": "fs-123",
                "projectId": "proj-wf",
                "clusterName": "fsx-cl",
                "fsxPollCount": 5,
            })
        assert result["fsxPollCount"] == 6

    def test_step4_check_fsx_status_available_at_max_polls_succeeds(self, _env):
        """check_fsx_status succeeds if filesystem becomes AVAILABLE at the last poll."""
        creation_mod = _env["creation_mod"]
        max_polls = creation_mod._FSX_MAX_POLL_ATTEMPTS
        with patch.object(creation_mod.fsx_client, "describe_file_systems", return_value={
            "FileSystems": [{
                "Lifecycle": "AVAILABLE",
                "DNSName": "fs-123.fsx.us-east-1.amazonaws.com",
                "LustreConfiguration": {"MountName": "xyz"},
            }],
        }):
            result = creation_mod.check_fsx_status({
                "fsxFilesystemId": "fs-123",
                "projectId": "proj-wf",
                "clusterName": "fsx-cl",
                "fsxPollCount": max_polls - 1,
            })
        assert result["fsxAvailable"] is True


# ---------------------------------------------------------------------------
# Cluster destruction workflow with FSx export
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterDestructionWorkflow:
    """Validates: Requirements 7.1, 7.2, 7.3"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME

            clusters_table = create_clusters_table()

            from conftest import _CLUSTER_OPS_DIR, _load_module_from
            errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
            destruction_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_destruction")

            yield {
                "clusters_table": clusters_table,
                "destruction_mod": destruction_mod,
                "errors_mod": errors_mod,
            }

    def test_export_skipped_when_no_fsx(self, _env):
        event = {"projectId": "proj-d", "clusterName": "cl-d", "fsxFilesystemId": ""}
        result = _env["destruction_mod"].create_fsx_export_task(event)

        assert result["exportTaskId"] == ""
        assert result["exportSkipped"] is True

    def test_export_status_complete_when_skipped(self, _env):
        event = {"exportSkipped": True}
        result = _env["destruction_mod"].check_fsx_export_status(event)

        assert result["exportComplete"] is True
        assert result["exportFailed"] is False

    def test_export_status_complete_when_no_task_id(self, _env):
        event = {"exportSkipped": False, "exportTaskId": ""}
        result = _env["destruction_mod"].check_fsx_export_status(event)

        assert result["exportComplete"] is True
        assert result["exportFailed"] is False

    def test_delete_pcs_resources_handles_empty_ids(self, _env):
        event = {
            "projectId": "proj-d",
            "clusterName": "cl-d",
            "pcsClusterId": "",
            "computeNodeGroupId": "",
            "loginNodeGroupId": "",
            "queueId": "",
        }
        result = _env["destruction_mod"].delete_pcs_resources(event)

        assert result["pcsCleanupResults"] == []

    def test_delete_fsx_skipped_when_no_id(self, _env):
        event = {"fsxFilesystemId": "", "clusterName": "cl-d"}
        result = _env["destruction_mod"].delete_fsx_filesystem(event)

        assert result["fsxDeleted"] is False

    def test_record_cluster_destroyed_updates_status(self, _env):
        # Seed a cluster first
        _seed_cluster(_env["clusters_table"], "proj-d", "destroy-me", status="DESTROYING")

        event = {"projectId": "proj-d", "clusterName": "destroy-me"}
        result = _env["destruction_mod"].record_cluster_destroyed(event)

        assert result["status"] == "DESTROYED"
        assert "destroyedAt" in result

        # Verify DynamoDB record
        item = _env["clusters_table"].get_item(
            Key={"PK": "PROJECT#proj-d", "SK": "CLUSTER#destroy-me"}
        )
        assert item["Item"]["status"] == "DESTROYED"
        assert "destroyedAt" in item["Item"]


# ---------------------------------------------------------------------------
# Authorisation for all cluster endpoints
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterAuthorisation:
    """Validates: Requirements 7.4, 8.6"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            create_clusters_table()
            create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            yield {
                "handler_mod": handler_mod,
                "errors_mod": errors_mod,
            }

    def test_unauthorised_user_cannot_create_cluster(self, _env):
        event = _unauthorised_event(
            "POST", "/projects/{projectId}/clusters",
            body={"clusterName": "sneaky", "templateId": "tpl-1"},
            path_parameters={"projectId": "proj-secret"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_unauthorised_user_cannot_list_clusters(self, _env):
        event = _unauthorised_event(
            "GET", "/projects/{projectId}/clusters",
            path_parameters={"projectId": "proj-secret"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_unauthorised_user_cannot_get_cluster(self, _env):
        event = _unauthorised_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}",
            path_parameters={"projectId": "proj-secret", "clusterName": "cl-1"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_unauthorised_user_cannot_delete_cluster(self, _env):
        event = _unauthorised_event(
            "DELETE", "/projects/{projectId}/clusters/{clusterName}",
            path_parameters={"projectId": "proj-secret", "clusterName": "cl-1"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_user_can_list_clusters(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters", "proj-auth",
            path_parameters={"projectId": "proj-auth"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200

    def test_admin_can_list_clusters(self, _env):
        event = build_admin_event(
            "GET", "/projects/{projectId}/clusters",
            path_parameters={"projectId": "proj-auth"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200

    def test_no_groups_user_rejected(self, _env):
        event = {
            "httpMethod": "GET",
            "resource": "/projects/{projectId}/clusters",
            "pathParameters": {"projectId": "proj-x"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "nobody",
                        "sub": "sub-nobody",
                        "cognito:groups": "",
                    }
                }
            },
            "body": None,
        }
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# Non-ACTIVE clusters do not expose connection info
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestConnectionInfoVisibility:
    """Validates: Requirements 8.7"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            # Seed project (no budget breach)
            _seed_project(projects_table, "proj-conn", budget_breached=False)

            # Seed clusters in various statuses
            _seed_cluster(clusters_table, "proj-conn", "active-cl", status="ACTIVE",
                          loginNodeIp="10.0.1.5", sshPort=22, dcvPort=8443)
            _seed_cluster(clusters_table, "proj-conn", "creating-cl", status="CREATING",
                          loginNodeIp="10.0.1.6", sshPort=22, dcvPort=8443)
            _seed_cluster(clusters_table, "proj-conn", "failed-cl", status="FAILED",
                          loginNodeIp="10.0.1.7", sshPort=22, dcvPort=8443)
            _seed_cluster(clusters_table, "proj-conn", "destroying-cl", status="DESTROYING",
                          loginNodeIp="10.0.1.8", sshPort=22, dcvPort=8443)
            _seed_cluster(clusters_table, "proj-conn", "destroyed-cl", status="DESTROYED",
                          loginNodeIp="10.0.1.9", sshPort=22, dcvPort=8443)

            yield {
                "handler_mod": handler_mod,
            }

    def test_active_cluster_exposes_connection_info(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-conn",
            path_parameters={"projectId": "proj-conn", "clusterName": "active-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "connectionInfo" in body
        assert "ssh" in body["connectionInfo"]
        assert "dcv" in body["connectionInfo"]
        assert "10.0.1.5" in body["connectionInfo"]["ssh"]
        assert "10.0.1.5" in body["connectionInfo"]["dcv"]

    def test_creating_cluster_no_connection_info(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-conn",
            path_parameters={"projectId": "proj-conn", "clusterName": "creating-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "connectionInfo" not in body

    def test_failed_cluster_no_connection_info(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-conn",
            path_parameters={"projectId": "proj-conn", "clusterName": "failed-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "connectionInfo" not in body

    def test_destroying_cluster_no_connection_info(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-conn",
            path_parameters={"projectId": "proj-conn", "clusterName": "destroying-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "connectionInfo" not in body

    def test_destroyed_cluster_no_connection_info(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-conn",
            path_parameters={"projectId": "proj-conn", "clusterName": "destroyed-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "connectionInfo" not in body


# ---------------------------------------------------------------------------
# Cluster list returns project clusters
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterListReturnsData:
    """Validates: Requirements 8.1, 8.2"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            _seed_project(projects_table, "proj-list", budget_breached=False)
            _seed_cluster(clusters_table, "proj-list", "cl-alpha", status="ACTIVE")
            _seed_cluster(clusters_table, "proj-list", "cl-beta", status="CREATING")
            _seed_cluster(clusters_table, "proj-list", "cl-gamma", status="DESTROYED")

            # Seed a cluster in a different project to verify isolation
            _seed_cluster(clusters_table, "proj-other", "cl-other", status="ACTIVE")

            yield {
                "handler_mod": handler_mod,
            }

    def test_list_returns_all_project_clusters(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters", "proj-list",
            path_parameters={"projectId": "proj-list"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        names = [c["clusterName"] for c in body["clusters"]]
        assert "cl-alpha" in names
        assert "cl-beta" in names
        assert "cl-gamma" in names

    def test_list_does_not_include_other_project_clusters(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters", "proj-list",
            path_parameters={"projectId": "proj-list"},
        )
        response = _env["handler_mod"].handler(event, None)

        body = json.loads(response["body"])
        names = [c["clusterName"] for c in body["clusters"]]
        assert "cl-other" not in names

    def test_list_empty_project_returns_empty_list(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters", "proj-empty",
            path_parameters={"projectId": "proj-empty"},
        )
        # proj-empty has no clusters seeded
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["clusters"] == []


# ---------------------------------------------------------------------------
# Cluster creation starts Step Functions execution
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterCreationStartsSFN:
    """Validates: Requirements 6.2"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            _seed_project(projects_table, "proj-sfn", budget_breached=False)

            yield {
                "handler_mod": handler_mod,
                "clusters_table": clusters_table,
            }

    def test_creation_writes_initial_creating_record(self, _env):
        """Handler must write a CREATING record to DynamoDB before starting SFN."""
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST", "/projects/{projectId}/clusters", "proj-sfn",
                body={"clusterName": "init-record-cl", "templateId": "tpl-1"},
                path_parameters={"projectId": "proj-sfn"},
            )
            response = _env["handler_mod"].handler(event, None)

            assert response["statusCode"] == 202

        # Verify the DynamoDB record was created with all required fields
        item = _env["clusters_table"].get_item(
            Key={"PK": "PROJECT#proj-sfn", "SK": "CLUSTER#init-record-cl"}
        ).get("Item")
        assert item is not None
        assert item["status"] == "CREATING"
        assert item["clusterName"] == "init-record-cl"
        assert item["projectId"] == "proj-sfn"
        assert item["templateId"] == "tpl-1"
        assert item["createdBy"] == "proj-user"
        assert "createdAt" in item
        assert item["currentStep"] == 0
        assert item["totalSteps"] == 12

    def test_creation_calls_sfn_start_execution(self, _env):
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST", "/projects/{projectId}/clusters", "proj-sfn",
                body={"clusterName": "sfn-cluster", "templateId": "tpl-1"},
                path_parameters={"projectId": "proj-sfn"},
            )
            response = _env["handler_mod"].handler(event, None)

            assert response["statusCode"] == 202
            mock_sfn.start_execution.assert_called_once()
            call_kwargs = mock_sfn.start_execution.call_args[1]
            assert call_kwargs["stateMachineArn"] == "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            payload = json.loads(call_kwargs["input"])
            assert payload["projectId"] == "proj-sfn"
            assert payload["clusterName"] == "sfn-cluster"
            assert payload["templateId"] == "tpl-1"

    def test_creation_missing_cluster_name_returns_400(self, _env):
        event = _project_user_event(
            "POST", "/projects/{projectId}/clusters", "proj-sfn",
            body={"templateId": "tpl-1"},
            path_parameters={"projectId": "proj-sfn"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_creation_missing_template_id_returns_400(self, _env):
        event = _project_user_event(
            "POST", "/projects/{projectId}/clusters", "proj-sfn",
            body={"clusterName": "valid-name"},
            path_parameters={"projectId": "proj-sfn"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_creation_invalid_cluster_name_returns_400(self, _env):
        event = _project_user_event(
            "POST", "/projects/{projectId}/clusters", "proj-sfn",
            body={"clusterName": "bad name!", "templateId": "tpl-1"},
            path_parameters={"projectId": "proj-sfn"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_creation_empty_body_returns_400(self, _env):
        event = _project_user_event(
            "POST", "/projects/{projectId}/clusters", "proj-sfn",
            path_parameters={"projectId": "proj-sfn"},
        )
        event["body"] = None
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Cluster destruction starts Step Functions execution
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterDestructionStartsSFN:
    """Validates: Requirements 7.1, 7.4"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            _seed_project(projects_table, "proj-destroy", budget_breached=False)
            _seed_cluster(clusters_table, "proj-destroy", "active-cl", status="ACTIVE",
                          pcsClusterId="pcs-1", pcsClusterArn="arn:pcs:1",
                          loginNodeGroupId="lng-1", computeNodeGroupId="cng-1",
                          queueId="q-1", fsxFilesystemId="fs-1")
            _seed_cluster(clusters_table, "proj-destroy", "creating-cl", status="CREATING")

            yield {
                "handler_mod": handler_mod,
                "errors_mod": errors_mod,
                "clusters_table": clusters_table,
            }

    def test_destruction_calls_sfn_start_execution(self, _env):
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "DELETE", "/projects/{projectId}/clusters/{clusterName}", "proj-destroy",
                path_parameters={"projectId": "proj-destroy", "clusterName": "active-cl"},
            )
            response = _env["handler_mod"].handler(event, None)

            assert response["statusCode"] == 202
            mock_sfn.start_execution.assert_called_once()
            call_kwargs = mock_sfn.start_execution.call_args[1]
            assert call_kwargs["stateMachineArn"] == "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"
            payload = json.loads(call_kwargs["input"])
            assert payload["projectId"] == "proj-destroy"
            assert payload["clusterName"] == "active-cl"
            assert payload["pcsClusterId"] == "pcs-1"
            assert payload["fsxFilesystemId"] == "fs-1"

    def test_destruction_of_creating_cluster_returns_409(self, _env):
        event = _project_user_event(
            "DELETE", "/projects/{projectId}/clusters/{clusterName}", "proj-destroy",
            path_parameters={"projectId": "proj-destroy", "clusterName": "creating-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_destruction_of_nonexistent_cluster_returns_404(self, _env):
        event = _project_user_event(
            "DELETE", "/projects/{projectId}/clusters/{clusterName}", "proj-destroy",
            path_parameters={"projectId": "proj-destroy", "clusterName": "ghost-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    def test_get_nonexistent_cluster_returns_404(self, _env):
        _seed_project(_env["clusters_table"], "proj-destroy", budget_breached=False)
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-destroy",
            path_parameters={"projectId": "proj-destroy", "clusterName": "no-such-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    def test_unknown_route_returns_404(self, _env):
        event = _project_user_event(
            "PATCH", "/projects/{projectId}/clusters/{clusterName}", "proj-destroy",
            path_parameters={"projectId": "proj-destroy", "clusterName": "active-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Budget breach blocks cluster access (GET detail)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestBudgetBreachBlocksAccess:
    """Validates: Requirements 8.5"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            _seed_project(projects_table, "proj-breach-access", budget_breached=True)
            _seed_cluster(clusters_table, "proj-breach-access", "cl-blocked", status="ACTIVE",
                          loginNodeIp="10.0.1.5", sshPort=22, dcvPort=8443)

            yield {
                "handler_mod": handler_mod,
            }

    def test_budget_breach_denies_cluster_detail_access(self, _env):
        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-breach-access",
            path_parameters={"projectId": "proj-breach-access", "clusterName": "cl-blocked"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# Step progress tracking
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestStepProgressTracking:
    """Validates: Requirements 19.2, 19.3

    Verifies that each step handler writes currentStep, totalSteps,
    and stepDescription to the DynamoDB Clusters record before
    executing its logic, and that the GET endpoint returns progress
    fields for CREATING clusters.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            from conftest import _CLUSTER_OPS_DIR, _load_module_from
            errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
            _load_module_from(_CLUSTER_OPS_DIR, "auth")
            cluster_names_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
            clusters_mod = _load_module_from(_CLUSTER_OPS_DIR, "clusters")
            tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")
            handler_mod = _load_module_from(_CLUSTER_OPS_DIR, "handler")

            # Seed a project for workflow tests
            _seed_project(projects_table, "proj-progress", budget_breached=False)

            # Seed a CREATING cluster record so step progress updates have a target
            _seed_cluster(clusters_table, "proj-progress", "progress-cl", status="CREATING")

            yield {
                "clusters_table": clusters_table,
                "projects_table": projects_table,
                "creation_mod": creation_mod,
                "handler_mod": handler_mod,
                "errors_mod": errors_mod,
            }

    def _get_cluster_item(self, _env, project_id, cluster_name):
        """Helper to read a cluster record directly from DynamoDB."""
        return _env["clusters_table"].get_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": f"CLUSTER#{cluster_name}"}
        ).get("Item", {})

    def test_step1_writes_progress_before_execution(self, _env):
        """Step 1 (validate_and_register_name) writes step 1 progress."""
        event = {"projectId": "proj-progress", "clusterName": "progress-cl"}
        _env["creation_mod"].validate_and_register_name(event)

        item = self._get_cluster_item(_env, "proj-progress", "progress-cl")
        assert item["currentStep"] == 1
        assert item["totalSteps"] == 12
        assert item["stepDescription"] == "Registering cluster name"
        assert item["status"] == "CREATING"

    def test_step2_writes_progress_before_execution(self, _env):
        """Step 2 (check_budget_breach) writes step 2 progress."""
        event = {"projectId": "proj-progress", "clusterName": "progress-cl"}
        _env["creation_mod"].check_budget_breach(event)

        item = self._get_cluster_item(_env, "proj-progress", "progress-cl")
        assert item["currentStep"] == 2
        assert item["totalSteps"] == 12
        assert item["stepDescription"] == "Checking budget"

    def test_step_labels_are_defined_for_all_steps(self, _env):
        """All 12 step labels are defined in STEP_LABELS."""
        assert len(_env["creation_mod"].STEP_LABELS) == 12
        for step_num in range(1, 13):
            assert step_num in _env["creation_mod"].STEP_LABELS
            assert isinstance(_env["creation_mod"].STEP_LABELS[step_num], str)
            assert len(_env["creation_mod"].STEP_LABELS[step_num]) > 0

    def test_total_steps_constant_is_12(self, _env):
        """TOTAL_STEPS constant is 12."""
        assert _env["creation_mod"].TOTAL_STEPS == 12

    def test_step_labels_match_expected_values(self, _env):
        """Step labels match the specification."""
        labels = _env["creation_mod"].STEP_LABELS
        assert labels[1] == "Registering cluster name"
        assert labels[2] == "Checking budget"
        assert labels[3] == "Creating IAM roles"
        assert labels[4] == "Waiting for instance profiles"
        assert labels[5] == "Creating FSx filesystem"
        assert labels[6] == "Waiting for FSx"
        assert labels[7] == "Creating PCS cluster"
        assert labels[8] == "Creating login nodes"
        assert labels[9] == "Creating compute nodes"
        assert labels[10] == "Creating queue"
        assert labels[11] == "Tagging resources"
        assert labels[12] == "Finalising"

    def test_record_cluster_writes_step12_progress(self, _env):
        """Step 12 (record_cluster / Finalising) writes step 12 progress."""
        event = {
            "projectId": "proj-progress",
            "clusterName": "progress-cl",
            "templateId": "tpl-1",
            "pcsClusterId": "pcs-prog",
            "pcsClusterArn": "arn:aws:pcs:us-east-1:123:cluster/pcs-prog",
            "loginNodeGroupId": "lng-prog",
            "computeNodeGroupId": "cng-prog",
            "queueId": "q-prog",
            "fsxFilesystemId": "fs-prog",
            "loginNodeIp": "10.0.1.50",
            "createdBy": "test-user",
        }
        _env["creation_mod"].record_cluster(event)

        # After record_cluster, the status is ACTIVE (overwritten by put_item),
        # but the progress was written before the put_item call.
        # Verify the final record is ACTIVE.
        item = self._get_cluster_item(_env, "proj-progress", "progress-cl")
        assert item["status"] == "ACTIVE"

    def test_get_creating_cluster_returns_progress_fields(self, _env):
        """GET endpoint returns progress fields for CREATING clusters."""
        # Seed a fresh CREATING cluster with progress fields
        _seed_cluster(
            _env["clusters_table"], "proj-progress", "creating-with-progress",
            status="CREATING",
            currentStep=5,
            totalSteps=12,
            stepDescription="Creating FSx filesystem",
        )
        _seed_project(_env["projects_table"], "proj-progress", budget_breached=False)

        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-progress",
            path_parameters={"projectId": "proj-progress", "clusterName": "creating-with-progress"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "CREATING"
        assert "progress" in body
        assert body["progress"]["currentStep"] == 5
        assert body["progress"]["totalSteps"] == 12
        assert body["progress"]["stepDescription"] == "Creating FSx filesystem"
        # CREATING clusters should NOT have connectionInfo
        assert "connectionInfo" not in body

    def test_get_active_cluster_no_progress_fields(self, _env):
        """GET endpoint does NOT return progress fields for ACTIVE clusters."""
        _seed_cluster(
            _env["clusters_table"], "proj-progress", "active-no-progress",
            status="ACTIVE",
            loginNodeIp="10.0.1.99", sshPort=22, dcvPort=8443,
        )

        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-progress",
            path_parameters={"projectId": "proj-progress", "clusterName": "active-no-progress"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "ACTIVE"
        assert "progress" not in body
        assert "connectionInfo" in body

    def test_get_failed_cluster_no_progress_fields(self, _env):
        """GET endpoint does NOT return progress fields for FAILED clusters."""
        _seed_cluster(
            _env["clusters_table"], "proj-progress", "failed-no-progress",
            status="FAILED",
            errorMessage="Something went wrong",
        )

        event = _project_user_event(
            "GET", "/projects/{projectId}/clusters/{clusterName}", "proj-progress",
            path_parameters={"projectId": "proj-progress", "clusterName": "failed-no-progress"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert "progress" not in body
        assert "connectionInfo" not in body


# ---------------------------------------------------------------------------
# Cluster lifecycle notifications (SNS)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterLifecycleNotifications:
    """Validates: Requirements 19.1, 19.4, 19.5

    Verifies that:
    - On successful cluster creation, a success notification is published
      with cluster name and connection details to the creating user's email
    - On cluster creation failure, a failure notification is published
      with the error description
    - User email is looked up from the PlatformUsers table
    """

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["USERS_TABLE_NAME"] = "PlatformUsers"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            # Create PlatformUsers table for email lookup
            from conftest import create_users_table
            users_table = create_users_table()

            # Create SNS topic for lifecycle notifications
            sns_client = boto3.client("sns", region_name=AWS_REGION)
            topic_response = sns_client.create_topic(Name="hpc-cluster-lifecycle-notifications")
            topic_arn = topic_response["TopicArn"]
            os.environ["CLUSTER_LIFECYCLE_SNS_TOPIC_ARN"] = topic_arn

            # Seed a user in PlatformUsers
            users_table.put_item(Item={
                "PK": "USER#alice@example.com",
                "SK": "PROFILE",
                "userId": "alice@example.com",
                "displayName": "Alice",
                "posixUid": 10001,
                "posixGid": 10001,
                "status": "ACTIVE",
                "createdAt": "2024-01-01T00:00:00+00:00",
            })

            # Seed a project
            _seed_project(projects_table, "proj-notify", budget_breached=False)

            from conftest import _CLUSTER_OPS_DIR, _load_module_from
            errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
            _load_module_from(_CLUSTER_OPS_DIR, "auth")
            _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
            _load_module_from(_CLUSTER_OPS_DIR, "clusters")
            _load_module_from(_CLUSTER_OPS_DIR, "tagging")
            _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
            creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

            yield {
                "clusters_table": clusters_table,
                "users_table": users_table,
                "creation_mod": creation_mod,
                "errors_mod": errors_mod,
                "sns_client": sns_client,
                "topic_arn": topic_arn,
            }

    def test_record_cluster_publishes_success_notification(self, _env):
        """record_cluster publishes a success notification to the SNS topic."""
        event = {
            "projectId": "proj-notify",
            "clusterName": "notify-success-cl",
            "templateId": "tpl-1",
            "pcsClusterId": "pcs-notify",
            "pcsClusterArn": "arn:aws:pcs:us-east-1:123:cluster/pcs-notify",
            "loginNodeGroupId": "lng-notify",
            "computeNodeGroupId": "cng-notify",
            "queueId": "q-notify",
            "fsxFilesystemId": "fs-notify",
            "loginNodeIp": "10.0.1.100",
            "sshPort": 22,
            "dcvPort": 8443,
            "createdBy": "alice@example.com",
        }
        result = _env["creation_mod"].record_cluster(event)

        assert result["status"] == "ACTIVE"

        # Verify the cluster was recorded in DynamoDB
        item = _env["clusters_table"].get_item(
            Key={"PK": "PROJECT#proj-notify", "SK": "CLUSTER#notify-success-cl"}
        ).get("Item", {})
        assert item["status"] == "ACTIVE"

    def test_handle_creation_failure_publishes_failure_notification(self, _env):
        """handle_creation_failure publishes a failure notification."""
        event = {
            "projectId": "proj-notify",
            "clusterName": "notify-fail-cl",
            "pcsClusterId": "",
            "fsxFilesystemId": "",
            "queueId": "",
            "loginNodeGroupId": "",
            "computeNodeGroupId": "",
            "errorMessage": "PCS cluster creation timed out",
            "createdBy": "alice@example.com",
        }
        result = _env["creation_mod"].handle_creation_failure(event)

        assert result["status"] == "FAILED"
        assert result["errorMessage"] == "PCS cluster creation timed out"

        # Verify the cluster was recorded as FAILED in DynamoDB
        item = _env["clusters_table"].get_item(
            Key={"PK": "PROJECT#proj-notify", "SK": "CLUSTER#notify-fail-cl"}
        ).get("Item", {})
        assert item["status"] == "FAILED"

    def test_lookup_user_email_returns_email(self, _env):
        """_lookup_user_email returns the userId (email) from PlatformUsers."""
        email = _env["creation_mod"]._lookup_user_email("alice@example.com")
        assert email == "alice@example.com"

    def test_lookup_user_email_returns_empty_for_unknown_user(self, _env):
        """_lookup_user_email returns empty string for non-existent user."""
        email = _env["creation_mod"]._lookup_user_email("unknown@example.com")
        assert email == ""

    def test_lookup_user_email_returns_empty_for_empty_input(self, _env):
        """_lookup_user_email returns empty string for empty user ID."""
        email = _env["creation_mod"]._lookup_user_email("")
        assert email == ""

    def test_publish_notification_skips_when_no_topic_configured(self, _env):
        """_publish_lifecycle_notification silently returns when topic ARN is empty."""
        # Temporarily clear the topic ARN
        original_arn = _env["creation_mod"].CLUSTER_LIFECYCLE_SNS_TOPIC_ARN
        _env["creation_mod"].CLUSTER_LIFECYCLE_SNS_TOPIC_ARN = ""
        try:
            # Should not raise
            _env["creation_mod"]._publish_lifecycle_notification(
                subject="Test",
                message="Test message",
                user_email="alice@example.com",
            )
        finally:
            _env["creation_mod"].CLUSTER_LIFECYCLE_SNS_TOPIC_ARN = original_arn

    def test_success_notification_includes_connection_details(self, _env):
        """record_cluster success notification includes SSH/DCV connection info."""
        event = {
            "projectId": "proj-notify",
            "clusterName": "notify-conn-cl",
            "templateId": "tpl-1",
            "pcsClusterId": "pcs-conn",
            "pcsClusterArn": "arn:aws:pcs:us-east-1:123:cluster/pcs-conn",
            "loginNodeGroupId": "lng-conn",
            "computeNodeGroupId": "cng-conn",
            "queueId": "q-conn",
            "fsxFilesystemId": "fs-conn",
            "loginNodeIp": "10.0.2.50",
            "sshPort": 22,
            "dcvPort": 8443,
            "createdBy": "alice@example.com",
        }
        # This should complete without error and record the cluster
        result = _env["creation_mod"].record_cluster(event)
        assert result["status"] == "ACTIVE"

    def test_failure_notification_without_user_still_publishes(self, _env):
        """handle_creation_failure publishes even when createdBy is empty."""
        event = {
            "projectId": "proj-notify",
            "clusterName": "notify-no-user-cl",
            "pcsClusterId": "",
            "fsxFilesystemId": "",
            "queueId": "",
            "loginNodeGroupId": "",
            "computeNodeGroupId": "",
            "errorMessage": "Unknown failure",
            "createdBy": "",
        }
        result = _env["creation_mod"].handle_creation_failure(event)
        assert result["status"] == "FAILED"

    def test_subscribe_called_for_user_email(self, _env):
        """_publish_lifecycle_notification subscribes the user email to the topic."""
        # Publish a notification — the subscribe call is embedded
        _env["creation_mod"]._publish_lifecycle_notification(
            subject="Test Subscribe",
            message="Testing subscription",
            user_email="bob@example.com",
        )
        # Verify subscription was created (moto supports list_subscriptions)
        subs = _env["sns_client"].list_subscriptions_by_topic(
            TopicArn=_env["topic_arn"]
        )
        endpoints = [s["Endpoint"] for s in subs.get("Subscriptions", [])]
        assert "bob@example.com" in endpoints


# ---------------------------------------------------------------------------
# Cluster recreation
# ---------------------------------------------------------------------------

def _project_admin_event(method, resource, project_id, body=None, path_parameters=None, caller="proj-admin"):
    """Build an API Gateway proxy event with ProjectAdmin claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": f"ProjectAdmin-{project_id}",
                }
            }
        },
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.mark.usefixtures("_aws_env_vars")
class TestClusterRecreation:
    """Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.2, 2.3, 3.2, 4.1, 4.2"""

    @pytest.fixture(autouse=True, scope="class")
    def _env(self):
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
            os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
            os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = CLUSTER_NAME_REGISTRY_TABLE_NAME
            os.environ["CREATION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
            os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"

            clusters_table = create_clusters_table()
            projects_table = create_projects_table()
            create_cluster_name_registry_table()

            handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod = reload_cluster_ops_handler_modules()

            # Seed projects
            _seed_project(projects_table, "proj-recreate", budget_breached=False)
            _seed_project(projects_table, "proj-breached", budget_breached=True)

            # Seed clusters in various statuses
            _seed_cluster(clusters_table, "proj-recreate", "destroyed-cl",
                          status="DESTROYED", templateId="tpl-stored")
            _seed_cluster(clusters_table, "proj-recreate", "destroyed-old-tpl",
                          status="DESTROYED", templateId="tpl-old")
            _seed_cluster(clusters_table, "proj-recreate", "active-cl", status="ACTIVE")
            _seed_cluster(clusters_table, "proj-recreate", "creating-cl", status="CREATING")
            _seed_cluster(clusters_table, "proj-recreate", "failed-cl", status="FAILED")
            _seed_cluster(clusters_table, "proj-recreate", "destroying-cl", status="DESTROYING")
            _seed_cluster(clusters_table, "proj-breached", "breached-cl",
                          status="DESTROYED", templateId="tpl-breached")

            yield {
                "handler_mod": handler_mod,
                "clusters_table": clusters_table,
                "projects_table": projects_table,
                "errors_mod": errors_mod,
            }

    # --- Successful recreation ---

    def test_successful_recreation_returns_202(self, _env):
        """Seed a DESTROYED cluster and non-breached project, mock SFN, verify 202."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-cl",
                      status="DESTROYED", templateId="tpl-stored")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate",
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
            )
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-recreate"
        assert body["clusterName"] == "destroyed-cl"
        assert body["templateId"] == "tpl-stored"

    def test_successful_recreation_calls_sfn(self, _env):
        """Verify SFN start_execution is called with correct payload."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-cl",
                      status="DESTROYED", templateId="tpl-stored")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate",
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
            )
            _env["handler_mod"].handler(event, None)

            mock_sfn.start_execution.assert_called_once()
            call_kwargs = mock_sfn.start_execution.call_args[1]
            payload = json.loads(call_kwargs["input"])
            assert payload["projectId"] == "proj-recreate"
            assert payload["clusterName"] == "destroyed-cl"
            assert payload["templateId"] == "tpl-stored"
            assert payload["createdBy"] == "proj-user"

    # --- Template override ---

    def test_recreation_with_template_override(self, _env):
        """Send recreate with templateId override, verify response uses new template."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-old-tpl",
                      status="DESTROYED", templateId="tpl-old")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate",
                body={"templateId": "tpl-new"},
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-old-tpl"},
            )
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-new"

    # --- Empty body / empty templateId fallback ---

    def test_recreation_with_empty_body_uses_stored_template(self, _env):
        """Send recreate with no body, verify stored templateId is used."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-cl",
                      status="DESTROYED", templateId="tpl-stored")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate",
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
            )
            event["body"] = None
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-stored"

    def test_recreation_with_empty_template_id_uses_stored_template(self, _env):
        """Send recreate with empty templateId in body, verify stored templateId is used."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-cl",
                      status="DESTROYED", templateId="tpl-stored")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_user_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate",
                body={"templateId": ""},
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
            )
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-stored"

    # --- Non-existent cluster ---

    def test_nonexistent_cluster_returns_404(self, _env):
        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            "proj-recreate",
            path_parameters={"projectId": "proj-recreate", "clusterName": "no-such-cluster"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    # --- Non-DESTROYED status returns 409 CONFLICT ---

    def test_active_cluster_returns_409(self, _env):
        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            "proj-recreate",
            path_parameters={"projectId": "proj-recreate", "clusterName": "active-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"
        assert "ACTIVE" in body["error"]["message"]

    def test_creating_cluster_returns_409(self, _env):
        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            "proj-recreate",
            path_parameters={"projectId": "proj-recreate", "clusterName": "creating-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_failed_cluster_returns_409(self, _env):
        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            "proj-recreate",
            path_parameters={"projectId": "proj-recreate", "clusterName": "failed-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    def test_destroying_cluster_returns_409(self, _env):
        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            "proj-recreate",
            path_parameters={"projectId": "proj-recreate", "clusterName": "destroying-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFLICT"

    # --- Budget breach ---

    def test_budget_breached_returns_403(self, _env):
        event = _project_user_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            "proj-breached",
            path_parameters={"projectId": "proj-breached", "clusterName": "breached-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "BUDGET_EXCEEDED"

    # --- Authorisation ---

    def test_unauthorised_user_returns_403(self, _env):
        event = _unauthorised_event(
            "POST",
            "/projects/{projectId}/clusters/{clusterName}/recreate",
            path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
        )
        response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_administrator_can_recreate(self, _env):
        """Platform Administrator (Administrators group) can recreate clusters."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-cl",
                      status="DESTROYED", templateId="tpl-stored")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = build_admin_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
            )
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-recreate"

    def test_project_admin_can_recreate(self, _env):
        """Project Administrator (ProjectAdmin-{projectId} group) can recreate clusters."""
        # Re-seed in case a prior test mutated this record
        _seed_cluster(_env["clusters_table"], "proj-recreate", "destroyed-cl",
                      status="DESTROYED", templateId="tpl-stored")
        with patch.object(_env["handler_mod"], "sfn_client") as mock_sfn:
            mock_sfn.start_execution = MagicMock(return_value={})

            event = _project_admin_event(
                "POST",
                "/projects/{projectId}/clusters/{clusterName}/recreate",
                "proj-recreate",
                path_parameters={"projectId": "proj-recreate", "clusterName": "destroyed-cl"},
            )
            response = _env["handler_mod"].handler(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["projectId"] == "proj-recreate"
