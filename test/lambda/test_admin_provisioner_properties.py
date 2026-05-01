"""Property-based tests for the admin provisioner Lambda.

Feature: admin-user-provisioning

Uses Hypothesis to verify correctness properties of the admin provisioner
handler defined in ``lambda/admin_provisioner/handler.py``.
"""

import importlib
import os
import sys

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AWS_REGION = "us-east-1"
USERS_TABLE_NAME = "PlatformUsers"

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_ADMIN_DIR = os.path.join(_LAMBDA_ROOT, "admin_provisioner")


def _reload_admin_provisioner():
    """Load admin provisioner module so boto3 clients bind to moto."""
    filepath = os.path.join(_ADMIN_DIR, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "admin_provisioner_handler", filepath
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
    table.meta.client.get_waiter("table_exists").wait(TableName=USERS_TABLE_NAME)
    table.put_item(Item={"PK": "COUNTER", "SK": "POSIX_UID", "currentValue": 10000})
    return table


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy for a single user record with varying userId, role, and SK.
user_record_st = st.fixed_dictionaries(
    {
        "userId": st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1,
            max_size=20,
        ),
        "role": st.sampled_from(["User", "Administrator"]),
        "SK": st.sampled_from(["PROFILE", "OTHER"]),
    }
)

# Strategy for a list of user records (0 to 10 records).
user_records_st = st.lists(user_record_st, min_size=0, max_size=10)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


