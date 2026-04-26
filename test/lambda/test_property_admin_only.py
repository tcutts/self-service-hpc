# Feature: self-service-hpc, Property 3: Admin-only operations reject non-administrators
# Feature: user-reactivation, Property 4: Non-administrator reactivation is rejected
"""Property-based test verifying that admin-only user management operations
reject non-administrator callers with an authorisation error.

**Validates: Requirements 1.4, 2.4, 3.4**
**Validates: Requirements 2.1**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    USERS_TABLE_NAME,
    create_users_table,
    create_cognito_pool,
    reload_user_mgmt_modules,
)

# Strategies
caller_strategy = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
target_user_strategy = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))

NON_ADMIN_GROUPS = ["ProjectUser-alpha", "ProjectAdmin-alpha", "ProjectUser-beta", "Viewers", "ReadOnly"]
non_admin_groups_strategy = st.lists(st.sampled_from(NON_ADMIN_GROUPS), min_size=0, max_size=3, unique=True)

ADMIN_ONLY_OPERATIONS = [
    ("POST", "/users", True, False),
    ("DELETE", "/users/{userId}", False, True),
    ("GET", "/users", False, False),
    ("POST", "/users/{userId}/reactivate", False, True),
]
admin_operation_strategy = st.sampled_from(ADMIN_ONLY_OPERATIONS)


def _build_non_admin_event(http_method, resource, caller_username, groups, body=None, path_parameters=None):
    groups_str = ", ".join(groups) if groups else ""
    return {
        "httpMethod": http_method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {"authorizer": {"claims": {
            "cognito:username": caller_username,
            "sub": f"sub-{caller_username}",
            "cognito:groups": groups_str,
        }}},
        "body": json.dumps(body) if body else None,
    }


@given(caller=caller_strategy, groups=non_admin_groups_strategy, operation=admin_operation_strategy, target_user=target_user_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@mock_aws
def test_admin_only_operations_reject_non_administrators(caller, groups, operation, target_user):
    """For any non-admin user and any admin-only operation, the request SHALL
    be rejected with an authorisation error.

    **Validates: Requirements 1.4, 2.4, 3.4**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION, "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing", "USERS_TABLE_NAME": USERS_TABLE_NAME,
    })

    create_users_table()
    pool_id = create_cognito_pool()
    os.environ["USER_POOL_ID"] = pool_id

    handler_mod, _, _ = reload_user_mgmt_modules()

    http_method, resource, needs_body, needs_path_params = operation
    body = {"userId": target_user, "displayName": f"D {target_user}", "email": f"{target_user}@example.com"} if needs_body else None
    path_parameters = {"userId": target_user} if needs_path_params else None

    event = _build_non_admin_event(http_method, resource, caller, groups, body, path_parameters)
    response = handler_mod.handler(event, None)

    assert response["statusCode"] == 403
    response_body = json.loads(response["body"])
    assert response_body["error"]["code"] == "AUTHORISATION_ERROR"
