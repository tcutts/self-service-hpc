# Feature: stuck-fsx-deployment, Property 2: Preservation
"""Property-based tests for preservation of normal creation and failure flows.

These tests observe and encode the EXISTING behaviour of the unfixed code
to ensure that the bugfix does not introduce regressions. All tests MUST
PASS on the unfixed code.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ClientError
from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

# ---------------------------------------------------------------------------
# Environment variables required by modules at import
# ---------------------------------------------------------------------------
os.environ.setdefault("CLUSTERS_TABLE_NAME", "Clusters")
os.environ.setdefault("CLUSTER_NAME_REGISTRY_TABLE_NAME", "ClusterNameRegistry")
os.environ.setdefault("PROJECTS_TABLE_NAME", "Projects")
os.environ.setdefault("USERS_TABLE_NAME", "PlatformUsers")
os.environ.setdefault("TEMPLATES_TABLE_NAME", "ClusterTemplates")
os.environ.setdefault("CREATION_STATE_MACHINE_ARN",
                       "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("DESTRUCTION_STATE_MACHINE_ARN",
                       "arn:aws:states:us-east-1:123456789012:stateMachine:test-destroy")
os.environ.setdefault("USER_POOL_ID", "us-east-1_TestPool")

AWS_REGION = "us-east-1"

# ---------------------------------------------------------------------------
# Module loading — reuse conftest helpers
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import (  # noqa: E402
    _CLUSTER_OPS_DIR,
    _load_module_from,
    _ensure_shared_modules,
    create_clusters_table,
    create_projects_table,
)


def _load_cluster_creation_module():
    """Load cluster_creation and all its intra-package dependencies."""
    _ensure_shared_modules()
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    return _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")


def _load_handler_module():
    """Load the cluster operations handler and its dependencies."""
    _ensure_shared_modules()
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    return _load_module_from(_CLUSTER_OPS_DIR, "handler")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_project_id = st.from_regex(r"proj-[a-z0-9]{4,8}", fullmatch=True)
_cluster_name = st.from_regex(r"[a-z][a-z0-9\-]{2,12}", fullmatch=True)

# createdAt within the last 2 hours (legitimate CREATING window)
_recent_created_at = st.integers(
    min_value=60,       # 1 minute ago
    max_value=7200 - 1, # just under 2 hours
).map(lambda secs: (datetime.now(timezone.utc) - timedelta(seconds=secs))
      .strftime("%Y-%m-%dT%H:%M:%S+00:00"))

# FSx poll count within the 30-minute window (< 60 polls)
_fsx_poll_count = st.integers(min_value=0, max_value=58)

# Error messages for failure scenarios
_error_message = st.from_regex(r"[A-Za-z ]{5,40}", fullmatch=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_creating_cluster(table, project_id, cluster_name, created_at=None,
                           current_step=6, step_description="Waiting for FSx"):
    """Insert a cluster record in CREATING status."""
    if created_at is None:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": "CREATING",
        "currentStep": current_step,
        "totalSteps": 12,
        "stepDescription": step_description,
        "createdAt": created_at,
    })


def _seed_cluster(table, project_id, cluster_name, status, **extra):
    """Insert a cluster record with the given status."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": status,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        **extra,
    }
    table.put_item(Item=item)


def _seed_project(table, project_id):
    """Insert a minimal project record."""
    table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "status": "ACTIVE",
        "budgetBreached": False,
    })


def _get_cluster_status(table, project_id, cluster_name):
    """Read the cluster record and return its status."""
    resp = table.get_item(Key={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
    })
    item = resp.get("Item")
    return item.get("status") if item else None


def _get_cluster_record(table, project_id, cluster_name):
    """Read the full cluster record."""
    resp = table.get_item(Key={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
    })
    return resp.get("Item")



