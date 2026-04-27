# Feature: bulk-actions-ui, Property 10: Batch error isolation
"""Property-based tests verifying batch error isolation.

Property 10: Batch error isolation — failures do not block remaining items.
For any batch of N identifiers where some items will fail (due to wrong status,
non-existence, or other errors), the batch endpoint still processes all N items
and returns exactly N result entries. The number of "success" entries equals the
number of eligible items, regardless of how many items failed.

**Validates: Requirements 10.1**
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

# An item in the batch: (project_id, status_or_None)
# status=None means the project does NOT exist in DynamoDB (triggers NotFoundError)
item_strategy = st.tuples(
    st.text(
        min_size=3,
        max_size=12,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    ),
    st.one_of(st.sampled_from(ALL_STATUSES), st.none()),
)

# Generate 1-10 items with unique IDs, mix of existing and non-existing
batch_strategy = st.lists(
    item_strategy,
    min_size=1,
    max_size=10,
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


# ---------------------------------------------------------------------------
# Property 10a: Batch update — failures do not block remaining items
# ---------------------------------------------------------------------------

@given(items=batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_update_error_isolation(items):
    """For any batch containing a mix of existing projects (various statuses)
    and non-existing project IDs, batch update processes ALL N items and
    returns exactly N result entries. Failures (NotFoundError for missing
    projects, ConflictError for wrong status) do not block subsequent items.

    **Validates: Requirements 10.1**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    # Seed only projects that "exist" (status is not None)
    for pid, status in items:
        if status is not None:
            _seed_project(projects_table, pid, status)

    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in items]
    n = len(project_ids)
    event = build_admin_event(
        "POST",
        "/projects/batch/update",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Core property: exactly N results returned
    assert len(results) == n, (
        f"Expected {n} results, got {len(results)}. "
        f"Failures must not block remaining items."
    )

    # All input IDs are represented
    result_ids = [r["id"] for r in results]
    assert set(result_ids) == set(project_ids)

    # Summary is consistent
    assert summary["total"] == n
    assert summary["succeeded"] + summary["failed"] == n

    # Verify eligible items succeeded and ineligible failed
    expected_success = sum(1 for _, status in items if status == "ACTIVE")
    assert summary["succeeded"] == expected_success


# ---------------------------------------------------------------------------
# Property 10b: Batch deploy — failures do not block remaining items
# ---------------------------------------------------------------------------

@given(items=batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_deploy_error_isolation(items):
    """For any batch containing a mix of existing projects (various statuses)
    and non-existing project IDs, batch deploy processes ALL N items and
    returns exactly N result entries. Failures do not block subsequent items.

    **Validates: Requirements 10.1**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status in items:
        if status is not None:
            _seed_project(projects_table, pid, status)

    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in items]
    n = len(project_ids)
    event = build_admin_event(
        "POST",
        "/projects/batch/deploy",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Core property: exactly N results returned
    assert len(results) == n, (
        f"Expected {n} results, got {len(results)}. "
        f"Failures must not block remaining items."
    )

    result_ids = [r["id"] for r in results]
    assert set(result_ids) == set(project_ids)

    assert summary["total"] == n
    assert summary["succeeded"] + summary["failed"] == n

    expected_success = sum(1 for _, status in items if status == "CREATED")
    assert summary["succeeded"] == expected_success


# ---------------------------------------------------------------------------
# Property 10c: Batch destroy — failures do not block remaining items
# ---------------------------------------------------------------------------

@given(items=batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_destroy_error_isolation(items):
    """For any batch containing a mix of existing projects (various statuses)
    and non-existing project IDs, batch destroy processes ALL N items and
    returns exactly N result entries. Failures do not block subsequent items.

    **Validates: Requirements 10.1**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status in items:
        if status is not None:
            _seed_project(projects_table, pid, status)

    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in items]
    n = len(project_ids)
    event = build_admin_event(
        "POST",
        "/projects/batch/destroy",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Core property: exactly N results returned
    assert len(results) == n, (
        f"Expected {n} results, got {len(results)}. "
        f"Failures must not block remaining items."
    )

    result_ids = [r["id"] for r in results]
    assert set(result_ids) == set(project_ids)

    assert summary["total"] == n
    assert summary["succeeded"] + summary["failed"] == n

    # For destroy, eligible = ACTIVE with no clusters (we seeded none)
    expected_success = sum(1 for _, status in items if status == "ACTIVE")
    assert summary["succeeded"] == expected_success
