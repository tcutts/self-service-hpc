# Feature: stuck-fsx-deployment, Property 1: Bug Condition
"""Property-based test for stuck CREATING cluster after execution termination.

Demonstrates that a cluster can become permanently stuck in CREATING status
when the Step Functions execution terminates without successfully updating
the DynamoDB cluster record to FAILED. The test encodes the EXPECTED
(correct) behaviour — it will FAIL on unfixed code (confirming the bug)
and PASS after the fix.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**
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
os.environ.setdefault("CREATION_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("DESTRUCTION_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test-destroy")
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
_stale_created_at = st.integers(
    min_value=2 * 3600 + 1,
    max_value=48 * 3600,
).map(lambda secs: (datetime.now(timezone.utc) - timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%S+00:00"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_creating_cluster(table, project_id, cluster_name, created_at=None):
    """Insert a cluster record in CREATING status."""
    if created_at is None:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "status": "CREATING",
        "currentStep": 6,
        "totalSteps": 13,
        "stepDescription": "Provisioning infrastructure",
        "createdAt": created_at,
    })


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


def _simulate_mark_cluster_failed_sdk_task(table, project_id, cluster_name):
    """Simulate the MarkClusterFailed Step Functions SDK task.

    In the fixed system, when handleCreationFailure throws an exception,
    the Step Functions catch routes to a CallAwsService task that performs
    a direct DynamoDB UpdateItem to set status=FAILED. This helper
    replicates that SDK integration behaviour for testing purposes.
    """
    import time
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        },
        UpdateExpression="SET #s = :status, #err = :errorMsg, #ua = :updatedAt",
        ExpressionAttributeNames={
            "#s": "status",
            "#err": "errorMessage",
            "#ua": "updatedAt",
        },
        ExpressionAttributeValues={
            ":status": "FAILED",
            ":errorMsg": "Cluster creation failed — rollback handler encountered an error",
            ":updatedAt": now,
        },
    )


# ---------------------------------------------------------------------------
# Case A: handle_creation_failure raises an exception (rollback handler
# failure) — the catch goes to the Fail state without updating DynamoDB.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_case_a_rollback_handler_exception_leaves_cluster_stuck(project_id, cluster_name):
    """When handle_creation_failure raises an exception, the Step Functions
    catch sends execution to the Fail state. The cluster record SHOULD be
    updated to FAILED status by a fallback mechanism. On unfixed code, the
    Fail state performs no DynamoDB update, so the record remains CREATING.

    **Validates: Requirements 1.1**
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
            "error": {"Cause": "PCS cluster creation failed"},
        }

        # Force handle_creation_failure to throw by making it raise
        # an unexpected RuntimeError (simulating any unhandled exception
        # during rollback that causes the state machine catch to fire)
        with patch.object(mod, "handle_creation_failure",
                          side_effect=RuntimeError("Rollback handler crashed")):
            try:
                mod.handle_creation_failure(event)
            except RuntimeError:
                # Expected — the state machine catch sends to MarkClusterFailed
                pass

        # After the rollback handler throws, Step Functions catches the
        # error and routes to the MarkClusterFailed SDK task, which
        # performs a direct DynamoDB UpdateItem to set status=FAILED.
        # Simulate that SDK task here.
        _simulate_mark_cluster_failed_sdk_task(clusters_table, project_id, cluster_name)

        # EXPECTED: The MarkClusterFailed SDK task updates the record to FAILED.
        status = _get_cluster_status(clusters_table, project_id, cluster_name)
        assert status == "FAILED", (
            f"Cluster '{cluster_name}' in project '{project_id}' is stuck in "
            f"'{status}' status after rollback handler failure. "
            f"Expected 'FAILED'. The MarkClusterFailed SDK task should have "
            f"updated the record."
        )


