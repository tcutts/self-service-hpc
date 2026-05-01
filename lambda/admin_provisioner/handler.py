"""Admin Provisioner CloudFormation custom resource handler.

Ensures at least one Administrator user exists after Foundation stack
deployment. Scans DynamoDB for existing admins, and conditionally creates
a default admin user in both Cognito and DynamoDB when none is found.

Resource Properties (from CloudFormation event):
    TableName: DynamoDB PlatformUsers table name
    UserPoolId: Cognito User Pool ID
    AdminEmail: Email address for the default admin user
"""

import json
import logging
import secrets
import string
import time
from datetime import datetime, timezone
from typing import Any
from urllib import request as urllib_request

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
cognito = boto3.client("cognito-idp")

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def handler(event: dict[str, Any], context: Any) -> None:
    """CloudFormation custom resource handler.

    Routes Create, Update, and Delete request types. Create and Update
    trigger admin provisioning logic; Delete is a no-op.
    """
    request_type = event.get("RequestType", "")
    logger.info("Admin provisioner received %s request", request_type)

    if request_type == "Delete":
        _send_response(event, context, SUCCESS, {})
        return

    props = event.get("ResourceProperties", {})
    table_name = props.get("TableName", "")
    user_pool_id = props.get("UserPoolId", "")
    admin_email = props.get("AdminEmail", "")

    _provision_admin(event, context, table_name, user_pool_id, admin_email)


def _provision_admin(
    event: dict, context: Any,
    table_name: str, user_pool_id: str, admin_email: str,
) -> None:
    """Check for existing admin and create one if none found."""
    try:
        if _scan_for_admin(table_name):
            logger.info("Administrator already exists, skipping creation")
            _send_response(event, context, SUCCESS, {},
                           physical_resource_id="AdminProvisioner-existing")
            return
        _provision_new_admin(event, context, table_name, user_pool_id, admin_email)
    except Exception as exc:
        logger.exception("Admin provisioning failed")
        _send_response(event, context, FAILED, {},
                       reason=str(exc),
                       physical_resource_id="AdminProvisioner-failed")


def _provision_new_admin(
    event: dict, context: Any,
    table_name: str, user_pool_id: str, admin_email: str,
) -> None:
    """Create a new admin user and send SUCCESS response with credentials."""
    password = _generate_password()
    _create_admin_user(table_name, user_pool_id, admin_email, password)
    timestamp = str(int(time.time()))
    data = {"AdminUserName": "admin", "AdminUserPassword": password}
    _send_response(event, context, SUCCESS, data,
                   physical_resource_id=f"AdminProvisioner-{timestamp}")


def _scan_for_admin(table_name: str) -> bool:
    """Scan PlatformUsers for any record with role=Administrator AND SK=PROFILE.

    Returns True if at least one Administrator profile record exists.
    Uses a FilterExpression to check both conditions.
    """
    table = dynamodb.Table(table_name)
    scan_kwargs = {
        "FilterExpression": (
            boto3.dynamodb.conditions.Attr("role").eq("Administrator")
            & boto3.dynamodb.conditions.Attr("SK").eq("PROFILE")
        ),
        "Limit": 1,
    }
    response = table.scan(**scan_kwargs)
    if response.get("Items"):
        return True

    while response.get("LastEvaluatedKey"):
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = table.scan(**scan_kwargs)
        if response.get("Items"):
            return True

    return False


def _generate_password(length: int = 16) -> str:
    """Generate a cryptographically secure password.

    Guarantees at least one uppercase letter, one lowercase letter,
    one digit, and one symbol character. Uses the secrets module
    for cryptographic security.
    """
    symbols = "!@#$%^&*()_+-=[]{}|"
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(symbols),
    ]
    alphabet = string.ascii_letters + string.digits + symbols
    remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]
    password_chars = required + remaining
    # Shuffle to avoid predictable positions for required characters
    shuffled = list(password_chars)
    secrets.SystemRandom().shuffle(shuffled)
    return "".join(shuffled)


