# Feature: self-service-hpc, Property 18: Non-ACTIVE clusters do not expose login credentials
"""Property-based test verifying that clusters not in ACTIVE status do not
expose SSH or DCV connection information in the GET cluster detail response.

**Validates: Requirements 8.7, 19.6**
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

# Non-ACTIVE statuses only — the property asserts these never expose credentials
non_active_status_strategy = st.sampled_from(["CREATING", "FAILED", "DESTROYING", "DESTROYED"])


def _seed_project(projects_table, project_id):
    """Insert a project record with budgetBreached=False."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": "ACTIVE",
        "budgetBreached": False,
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster(clusters_table, project_id, cluster_name, status):
    """Insert a cluster record with connection fields populated in DynamoDB.

    Even though the raw DynamoDB record contains loginNodeIp, sshPort, and
    dcvPort, the handler must NOT expose connectionInfo for non-ACTIVE clusters.
    """
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "templateId": "cpu-general",
        "status": status,
        "createdBy": "test-user",
        "createdAt": "2024-01-01T00:00:00+00:00",
        # Include connection fields to prove the handler filters them out
        "loginNodeIp": "10.0.1.100",
        "sshPort": 22,
        "dcvPort": 8443,
    }
    if status == "CREATING":
        item.update({
            "currentStep": 3,
            "totalSteps": 10,
            "stepDescription": "Creating FSx filesystem",
        })
    if status == "FAILED":
        item["errorMessage"] = "PCS cluster creation failed"
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
    cluster_status=non_active_status_strategy,
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_non_active_clusters_do_not_expose_credentials(
    project_id, cluster_name, cluster_status
):
    """For any cluster not in ACTIVE status, the GET cluster detail response
    SHALL NOT include SSH or DCV connection information.

    **Validates: Requirements 8.7, 19.6**
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

    # Seed a project (budget NOT breached) and a cluster in non-ACTIVE status
    _seed_project(projects_table, project_id)
    _seed_cluster(clusters_table, project_id, cluster_name, cluster_status)

    event = _build_get_cluster_event(project_id, cluster_name)
    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 200, (
        f"Expected 200 for {cluster_status} cluster, got {response['statusCode']}: "
        f"{response['body']}"
    )

    body = json.loads(response["body"])

    # The response MUST NOT contain connectionInfo for non-ACTIVE clusters
    assert "connectionInfo" not in body, (
        f"Cluster in {cluster_status} status should not expose connectionInfo, "
        f"but response contained: {body.get('connectionInfo')}"
    )