class TestProperty1AdminDetectionScansByRoleNotUserId:
    """Property 1: Admin detection scans by role, not userId.

    For any set of user records in the PlatformUsers table with varying
    ``userId``, ``role``, and ``SK`` values, ``_scan_for_admin`` SHALL
    return ``True`` if and only if at least one record has
    ``role=Administrator`` AND ``SK=PROFILE``, regardless of the
    ``userId`` value.

    **Validates: Requirements 1.1, 1.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(records=user_records_st)
    def test_scan_returns_true_iff_admin_profile_exists(
        self, records: list[dict]
    ) -> None:
        """_scan_for_admin returns True iff at least one record has
        role=Administrator AND SK=PROFILE."""
        # Compute expected result from the generated records.
        expected = any(
            r["role"] == "Administrator" and r["SK"] == "PROFILE"
            for r in records
        )

        with mock_aws():
            # Set AWS env vars for moto.
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            table = _create_users_table()

            # Insert generated user records into the table.
            for idx, record in enumerate(records):
                table.put_item(
                    Item={
                        "PK": f"USER#{record['userId']}_{idx}",
                        "SK": record["SK"],
                        "userId": record["userId"],
                        "role": record["role"],
                        "status": "ACTIVE",
                    }
                )

            # Reload the handler module inside the moto context so
            # the module-level boto3 resource binds to moto.
            mod = _reload_admin_provisioner()
            result = mod._scan_for_admin(USERS_TABLE_NAME)

            assert result == expected, (
                f"Expected _scan_for_admin to return {expected} "
                f"for records {records}, but got {result}"
            )


# ---------------------------------------------------------------------------
# Strategies for Property 2
# ---------------------------------------------------------------------------

# Strategy for table records that always include at least one admin.
# Uses @composite to guarantee the forced admin record is always present.
@st.composite
def records_with_admin(draw):
    """Draw a list of user records and append a guaranteed admin record."""
    base = draw(st.lists(user_record_st, min_size=0, max_size=5))
    return base + [
        {"userId": "forced_admin", "role": "Administrator", "SK": "PROFILE"}
    ]


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


class TestProperty2ExistingAdminPreventsAllWriteOperations:
    """Property 2: Existing admin prevents all write operations.

    For any PlatformUsers table state that contains at least one record
    with ``role=Administrator`` and ``SK=PROFILE``, the provisioner handler
    SHALL make zero DynamoDB write calls and zero Cognito mutating calls,
    and SHALL return a SUCCESS response with empty Data.

    **Validates: Requirements 1.2, 6.1**
    """

    @settings(max_examples=100, deadline=None)
    @given(records=records_with_admin())
    def test_existing_admin_causes_no_writes(
        self, records: list[dict]
    ) -> None:
        """Handler makes zero writes when an admin already exists."""
        import json
        from unittest.mock import MagicMock, patch

        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            # --- Set up DynamoDB table and seed records ----------------------
            table = _create_users_table()

            for idx, record in enumerate(records):
                table.put_item(
                    Item={
                        "PK": f"USER#{record['userId']}_{idx}",
                        "SK": record["SK"],
                        "userId": record["userId"],
                        "role": record["role"],
                        "status": "ACTIVE",
                    }
                )

            # Snapshot items before handler runs (to verify no new writes).
            items_before = table.scan()["Items"]

            # --- Set up Cognito pool (handler needs a valid pool id) ---------
            cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
            pool_resp = cognito_client.create_user_pool(PoolName="TestPool")
            pool_id = pool_resp["UserPool"]["Id"]

            # --- Build CloudFormation Create event ---------------------------
            event = {
                "RequestType": "Create",
                "ResponseURL": "https://example.com/cfn-response",
                "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/guid",
                "RequestId": "unique-id",
                "LogicalResourceId": "AdminProvisionerResource",
                "ResourceProperties": {
                    "TableName": USERS_TABLE_NAME,
                    "UserPoolId": pool_id,
                    "AdminEmail": "admin@example.com",
                },
            }

            class FakeContext:
                log_stream_name = "test-log-stream"

            # --- Reload handler inside moto context --------------------------
            mod = _reload_admin_provisioner()

            # Verify scan finds admin before invoking handler.
            scan_result = mod._scan_for_admin(USERS_TABLE_NAME)
            assert scan_result is True, (
                f"_scan_for_admin returned {scan_result} for records {records}"
            )

            # --- Invoke handler with urlopen mocked --------------------------
            # Patch urllib.request.urlopen globally so the handler's
            # reference is intercepted regardless of module reload order.
            with patch("urllib.request.urlopen", return_value=MagicMock()) as mock_urlopen:
                mod.handler(event, FakeContext())

            # --- Verify cfnresponse was SUCCESS with empty Data --------------
            assert mock_urlopen.call_count == 1
            sent_request = mock_urlopen.call_args[0][0]
            body = json.loads(sent_request.data.decode("utf-8"))

            assert body["Status"] == "SUCCESS", (
                f"Expected SUCCESS but got {body['Status']}: "
                f"{body.get('Reason', '')}"
            )
            assert body["Data"] == {}, (
                f"Expected empty Data but got {body['Data']}"
            )

            # --- Verify zero DynamoDB writes ---------------------------------
            items_after = table.scan()["Items"]
            assert len(items_after) == len(items_before), (
                f"DynamoDB item count changed from {len(items_before)} "
                f"to {len(items_after)} — unexpected write occurred"
            )

            # --- Verify zero Cognito user creations --------------------------
            users_resp = cognito_client.list_users(UserPoolId=pool_id)
            assert len(users_resp["Users"]) == 0, (
                f"Expected zero Cognito users but found "
                f"{len(users_resp['Users'])} — unexpected Cognito write"
            )


# ---------------------------------------------------------------------------
# Strategies for Property 3
# ---------------------------------------------------------------------------

# Strategy for valid email strings.
email_st = st.emails()

# Strategy for POSIX UID counter values (the value the counter will return).
posix_uid_st = st.integers(min_value=10001, max_value=99999)

# Strategy for Cognito sub strings (UUIDs).
cognito_sub_st = st.uuids()


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


class TestProperty3CreatedRecordContainsAllRequiredAttributes:
    """Property 3: Created DynamoDB record contains all required attributes
    with correct values.

    For any valid admin email string, POSIX UID counter value, and Cognito
    sub string, the DynamoDB PutItem call SHALL produce a record containing:
    PK=USER#admin, SK=PROFILE, userId=admin, displayName=Admin,
    email=<input email>, role=Administrator, posixUid=<counter value>,
    posixGid=<counter value>, status=ACTIVE, cognitoSub=<input sub>,
    and valid ISO 8601 createdAt and updatedAt timestamps.

    **Validates: Requirements 2.2, 3.2**
    """

    @settings(max_examples=10, deadline=None)
    @given(
        email=email_st,
        posix_uid=posix_uid_st,
        cognito_sub=cognito_sub_st,
    )
    def test_created_record_has_all_required_attributes(
        self, email: str, posix_uid: int, cognito_sub,
    ) -> None:
        """_create_admin_user writes a DynamoDB record with all required
        attributes set to the correct values."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        sub_str = str(cognito_sub)

        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            # --- Set up DynamoDB table -----------------------------------
            table = _create_users_table()

            # Seed the POSIX UID counter so the next allocation returns
            # the generated posix_uid value.
            table.put_item(
                Item={
                    "PK": "COUNTER",
                    "SK": "POSIX_UID",
                    "currentValue": posix_uid - 1,
                }
            )

            # --- Set up Cognito pool with Administrators group -----------
            cognito_client = boto3.client(
                "cognito-idp", region_name=AWS_REGION
            )
            pool_resp = cognito_client.create_user_pool(
                PoolName="TestPool"
            )
            pool_id = pool_resp["UserPool"]["Id"]
            cognito_client.create_group(
                GroupName="Administrators",
                UserPoolId=pool_id,
            )

            # --- Reload handler inside moto context ----------------------
            mod = _reload_admin_provisioner()

            # Capture time just before creation for timestamp validation.
            before = datetime.now(timezone.utc)

            # Patch _create_cognito_admin to return our generated sub
            # while still creating the real Cognito user via moto.
            original_create = mod._create_cognito_admin

            def _patched_create(up_id, em, pw):
                original_create(up_id, em, pw)
                return sub_str

            with patch.object(
                mod, "_create_cognito_admin", side_effect=_patched_create
            ):
                mod._create_admin_user(
                    USERS_TABLE_NAME, pool_id, email, "Temp1234!@#$abcd"
                )

            after = datetime.now(timezone.utc)

            # --- Read the created record ---------------------------------
            resp = table.get_item(
                Key={"PK": "USER#admin", "SK": "PROFILE"}
            )
            assert "Item" in resp, "Admin record not found in DynamoDB"
            item = resp["Item"]

            # --- Verify all required attributes --------------------------
            assert item["PK"] == "USER#admin"
            assert item["SK"] == "PROFILE"
            assert item["userId"] == "admin"
            assert item["displayName"] == "Admin"
            assert item["email"] == email
            assert item["role"] == "Administrator"
            assert int(item["posixUid"]) == posix_uid
            assert int(item["posixGid"]) == posix_uid
            assert item["status"] == "ACTIVE"
            assert item["cognitoSub"] == sub_str

            # Verify timestamps are valid ISO 8601 and within the
            # expected time window.
            created_at = datetime.fromisoformat(item["createdAt"])
            updated_at = datetime.fromisoformat(item["updatedAt"])
            assert created_at.tzinfo is not None, (
                "createdAt must be timezone-aware"
            )
            assert updated_at.tzinfo is not None, (
                "updatedAt must be timezone-aware"
            )
            assert before <= created_at <= after, (
                f"createdAt {created_at} not in [{before}, {after}]"
            )
            assert before <= updated_at <= after, (
                f"updatedAt {updated_at} not in [{before}, {after}]"
            )
            assert created_at == updated_at, (
                "createdAt and updatedAt should be equal for a new record"
            )


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


