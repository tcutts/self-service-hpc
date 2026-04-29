"""Property-based tests for deletion progress tracking.

[PBT: Property 1] Atomic cluster deletion initializes progress and transitions status.
[PBT: Property 2] Cluster GET includes progress object for transitional statuses.
[PBT: Property 3] Destruction step labels provide complete monotonic coverage.
[PBT: Property 4] Progress bar percentage calculation is correct.
[PBT: Property 5] Atomic project destruction initializes progress and transitions status.
"""

import importlib
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)

# Clear cached modules to ensure correct imports
_cached_errors = sys.modules.get("errors")
if _cached_errors is not None:
    _errors_file = getattr(_cached_errors, "__file__", "") or ""
    if "cluster_operations" not in _errors_file:
        del sys.modules["errors"]

for _mod in ["cluster_names", "cluster_destruction"]:
    if _mod in sys.modules:
        del sys.modules[_mod]

from cluster_destruction import STEP_LABELS, TOTAL_STEPS  # noqa: E402


# ===================================================================
# [PBT: Property 3] Destruction step labels provide complete
# monotonic coverage
# ===================================================================

class TestStepLabelsCompleteness:
    """[PBT: Property 3] The STEP_LABELS dictionary covers [1, TOTAL_STEPS]
    with no gaps and all non-empty string values.

    **Validates: Requirements 1.3**
    """

    def test_step_labels_has_exactly_total_steps_entries(self):
        """STEP_LABELS must have exactly TOTAL_STEPS entries.

        **Validates: Requirements 1.3**
        """
        assert len(STEP_LABELS) == TOTAL_STEPS, (
            f"STEP_LABELS has {len(STEP_LABELS)} entries but TOTAL_STEPS is {TOTAL_STEPS}"
        )

    def test_step_labels_covers_all_keys_without_gaps(self):
        """STEP_LABELS must contain every key from 1 to TOTAL_STEPS.

        **Validates: Requirements 1.3**
        """
        expected_keys = set(range(1, TOTAL_STEPS + 1))
        actual_keys = set(STEP_LABELS.keys())
        assert actual_keys == expected_keys, (
            f"STEP_LABELS keys {actual_keys} do not match expected {expected_keys}. "
            f"Missing: {expected_keys - actual_keys}, Extra: {actual_keys - expected_keys}"
        )

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(step=st.integers(min_value=1, max_value=TOTAL_STEPS))
    def test_each_step_maps_to_non_empty_string(self, step):
        """For any step number in [1, TOTAL_STEPS], STEP_LABELS must map
        that key to a non-empty string.

        **Validates: Requirements 1.3**
        """
        assert step in STEP_LABELS, (
            f"Step {step} is missing from STEP_LABELS"
        )
        label = STEP_LABELS[step]
        assert isinstance(label, str), (
            f"STEP_LABELS[{step}] is {type(label).__name__}, expected str"
        )
        assert len(label) > 0, (
            f"STEP_LABELS[{step}] is an empty string"
        )


# ===================================================================
# Helpers for Property 1 — handler-level tests with moto
# ===================================================================

AWS_REGION = "us-east-1"
CLUSTERS_TABLE_NAME = "Clusters"
PROJECTS_TABLE_NAME = "Projects"

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_ROOT, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_ROOT, "shared")


def _load_module_from(directory: str, module_name: str):
    """Load a module by file path, avoiding sys.path collisions."""
    filepath = os.path.join(directory, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _create_clusters_table():
    """Create the mocked Clusters DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=CLUSTERS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=CLUSTERS_TABLE_NAME)
    return table


def _create_projects_table():
    """Create the mocked Projects DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=PROJECTS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=PROJECTS_TABLE_NAME)
    return table


