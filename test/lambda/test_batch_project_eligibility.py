# Feature: bulk-actions-ui, Property 7: Batch project eligibility
"""Property-based tests verifying batch project eligibility.

Property 7: Batch project eligibility — only projects in the required status
succeed. For any batch of project identifiers where each project has a random
status from the lifecycle state machine, the batch update endpoint returns
"success" only for ACTIVE projects, the batch deploy endpoint returns "success"
only for CREATED projects, and the batch destroy endpoint returns "success"
only for ACTIVE projects with no active or creating clusters.

**Validates: Requirements 3.3, 3.6, 4.3, 4.6, 5.4, 5.7**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
from unittest.mock import patch, MagicMock

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    _PROJECT_MGMT_DIR,
    create_projects_table,
    create_clusters_table,
    reload_project_mgmt_modules,
    build_admin_event,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_STATUSES = ["CREATED", "DEPLOYING", "ACTIVE", "UPDATING", "DESTROYING", "ARCHIVED"]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

project_status_strategy = st.sampled_from(ALL_STATUSES)

# Generate 1-5 projects, each with a unique ID and random status
project_batch_strategy = st.lists(
    st.tuples(
        st.text(
            min_size=3,
            max_size=12,
            alphabet=st.characters(whitelist_categories=("L", "N")),
        ),
        project_status_strategy,
    ),
    min_size=1,
    max_size=5,
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
# Property 7a: Batch update — only ACTIVE projects succeed
# ---------------------------------------------------------------------------

@given(projects=project_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_update_only_active_succeed(projects):
    """For any batch of projects with random statuses, batch update returns
    "success" only for projects with status ACTIVE. All others get "error".

    **Validates: Requirements 3.3, 3.6**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    # Seed projects with their assigned statuses
    for pid, status in projects:
        _seed_project(projects_table, pid, status)

    # Re-import handler after reload
    from conftest import _load_module_from
    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in projects]
    event = build_admin_event(
        "POST",
        "/projects/batch/update",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Verify each result matches eligibility
    status_map = {pid: status for pid, status in projects}
    for result in results:
        pid = result["id"]
        if status_map[pid] == "ACTIVE":
            assert result["status"] == "success", (
                f"Project '{pid}' with status ACTIVE should succeed for update, "
                f"got: {result}"
            )
        else:
            assert result["status"] == "error", (
                f"Project '{pid}' with status {status_map[pid]} should fail for update, "
                f"got: {result}"
            )

    # Verify summary counts
    expected_success = sum(1 for _, s in projects if s == "ACTIVE")
    assert summary["total"] == len(projects)
    assert summary["succeeded"] == expected_success
    assert summary["failed"] == len(projects) - expected_success


# ---------------------------------------------------------------------------
# Property 7b: Batch deploy — only CREATED projects succeed
# ---------------------------------------------------------------------------

@given(projects=project_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_deploy_only_created_succeed(projects):
    """For any batch of projects with random statuses, batch deploy returns
    "success" only for projects with status CREATED. All others get "error".

    **Validates: Requirements 4.3, 4.6**
    """
    _setup_env()
    projects_table = create_projects_table()
    create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status in projects:
        _seed_project(projects_table, pid, status)

    from conftest import _load_module_from
    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _ in projects]
    event = build_admin_event(
        "POST",
        "/projects/batch/deploy",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    status_map = {pid: status for pid, status in projects}
    for result in results:
        pid = result["id"]
        if status_map[pid] == "CREATED":
            assert result["status"] == "success", (
                f"Project '{pid}' with status CREATED should succeed for deploy, "
                f"got: {result}"
            )
        else:
            assert result["status"] == "error", (
                f"Project '{pid}' with status {status_map[pid]} should fail for deploy, "
                f"got: {result}"
            )

    expected_success = sum(1 for _, s in projects if s == "CREATED")
    assert summary["total"] == len(projects)
    assert summary["succeeded"] == expected_success
    assert summary["failed"] == len(projects) - expected_success


# ---------------------------------------------------------------------------
# Property 7c: Batch destroy — only ACTIVE projects with no active clusters succeed
# ---------------------------------------------------------------------------

# Strategy for whether a project has active clusters (only relevant for ACTIVE projects)
has_active_clusters_strategy = st.booleans()

# Generate projects with cluster info for destroy tests
project_destroy_batch_strategy = st.lists(
    st.tuples(
        st.text(
            min_size=3,
            max_size=12,
            alphabet=st.characters(whitelist_categories=("L", "N")),
        ),
        project_status_strategy,
        has_active_clusters_strategy,
    ),
    min_size=1,
    max_size=5,
    unique_by=lambda t: t[0],
)


@given(projects=project_destroy_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_destroy_only_active_no_clusters_succeed(projects):
    """For any batch of projects with random statuses, batch destroy returns
    "success" only for projects with status ACTIVE and no active/creating
    clusters. ACTIVE projects with clusters get "error", as do all non-ACTIVE
    projects.

    **Validates: Requirements 5.4, 5.7**
    """
    _setup_env()
    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    reload_project_mgmt_modules()

    for pid, status, has_clusters in projects:
        _seed_project(projects_table, pid, status)
        # If this project should have active clusters, seed one
        if has_clusters and status == "ACTIVE":
            clusters_table.put_item(Item={
                "PK": f"PROJECT#{pid}",
                "SK": "CLUSTER#cluster-1",
                "clusterName": "cluster-1",
                "status": "ACTIVE",
            })

    from conftest import _load_module_from
    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")

    project_ids = [pid for pid, _, _ in projects]
    event = build_admin_event(
        "POST",
        "/projects/batch/destroy",
        body={"projectIds": project_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Build expected eligibility map
    eligible = {}
    for pid, status, has_clusters in projects:
        # Eligible only if ACTIVE and no active clusters
        eligible[pid] = (status == "ACTIVE" and not has_clusters)

    for result in results:
        pid = result["id"]
        if eligible[pid]:
            assert result["status"] == "success", (
                f"Project '{pid}' (ACTIVE, no clusters) should succeed for destroy, "
                f"got: {result}"
            )
        else:
            assert result["status"] == "error", (
                f"Project '{pid}' should fail for destroy, got: {result}"
            )

    expected_success = sum(1 for pid in eligible if eligible[pid])
    assert summary["total"] == len(projects)
    assert summary["succeeded"] == expected_success
    assert summary["failed"] == len(projects) - expected_success