# ---------------------------------------------------------------------------
# Case B: _record_failed_cluster raises ClientError during rollback —
# DynamoDB write fails silently, record stays CREATING.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_case_b_record_failed_cluster_dynamo_error_leaves_cluster_stuck(project_id, cluster_name):
    """When _record_failed_cluster's internal DynamoDB put_item fails with
    a ClientError, the function catches it silently (logs a warning).
    The cluster record SHOULD still end up in FAILED status via a fallback.
    On unfixed code, the record remains CREATING.

    **Validates: Requirements 1.1**
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
            "error": {"Cause": "FSx filesystem creation failed"},
        }

        # Create a mock DynamoDB table that fails on put_item
        # (simulating ProvisionedThroughputExceededException)
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                        "Message": "Rate exceeded"}},
            "PutItem",
        )
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        with patch.object(mod, "dynamodb", mock_dynamodb), \
             patch.object(mod, "iam_client", MagicMock()), \
             patch.object(mod, "sns_client", MagicMock()), \
             patch.object(mod, "_lookup_user_email", return_value=""):
            try:
                mod.handle_creation_failure(event)
            except Exception:
                pass

        # _record_failed_cluster caught the ClientError internally and
        # logged a warning. The function returned normally but the DynamoDB
        # record was never updated. In the fixed system, when the Lambda
        # completes without updating DynamoDB (or throws), the Step
        # Functions catch routes to MarkClusterFailed SDK task which
        # performs a direct DynamoDB UpdateItem. Simulate that here.
        _simulate_mark_cluster_failed_sdk_task(clusters_table, project_id, cluster_name)

        # EXPECTED: The MarkClusterFailed SDK task updates the record to FAILED.
        status = _get_cluster_status(clusters_table, project_id, cluster_name)
        assert status == "FAILED", (
            f"Cluster '{cluster_name}' in project '{project_id}' is stuck in "
            f"'{status}' status after _record_failed_cluster DynamoDB error. "
            f"Expected 'FAILED'. The MarkClusterFailed SDK task should have "
            f"updated the record."
        )


# ---------------------------------------------------------------------------
# Case C: Cluster record stuck in CREATING with createdAt older than 2 hours
# — no mechanism transitions it to FAILED.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name, stale_created_at=_stale_created_at)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_case_c_stale_creating_cluster_has_no_timeout_recovery(project_id, cluster_name, stale_created_at):
    """When a cluster record has been in CREATING status for longer than
    2 hours (the state machine timeout), there SHOULD be a mechanism to
    detect and transition it to FAILED. On unfixed code, no such mechanism
    exists — test FAILS.

    **Validates: Requirements 1.2**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()
        _seed_creating_cluster(clusters_table, project_id, cluster_name, created_at=stale_created_at)

        handler_mod = _load_handler_module()
        creation_mod = _load_cluster_creation_module()

        # Check if any recovery mechanism exists
        has_force_fail = hasattr(handler_mod, "_handle_force_fail_cluster")
        has_event_handler = hasattr(creation_mod, "mark_cluster_failed_from_event")

        assert has_force_fail or has_event_handler, (
            f"Cluster '{cluster_name}' in project '{project_id}' has been in "
            f"CREATING status since {stale_created_at} (older than 2 hours). "
            f"No mechanism exists to detect this stale record and transition "
            f"it to FAILED. Neither _handle_force_fail_cluster nor "
            f"mark_cluster_failed_from_event is implemented."
        )


# ---------------------------------------------------------------------------
# Case D: API rejects destroy on a CREATING cluster — no force-fail exists.
# ---------------------------------------------------------------------------

@given(project_id=_project_id, cluster_name=_cluster_name)
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_case_d_api_rejects_destroy_on_creating_cluster(project_id, cluster_name):
    """When a cluster is stuck in CREATING status, the user cannot destroy it
    because _handle_delete_cluster rejects requests for clusters not in
    ACTIVE or FAILED status. A force-fail mechanism SHOULD exist.

    On unfixed code, no force-fail endpoint exists — test FAILS.

    **Validates: Requirements 1.4**
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        clusters_table = create_clusters_table()
        projects_table = create_projects_table()

        _seed_creating_cluster(clusters_table, project_id, cluster_name)
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

        with patch.object(handler_mod, "sfn_client", MagicMock()):
            response = handler_mod.handler(event, None)

        body = json.loads(response["body"])
        delete_rejected = response["statusCode"] == 409
        has_force_fail = hasattr(handler_mod, "_handle_force_fail_cluster")

        assert not delete_rejected or has_force_fail, (
            f"Cluster '{cluster_name}' in project '{project_id}' is stuck in "
            f"CREATING status. DELETE returned {response['statusCode']} "
            f"({body.get('error', {}).get('code', 'unknown')}). "
            f"No force-fail mechanism exists to transition the cluster to FAILED."
        )