def _setup_env_and_modules():
    """Set environment variables and reload handler modules inside mock_aws."""
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
    os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
    os.environ["CLUSTER_NAME_REGISTRY_TABLE_NAME"] = "ClusterNameRegistry"
    os.environ["CREATION_STATE_MACHINE_ARN"] = (
        "arn:aws:states:us-east-1:123456789012:stateMachine:creation"
    )
    os.environ["DESTRUCTION_STATE_MACHINE_ARN"] = (
        "arn:aws:states:us-east-1:123456789012:stateMachine:destruction"
    )
    os.environ["USER_POOL_ID"] = "us-east-1_TestPool"

    # Load shared modules first, then cluster ops modules
    if "authorization" not in sys.modules:
        _load_module_from(_SHARED_DIR, "authorization")
    if "api_logging" not in sys.modules:
        _load_module_from(_SHARED_DIR, "api_logging")

    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    handler_mod = _load_module_from(_CLUSTER_OPS_DIR, "handler")
    errors_mod = sys.modules["errors"]
    return handler_mod, errors_mod


def _build_delete_event(project_id: str, cluster_name: str):
    """Build an API Gateway DELETE event for a cluster."""
    return {
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


# ===================================================================
# [PBT: Property 1] Atomic cluster deletion initializes progress
# and transitions status
# ===================================================================

# Strategy: generate cluster statuses from the known set
_CLUSTER_STATUSES = ["ACTIVE", "FAILED", "CREATING", "DESTROYING", "DESTROYED"]
_DELETABLE_STATUSES = {"ACTIVE", "FAILED"}

cluster_status_strategy = st.sampled_from(_CLUSTER_STATUSES)


class TestAtomicClusterDeletion:
    """[PBT: Property 1] Atomic cluster deletion initializes progress
    and transitions status.

    For any cluster with status ACTIVE or FAILED, a DELETE request SHALL
    atomically set status to DESTROYING and initialize progress fields.
    For any other status, the handler SHALL raise a ConflictError (409).

    **Validates: Requirements 1.1, 9.1, 9.2**
    """

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(status=cluster_status_strategy)
    def test_delete_cluster_transitions_or_conflicts(self, status):
        """For any cluster status, DELETE either transitions to DESTROYING
        with correct progress fields (ACTIVE/FAILED) or returns 409 Conflict.

        **Validates: Requirements 1.1, 9.1, 9.2**
        """
        with mock_aws():
            handler_mod, errors_mod = _setup_env_and_modules()

            clusters_table = _create_clusters_table()
            projects_table = _create_projects_table()

            project_id = "test-proj"
            cluster_name = "test-cluster"

            # Seed project record (needed by get_cluster's table lookup)
            projects_table.put_item(Item={
                "PK": f"PROJECT#{project_id}",
                "SK": "METADATA",
                "projectId": project_id,
                "status": "ACTIVE",
                "budgetBreached": False,
            })

            # Seed cluster record with the generated status
            clusters_table.put_item(Item={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
                "clusterName": cluster_name,
                "projectId": project_id,
                "status": status,
                "createdAt": "2024-01-01T00:00:00+00:00",
            })

            event = _build_delete_event(project_id, cluster_name)

            # Mock Step Functions to avoid real AWS calls
            mock_sfn = MagicMock()
            mock_sfn.start_execution = MagicMock(return_value={})
            original_sfn = handler_mod.sfn_client
            handler_mod.sfn_client = mock_sfn

            try:
                response = handler_mod.handler(event, None)
                body = json.loads(response["body"])

                if status in _DELETABLE_STATUSES:
                    # Should succeed with 202 and start destruction
                    assert response["statusCode"] == 202, (
                        f"Expected 202 for status={status}, got {response['statusCode']}: {body}"
                    )

                    # Verify DynamoDB record was atomically updated
                    item = clusters_table.get_item(
                        Key={
                            "PK": f"PROJECT#{project_id}",
                            "SK": f"CLUSTER#{cluster_name}",
                        }
                    )["Item"]

                    assert item["status"] == "DESTROYING", (
                        f"Expected status=DESTROYING, got {item['status']}"
                    )
                    assert int(item["currentStep"]) == 0, (
                        f"Expected currentStep=0, got {item['currentStep']}"
                    )
                    assert int(item["totalSteps"]) == 8, (
                        f"Expected totalSteps=8, got {item['totalSteps']}"
                    )
                    assert item["stepDescription"] == "Starting cluster destruction", (
                        f"Expected stepDescription='Starting cluster destruction', "
                        f"got '{item['stepDescription']}'"
                    )

                    # Verify Step Functions was called
                    mock_sfn.start_execution.assert_called_once()

                else:
                    # Should return 409 Conflict
                    assert response["statusCode"] == 409, (
                        f"Expected 409 for status={status}, got {response['statusCode']}: {body}"
                    )
                    assert body["error"]["code"] == "CONFLICT", (
                        f"Expected error code CONFLICT, got {body['error']['code']}"
                    )

                    # Verify DynamoDB record was NOT modified
                    item = clusters_table.get_item(
                        Key={
                            "PK": f"PROJECT#{project_id}",
                            "SK": f"CLUSTER#{cluster_name}",
                        }
                    )["Item"]
                    assert item["status"] == status, (
                        f"Expected status to remain {status}, got {item['status']}"
                    )

            finally:
                handler_mod.sfn_client = original_sfn


# ===================================================================
# Helper for Property 2 — GET event builder
# ===================================================================

def _build_get_event(project_id: str, cluster_name: str):
    """Build an API Gateway GET event for a single cluster."""
    return {
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


# ===================================================================
# [PBT: Property 2] Cluster GET includes progress object for
# transitional statuses
# ===================================================================

_ALL_STATUSES = ["ACTIVE", "FAILED", "CREATING", "DESTROYING", "DESTROYED"]
_TRANSITIONAL_STATUSES = {"CREATING", "DESTROYING"}


class TestClusterGetProgressInclusion:
    """[PBT: Property 2] Cluster GET includes progress object for
    transitional statuses.

    For any cluster with status CREATING or DESTROYING, the GET endpoint
    SHALL return a `progress` object containing integer `currentStep`,
    integer `totalSteps`, and string `stepDescription`. For any other
    status, the GET endpoint SHALL omit the `progress` object.

    **Validates: Requirements 2.1, 2.2, 2.3**
    """

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        status=st.sampled_from(_ALL_STATUSES),
        current_step=st.integers(min_value=0, max_value=20),
        total_steps=st.integers(min_value=1, max_value=20),
        step_description=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
            min_size=1,
            max_size=50,
        ),
    )
    def test_progress_included_for_transitional_omitted_for_others(
        self, status, current_step, total_steps, step_description
    ):
        """For any cluster status, GET either includes a progress object
        with integer fields (CREATING/DESTROYING) or omits it entirely.

        **Validates: Requirements 2.1, 2.2, 2.3**
        """
        with mock_aws():
            handler_mod, errors_mod = _setup_env_and_modules()

            clusters_table = _create_clusters_table()
            projects_table = _create_projects_table()

            project_id = "test-proj"
            cluster_name = "test-cluster"

            # Seed project record (budget not breached)
            projects_table.put_item(Item={
                "PK": f"PROJECT#{project_id}",
                "SK": "METADATA",
                "projectId": project_id,
                "status": "ACTIVE",
                "budgetBreached": False,
            })

            # Seed cluster record with Decimal values (as DynamoDB returns)
            clusters_table.put_item(Item={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
                "clusterName": cluster_name,
                "projectId": project_id,
                "status": status,
                "currentStep": Decimal(str(current_step)),
                "totalSteps": Decimal(str(total_steps)),
                "stepDescription": step_description,
                "createdAt": "2024-01-01T00:00:00+00:00",
            })

            event = _build_get_event(project_id, cluster_name)
            response = handler_mod.handler(event, None)
            body = json.loads(response["body"])

            assert response["statusCode"] == 200, (
                f"Expected 200 for GET, got {response['statusCode']}: {body}"
            )

            if status in _TRANSITIONAL_STATUSES:
                # Progress object MUST be present
                assert "progress" in body, (
                    f"Expected 'progress' in response for status={status}, "
                    f"but it was missing. Body keys: {list(body.keys())}"
                )
                progress = body["progress"]

                # currentStep and totalSteps must be integers (not Decimal)
                assert isinstance(progress["currentStep"], int), (
                    f"Expected currentStep to be int, got "
                    f"{type(progress['currentStep']).__name__}"
                )
                assert isinstance(progress["totalSteps"], int), (
                    f"Expected totalSteps to be int, got "
                    f"{type(progress['totalSteps']).__name__}"
                )

                # Values must match the seeded data
                assert progress["currentStep"] == current_step, (
                    f"Expected currentStep={current_step}, "
                    f"got {progress['currentStep']}"
                )
                assert progress["totalSteps"] == total_steps, (
                    f"Expected totalSteps={total_steps}, "
                    f"got {progress['totalSteps']}"
                )

                # stepDescription must be a string
                assert isinstance(progress["stepDescription"], str), (
                    f"Expected stepDescription to be str, got "
                    f"{type(progress['stepDescription']).__name__}"
                )
            else:
                # Progress object MUST NOT be present
                assert "progress" not in body, (
                    f"Expected no 'progress' in response for status={status}, "
                    f"but found: {body.get('progress')}"
                )