# ---------------------------------------------------------------------------
# Property 1: Successful cluster creation → status becomes ACTIVE
# For all valid cluster creation payloads where all steps succeed,
# record_cluster is called and status becomes ACTIVE.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_successful_creation_records_active_status(project_id, cluster_name):
    """When all creation steps succeed, record_cluster sets the cluster
    status to ACTIVE with resource IDs stored in DynamoDB.

    **Validates: Requirements 3.1**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()

        # Seed a CREATING record (as the handler does before starting SFN)
        _seed_creating_cluster(clusters_table, project_id, cluster_name)

        mod = _load_cluster_creation_module()

        # Build a complete event as it would look at step 12 (record_cluster)
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "templateId": "tmpl-001",
            "createdBy": "test-user",
            "pcsClusterId": "pcs-123",
            "pcsClusterArn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123",
            "loginNodeGroupId": "lng-456",
            "computeNodeGroupId": "cng-789",
            "queueId": "q-abc",
            "fsxFilesystemId": "fs-def",
            "loginNodeIp": "10.0.1.100",
            "sshPort": 22,
            "dcvPort": 8443,
        }

        # Mock SNS to avoid real calls
        with patch.object(mod, "sns_client", MagicMock()), \
             patch.object(mod, "_lookup_user_email", return_value=""):
            result = mod.record_cluster(event)

        # Verify status is ACTIVE
        assert result["status"] == "ACTIVE", (
            f"record_cluster returned status '{result['status']}', expected 'ACTIVE'"
        )

        # Verify DynamoDB record
        record = _get_cluster_record(clusters_table, project_id, cluster_name)
        assert record is not None, "Cluster record not found in DynamoDB"
        assert record["status"] == "ACTIVE", (
            f"DynamoDB status is '{record['status']}', expected 'ACTIVE'"
        )
        assert record.get("pcsClusterId") == "pcs-123"
        assert record.get("fsxFilesystemId") == "fs-def"


# ---------------------------------------------------------------------------
# Property 2: Failure with successful rollback → status becomes FAILED
# For all failure scenarios where handle_creation_failure succeeds,
# _record_failed_cluster is called and status becomes FAILED with error msg.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name, error_msg=_error_message)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_successful_rollback_records_failed_status(project_id, cluster_name, error_msg):
    """When handle_creation_failure succeeds (no exceptions), the cluster
    status is set to FAILED with the error message in DynamoDB.

    **Validates: Requirements 3.2**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()

        _seed_creating_cluster(clusters_table, project_id, cluster_name)

        mod = _load_cluster_creation_module()

        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "pcsClusterId": "",
            "fsxFilesystemId": "",
            "queueId": "",
            "loginNodeGroupId": "",
            "computeNodeGroupId": "",
            "error": {"Cause": error_msg},
        }

        # Mock IAM, PCS, FSx, SNS clients so cleanup doesn't hit real AWS
        with patch.object(mod, "iam_client", MagicMock()), \
             patch.object(mod, "pcs_client", MagicMock()), \
             patch.object(mod, "fsx_client", MagicMock()), \
             patch.object(mod, "sns_client", MagicMock()), \
             patch.object(mod, "_lookup_user_email", return_value=""):
            result = mod.handle_creation_failure(event)

        # Verify the function returns FAILED status
        assert result["status"] == "FAILED", (
            f"handle_creation_failure returned status '{result['status']}', expected 'FAILED'"
        )
        assert result.get("errorMessage") == error_msg

        # Verify DynamoDB record is FAILED
        status = _get_cluster_status(clusters_table, project_id, cluster_name)
        assert status == "FAILED", (
            f"DynamoDB status is '{status}', expected 'FAILED' after successful rollback"
        )


