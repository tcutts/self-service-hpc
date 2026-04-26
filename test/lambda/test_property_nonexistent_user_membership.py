# Feature: self-service-hpc, Property 8: Non-existent user cannot be added to a project
"""Property-based test verifying that adding a non-existent user to a
project is rejected with a descriptive error message.

**Validates: Requirements 4.3**
"""

import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
import pytest

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

_identifier = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)


def _seed_project(projects_table, project_id):
    """Seed a project record in DynamoDB."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    })


@given(project_id=_identifier, nonexistent_user_id=_identifier)
@settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_nonexistent_user_cannot_be_added_to_project(project_id, nonexistent_user_id):
    """For any user identifier not present on the platform, adding that user
    to any project SHALL be rejected with a descriptive error mentioning the user.

    **Validates: Requirements 4.3**
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
    })

    projects_table = create_projects_table()
    create_clusters_table()
    create_users_table()
    pool_id = create_cognito_pool()
    os.environ["USER_POOL_ID"] = pool_id
    os.environ["BUDGET_SNS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123456789012:budget-topic"

    _, _, members_mod, errors_mod = reload_project_mgmt_modules()

    # Seed the project but do NOT seed the user
    _seed_project(projects_table, project_id)

    with pytest.raises(errors_mod.NotFoundError) as exc_info:
        members_mod.add_member(
            projects_table_name=PROJECTS_TABLE_NAME,
            users_table_name=USERS_TABLE_NAME,
            user_pool_id=pool_id,
            project_id=project_id,
            user_id=nonexistent_user_id,
        )

    # Error message must mention the non-existent user
    assert nonexistent_user_id in exc_info.value.message