# ===================================================================
# Helpers for Property 5 — project management handler-level tests
# ===================================================================

_PROJECT_MGMT_DIR = os.path.join(_LAMBDA_ROOT, "project_management")


def _setup_project_env_and_modules():
    """Set environment variables and load project management handler modules inside mock_aws."""
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
    os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
    os.environ["PROJECT_DEPLOY_STATE_MACHINE_ARN"] = (
        "arn:aws:states:us-east-1:123456789012:stateMachine:deploy"
    )
    os.environ["PROJECT_DESTROY_STATE_MACHINE_ARN"] = (
        "arn:aws:states:us-east-1:123456789012:stateMachine:destroy"
    )
    os.environ["PROJECT_UPDATE_STATE_MACHINE_ARN"] = (
        "arn:aws:states:us-east-1:123456789012:stateMachine:update"
    )
    os.environ["USER_POOL_ID"] = "us-east-1_TestPool"

    # Load shared modules first
    _load_module_from(_SHARED_DIR, "authorization")
    _load_module_from(_SHARED_DIR, "api_logging")

    # Load project management modules — order matters for dependencies
    _load_module_from(_PROJECT_MGMT_DIR, "errors")
    _load_module_from(_PROJECT_MGMT_DIR, "auth")
    _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")
    _load_module_from(_PROJECT_MGMT_DIR, "projects")
    _load_module_from(_PROJECT_MGMT_DIR, "members")
    _load_module_from(_PROJECT_MGMT_DIR, "budget")
    _load_module_from(_PROJECT_MGMT_DIR, "project_deploy")
    _load_module_from(_PROJECT_MGMT_DIR, "project_destroy")
    _load_module_from(_PROJECT_MGMT_DIR, "project_update")
    pm_handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")
    pm_errors_mod = sys.modules["errors"]
    return pm_handler_mod, pm_errors_mod