# ---------------------------------------------------------------------------
# Property 3: Legitimate CREATING cluster shows progress fields
# For all clusters in CREATING status with createdAt within the last 2 hours,
# the polling response includes progress fields.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name,
       created_at=_recent_created_at)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_creating_cluster_within_window_shows_progress(project_id, cluster_name, created_at):
    """When a cluster is legitimately in CREATING status (within 2 hours),
    the GET cluster detail response includes progress fields (currentStep,
    totalSteps, stepDescription).

    **Validates: Requirements 3.3**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()
        projects_table = create_projects_table()

        _seed_creating_cluster(clusters_table, project_id, cluster_name,
                               created_at=created_at, current_step=6,
                               step_description="Waiting for FSx")
        _seed_project(projects_table, project_id)

        handler_mod = _load_handler_module()

        event = {
            "httpMethod": "GET",
            "resource": "/projects/{projectId}/clusters/{clusterName}",
            "pathParameters": {
                "projectId": project_id,
                "clusterName": cluster_name,
            },
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "test-user",
                        "sub": "sub-test-user",
                        "cognito:groups": f"ProjectUser-{project_id}",
                    }
                }
            },
            "body": None,
        }

        response = handler_mod.handler(event, None)
        assert response["statusCode"] == 200, (
            f"Expected 200, got {response['statusCode']}"
        )

        body = json.loads(response["body"])
        assert body.get("status") == "CREATING"

        # Verify progress fields are present
        progress = body.get("progress", {})
        assert "currentStep" in progress, "Missing 'currentStep' in progress"
        assert "totalSteps" in progress, "Missing 'totalSteps' in progress"
        assert "stepDescription" in progress, "Missing 'stepDescription' in progress"
        assert progress["currentStep"] == 6
        assert progress["totalSteps"] == 12
        assert progress["stepDescription"] == "Waiting for FSx"


# ---------------------------------------------------------------------------
# Property 4: ACTIVE/FAILED clusters can be destroyed (returns 202)
# For all clusters in ACTIVE or FAILED status, _handle_delete_cluster
# starts the destruction workflow.
# ---------------------------------------------------------------------------

_deletable_status = st.sampled_from(["ACTIVE", "FAILED"])


@given(project_id=_project_id, cluster_name=_cluster_name,
       status=_deletable_status)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_active_or_failed_cluster_can_be_destroyed(project_id, cluster_name, status):
    """When a cluster is in ACTIVE or FAILED status, the DELETE endpoint
    starts the destruction workflow and returns 202.

    **Validates: Requirements 3.4**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()
        projects_table = create_projects_table()

        _seed_cluster(clusters_table, project_id, cluster_name, status,
                      pcsClusterId="pcs-123", fsxFilesystemId="fs-def")
        _seed_project(projects_table, project_id)

        handler_mod = _load_handler_module()

        event = {
            "httpMethod": "DELETE",
            "resource": "/projects/{projectId}/clusters/{clusterName}",
            "pathParameters": {
                "projectId": project_id,
                "clusterName": cluster_name,
            },
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "test-user",
                        "sub": "sub-test-user",
                        "cognito:groups": f"ProjectUser-{project_id}",
                    }
                }
            },
            "body": None,
        }

        # Mock SFN client so start_execution doesn't hit real AWS
        with patch.object(handler_mod, "sfn_client", MagicMock()):
            response = handler_mod.handler(event, None)

        assert response["statusCode"] == 202, (
            f"Expected 202 for {status} cluster deletion, got {response['statusCode']}. "
            f"Body: {response['body']}"
        )

        body = json.loads(response["body"])
        assert "destruction started" in body.get("message", "").lower() or \
               "destruction started" in body.get("message", ""), (
            f"Expected destruction started message, got: {body.get('message')}"
        )


# ---------------------------------------------------------------------------
# Property 5: FSx polling within 30-minute window returns without raising
# For all FSx poll attempts where fsxPollCount < 60 and status is not
# terminal, check_fsx_status returns without raising.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name,
       poll_count=_fsx_poll_count)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fsx_polling_within_window_does_not_raise(project_id, cluster_name, poll_count):
    """When FSx is still creating (not in a terminal state) and the poll
    count is below 60, check_fsx_status returns normally with progress
    information and does not raise an error.

    **Validates: Requirements 3.5**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()

        _seed_creating_cluster(clusters_table, project_id, cluster_name)

        mod = _load_cluster_creation_module()

        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "fsxFilesystemId": "fs-test123",
            "fsxPollCount": poll_count,
        }

        # Mock fsx_client to return CREATING status (non-terminal, not yet available)
        mock_fsx = MagicMock()
        mock_fsx.describe_file_systems.return_value = {
            "FileSystems": [{
                "FileSystemId": "fs-test123",
                "Lifecycle": "CREATING",
                "DNSName": "",
                "LustreConfiguration": {"MountName": ""},
            }]
        }

        with patch.object(mod, "fsx_client", mock_fsx):
            # Should NOT raise — FSx is still creating within the window
            result = mod.check_fsx_status(event)

        assert result["fsxAvailable"] is False, (
            f"Expected fsxAvailable=False for CREATING filesystem, got {result['fsxAvailable']}"
        )
        assert result["fsxPollCount"] == poll_count + 1, (
            f"Expected fsxPollCount={poll_count + 1}, got {result['fsxPollCount']}"
        )
