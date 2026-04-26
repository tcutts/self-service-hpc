# Feature: self-service-hpc, Property 4: Project admin operations reject non-project-administrators
"""Property-based test verifying that project admin operations
(membership management, budget modification) reject callers who
are not Project Administrators for the target project.

**Validates: Requirements 4.4, 5.4**
"""

import json
import os

import boto3
from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    USERS_TABLE_NAME,
    create_projects_table,
    create_clusters_table,
    create_users_table,
    create_cognito_pool,
    reload_project_mgmt_modules,
)

BUDGET_SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:budget-topic"

_identifier = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))

PROJECT_ADMIN_OPERATIONS = [
    ("POST", "/projects/{projectId}/members", True, False),
    ("DELETE", "/projects/{projectId}/members/{userId}", False, True),
    ("PUT", "/projects/{projectId}/budget", True, False),
]
_operation = st.sampled_from(PROJECT_ADMIN_OPERATIONS)


def _seed_project(project_id):
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(PROJECTS_TABLE_NAME).put_item(Item={
        "PK": f"PROJECT#{project_id}", "SK": "METADATA",
        "projectId": project_id, "projectName": f"Project {project_id}",
        "costAllocationTag": project_id, "status": "ACTIVE",
        "budgetLimit": 50, "budgetBreached": False,
    })


def _seed_user(user_id):
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(USERS_TABLE_NAME).put_item(Item={
        "PK": f"USER#{user_id}", "SK": "PROFILE",
        "userId": user_id, "displayName": f"Display {user_id}",
        "status": "ACTIVE", "posixUid": 10001, "posixGid": 10001,
    })


def _seed_membership(project_id, user_id):
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(PROJECTS_TABLE_NAME).put_item(Item={
        "PK": f"PROJECT#{project_id}", "SK": f"MEMBER#{user_id}",
        "userId": user_id, "projectId": project_id,
        "role": "PROJECT_USER", "addedAt": "2024-01-01T00:00:00+00:00",
    })


def _build_event(http_method, resource, caller_username, groups, body=None, path_parameters=None):
    groups_str = ", ".join(groups) if groups else ""
    return {
        "httpMethod": http_method, "resource": resource, "pathParameters": path_parameters,
        "requestContext": {"authorizer": {"claims": {
            "cognito:username": caller_username, "sub": f"sub-{caller_username}",
            "cognito:groups": groups_str,
        }}},
        "body": json.dumps(body) if body else None,
    }


@st.composite
def _non_project_admin_scenario(draw):
    caller = draw(_identifier)
    project_id = draw(_identifier)
    target_user = draw(_identifier)
    operation = draw(_operation)
    safe_groups = [f"ProjectUser-{project_id}", "ProjectAdmin-otherproject", "Viewers", "ReadOnly"]
    groups = draw(st.lists(st.sampled_from(safe_groups), min_size=0, max_size=3, unique=True))
    return {"caller": caller, "project_id": project_id, "target_user": target_user, "operation": operation, "groups": groups}


@given(scenario=_non_project_admin_scenario())
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@mock_aws
def test_project_admin_operations_reject_non_project_administrators(scenario):
    """For any non-project-admin user and any project admin operation,
    the request SHALL be rejected with an authorisation error.

    **Validates: Requirements 4.4, 5.4**
    """
    caller = scenario["caller"]
    project_id = scenario["project_id"]
    target_user = scenario["target_user"]
    http_method, resource, needs_body, needs_path_user = scenario["operation"]
    groups = scenario["groups"]

    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION, "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
        "BUDGET_SNS_TOPIC_ARN": BUDGET_SNS_TOPIC_ARN,
    })

    create_projects_table()
    create_clusters_table()
    create_users_table()
    pool_id = create_cognito_pool()
    os.environ["USER_POOL_ID"] = pool_id

    _seed_project(project_id)
    _seed_user(target_user)
    _seed_membership(project_id, target_user)

    handler_mod, _, _, _ = reload_project_mgmt_modules()

    body = None
    if needs_body and "members" in resource:
        body = {"userId": target_user, "role": "PROJECT_USER"}
    elif needs_body and "budget" in resource:
        body = {"budgetLimit": 1000}

    path_parameters = {"projectId": project_id}
    if needs_path_user:
        path_parameters["userId"] = target_user

    event = _build_event(http_method, resource, caller, groups, body, path_parameters)
    response = handler_mod.handler(event, None)

    assert response["statusCode"] == 403
    response_body = json.loads(response["body"])
    assert response_body["error"]["code"] == "AUTHORISATION_ERROR"