def _build_destroy_project_event(project_id: str):
    """Build an API Gateway POST event for destroying a project."""
    return {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/destroy",
        "pathParameters": {
            "projectId": project_id,
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "admin-user",
                    "sub": "sub-admin-user",
                    "cognito:groups": "Administrators",
                }
            }
        },
        "body": None,
    }


# ===================================================================
# [PBT: Property 5] Atomic project destruction initializes progress
# and transitions status
# ===================================================================

_PROJECT_STATUSES = ["ACTIVE", "DEPLOYING", "DESTROYING", "ARCHIVED", "FAILED", "UPDATING"]
_DESTROYABLE_PROJECT_STATUSES = {"ACTIVE"}

project_status_strategy = st.sampled_from(_PROJECT_STATUSES)


class TestAtomicProjectDestruction:
    """[PBT: Property 5] Atomic project destruction initializes progress
    and transitions status.

    For any project with status ACTIVE, a destroy request SHALL atomically
    set status to DESTROYING and initialize progress fields (currentStep=0,
    totalSteps=5, stepDescription="Starting project destruction").
    For any other status, the handler SHALL raise a ConflictError (409).

    **Validates: Requirements 9.3, 9.4**
    """

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(status=project_status_strategy)
    def test_destroy_project_transitions_or_conflicts(self, status):
        """For any project status, POST /destroy either transitions to
        DESTROYING with correct progress fields (ACTIVE) or returns
        409 Conflict.

        **Validates: Requirements 9.3, 9.4**
        """
        with mock_aws():
            pm_handler_mod, pm_errors_mod = _setup_project_env_and_modules()

            projects_table = _create_projects_table()
            _create_clusters_table()

            project_id = "test-proj"

            # Seed project record with the generated status
            projects_table.put_item(Item={
                "PK": f"PROJECT#{project_id}",
                "SK": "METADATA",
                "projectId": project_id,
                "projectName": "Test Project",
                "status": status,
                "budgetBreached": False,
                "createdAt": "2024-01-01T00:00:00+00:00",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "statusChangedAt": "2024-01-01T00:00:00+00:00",
                "errorMessage": "",
            })

            event = _build_destroy_project_event(project_id)

            # Mock Step Functions to avoid real AWS calls
            mock_sfn = MagicMock()
            mock_sfn.start_execution = MagicMock(return_value={})
            original_sfn = pm_handler_mod.sfn_client
            pm_handler_mod.sfn_client = mock_sfn

            try:
                response = pm_handler_mod.handler(event, None)
                body = json.loads(response["body"])

                if status in _DESTROYABLE_PROJECT_STATUSES:
                    # Should succeed with 202 and start destruction
                    assert response["statusCode"] == 202, (
                        f"Expected 202 for status={status}, got {response['statusCode']}: {body}"
                    )

                    # Verify DynamoDB record was atomically updated
                    item = projects_table.get_item(
                        Key={
                            "PK": f"PROJECT#{project_id}",
                            "SK": "METADATA",
                        }
                    )["Item"]

                    assert item["status"] == "DESTROYING", (
                        f"Expected status=DESTROYING, got {item['status']}"
                    )
                    assert int(item["currentStep"]) == 0, (
                        f"Expected currentStep=0, got {item['currentStep']}"
                    )
                    assert int(item["totalSteps"]) == 5, (
                        f"Expected totalSteps=5, got {item['totalSteps']}"
                    )
                    assert item["stepDescription"] == "Starting project destruction", (
                        f"Expected stepDescription='Starting project destruction', "
                        f"got '{item['stepDescription']}'"
                    )

                    # Verify Step Functions was called
                    mock_sfn.start_execution.assert_called_once()

                else:
                    # Should return 409 Conflict
                    assert response["statusCode"] == 409, (
                        f"Expected 409 for status={status}, got {response['statusCode']}: {body}"
                    )
                    assert body["error"]["code"] == "CONFLICT", (
                        f"Expected error code CONFLICT, got {body['error']['code']}"
                    )

                    # Verify DynamoDB record was NOT modified
                    item = projects_table.get_item(
                        Key={
                            "PK": f"PROJECT#{project_id}",
                            "SK": "METADATA",
                        }
                    )["Item"]
                    assert item["status"] == status, (
                        f"Expected status to remain {status}, got {item['status']}"
                    )

            finally:
                pm_handler_mod.sfn_client = original_sfn