class TestProperty4GeneratedPasswordMeetsCognitoPolicy:
    """Property 4: Generated password meets Cognito policy.

    For any invocation of ``_generate_password`` with a length parameter
    between 16 and 64, the returned string SHALL be at least as long as
    the requested length and contain at least one uppercase letter, one
    lowercase letter, one digit, and one symbol character.

    **Validates: Requirements 4.2**
    """

    SYMBOLS = set("!@#$%^&*()_+-=[]{}|")

    @settings(max_examples=10, deadline=None)
    @given(length=st.integers(min_value=16, max_value=64))
    def test_password_meets_cognito_policy(self, length: int) -> None:
        """_generate_password returns a password that satisfies the
        Cognito password policy for the given length."""
        mod = _reload_admin_provisioner()
        password = mod._generate_password(length)

        assert len(password) >= length, (
            f"Password length {len(password)} is less than "
            f"requested length {length}"
        )
        assert any(c.isupper() for c in password), (
            "Password must contain at least one uppercase letter"
        )
        assert any(c.islower() for c in password), (
            "Password must contain at least one lowercase letter"
        )
        assert any(c.isdigit() for c in password), (
            "Password must contain at least one digit"
        )
        assert any(c in self.SYMBOLS for c in password), (
            "Password must contain at least one symbol from "
            f"{self.SYMBOLS}"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 5
# ---------------------------------------------------------------------------

# Strategy for error injection points in the creation sequence.
error_injection_st = st.sampled_from(
    ["cognito_create", "cognito_group", "dynamodb_put"]
)


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------


class TestProperty5CreationFailureLeavesNoPartialState:
    """Property 5: Creation failure leaves no partial state.

    For any failure during the admin creation sequence — whether at the
    Cognito creation step, the Cognito group step, or the DynamoDB write
    step — the system SHALL NOT leave orphaned resources. Specifically:
    if Cognito user creation fails, no DynamoDB PutItem SHALL be attempted;
    if DynamoDB PutItem fails after Cognito user creation, the Cognito user
    SHALL be deleted.

    **Validates: Requirements 6.3, 6.4**
    """

    @settings(max_examples=10, deadline=None)
    @given(injection_point=error_injection_st)
    def test_creation_failure_leaves_no_partial_state(
        self, injection_point: str
    ) -> None:
        """Injecting an error at any creation step leaves no orphaned
        resources in Cognito or DynamoDB."""
        from unittest.mock import patch

        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            # --- Set up DynamoDB table -----------------------------------
            table = _create_users_table()

            # --- Set up Cognito pool with Administrators group -----------
            cognito_client = boto3.client(
                "cognito-idp", region_name=AWS_REGION
            )
            pool_resp = cognito_client.create_user_pool(
                PoolName="TestPool"
            )
            pool_id = pool_resp["UserPool"]["Id"]
            cognito_client.create_group(
                GroupName="Administrators",
                UserPoolId=pool_id,
            )

            # --- Reload handler inside moto context ----------------------
            mod = _reload_admin_provisioner()

            # --- Inject error at the specified point ---------------------
            error_msg = f"Injected {injection_point} error"

            if injection_point == "cognito_create":
                patch_target = "_create_cognito_admin"
            elif injection_point == "cognito_group":
                patch_target = "_add_to_admin_group"
            else:  # dynamodb_put
                patch_target = "_write_admin_record"

            with patch.object(
                mod,
                patch_target,
                side_effect=Exception(error_msg),
            ):
                raised = False
                try:
                    mod._create_admin_user(
                        USERS_TABLE_NAME,
                        pool_id,
                        "admin@example.com",
                        "Temp1234!@#$abcd",
                    )
                except Exception:
                    raised = True

            assert raised, (
                f"Expected _create_admin_user to raise when "
                f"{injection_point} fails"
            )

            # --- Verify no partial state remains -------------------------

            # In ALL cases, no USER#admin record should exist in DynamoDB.
            ddb_resp = table.get_item(
                Key={"PK": "USER#admin", "SK": "PROFILE"}
            )
            assert "Item" not in ddb_resp, (
                f"DynamoDB record USER#admin should not exist after "
                f"{injection_point} failure, but found: {ddb_resp.get('Item')}"
            )

            # Check Cognito user state based on injection point.
            users_resp = cognito_client.list_users(
                UserPoolId=pool_id
            )
            cognito_users = [
                u for u in users_resp["Users"]
                if u["Username"] == "admin"
            ]

            if injection_point == "cognito_create":
                # Cognito creation failed — no Cognito user should exist.
                assert len(cognito_users) == 0, (
                    "No Cognito user should exist when cognito_create fails"
                )
            elif injection_point == "cognito_group":
                # Group add failed after Cognito creation — rollback
                # should have deleted the Cognito user.
                assert len(cognito_users) == 0, (
                    "Cognito user should be deleted (rolled back) when "
                    "cognito_group fails"
                )
            else:  # dynamodb_put
                # DynamoDB write failed after Cognito creation — rollback
                # should have deleted the Cognito user.
                assert len(cognito_users) == 0, (
                    "Cognito user should be deleted (rolled back) when "
                    "dynamodb_put fails"
                )


# ---------------------------------------------------------------------------
# Strategies for Property 6
# ---------------------------------------------------------------------------

# Strategy for random error messages.
error_message_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    min_size=1,
    max_size=100,
)

