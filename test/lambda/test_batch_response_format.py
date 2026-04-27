# Feature: bulk-actions-ui, Property 6: Batch response format consistency
"""Property-based tests verifying batch response format consistency.

Property 6: Batch response format consistency — for any valid batch request
containing N identifiers (1 ≤ N ≤ 25), the batch endpoint returns HTTP 200
with a `results` array of exactly N entries, each containing an `id` field,
a `status` field that is either "success" or "error", and a `message` field.
The `summary.total` equals N, and `summary.succeeded + summary.failed` equals N.

**Validates: Requirements 3.5, 4.5, 5.6, 9.1, 9.2, 9.3**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    _PROJECT_MGMT_DIR,
    create_projects_table,
    create_clusters_table,
    reload_project_mgmt_modules,
    build_admin_event,
    _load_module_from,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_STATUSES = ["CREATED", "DEPLOYING", "ACTIVE", "UPDATING", "DESTROYING", "ARCHIVED"]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate 1-25 projects, each with a unique ID and random status
project_batch_strategy = st.lists(
    st.tuples(
        st.text(
            min_size=3,
            max_size=12,
            alphabet=st.characters(whitelist_categories=("L", "N")),
        ),
        st.sampled_from(ALL_STATUSES),
    ),
    min_size=1,
    max_size=25,
    unique_by=lambda t: t[0],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id, status):
    """Insert a project with the given status into the mocked DynamoDB table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "errorMessage": "",
        "currentStep": 0,
        "totalSteps": 0,
        "stepDescription": "",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _setup_env():
    """Set environment variables for mocked AWS."""
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "PROJECT_UPDATE_STATE_MACHINE_ARN": "",
        "PROJECT_DEPLOY_STATE_MACHINE_ARN": "",
        "PROJECT_DESTROY_STATE_MACHINE_ARN": "",
    })


def _parse_batch_response(response):
    """Parse the batch response body and return (results, summary)."""
    body = json.loads(response["body"])
    return body["results"], body["summary"]


def _assert_batch_format(response, expected_n, input_ids):
    """Assert the batch response has the correct format and structure.

    Checks:
    - HTTP 200 status
    - Exactly N results
    - Each result has id, status, message fields
    - status is "success" or "error"
    - summary.total == N
    - summary.succeeded + summary.failed == N
    - All input IDs are present in results
    """
    assert response["statusCode"] == 200, (
        f"Expected HTTP 200, got {response['statusCode']}"
    )

    results, summary = _parse_batch_response(response)

    # Exactly N results
    assert len(results) == expected_n, (
        f"Expected {expected_n} results, got {len(results)}"
    )

    # Each result has required fields with correct types
    result_ids = []
    success_count = 0
    error_count = 0
    for result in results:
        assert "id" in result, f"Result missing 'id' field: {result}"
        assert "status" in result, f"Result missing 'status' field: {result}"
        assert "message" in result, f"Result missing 'message' field: {result}"

        assert result["status"] in ("success", "error"), (
            f"Result status must be 'success' or 'error', got: {result['status']}"
        )

        result_ids.append(result["id"])
        if result["status"] == "success":
            success_count += 1
        else:
            error_count += 1

    # All input IDs are represented in results
    assert set(result_ids) == set(input_ids), (
        f"Result IDs {set(result_ids)} don't match input IDs {set(input_ids)}"
    )

    # Summary checks
    assert summary["total"] == expected_n, (
        f"summary.total should be {expected_n}, got {summary['total']}"
    )
    assert summary["succeeded"] + summary["failed"] == expected_n, (
        f"summary.succeeded ({summary['succeeded']}) + summary.failed ({summary['failed']}) "
        f"should equal {expected_n}"
    )
    assert summary["succeeded"] == success_count, (
        f"summary.succeeded ({summary['succeeded']}) doesn't match actual success count ({success_count})"
    )
    assert summary["failed"] == error_count, (
        f"summary.failed ({summary['failed']}) doesn't match actual error count ({error_count})"
    )


# ---------------------------------------------------------------------------
# Property 6a: Batch update response format consistency
# ---------------------------------------------------------------------------

@given(projects=project_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_update_response_format(projects):
    """For any batch of N project IDs (1-25), batch update returns HTTP 200
    with exactly N results, each having id/status/message fields, status is
    "success" or "error", and summary counts are consistent.

    **Validates: Requirements 3.5, 9.1, 9.2, 9.3**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status in projects:
        _seed_project(projects_table, pid, status)

    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in projects]
    event = build_admin_event(
        "POST",
        "/projects/batch/update",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    _assert_batch_format(response, len(projects), project_ids)


# ---------------------------------------------------------------------------
# Property 6b: Batch deploy response format consistency
# ---------------------------------------------------------------------------

@given(projects=project_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_deploy_response_format(projects):
    """For any batch of N project IDs (1-25), batch deploy returns HTTP 200
    with exactly N results, each having id/status/message fields, status is
    "success" or "error", and summary counts are consistent.

    **Validates: Requirements 4.5, 9.1, 9.2, 9.3**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status in projects:
        _seed_project(projects_table, pid, status)

    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in projects]
    event = build_admin_event(
        "POST",
        "/projects/batch/deploy",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    _assert_batch_format(response, len(projects), project_ids)


# ---------------------------------------------------------------------------
# Property 6c: Batch destroy response format consistency
# ---------------------------------------------------------------------------

@given(projects=project_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_destroy_response_format(projects):
    """For any batch of N project IDs (1-25), batch destroy returns HTTP 200
    with exactly N results, each having id/status/message fields, status is
    "success" or "error", and summary counts are consistent.

    **Validates: Requirements 5.6, 9.1, 9.2, 9.3**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status in projects:
        _seed_project(projects_table, pid, status)

    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in projects]
    event = build_admin_event(
        "POST",
        "/projects/batch/destroy",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    _assert_batch_format(response, len(projects), project_ids)
