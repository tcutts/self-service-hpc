"""Example-based unit tests for the admin provisioner Lambda.

Feature: admin-user-provisioning

Covers:
- Happy path: Create event with no existing admin
- Idempotent skip when admin already exists (Create and Update)
- Delete event no-op
- Cognito FORCE_CHANGE_PASSWORD status
- DynamoDB condition expression on PutItem
- POSIX UID atomic increment
- Rollback on DynamoDB PutItem failure
- Rollback on Cognito group add failure
- FAILED cfnresponse on scan error
- Update with changed email does not create second admin
- Update event never modifies existing credentials

Requirements: 1.1, 1.2, 2.1, 2.3, 2.4, 2.5, 3.1, 3.3, 5.1, 5.2, 5.3,
              6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 8.1, 8.2, 8.3, 8.4, 8.5
"""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AWS_REGION = "us-east-1"
USERS_TABLE_NAME = "PlatformUsers"

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_ADMIN_DIR = os.path.join(_LAMBDA_ROOT, "admin_provisioner")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_admin_provisioner():
    """Load admin provisioner module so boto3 clients bind to moto."""
    filepath = os.path.join(_ADMIN_DIR, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "admin_provisioner_handler", filepath,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_provisioner_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


def _create_users_table():
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
    table.meta.client.get_waiter("table_exists").wait(
        TableName=USERS_TABLE_NAME,
    )
    table.put_item(
        Item={"PK": "COUNTER", "SK": "POSIX_UID", "currentValue": 10000},
    )
    return table


def _set_aws_env():
    """Set fake AWS credentials for moto."""
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


def _create_cognito_pool():
    """Create a mocked Cognito User Pool with Administrators group."""
    client = boto3.client("cognito-idp", region_name=AWS_REGION)
    resp = client.create_user_pool(PoolName="TestPool")
    pool_id = resp["UserPool"]["Id"]
    client.create_group(
        GroupName="Administrators",
        UserPoolId=pool_id,
    )
    return pool_id


class FakeContext:
    """Minimal Lambda context for cfnresponse."""
    log_stream_name = "test-log-stream"


def _build_event(request_type, pool_id, admin_email="admin@example.com"):
    """Build a CloudFormation custom resource event."""
    event = {
        "RequestType": request_type,
        "ResponseURL": "https://example.com/cfn-response",
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/guid",
        "RequestId": "unique-id",
        "LogicalResourceId": "AdminProvisionerResource",
        "ResourceProperties": {
            "TableName": USERS_TABLE_NAME,
            "UserPoolId": pool_id,
            "AdminEmail": admin_email,
        },
    }
    if request_type in ("Update", "Delete"):
        event["PhysicalResourceId"] = "AdminProvisioner-existing"
    return event


def _invoke_handler(mod, event):
    """Invoke handler with urlopen patched, return parsed cfnresponse body."""
    with patch(
        "urllib.request.urlopen", return_value=MagicMock(),
    ) as mock_urlopen:
        mod.handler(event, FakeContext())
    assert mock_urlopen.call_count == 1
    sent_request = mock_urlopen.call_args[0][0]
    return json.loads(sent_request.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Test 1: Happy path — Create event, no existing admin
# ---------------------------------------------------------------------------


class TestCreateEventNoExistingAdmin:
    """Validates: Requirements 2.1, 2.3, 2.4, 2.5, 3.1, 3.3, 5.1, 5.2"""

    def test_create_event_no_existing_admin(self):
        """Create event with empty table creates admin in Cognito + DynamoDB
        and returns credentials in the cfnresponse Data."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()
            mod = _reload_admin_provisioner()

            event = _build_event("Create", pool_id)
            body = _invoke_handler(mod, event)

            # cfnresponse is SUCCESS with credentials
            assert body["Status"] == "SUCCESS"
            assert body["Data"]["AdminUserName"] == "admin"
            assert len(body["Data"]["AdminUserPassword"]) >= 16

            # DynamoDB record exists with correct attributes
            item = table.get_item(
                Key={"PK": "USER#admin", "SK": "PROFILE"},
            )
            assert "Item" in item
            record = item["Item"]
            assert record["userId"] == "admin"
            assert record["role"] == "Administrator"
            assert record["email"] == "admin@example.com"
            assert record["status"] == "ACTIVE"
            assert "cognitoSub" in record
            assert "posixUid" in record

            # Cognito user exists
            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            user = cog.admin_get_user(
                UserPoolId=pool_id, Username="admin",
            )
            assert user["Username"] == "admin"


# ---------------------------------------------------------------------------
# Test 2: Existing admin skips creation (Create event)
# ---------------------------------------------------------------------------


class TestCreateEventExistingAdminSkips:
    """Validates: Requirements 1.2, 5.3"""

    def test_create_event_existing_admin_skips(self):
        """Create event with existing admin returns SUCCESS with empty Data
        and makes no writes."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()

            # Seed an existing admin record
            table.put_item(Item={
                "PK": "USER#existing_admin",
                "SK": "PROFILE",
                "userId": "existing_admin",
                "role": "Administrator",
                "status": "ACTIVE",
            })
            items_before = table.scan()["Items"]

            mod = _reload_admin_provisioner()
            event = _build_event("Create", pool_id)
            body = _invoke_handler(mod, event)

            assert body["Status"] == "SUCCESS"
            assert body["Data"] == {}

            # No new DynamoDB items
            items_after = table.scan()["Items"]
            assert len(items_after) == len(items_before)

            # No Cognito users created
            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            users = cog.list_users(UserPoolId=pool_id)
            assert len(users["Users"]) == 0


# ---------------------------------------------------------------------------
# Test 3: Update event with existing admin skips
# ---------------------------------------------------------------------------


class TestUpdateEventExistingAdminSkips:
    """Validates: Requirements 6.2, 8.1"""

    def test_update_event_existing_admin_skips(self):
        """Update event with existing admin returns SUCCESS with empty Data."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()

            table.put_item(Item={
                "PK": "USER#admin",
                "SK": "PROFILE",
                "userId": "admin",
                "role": "Administrator",
                "status": "ACTIVE",
            })

            mod = _reload_admin_provisioner()
            event = _build_event("Update", pool_id)
            body = _invoke_handler(mod, event)

            assert body["Status"] == "SUCCESS"
            assert body["Data"] == {}


# ---------------------------------------------------------------------------
# Test 4: Delete event is a no-op
# ---------------------------------------------------------------------------


class TestDeleteEventNoop:
    """Delete lifecycle returns SUCCESS with no side effects."""

    def test_delete_event_noop(self):
        """Delete event returns SUCCESS immediately without touching
        DynamoDB or Cognito."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()

            # Seed an admin so we can verify it is NOT deleted
            table.put_item(Item={
                "PK": "USER#admin",
                "SK": "PROFILE",
                "userId": "admin",
                "role": "Administrator",
                "status": "ACTIVE",
            })
            items_before = table.scan()["Items"]

            mod = _reload_admin_provisioner()
            event = _build_event("Delete", pool_id)
            body = _invoke_handler(mod, event)

            assert body["Status"] == "SUCCESS"

            # Admin record still exists
            items_after = table.scan()["Items"]
            assert len(items_after) == len(items_before)


# ---------------------------------------------------------------------------
# Test 5: Cognito user has FORCE_CHANGE_PASSWORD status
# ---------------------------------------------------------------------------


class TestCognitoUserForceChangePassword:
    """Validates: Requirements 2.4"""

    def test_cognito_user_force_change_password(self):
        """AdminCreateUser sets TemporaryPassword, resulting in
        FORCE_CHANGE_PASSWORD user status."""
        with mock_aws():
            _set_aws_env()
            _create_users_table()
            pool_id = _create_cognito_pool()
            mod = _reload_admin_provisioner()

            event = _build_event("Create", pool_id)
            _invoke_handler(mod, event)

            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            user = cog.admin_get_user(
                UserPoolId=pool_id, Username="admin",
            )
            assert user["UserStatus"] == "FORCE_CHANGE_PASSWORD"


# ---------------------------------------------------------------------------
# Test 6: Condition expression on PutItem
# ---------------------------------------------------------------------------


class TestConditionExpressionOnPutItem:
    """Validates: Requirements 3.3, 8.6"""

    def test_condition_expression_on_putitem(self):
        """PutItem uses attribute_not_exists(PK). When a record with
        PK=USER#admin already exists, the handler catches the
        ConditionalCheckFailedException and rolls back."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()

            # Pre-insert a record with PK=USER#admin (non-admin role)
            table.put_item(Item={
                "PK": "USER#admin",
                "SK": "PROFILE",
                "userId": "admin",
                "role": "User",
                "status": "ACTIVE",
            })

            mod = _reload_admin_provisioner()
            event = _build_event("Create", pool_id)
            body = _invoke_handler(mod, event)

            # The handler should report FAILED because the condition
            # expression prevents overwriting the existing record
            assert body["Status"] == "FAILED"
            assert "ConditionalCheckFailed" in body["Reason"] or \
                   "conditional" in body["Reason"].lower() or \
                   "already exists" in body["Reason"].lower() or \
                   "ConditionalCheckFailedException" in body["Reason"]

            # Cognito user should be rolled back (deleted)
            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            users = cog.list_users(UserPoolId=pool_id)
            admin_users = [
                u for u in users["Users"] if u["Username"] == "admin"
            ]
            assert len(admin_users) == 0, (
                "Cognito user should be deleted after PutItem failure"
            )


# ---------------------------------------------------------------------------
# Test 7: POSIX UID atomic increment
# ---------------------------------------------------------------------------


class TestPosixUidAtomicIncrement:
    """Validates: Requirements 3.1"""

    def test_posix_uid_atomic_increment(self):
        """_allocate_posix_uid uses UpdateItem with ADD, incrementing
        the counter from 10001 to 10002 on successive calls."""
        with mock_aws():
            _set_aws_env()
            _create_users_table()  # seeds counter at 10000
            mod = _reload_admin_provisioner()

            uid1 = mod._allocate_posix_uid(USERS_TABLE_NAME)
            uid2 = mod._allocate_posix_uid(USERS_TABLE_NAME)

            assert uid1 == 10001
            assert uid2 == 10002


# ---------------------------------------------------------------------------
# Test 8: DynamoDB PutItem failure rolls back Cognito user
# ---------------------------------------------------------------------------


class TestDynamoDBPutFailureRollsBackCognitoUser:
    """Validates: Requirements 6.4"""

    def test_dynamodb_put_failure_rolls_back_cognito_user(self):
        """When _write_admin_record raises, the Cognito user is deleted."""
        with mock_aws():
            _set_aws_env()
            _create_users_table()
            pool_id = _create_cognito_pool()
            mod = _reload_admin_provisioner()

            with patch.object(
                mod, "_write_admin_record",
                side_effect=Exception("DynamoDB PutItem failed"),
            ):
                with pytest.raises(Exception, match="DynamoDB PutItem"):
                    mod._create_admin_user(
                        USERS_TABLE_NAME, pool_id,
                        "admin@example.com", "Temp1234!@#$abcd",
                    )

            # Cognito user should have been rolled back
            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            users = cog.list_users(UserPoolId=pool_id)
            admin_users = [
                u for u in users["Users"] if u["Username"] == "admin"
            ]
            assert len(admin_users) == 0, (
                "Cognito user should be deleted on DynamoDB failure"
            )


# ---------------------------------------------------------------------------
# Test 9: Cognito group failure rolls back Cognito user
# ---------------------------------------------------------------------------


class TestCognitoGroupFailureRollsBackCognitoUser:
    """Validates: Requirements 6.3"""

    def test_cognito_group_failure_rolls_back_cognito_user(self):
        """When _add_to_admin_group raises, the Cognito user is deleted."""
        with mock_aws():
            _set_aws_env()
            _create_users_table()
            pool_id = _create_cognito_pool()
            mod = _reload_admin_provisioner()

            with patch.object(
                mod, "_add_to_admin_group",
                side_effect=Exception("Group add failed"),
            ):
                with pytest.raises(Exception, match="Group add failed"):
                    mod._create_admin_user(
                        USERS_TABLE_NAME, pool_id,
                        "admin@example.com", "Temp1234!@#$abcd",
                    )

            # Cognito user should have been rolled back
            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            users = cog.list_users(UserPoolId=pool_id)
            admin_users = [
                u for u in users["Users"] if u["Username"] == "admin"
            ]
            assert len(admin_users) == 0, (
                "Cognito user should be deleted on group add failure"
            )


# ---------------------------------------------------------------------------
# Test 10: Scan failure returns FAILED cfnresponse
# ---------------------------------------------------------------------------


class TestScanFailureReturnsFailed:
    """Validates: Requirements 7.1"""

    def test_scan_failure_returns_failed_response(self):
        """When _scan_for_admin raises, the handler sends a FAILED
        cfnresponse with the error message."""
        with mock_aws():
            _set_aws_env()
            _create_users_table()
            pool_id = _create_cognito_pool()
            mod = _reload_admin_provisioner()

            event = _build_event("Create", pool_id)

            with patch.object(
                mod, "_scan_for_admin",
                side_effect=Exception("Scan exploded"),
            ):
                body = _invoke_handler(mod, event)

            assert body["Status"] == "FAILED"
            assert "Scan exploded" in body["Reason"]


# ---------------------------------------------------------------------------
# Test 11: Update with changed email does not create second admin
# ---------------------------------------------------------------------------


class TestUpdateEventChangedEmailDoesNotCreateSecondAdmin:
    """Validates: Requirements 8.2, 8.5"""

    def test_update_event_changed_email_does_not_create_second_admin(self):
        """Update event with a different AdminEmail still skips creation
        when an admin already exists."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()

            # Seed existing admin with original email
            table.put_item(Item={
                "PK": "USER#admin",
                "SK": "PROFILE",
                "userId": "admin",
                "role": "Administrator",
                "email": "original@example.com",
                "status": "ACTIVE",
            })
            items_before = table.scan()["Items"]

            mod = _reload_admin_provisioner()

            # Update event with a DIFFERENT email
            event = _build_event(
                "Update", pool_id, admin_email="changed@example.com",
            )
            body = _invoke_handler(mod, event)

            assert body["Status"] == "SUCCESS"
            assert body["Data"] == {}

            # No new items written
            items_after = table.scan()["Items"]
            assert len(items_after) == len(items_before)

            # No Cognito users created
            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            users = cog.list_users(UserPoolId=pool_id)
            assert len(users["Users"]) == 0


# ---------------------------------------------------------------------------
# Test 12: Update event never modifies existing credentials
# ---------------------------------------------------------------------------


class TestUpdateEventNoCredentialModification:
    """Validates: Requirements 8.3"""

    def test_update_event_no_credential_modification(self):
        """Update event does not modify existing Cognito user or DynamoDB
        record when an admin already exists."""
        with mock_aws():
            _set_aws_env()
            table = _create_users_table()
            pool_id = _create_cognito_pool()

            # Create an admin user first via the handler
            mod = _reload_admin_provisioner()
            create_event = _build_event("Create", pool_id)
            create_body = _invoke_handler(mod, create_event)
            assert create_body["Status"] == "SUCCESS"

            # Snapshot the DynamoDB record and Cognito user state
            ddb_item_before = table.get_item(
                Key={"PK": "USER#admin", "SK": "PROFILE"},
            )["Item"]

            cog = boto3.client("cognito-idp", region_name=AWS_REGION)
            cognito_user_before = cog.admin_get_user(
                UserPoolId=pool_id, Username="admin",
            )

            # Reload module to get fresh boto3 bindings
            mod = _reload_admin_provisioner()

            # Send an Update event
            update_event = _build_event("Update", pool_id)
            update_body = _invoke_handler(mod, update_event)

            assert update_body["Status"] == "SUCCESS"
            assert update_body["Data"] == {}

            # Verify DynamoDB record is unchanged
            ddb_item_after = table.get_item(
                Key={"PK": "USER#admin", "SK": "PROFILE"},
            )["Item"]
            assert ddb_item_before == ddb_item_after

            # Verify Cognito user attributes are unchanged
            cognito_user_after = cog.admin_get_user(
                UserPoolId=pool_id, Username="admin",
            )
            attrs_before = {
                a["Name"]: a["Value"]
                for a in cognito_user_before["UserAttributes"]
            }
            attrs_after = {
                a["Name"]: a["Value"]
                for a in cognito_user_after["UserAttributes"]
            }
            assert attrs_before == attrs_after