# Strategy for error injection points in the handler flow.
error_injection_handler_st = st.sampled_from(
    ["scan", "cognito_create", "posix_uid"]
)


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


class TestProperty6ServiceErrorsPropagateToCloudFormationResponse:
    """Property 6: Service errors propagate to CloudFormation response.

    For any service error (DynamoDB scan failure, Cognito creation error,
    POSIX UID allocation failure), the provisioner SHALL return a FAILED
    cfnresponse with a Reason string that contains the original error
    message.

    **Validates: Requirements 7.1, 7.2, 7.3**
    """

    @settings(max_examples=10, deadline=None)
    @given(
        error_msg=error_message_st,
        injection_point=error_injection_handler_st,
    )
    def test_service_errors_propagate_to_cfn_response(
        self, error_msg: str, injection_point: str,
    ) -> None:
        """Injecting an error at any service call results in a FAILED
        cfnresponse whose Reason includes the original error message."""
        import json
        from unittest.mock import MagicMock, patch

        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            # --- Set up DynamoDB table (empty — no admin) ----------------
            _create_users_table()

            # --- Set up Cognito pool with Administrators group -----------
            cognito_client = boto3.client(
                "cognito-idp", region_name=AWS_REGION
            )
            pool_resp = cognito_client.create_user_pool(
                PoolName="TestPool"
            )
            pool_id = pool_resp["UserPool"]["Id"]
            cognito_client.create_group(
                GroupName="Administrators",
                UserPoolId=pool_id,
            )

            # --- Reload handler inside moto context ----------------------
            mod = _reload_admin_provisioner()

            # --- Build CloudFormation Create event -----------------------
            event = {
                "RequestType": "Create",
                "ResponseURL": "https://example.com/cfn-response",
                "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/guid",
                "RequestId": "unique-id",
                "LogicalResourceId": "AdminProvisionerResource",
                "ResourceProperties": {
                    "TableName": USERS_TABLE_NAME,
                    "UserPoolId": pool_id,
                    "AdminEmail": "admin@example.com",
                },
            }

            class FakeContext:
                log_stream_name = "test-log-stream"

            # --- Inject error at the specified point ---------------------
            patches = []

            if injection_point == "scan":
                patches.append(patch.object(
                    mod, "_scan_for_admin",
                    side_effect=Exception(error_msg),
                ))
            elif injection_point == "cognito_create":
                # Ensure scan returns False so creation path is taken.
                patches.append(patch.object(
                    mod, "_scan_for_admin", return_value=False,
                ))
                patches.append(patch.object(
                    mod, "_create_cognito_admin",
                    side_effect=Exception(error_msg),
                ))
            else:  # posix_uid
                # Ensure scan returns False so creation path is taken.
                patches.append(patch.object(
                    mod, "_scan_for_admin", return_value=False,
                ))
                patches.append(patch.object(
                    mod, "_allocate_posix_uid",
                    side_effect=Exception(error_msg),
                ))

            # Patch urlopen to capture the cfnresponse.
            mock_urlopen = MagicMock()
            patches.append(patch(
                "urllib.request.urlopen", mock_urlopen,
            ))

            # Apply all patches and invoke the handler.
            for p in patches:
                p.start()
            try:
                mod.handler(event, FakeContext())
            finally:
                for p in patches:
                    p.stop()

            # --- Parse and verify the cfnresponse ------------------------
            assert mock_urlopen.call_count == 1, (
                f"Expected exactly 1 cfnresponse call, "
                f"got {mock_urlopen.call_count}"
            )
            sent_request = mock_urlopen.call_args[0][0]
            body = json.loads(sent_request.data.decode("utf-8"))

            assert body["Status"] == "FAILED", (
                f"Expected FAILED but got {body['Status']} "
                f"for injection_point={injection_point}"
            )
            assert error_msg in body["Reason"], (
                f"Expected error message '{error_msg}' in Reason "
                f"'{body['Reason']}' for injection_point={injection_point}"
            )


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