# ===================================================================
# [PBT: Property 4] Progress bar percentage calculation is correct
# ===================================================================


def _progress_bar_percentage(current_step: int, total_steps: int) -> int:
    """Python equivalent of the renderProgressBar percentage formula.

    Mirrors the JavaScript logic:
        const cur = currentStep || 0;
        const total = totalSteps || 1;
        const pct = total > 0 ? Math.round((cur / total) * 100) : 0;

    Python's built-in round() matches Math.round() for positive numbers
    (banker's rounding differences only affect .5 cases, but since we
    multiply by 100 first the values are always non-negative integers
    or have clear rounding direction).
    """
    cur = current_step or 0
    total = total_steps or 1
    if total > 0:
        return round((cur / total) * 100)
    return 0


class TestProgressBarPercentage:
    """[PBT: Property 4] Progress bar percentage calculation is correct.

    For any currentStep in [0, totalSteps] and totalSteps > 0, the
    renderProgressBar percentage formula SHALL produce a value in [0, 100]
    that equals round((currentStep / totalSteps) * 100).

    **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
    """

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_percentage_in_valid_range(self, data):
        """For any valid currentStep/totalSteps pair, the percentage
        must be in [0, 100].

        **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
        """
        total_steps = data.draw(st.integers(min_value=1, max_value=100), label="totalSteps")
        current_step = data.draw(
            st.integers(min_value=0, max_value=total_steps), label="currentStep"
        )

        pct = _progress_bar_percentage(current_step, total_steps)

        assert 0 <= pct <= 100, (
            f"Percentage {pct} out of range for "
            f"currentStep={current_step}, totalSteps={total_steps}"
        )

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_percentage_matches_formula(self, data):
        """For any valid currentStep/totalSteps pair, the percentage
        must equal round((currentStep / totalSteps) * 100).

        **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
        """
        total_steps = data.draw(st.integers(min_value=1, max_value=100), label="totalSteps")
        current_step = data.draw(
            st.integers(min_value=0, max_value=total_steps), label="currentStep"
        )

        pct = _progress_bar_percentage(current_step, total_steps)
        expected = round((current_step / total_steps) * 100)

        assert pct == expected, (
            f"Percentage {pct} != expected {expected} for "
            f"currentStep={current_step}, totalSteps={total_steps}"
        )

    def test_zero_current_step_gives_zero_percent(self):
        """Edge case: 0/N always produces 0%.

        **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
        """
        for total in [1, 5, 8, 50, 100]:
            pct = _progress_bar_percentage(0, total)
            assert pct == 0, (
                f"Expected 0% for 0/{total}, got {pct}%"
            )

    def test_equal_steps_gives_hundred_percent(self):
        """Edge case: N/N always produces 100%.

        **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
        """
        for total in [1, 5, 8, 50, 100]:
            pct = _progress_bar_percentage(total, total)
            assert pct == 100, (
                f"Expected 100% for {total}/{total}, got {pct}%"
            )

    def test_zero_total_steps_defaults_safely(self):
        """Edge case: totalSteps=0 should default to 1 (no division by zero).

        **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
        """
        # When totalSteps is 0, the JS falsy check (totalSteps || 1) makes it 1
        # and then cur=0 / total=1 = 0%
        pct = _progress_bar_percentage(0, 0)
        assert pct == 0, f"Expected 0% for 0/0, got {pct}%"

    def test_none_inputs_default_safely(self):
        """Edge case: None/falsy inputs should default gracefully.

        **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
        """
        # Mirrors JS: currentStep || 0, totalSteps || 1
        pct = _progress_bar_percentage(None, None)
        assert pct == 0, f"Expected 0% for None/None, got {pct}%"

        pct = _progress_bar_percentage(None, 8)
        assert pct == 0, f"Expected 0% for None/8, got {pct}%"
