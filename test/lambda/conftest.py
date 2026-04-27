"""Shared pytest fixtures for Lambda test infrastructure.

Provides mocked AWS resources (DynamoDB tables, Cognito User Pool) that can
be scoped per-class or per-module to avoid recreating identical infrastructure
for every individual test.  This dramatically reduces test runtime.

Usage in unit tests:
    class TestSomething:
        def test_foo(self, user_mgmt_env):
            handler_mod, users_mod, errors_mod = user_mgmt_env["modules"]
            table = user_mgmt_env["table"]
            ...

Usage in property tests:
    Property tests that use @mock_aws per-example still need their own setup
    because Hypothesis controls the mock lifecycle.  Import the helper
    functions directly for those cases.
"""

import importlib
import os
import sys

import boto3
from moto import mock_aws
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AWS_REGION = "us-east-1"
USERS_TABLE_NAME = "PlatformUsers"
PROJECTS_TABLE_NAME = "Projects"
CLUSTERS_TABLE_NAME = "Clusters"
TEMPLATES_TABLE_NAME = "ClusterTemplates"
CLUSTER_NAME_REGISTRY_TABLE_NAME = "ClusterNameRegistry"

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_USER_MGMT_DIR = os.path.join(_LAMBDA_ROOT, "user_management")
_PROJECT_MGMT_DIR = os.path.join(_LAMBDA_ROOT, "project_management")
_TEMPLATE_MGMT_DIR = os.path.join(_LAMBDA_ROOT, "template_management")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_ROOT, "cluster_operations")
_ACCOUNTING_DIR = os.path.join(_LAMBDA_ROOT, "accounting")
_BUDGET_NOTIFICATION_DIR = os.path.join(_LAMBDA_ROOT, "budget_notification")
_FSX_CLEANUP_DIR = os.path.join(_LAMBDA_ROOT, "fsx_cleanup")


def _load_module_from(directory: str, module_name: str):
    """Load (or reload) a module by file path, avoiding sys.path collisions.

    Both Lambda packages share identically-named files (handler.py, errors.py,
    auth.py).  Using importlib.util.spec_from_file_location lets us load the
    correct one without relying on sys.path ordering.
    """
    filepath = os.path.join(directory, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # register so intra-package imports resolve
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Table creation helpers (also usable from property tests)
# ---------------------------------------------------------------------------

def create_users_table() -> "boto3.resource.Table":
    """Create the mocked PlatformUsers DynamoDB table with POSIX counter seed."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "StatusIndex",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "PK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=USERS_TABLE_NAME)
    table.put_item(Item={"PK": "COUNTER", "SK": "POSIX_UID", "currentValue": 10000})
    return table


def create_projects_table() -> "boto3.resource.Table":
    """Create the mocked Projects DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=PROJECTS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=PROJECTS_TABLE_NAME)
    return table


def create_clusters_table() -> "boto3.resource.Table":
    """Create the mocked Clusters DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=CLUSTERS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=CLUSTERS_TABLE_NAME)
    return table


def create_templates_table() -> "boto3.resource.Table":
    """Create the mocked ClusterTemplates DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=TEMPLATES_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=TEMPLATES_TABLE_NAME)
    return table