class TestProperty7UpdateEventsCannotBypassAdminDetection:
    """Property 7: Update events with changed properties cannot bypass
    admin detection.

    For any Update event where the ``AdminEmail`` resource property differs
    from the original Create event, if an Administrator user already exists
    in the PlatformUsers table, the provisioner SHALL make zero DynamoDB
    write calls, zero Cognito mutating calls, and SHALL return a SUCCESS
    response with empty Data — identical behaviour to an Update with
    unchanged properties.

    **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**
    """

    @settings(max_examples=10, deadline=None)
    @given(admin_email=st.emails())
    def test_update_with_varying_email_causes_no_writes(
        self, admin_email: str,
    ) -> None:
        """Handler makes zero writes on Update when an admin exists,
        regardless of the AdminEmail value."""
        import json
        from unittest.mock import MagicMock, patch

        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"

            # --- Set up DynamoDB table and seed admin record -------------
            table = _create_users_table()

            table.put_item(
                Item={
                    "PK": "USER#existing_admin_0",
                    "SK": "PROFILE",
                    "userId": "existing_admin",
                    "role": "Administrator",
                    "status": "ACTIVE",
                }
            )

            # Snapshot items before handler runs.
            items_before = table.scan()["Items"]

            # --- Set up Cognito pool ------------------------------------
            cognito_client = boto3.client(
                "cognito-idp", region_name=AWS_REGION,
            )
            pool_resp = cognito_client.create_user_pool(
                PoolName="TestPool",
            )
            pool_id = pool_resp["UserPool"]["Id"]

            # --- Build CloudFormation Update event -----------------------
            event = {
                "RequestType": "Update",
                "PhysicalResourceId": "AdminProvisioner-1234567890",
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

            class FakeContext:
                log_stream_name = "test-log-stream"

            # --- Reload handler inside moto context ----------------------
            mod = _reload_admin_provisioner()

            # --- Invoke handler with urlopen mocked ----------------------
            with patch(
                "urllib.request.urlopen",
                return_value=MagicMock(),
            ) as mock_urlopen:
                mod.handler(event, FakeContext())

            # --- Verify cfnresponse was SUCCESS with empty Data ----------
            assert mock_urlopen.call_count == 1
            sent_request = mock_urlopen.call_args[0][0]
            body = json.loads(sent_request.data.decode("utf-8"))

            assert body["Status"] == "SUCCESS", (
                f"Expected SUCCESS but got {body['Status']}: "
                f"{body.get('Reason', '')}"
            )
            assert body["Data"] == {}, (
                f"Expected empty Data but got {body['Data']}"
            )

            # --- Verify zero DynamoDB writes -----------------------------
            items_after = table.scan()["Items"]
            assert len(items_after) == len(items_before), (
                f"DynamoDB item count changed from {len(items_before)} "
                f"to {len(items_after)} — unexpected write occurred"
            )

            # --- Verify zero Cognito user creations ----------------------
            users_resp = cognito_client.list_users(UserPoolId=pool_id)
            assert len(users_resp["Users"]) == 0, (
                f"Expected zero Cognito users but found "
                f"{len(users_resp['Users'])} — unexpected Cognito write"
            )
