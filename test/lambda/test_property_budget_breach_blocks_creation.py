# Feature: self-service-hpc, Property 12: Budget breach blocks cluster creation
"""Property-based test verifying that cluster creation is rejected when the
project budget has been breached.

**Validates: Requirements 6.9**
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
template_id_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)


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


def _build_project_user_event(project_id, cluster_name, template_id):
    """Build an API Gateway event for cluster creation by a project user."""
    return {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/clusters",
        "pathParameters": {"projectId": project_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "test-user",
                    "sub": "sub-test-user",
                    "cognito:groups": f"ProjectUser-{project_id}",
                }
            }
        },
        "body": json.dumps({
            "clusterName": cluster_name,
            "templateId": template_id,
        }),
    }


@given(
    project_id=project_id_strategy,
    cluster_name=cluster_name_strategy,
    template_id=template_id_strategy,
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_budget_breach_blocks_cluster_creation(project_id, cluster_name, template_id):
    """For any project whose budget has been breached, a cluster creation
    request SHALL be rejected with a BUDGET_EXCEEDED error.

    **Validates: Requirements 6.9**
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
    create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, errors_mod, _ = reload_cluster_ops_handler_modules()

    _seed_breached_project(projects_table, project_id)

    event = _build_project_user_event(project_id, cluster_name, template_id)
    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 403

    body = json.loads(response["body"])
    assert body["error"]["code"] == "BUDGET_EXCEEDED"
    assert "budget" in body["error"]["message"].lower()