def create_cluster_name_registry_table() -> "boto3.resource.Table":
    """Create the mocked ClusterNameRegistry DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.create_table(
        TableName=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(
        TableName=CLUSTER_NAME_REGISTRY_TABLE_NAME
    )
    return table


def create_cognito_pool() -> str:
    """Create a mocked Cognito User Pool with Administrators group and return its ID."""
    client = boto3.client("cognito-idp", region_name=AWS_REGION)
    response = client.create_user_pool(PoolName="TestPool")
    pool_id = response["UserPool"]["Id"]
    client.create_group(
        GroupName="Administrators",
        UserPoolId=pool_id,
        Description="Platform administrators with full management access",
    )
    return pool_id


def reload_user_mgmt_modules():
    """Load user management Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with the
    project_management package which has identically-named files.
    """
    errors_mod = _load_module_from(_USER_MGMT_DIR, "errors")
    auth_mod = _load_module_from(_USER_MGMT_DIR, "auth")
    users_mod = _load_module_from(_USER_MGMT_DIR, "users")
    handler_mod = _load_module_from(_USER_MGMT_DIR, "handler")
    return handler_mod, users_mod, errors_mod


def reload_project_mgmt_modules():
    """Load project management Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with the
    user_management package which has identically-named files.
    """
    errors_mod = _load_module_from(_PROJECT_MGMT_DIR, "errors")
    auth_mod = _load_module_from(_PROJECT_MGMT_DIR, "auth")
    lifecycle_mod = _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")
    projects_mod = _load_module_from(_PROJECT_MGMT_DIR, "projects")
    members_mod = _load_module_from(_PROJECT_MGMT_DIR, "members")
    budget_mod = _load_module_from(_PROJECT_MGMT_DIR, "budget")
    handler_mod = _load_module_from(_PROJECT_MGMT_DIR, "handler")
    return handler_mod, projects_mod, members_mod, errors_mod


def reload_template_mgmt_modules():
    """Load template management Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with other
    Lambda packages which have identically-named files.
    """
    errors_mod = _load_module_from(_TEMPLATE_MGMT_DIR, "errors")
    auth_mod = _load_module_from(_TEMPLATE_MGMT_DIR, "auth")
    templates_mod = _load_module_from(_TEMPLATE_MGMT_DIR, "templates")
    ami_lookup_mod = _load_module_from(_TEMPLATE_MGMT_DIR, "ami_lookup")
    handler_mod = _load_module_from(_TEMPLATE_MGMT_DIR, "handler")
    return handler_mod, templates_mod, errors_mod


def reload_cluster_ops_modules():
    """Load cluster operations Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with other
    Lambda packages which have identically-named files.
    """
    errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
    cluster_names_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    return cluster_names_mod, errors_mod


def reload_cluster_ops_handler_modules():
    """Load all cluster operations Lambda modules including handler.

    Loads the full module graph needed for handler-level tests:
    errors, auth, cluster_names, clusters, tagging, and handler.
    """
    errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
    auth_mod = _load_module_from(_CLUSTER_OPS_DIR, "auth")
    cluster_names_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    clusters_mod = _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    tagging_mod = _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    handler_mod = _load_module_from(_CLUSTER_OPS_DIR, "handler")
    return handler_mod, clusters_mod, auth_mod, errors_mod, tagging_mod


def reload_accounting_modules():
    """Load accounting Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with other
    Lambda packages which have identically-named files.
    """
    errors_mod = _load_module_from(_ACCOUNTING_DIR, "errors")
    auth_mod = _load_module_from(_ACCOUNTING_DIR, "auth")
    accounting_mod = _load_module_from(_ACCOUNTING_DIR, "accounting")
    handler_mod = _load_module_from(_ACCOUNTING_DIR, "handler")
    return handler_mod, accounting_mod, auth_mod, errors_mod


def reload_budget_notification_modules():
    """Load budget notification Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with other
    Lambda packages which have identically-named files.
    """
    handler_mod = _load_module_from(_BUDGET_NOTIFICATION_DIR, "handler")
    return (handler_mod,)


def reload_fsx_cleanup_modules():
    """Load FSx cleanup Lambda modules so boto3 clients bind to moto.

    Uses explicit file-path loading to avoid collisions with other
    Lambda packages which have identically-named files.
    Loads cleanup first (dependency), then handler.
    """
    cleanup_mod = _load_module_from(_FSX_CLEANUP_DIR, "cleanup")
    handler_mod = _load_module_from(_FSX_CLEANUP_DIR, "handler")
    return handler_mod, cleanup_mod


# ---------------------------------------------------------------------------
# Fixtures — AWS environment variables (autouse, session-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _aws_env_vars(monkeypatch):
    """Set fake AWS credentials and region for every test."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ---------------------------------------------------------------------------