def _allocate_posix_uid(table_name: str) -> int:
    """Atomically increment the POSIX UID counter and return the new value.

    Uses DynamoDB UpdateItem with ADD to guarantee uniqueness.
    The counter item has PK=COUNTER, SK=POSIX_UID.
    """
    table = dynamodb.Table(table_name)
    response = table.update_item(
        Key={"PK": "COUNTER", "SK": "POSIX_UID"},
        UpdateExpression="ADD currentValue :inc",
        ExpressionAttributeValues={":inc": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["currentValue"])


def _create_admin_user(
    table_name: str, user_pool_id: str, email: str, password: str,
) -> None:
    """Orchestrate admin user creation in Cognito and DynamoDB.

    Sequence: allocate POSIX UID → create Cognito user → add to
    Administrators group → write DynamoDB record. Rolls back the
    Cognito user on group or DynamoDB failure.
    """
    posix_uid = _allocate_posix_uid(table_name)
    cognito_sub = _create_cognito_admin(user_pool_id, email, password)

    try:
        _add_to_admin_group(user_pool_id)
    except Exception:
        _rollback_cognito_user(user_pool_id)
        raise

    try:
        _write_admin_record(table_name, email, posix_uid, cognito_sub)
    except Exception:
        _rollback_cognito_user(user_pool_id)
        raise


def _create_cognito_admin(user_pool_id: str, email: str, password: str) -> str:
    """Create the admin Cognito user with a temporary password.

    Returns the Cognito user's sub attribute. The user will be in
    FORCE_CHANGE_PASSWORD status, requiring a password reset on first login.
    """
    response = cognito.admin_create_user(
        UserPoolId=user_pool_id,
        Username="admin",
        TemporaryPassword=password,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    for attr in response["User"]["Attributes"]:
        if attr["Name"] == "sub":
            return attr["Value"]
    return response["User"]["Username"]


def _add_to_admin_group(user_pool_id: str) -> None:
    """Add the admin user to the Administrators Cognito group."""
    cognito.admin_add_user_to_group(
        UserPoolId=user_pool_id,
        Username="admin",
        GroupName="Administrators",
    )


def _rollback_cognito_user(user_pool_id: str) -> None:
    """Delete the admin Cognito user as a rollback step.

    Best-effort cleanup — logs a warning if deletion fails.
    """
    try:
        cognito.admin_delete_user(
            UserPoolId=user_pool_id,
            Username="admin",
        )
        logger.info("Rolled back Cognito user 'admin'")
    except ClientError:
        logger.warning("Failed to roll back Cognito user 'admin'")


def _build_admin_record(
    email: str, posix_uid: int, cognito_sub: str,
) -> dict[str, Any]:
    """Build the DynamoDB item for the admin user profile."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "PK": "USER#admin", "SK": "PROFILE",
        "userId": "admin", "displayName": "Admin",
        "email": email, "role": "Administrator",
        "posixUid": posix_uid, "posixGid": posix_uid,
        "status": "ACTIVE", "cognitoSub": cognito_sub,
        "createdAt": now, "updatedAt": now,
    }


def _write_admin_record(
    table_name: str, email: str, posix_uid: int, cognito_sub: str,
) -> None:
    """Write the admin user record to DynamoDB.

    Uses attribute_not_exists(PK) condition to prevent overwriting
    an existing user record with the same userId.
    """
    table = dynamodb.Table(table_name)
    item = _build_admin_record(email, posix_uid, cognito_sub)
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")


def _build_cfn_body(
    event: dict, context: Any, status: str, data: dict,
    reason: str, physical_resource_id: str,
) -> bytes:
    """Build the cfnresponse JSON body for CloudFormation."""
    physical_id = physical_resource_id or event.get(
        "PhysicalResourceId", "AdminProvisioner-unknown"
    )
    return json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": data,
    }).encode("utf-8")


def _send_response(
    event: dict, context: Any, status: str, data: dict,
    reason: str = "", physical_resource_id: str = "",
) -> None:
    """Send a cfnresponse JSON payload to the CloudFormation pre-signed URL.

    Uses urllib.request to PUT the response. If the send fails,
    the error is logged — CloudFormation will eventually time out.
    """
    body = _build_cfn_body(event, context, status, data, reason, physical_resource_id)
    url = event.get("ResponseURL", "")
    if not url:
        logger.error("No ResponseURL in event, cannot send cfnresponse")
        return

    req = urllib_request.Request(url, data=body, method="PUT", headers={"Content-Type": ""})
    try:
        urllib_request.urlopen(req)
        logger.info("cfnresponse sent: %s", status)
    except Exception:
        logger.exception("Failed to send cfnresponse to %s", url)
