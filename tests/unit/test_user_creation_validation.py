"""Unit tests for backend validation integration in create_user().

Feature: posix-username-validation

Verifies that ``create_user()`` in ``lambda/user_management/users.py``
rejects invalid POSIX usernames with ``ValidationError`` *before* calling
Cognito or DynamoDB, and proceeds normally for valid usernames.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""

import os

import boto3
import pytest
from moto import mock_aws

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(os.path.dirname(__file__), "..", "conftest.py"))
_tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_tc)
load_lambda_module = _tc.load_lambda_module

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AWS_REGION = "us-east-1"
USERS_TABLE_NAME = "PlatformUsers"


def _create_users_table():
    """Create the mocked PlatformUsers DynamoDB table with POSIX counter."""
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
    table.meta.client.get_waiter("table_exists").wait(
        TableName=USERS_TABLE_NAME,
    )
    # Seed the POSIX UID counter
    table.put_item(
        Item={"PK": "COUNTER", "SK": "POSIX_UID", "currentValue": 10000},
    )
    return table


def _create_cognito_pool() -> str:
    """Create a mocked Cognito User Pool and return its ID."""
    client = boto3.client("cognito-idp", region_name=AWS_REGION)
    response = client.create_user_pool(PoolName="TestPool")
    pool_id = response["UserPool"]["Id"]
    client.create_group(
        GroupName="Administrators",
        UserPoolId=pool_id,
    )
    return pool_id


# ---------------------------------------------------------------------------
# Test class — Invalid usernames raise ValidationError
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestCreateUserRejectsInvalidUsernames:
    """Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set up moto DynamoDB + Cognito and reload user_management modules."""
        with mock_aws():
            self.table = _create_users_table()
            self.pool_id = _create_cognito_pool()

            # Load shared modules first, then user_management
            load_lambda_module("shared", "validators")
            load_lambda_module("shared", "authorization")
            self.errors_mod = load_lambda_module("user_management", "errors")
            self.users_mod = load_lambda_module("user_management", "users")

            # Capture references to the Cognito client and DynamoDB table
            # used by the users module so we can verify they were NOT called.
            self.cognito_client = self.users_mod.cognito
            self.dynamo_table = self.users_mod.dynamodb.Table(
                USERS_TABLE_NAME,
            )

            yield

    # -- Empty userId (Requirement 1.2) ------------------------------------

    def test_empty_userid_raises_validation_error(self) -> None:
        with pytest.raises(self.errors_mod.ValidationError) as exc_info:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="",
                display_name="Test User",
                email="test@example.com",
            )
        assert "userId is required" in str(exc_info.value)

    def test_empty_userid_does_not_call_cognito(self) -> None:
        """Cognito admin_create_user must NOT be called for empty userId."""
        try:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="",
                display_name="Test User",
                email="test@example.com",
            )
        except self.errors_mod.ValidationError:
            pass

        # If Cognito was called, there would be users in the pool.
        response = self.cognito_client.list_users(
            UserPoolId=self.pool_id,
        )
        assert len(response["Users"]) == 0

    def test_empty_userid_does_not_write_dynamodb(self) -> None:
        """DynamoDB put_item must NOT be called for empty userId."""
        try:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="",
                display_name="Test User",
                email="test@example.com",
            )
        except self.errors_mod.ValidationError:
            pass

        # Only the counter seed should exist — no USER# record.
        result = self.table.get_item(
            Key={"PK": "USER#", "SK": "PROFILE"},
        )
        assert "Item" not in result

    # -- Too long userId (Requirement 1.5) ---------------------------------

    def test_too_long_userid_raises_validation_error(self) -> None:
        long_name = "a" * 33
        with pytest.raises(self.errors_mod.ValidationError) as exc_info:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id=long_name,
                display_name="Test User",
                email="test@example.com",
            )
        assert "at most 32 characters" in str(exc_info.value)

    def test_too_long_userid_does_not_call_cognito(self) -> None:
        try:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="a" * 33,
                display_name="Test User",
                email="test@example.com",
            )
        except self.errors_mod.ValidationError:
            pass

        response = self.cognito_client.list_users(
            UserPoolId=self.pool_id,
        )
        assert len(response["Users"]) == 0

    # -- Invalid start character (Requirement 1.4) -------------------------

    def test_digit_start_raises_validation_error(self) -> None:
        with pytest.raises(self.errors_mod.ValidationError) as exc_info:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="1user",
                display_name="Test User",
                email="test@example.com",
            )
        assert "must start with a lowercase letter" in str(exc_info.value)

    def test_digit_start_does_not_call_cognito(self) -> None:
        try:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="1user",
                display_name="Test User",
                email="test@example.com",
            )
        except self.errors_mod.ValidationError:
            pass

        response = self.cognito_client.list_users(
            UserPoolId=self.pool_id,
        )
        assert len(response["Users"]) == 0

    # -- Invalid characters (Requirement 1.3, 1.6) -------------------------

    def test_at_sign_raises_validation_error(self) -> None:
        with pytest.raises(self.errors_mod.ValidationError) as exc_info:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="user@corp",
                display_name="Test User",
                email="test@example.com",
            )
        assert "invalid characters" in str(exc_info.value)

    def test_at_sign_does_not_call_cognito(self) -> None:
        try:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="user@corp",
                display_name="Test User",
                email="test@example.com",
            )
        except self.errors_mod.ValidationError:
            pass

        response = self.cognito_client.list_users(
            UserPoolId=self.pool_id,
        )
        assert len(response["Users"]) == 0

    def test_at_sign_does_not_write_dynamodb(self) -> None:
        try:
            self.users_mod.create_user(
                table_name=USERS_TABLE_NAME,
                user_pool_id=self.pool_id,
                user_id="user@corp",
                display_name="Test User",
                email="test@example.com",
            )
        except self.errors_mod.ValidationError:
            pass

        result = self.table.get_item(
            Key={"PK": "USER#user@corp", "SK": "PROFILE"},
        )
        assert "Item" not in result


# ---------------------------------------------------------------------------
# Test class — Valid usernames proceed normally
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_aws_env_vars")
class TestCreateUserAcceptsValidUsernames:
    """Validates: Requirements 1.1, 1.7"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set up moto DynamoDB + Cognito and reload user_management modules."""
        with mock_aws():
            self.table = _create_users_table()
            self.pool_id = _create_cognito_pool()

            load_lambda_module("shared", "validators")
            load_lambda_module("shared", "authorization")
            self.errors_mod = load_lambda_module("user_management", "errors")
            self.users_mod = load_lambda_module("user_management", "users")

            yield

    def test_valid_username_creates_user_record(self) -> None:
        result = self.users_mod.create_user(
            table_name=USERS_TABLE_NAME,
            user_pool_id=self.pool_id,
            user_id="jsmith",
            display_name="John Smith",
            email="jsmith@example.com",
        )
        assert result["userId"] == "jsmith"
        assert result["status"] == "ACTIVE"
        assert "posixUid" in result
        assert "cognitoSub" in result

    def test_valid_username_stored_in_dynamodb(self) -> None:
        self.users_mod.create_user(
            table_name=USERS_TABLE_NAME,
            user_pool_id=self.pool_id,
            user_id="jsmith",
            display_name="John Smith",
            email="jsmith@example.com",
        )
        item = self.table.get_item(
            Key={"PK": "USER#jsmith", "SK": "PROFILE"},
        )
        assert "Item" in item
        assert item["Item"]["userId"] == "jsmith"

    def test_valid_username_creates_cognito_user(self) -> None:
        self.users_mod.create_user(
            table_name=USERS_TABLE_NAME,
            user_pool_id=self.pool_id,
            user_id="jsmith",
            display_name="John Smith",
            email="jsmith@example.com",
        )
        cognito = self.users_mod.cognito
        response = cognito.list_users(UserPoolId=self.pool_id)
        usernames = [u["Username"] for u in response["Users"]]
        assert "jsmith" in usernames


# ---------------------------------------------------------------------------
# Fixture — fake AWS credentials
# ---------------------------------------------------------------------------

@pytest.fixture
def _aws_env_vars(monkeypatch):
    """Set fake AWS credentials and region for moto."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
