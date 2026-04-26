# Feature: self-service-hpc, Property 13: Budget breach blocks cluster access
"""Property-based test verifying that cluster connection detail requests are
denied when the project budget has been breached.

**Validates: Requirements 8.5**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
import pytest

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    create_projects_table,
    create_clusters_table,
    create_cluster_name_registry_table,
    reload_cluster_ops_handler_modules,
)

project_id_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)
cluster_name_strategy = st.from_regex(r"[a-zA-Z0-9_-]{1,20}", fullmatch=True)

# Generate cluster statuses — include ACTIVE to prove even active clusters
# are blocked when budget is breached.
cluster_status_strategy = st.sampled_from(["ACTIVE", "CREATING", "FAILED"])


def _seed_breached_project(projects_table, project_id):
    """Insert a project record with budgetBreached=True."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": "ACTIVE",
        "budgetBreached": True,
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster(clusters_table, project_id, cluster_name, status):
    """Insert a cluster record into the Clusters table."""
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "templateId": "cpu-general",
        "status": status,
        "createdBy": "test-user",
        "createdAt": "2024-01-01T00:00:00+00:00",
    }
    if status == "ACTIVE":
        item.update({
            "loginNodeIp": "10.0.1.100",
            "sshPort": 22,
            "dcvPort": 8443,
        })
    clusters_table.put_item(Item=item)


def _build_get_cluster_event(project_id, cluster_name):
    """Build an API Gateway event for GET /projects/{projectId}/clusters/{clusterName}."""
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


@given(
    project_id=project_id_strategy,
    cluster_name=cluster_name_strategy,
    cluster_status=cluster_status_strategy,
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_budget_breach_blocks_cluster_access(project_id, cluster_name, cluster_status):
    """For any project whose budget has been breached, a request to obtain
    cluster connection details SHALL be denied with a BUDGET_EXCEEDED error.

    **Validates: Requirements 8.5**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "CLUSTER_NAME_REGISTRY_TABLE_NAME": CLUSTER_NAME_REGISTRY_TABLE_NAME,
        "CREATION_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
        "DESTRUCTION_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test-destroy",
    })

    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, errors_mod, _ = reload_cluster_ops_handler_modules()

    # Seed a project with budget breached and a cluster record
    _seed_breached_project(projects_table, project_id)
    _seed_cluster(clusters_table, project_id, cluster_name, cluster_status)

    event = _build_get_cluster_event(project_id, cluster_name)
    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 403

    body = json.loads(response["body"])
    assert body["error"]["code"] == "BUDGET_EXCEEDED"
    assert "budget" in body["error"]["message"].lower()
