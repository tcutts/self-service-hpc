# Feature: self-service-hpc, Property 5: Project-scoped operations reject unauthorised users
"""Property-based test verifying that project-scoped cluster operations
(cluster destruction, cluster access) reject callers who are not authorised
for the target project.

A user is unauthorised if they belong to none of the following Cognito groups
for the target project: ProjectUser-{projectId}, ProjectAdmin-{projectId},
or Administrators.

**Validates: Requirements 7.4, 8.6**
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
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    create_projects_table,
    create_clusters_table,
    create_cluster_name_registry_table,
    reload_cluster_ops_handler_modules,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_identifier = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

_cluster_name = st.from_regex(r"[a-zA-Z0-9_-]{1,20}", fullmatch=True)

# Project-scoped operations that require project membership:
#   DELETE /projects/{projectId}/clusters/{clusterName}  — cluster destruction
#   GET    /projects/{projectId}/clusters/{clusterName}  — cluster access/details
PROJECT_SCOPED_OPERATIONS = [
    ("DELETE", "/projects/{projectId}/clusters/{clusterName}"),
    ("GET", "/projects/{projectId}/clusters/{clusterName}"),
]
_operation = st.sampled_from(PROJECT_SCOPED_OPERATIONS)


@st.composite
def _unauthorised_scenario(draw):
    """Generate a scenario where the caller has NO authorisation for the target project.

    The caller's groups are drawn from groups that reference *other* projects,
    ensuring they never include ProjectUser-{projectId}, ProjectAdmin-{projectId},
    or Administrators.
    """
    caller = draw(_identifier)
    project_id = draw(_identifier)
    cluster_name = draw(_cluster_name)
    operation = draw(_operation)

    # Build groups that are guaranteed NOT to grant access to project_id
    other_project_groups = [
        "ProjectUser-otherproject",
        "ProjectAdmin-otherproject",
        "ProjectUser-unrelated",
        "Viewers",
        "ReadOnly",
    ]
    groups = draw(
        st.lists(st.sampled_from(other_project_groups), min_size=0, max_size=3, unique=True)
    )

    return {
        "caller": caller,
        "project_id": project_id,
        "cluster_name": cluster_name,
        "operation": operation,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_project(projects_table, project_id):
    """Insert a project record so budget-breach checks don't raise NotFoundError."""
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


def _seed_cluster(clusters_table, project_id, cluster_name):
    """Insert an ACTIVE cluster record so GET/DELETE don't fail with NotFoundError."""
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "templateId": "cpu-general",
        "status": "ACTIVE",
        "loginNodeIp": "10.0.1.100",
        "sshPort": 22,
        "dcvPort": 8443,
        "createdBy": "owner-user",
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _build_unauthorised_event(http_method, resource, caller, groups, project_id, cluster_name):
    """Build an API Gateway proxy event for an unauthorised caller."""
    groups_str = ", ".join(groups) if groups else ""
    return {
        "httpMethod": http_method,
        "resource": resource,
        "pathParameters": {
            "projectId": project_id,
            "clusterName": cluster_name,
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": groups_str,
                }
            }
        },
        "body": None,
    }


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------

@given(scenario=_unauthorised_scenario())
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_project_scoped_operations_reject_unauthorised_users(scenario):
    """For any user who is not authorised for a given project (neither
    ProjectUser nor ProjectAdmin nor Administrator), and for any
    project-scoped operation (cluster destruction, cluster access),
    the Web Portal SHALL reject the request with an authorisation error.

    **Validates: Requirements 7.4, 8.6**
    """
    caller = scenario["caller"]
    project_id = scenario["project_id"]
    cluster_name = scenario["cluster_name"]
    http_method, resource = scenario["operation"]
    groups = scenario["groups"]

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

    # Seed project and cluster so the auth check is the first gate
    _seed_project(projects_table, project_id)
    _seed_cluster(clusters_table, project_id, cluster_name)

    event = _build_unauthorised_event(
        http_method, resource, caller, groups, project_id, cluster_name,
    )
    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 403

    body = json.loads(response["body"])
    assert body["error"]["code"] == "AUTHORISATION_ERROR"
