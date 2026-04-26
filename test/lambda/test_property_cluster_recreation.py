# Feature: cluster-recreation, Properties 1-4: Cluster recreation validation
"""Property-based tests for cluster recreation: template resolution,
status validation, budget enforcement, and authorisation.

**Validates: Requirements 1.2, 1.3, 2.3, 3.2, 4.1, 4.2**
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

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

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

username_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

non_destroyed_status_strategy = st.sampled_from(
    ["CREATING", "ACTIVE", "FAILED", "DESTROYING"]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_destroyed_cluster(clusters_table, project_id, cluster_name, template_id):
    """Insert a cluster record in DESTROYED status."""
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": template_id,
        "status": "DESTROYED",
        "createdBy": "original-creator",
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster_with_status(clusters_table, project_id, cluster_name, status):
    """Insert a cluster record with the given status."""
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": "tpl-default",
        "status": status,
        "createdBy": "original-creator",
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_non_breached_project(projects_table, project_id):
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
    })


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
    })


def _build_recreate_event(project_id, cluster_name, caller_groups, body=None):
    """Build an API Gateway event for cluster recreation."""
    return {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/clusters/{clusterName}/recreate",
        "pathParameters": {
            "projectId": project_id,
            "clusterName": cluster_name,
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "test-user",
                    "sub": "sub-test-user",
                    "cognito:groups": caller_groups,
                }
            }
        },
        "body": json.dumps(body) if body else None,
    }


def _setup_env():
    """Set environment variables for mocked AWS resources."""
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


def _create_mock_state_machine():
    """Create a mock Step Functions state machine for successful recreation tests."""
    import boto3 as _boto3
    sfn = _boto3.client("stepfunctions", region_name=AWS_REGION)
    sfn.create_state_machine(
        name="test",
        definition=json.dumps({
            "StartAt": "Pass",
            "States": {"Pass": {"Type": "Pass", "End": True}},
        }),
        roleArn="arn:aws:iam::123456789012:role/test-role",
    )


# ---------------------------------------------------------------------------
# Property 1: Template resolution uses override when provided, stored value
#              otherwise
# ---------------------------------------------------------------------------


@given(
    project_id=project_id_strategy,
    cluster_name=cluster_name_strategy,
    stored_template=template_id_strategy,
    override_template=st.one_of(st.just(""), template_id_strategy),
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_template_resolution(
    project_id, cluster_name, stored_template, override_template
):
    """For any destroyed cluster with a stored templateId, recreation SHALL
    use the override templateId when provided, or the stored value otherwise.

    **Validates: Requirements 1.2, 1.3**
    """
    _setup_env()

    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    create_cluster_name_registry_table()
    _create_mock_state_machine()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_destroyed_cluster(clusters_table, project_id, cluster_name, stored_template)
    _seed_non_breached_project(projects_table, project_id)

    body = {"templateId": override_template} if override_template else None
    event = _build_recreate_event(
        project_id,
        cluster_name,
        f"ProjectUser-{project_id}",
        body=body,
    )

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 202

    response_body = json.loads(response["body"])
    expected_template = override_template if override_template else stored_template
    assert response_body["templateId"] == expected_template


# ---------------------------------------------------------------------------
# Property 2: Non-DESTROYED cluster status rejects recreation
# ---------------------------------------------------------------------------


@given(
    project_id=project_id_strategy,
    cluster_name=cluster_name_strategy,
    status=non_destroyed_status_strategy,
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_non_destroyed_status_rejects_recreation(project_id, cluster_name, status):
    """For any cluster whose status is not DESTROYED, a recreation request
    SHALL be rejected with HTTP 409 Conflict.

    **Validates: Requirements 2.3**
    """
    _setup_env()

    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_cluster_with_status(clusters_table, project_id, cluster_name, status)
    _seed_non_breached_project(projects_table, project_id)

    event = _build_recreate_event(
        project_id,
        cluster_name,
        f"ProjectUser-{project_id}",
    )

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 409

    body = json.loads(response["body"])
    assert body["error"]["code"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Property 3: Budget breach blocks cluster recreation
# ---------------------------------------------------------------------------


@given(
    project_id=project_id_strategy,
    cluster_name=cluster_name_strategy,
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_budget_breach_blocks_recreation(project_id, cluster_name):
    """For any project whose budget has been breached, a recreation request
    for a DESTROYED cluster SHALL be rejected with HTTP 403 BUDGET_EXCEEDED.

    **Validates: Requirements 3.2**
    """
    _setup_env()

    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_destroyed_cluster(clusters_table, project_id, cluster_name, "tpl-default")
    _seed_breached_project(projects_table, project_id)

    event = _build_recreate_event(
        project_id,
        cluster_name,
        f"ProjectUser-{project_id}",
    )

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 403

    body = json.loads(response["body"])
    assert body["error"]["code"] == "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# Property 4: Unauthorised caller cannot recreate clusters
# ---------------------------------------------------------------------------


@given(
    project_id=project_id_strategy,
    cluster_name=cluster_name_strategy,
    caller_username=username_strategy,
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_unauthorised_caller_rejected(project_id, cluster_name, caller_username):
    """For any caller whose groups do not include the target project,
    a recreation request SHALL be rejected with HTTP 403 AUTHORISATION_ERROR.

    **Validates: Requirements 4.1, 4.2**
    """
    _setup_env()

    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_destroyed_cluster(clusters_table, project_id, cluster_name, "tpl-default")
    _seed_non_breached_project(projects_table, project_id)

    # Build event where caller belongs to a different project
    event = {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/clusters/{clusterName}/recreate",
        "pathParameters": {
            "projectId": project_id,
            "clusterName": cluster_name,
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller_username,
                    "sub": f"sub-{caller_username}",
                    "cognito:groups": "ProjectUser-unrelated-project",
                }
            }
        },
        "body": None,
    }

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 403

    body = json.loads(response["body"])
    assert body["error"]["code"] == "AUTHORISATION_ERROR"
