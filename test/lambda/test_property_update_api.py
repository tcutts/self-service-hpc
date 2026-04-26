# Feature: project-update, Property 4: Non-admin callers are rejected from update
"""Property-based test verifying that non-Administrator callers are rejected
from the project update endpoint with a 403 status code, and the project
status remains unchanged.

**Validates: Requirements 2.2**
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

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_identifier = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

NON_ADMIN_GROUPS = [
    "ProjectUser-alpha",
    "ProjectAdmin-alpha",
    "ProjectUser-beta",
    "Viewers",
    "ReadOnly",
]

_non_admin_groups = st.lists(
    st.sampled_from(NON_ADMIN_GROUPS),
    min_size=0,
    max_size=3,
    unique=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_active_project(project_id):
    """Insert a project in ACTIVE status into the mocked DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(PROJECTS_TABLE_NAME).put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": "ACTIVE",
        "errorMessage": "",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
    })


def _build_non_admin_event(caller_username, groups, project_id):
    """Build an API Gateway proxy event for a non-admin caller hitting the update endpoint."""
    groups_str = ", ".join(groups) if groups else ""
    return {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/update",
        "pathParameters": {"projectId": project_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller_username,
                    "sub": f"sub-{caller_username}",
                    "cognito:groups": groups_str,
                }
            }
        },
        "body": None,
    }


# ---------------------------------------------------------------------------
# Property 4: Non-admin callers are rejected from update
# ---------------------------------------------------------------------------

@given(
    caller=_identifier,
    groups=_non_admin_groups,
    project_id=_identifier,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_non_admin_callers_rejected_from_update(caller, groups, project_id):
    """For any non-Administrator caller identity, calling the update endpoint
    returns 403 and the project status remains unchanged.

    **Validates: Requirements 2.2**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
        "USER_POOL_ID": "test-pool-id",
        "BUDGET_SNS_TOPIC_ARN": BUDGET_SNS_TOPIC_ARN,
    })

    projects_table = create_projects_table()
    create_clusters_table()
    create_users_table()

    handler_mod, _, _, _ = reload_project_mgmt_modules()

    # Seed an ACTIVE project
    _seed_active_project(project_id)

    # Build a non-admin event and call the handler
    event = _build_non_admin_event(caller, groups, project_id)
    response = handler_mod.handler(event, None)

    # Assert 403 Forbidden
    assert response["statusCode"] == 403, (
        f"Expected 403 but got {response['statusCode']}"
    )
    response_body = json.loads(response["body"])
    assert response_body["error"]["code"] == "AUTHORISATION_ERROR", (
        f"Expected AUTHORISATION_ERROR but got {response_body['error']['code']}"
    )

    # Assert project status remains ACTIVE (unchanged)
    item = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )["Item"]
    assert item["status"] == "ACTIVE", (
        f"Expected project status to remain ACTIVE but got {item['status']}"
    )


# ---------------------------------------------------------------------------
# Property 5: Only ACTIVE projects can be updated
# ---------------------------------------------------------------------------

NON_ACTIVE_STATUSES = ["CREATED", "DEPLOYING", "UPDATING", "DESTROYING", "ARCHIVED"]

_non_active_status = st.sampled_from(NON_ACTIVE_STATUSES)


def _seed_project_with_status(project_id, status):
    """Insert a project with the given status into the mocked DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(PROJECTS_TABLE_NAME).put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": status,
        "errorMessage": "",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
    })


def _build_admin_event(project_id):
    """Build an API Gateway proxy event for an Administrator calling the update endpoint."""
    return {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/update",
        "pathParameters": {"projectId": project_id},
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


@given(
    project_id=_identifier,
    status=_non_active_status,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_only_active_projects_can_be_updated(project_id, status):
    """For any project status that is not ACTIVE (CREATED, DEPLOYING, UPDATING,
    DESTROYING, ARCHIVED), calling the update endpoint as an Administrator
    returns 409 and the project status remains unchanged.

    **Validates: Requirements 2.3, 3.1**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
        "USER_POOL_ID": "test-pool-id",
        "BUDGET_SNS_TOPIC_ARN": BUDGET_SNS_TOPIC_ARN,
    })

    projects_table = create_projects_table()
    create_clusters_table()
    create_users_table()

    handler_mod, _, _, _ = reload_project_mgmt_modules()

    # Seed a project with a non-ACTIVE status
    _seed_project_with_status(project_id, status)

    # Call the update endpoint as an Administrator
    event = _build_admin_event(project_id)
    response = handler_mod.handler(event, None)

    # Assert 409 Conflict
    assert response["statusCode"] == 409, (
        f"Expected 409 for status '{status}' but got {response['statusCode']}"
    )
    response_body = json.loads(response["body"])
    assert response_body["error"]["code"] == "CONFLICT", (
        f"Expected CONFLICT error code but got {response_body['error']['code']}"
    )

    # Assert project status remains unchanged
    item = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )["Item"]
    assert item["status"] == status, (
        f"Expected project status to remain '{status}' but got '{item['status']}'"
    )


# ---------------------------------------------------------------------------
# Property 6: Valid update triggers transition and returns 202
# ---------------------------------------------------------------------------


@given(
    project_id=_identifier,
    admin_caller=_identifier,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_valid_update_triggers_transition_and_returns_202(project_id, admin_caller):
    """For any ACTIVE project and any Administrator caller, calling update
    transitions to UPDATING, sets currentStep=0, totalSteps=5, and returns 202.

    **Validates: Requirements 2.5, 2.6**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "USERS_TABLE_NAME": USERS_TABLE_NAME,
        "USER_POOL_ID": "test-pool-id",
        "BUDGET_SNS_TOPIC_ARN": BUDGET_SNS_TOPIC_ARN,
    })

    projects_table = create_projects_table()
    create_clusters_table()
    create_users_table()

    handler_mod, _, _, _ = reload_project_mgmt_modules()

    # Seed an ACTIVE project
    _seed_active_project(project_id)

    # Build an admin event with the generated caller name
    event = {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/update",
        "pathParameters": {"projectId": project_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": admin_caller,
                    "sub": f"sub-{admin_caller}",
                    "cognito:groups": "Administrators",
                }
            }
        },
        "body": None,
    }

    response = handler_mod.handler(event, None)

    # Assert 202 Accepted
    assert response["statusCode"] == 202, (
        f"Expected 202 but got {response['statusCode']}"
    )
    response_body = json.loads(response["body"])
    assert response_body["status"] == "UPDATING", (
        f"Expected response status 'UPDATING' but got '{response_body['status']}'"
    )
    assert response_body["projectId"] == project_id

    # Verify project transitioned to UPDATING in DynamoDB
    item = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )["Item"]
    assert item["status"] == "UPDATING", (
        f"Expected project status 'UPDATING' but got '{item['status']}'"
    )

    # Verify progress tracking fields
    assert int(item["currentStep"]) == 0, (
        f"Expected currentStep=0 but got {item['currentStep']}"
    )
    assert int(item["totalSteps"]) == 5, (
        f"Expected totalSteps=5 but got {item['totalSteps']}"
    )