# Fixtures — User Management (class-scoped mock_aws)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def user_mgmt_env():
    """Provide a single moto mock_aws context shared across all tests in a class.

    Yields a dict with:
        table:   the PlatformUsers DynamoDB Table resource
        pool_id: the Cognito User Pool ID
        modules: (handler_mod, users_mod, errors_mod)
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"

        table = create_users_table()
        pool_id = create_cognito_pool()

        os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME
        os.environ["USER_POOL_ID"] = pool_id

        handler_mod, users_mod, errors_mod = reload_user_mgmt_modules()

        yield {
            "table": table,
            "pool_id": pool_id,
            "modules": (handler_mod, users_mod, errors_mod),
        }


# ---------------------------------------------------------------------------
# Fixtures — Project Management (class-scoped mock_aws)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def project_mgmt_env():
    """Provide a single moto mock_aws context for project management tests.

    Yields a dict with:
        projects_table: the Projects DynamoDB Table resource
        clusters_table: the Clusters DynamoDB Table resource
        users_table:    the PlatformUsers DynamoDB Table resource
        pool_id:        the Cognito User Pool ID
        modules:        (handler_mod, projects_mod, members_mod, errors_mod)
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"

        projects_table = create_projects_table()
        clusters_table = create_clusters_table()
        users_table = create_users_table()
        pool_id = create_cognito_pool()

        os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
        os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
        os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME
        os.environ["USER_POOL_ID"] = pool_id
        os.environ["BUDGET_SNS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123456789012:budget-topic"

        handler_mod, projects_mod, members_mod, errors_mod = reload_project_mgmt_modules()

        yield {
            "projects_table": projects_table,
            "clusters_table": clusters_table,
            "users_table": users_table,
            "pool_id": pool_id,
            "modules": (handler_mod, projects_mod, members_mod, errors_mod),
        }


# ---------------------------------------------------------------------------
# Fixtures — Template Management (class-scoped mock_aws)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def template_mgmt_env():
    """Provide a single moto mock_aws context for template management tests.

    Yields a dict with:
        table:   the ClusterTemplates DynamoDB Table resource
        modules: (handler_mod, templates_mod, errors_mod)
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"

        table = create_templates_table()
        pool_id = create_cognito_pool()

        os.environ["TEMPLATES_TABLE_NAME"] = TEMPLATES_TABLE_NAME
        os.environ["USER_POOL_ID"] = pool_id

        handler_mod, templates_mod, errors_mod = reload_template_mgmt_modules()

        yield {
            "table": table,
            "modules": (handler_mod, templates_mod, errors_mod),
        }


# ---------------------------------------------------------------------------
# Fixtures — Accounting (class-scoped mock_aws)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def accounting_env():
    """Provide a single moto mock_aws context for accounting query tests.

    Yields a dict with:
        clusters_table: the Clusters DynamoDB Table resource
        projects_table: the Projects DynamoDB Table resource
        modules:        (handler_mod, accounting_mod, auth_mod, errors_mod)
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"

        clusters_table = create_clusters_table()
        projects_table = create_projects_table()

        os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
        os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME

        handler_mod, accounting_mod, auth_mod, errors_mod = reload_accounting_modules()

        yield {
            "clusters_table": clusters_table,
            "projects_table": projects_table,
            "modules": (handler_mod, accounting_mod, auth_mod, errors_mod),
        }


# ---------------------------------------------------------------------------
# Event builder helpers
# ---------------------------------------------------------------------------

def build_admin_event(method, resource, body=None, path_parameters=None):
    """Build an API Gateway proxy event with Administrator claims."""
    import json
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "admin-user",
                    "sub": "sub-admin-user",
                    "cognito:groups": "Administrators",
                }
            }
        },
        "body": json.dumps(body) if body else None,
    }


def build_non_admin_event(method, resource, caller="regular-user", body=None, path_parameters=None):
    """Build an API Gateway proxy event for a non-admin caller."""
    import json
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": "ProjectUser-alpha",
                }
            }
        },
        "body": json.dumps(body) if body else None,
    }


# ---------------------------------------------------------------------------
# Fixtures — Budget Notification (class-scoped mock_aws)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def budget_notification_env():
    """Provide a single moto mock_aws context for budget notification tests.

    Yields a dict with:
        projects_table: the Projects DynamoDB Table resource
        users_table:    the PlatformUsers DynamoDB Table resource
        modules:        (handler_mod,)
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"

        projects_table = create_projects_table()
        users_table = create_users_table()

        os.environ["PROJECTS_TABLE_NAME"] = PROJECTS_TABLE_NAME
        os.environ["USERS_TABLE_NAME"] = USERS_TABLE_NAME

        (handler_mod,) = reload_budget_notification_modules()

        yield {
            "projects_table": projects_table,
            "users_table": users_table,
            "modules": (handler_mod,),
        }
