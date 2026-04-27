"""Core user management business logic.

Handles POSIX UID/GID allocation, Cognito user lifecycle, and
DynamoDB persistence for platform users.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import DuplicateError, InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
cognito = boto3.client("cognito-idp")


VALID_ROLES = ("User", "Administrator")


def create_user(
    table_name: str,
    user_pool_id: str,
    user_id: str,
    display_name: str,
    email: str,
    role: str = "User",
) -> dict[str, Any]:
    """Create a new platform user.

    1. Allocate a globally unique POSIX UID/GID via atomic counter.
    2. Create the Cognito user.
    3. Optionally add the user to the Administrators Cognito group.
    4. Store the user record in DynamoDB.

    Args:
        role: Platform role — ``"User"`` (default) or ``"Administrator"``.

    Returns the created user record.
    """
    if role not in VALID_ROLES:
        raise ValidationError(
            f"Invalid role '{role}'. Must be one of: {', '.join(VALID_ROLES)}.",
            {"field": "role"},
        )

    posix_uid = _allocate_posix_uid(table_name)

    cognito_sub = _create_cognito_user(user_pool_id, user_id, email)

    # Assign Cognito group based on role
    if role == "Administrator":
        _add_user_to_group(user_pool_id, user_id, "Administrators")

    now = datetime.now(timezone.utc).isoformat()
    user_record = {
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "displayName": display_name,
        "email": email,
        "role": role,
        "posixUid": posix_uid,
        "posixGid": posix_uid,  # GID matches UID
        "status": "ACTIVE",
        "cognitoSub": cognito_sub,
        "createdAt": now,
        "updatedAt": now,
    }

    table = dynamodb.Table(table_name)
    try:
        table.put_item(
            Item=user_record,
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Clean up the Cognito user we just created
            _delete_cognito_user(user_pool_id, user_id)
            raise DuplicateError(
                f"User '{user_id}' already exists.",
                {"userId": user_id},
            )
        raise InternalError(f"Failed to store user record: {exc}")

    return {
        "userId": user_id,
        "displayName": display_name,
        "email": email,
        "role": role,
        "posixUid": posix_uid,
        "posixGid": posix_uid,
        "status": "ACTIVE",
        "cognitoSub": cognito_sub,
        "createdAt": now,
        "updatedAt": now,
    }


def deactivate_user(
    table_name: str,
    user_pool_id: str,
    user_id: str,
) -> None:
    """Deactivate a platform user.

    1. Set status to INACTIVE in DynamoDB.
    2. Disable the Cognito user.
    3. Sign out all sessions.
    """
    table = dynamodb.Table(table_name)
    now = datetime.now(timezone.utc).isoformat()

    # Verify user exists
    response = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"})
    if "Item" not in response:
        raise NotFoundError(f"User '{user_id}' not found.", {"userId": user_id})

    user_record = response["Item"]

    # Validate user is currently ACTIVE
    if user_record.get("status") != "ACTIVE":
        raise ValidationError(
            f"User '{user_id}' is already inactive.", {"userId": user_id}
        )

    # Update DynamoDB status
    table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
        UpdateExpression="SET #status = :status, updatedAt = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "INACTIVE", ":now": now},
    )

    # Disable Cognito user and revoke sessions
    try:
        cognito.admin_disable_user(
            UserPoolId=user_pool_id,
            Username=user_id,
        )
        cognito.admin_user_global_sign_out(
            UserPoolId=user_pool_id,
            Username=user_id,
        )
    except ClientError as exc:
        logger.warning(
            "Failed to disable/sign-out Cognito user %s: %s",
            user_id,
            exc,
        )


def reactivate_user(
    table_name: str,
    user_pool_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Reactivate a previously deactivated user.

    1. Fetch the user record from DynamoDB.
    2. Validate the user exists and is currently INACTIVE.
    3. Update DynamoDB status to ACTIVE.
    4. Re-enable the Cognito user account.
    5. Return the updated user profile.

    Raises:
        NotFoundError: if the user does not exist.
        ValidationError: if the user is already ACTIVE.
    """
    table = dynamodb.Table(table_name)
    now = datetime.now(timezone.utc).isoformat()

    # Verify user exists
    response = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"})
    if "Item" not in response:
        raise NotFoundError(f"User '{user_id}' not found.", {"userId": user_id})

    user_record = response["Item"]

    # Validate user is currently INACTIVE
    if user_record.get("status") != "INACTIVE":
        raise ValidationError(
            f"User '{user_id}' is already active.", {"userId": user_id}
        )

    # Update DynamoDB status to ACTIVE
    table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
        UpdateExpression="SET #status = :status, updatedAt = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "ACTIVE", ":now": now},
    )

    # Re-enable Cognito user
    try:
        cognito.admin_enable_user(
            UserPoolId=user_pool_id,
            Username=user_id,
        )
    except ClientError as exc:
        logger.warning(
            "Failed to re-enable Cognito user %s: %s",
            user_id,
            exc,
        )

    # Return sanitised profile with updated status
    user_record["status"] = "ACTIVE"
    user_record["updatedAt"] = now
    return _sanitise_record(user_record)


def get_user(table_name: str, user_id: str) -> dict[str, Any]:
    """Retrieve a single user record by userId."""
    table = dynamodb.Table(table_name)
    response = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"})

    if "Item" not in response:
        raise NotFoundError(f"User '{user_id}' not found.", {"userId": user_id})

    return _sanitise_record(response["Item"])


def list_users(table_name: str) -> list[dict[str, Any]]:
    """List all platform users (both ACTIVE and INACTIVE)."""
    table = dynamodb.Table(table_name)
    scan_kwargs = {
        "FilterExpression": boto3.dynamodb.conditions.Attr("SK").eq("PROFILE"),
    }

    items = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return [_sanitise_record(item) for item in items]


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


def _create_cognito_user(
    user_pool_id: str, user_id: str, email: str
) -> str:
    """Create a Cognito user and return the sub (subject ID)."""
    try:
        response = cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=user_id,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        return response["User"]["Attributes"][0]["Value"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UsernameExistsException":
            raise DuplicateError(
                f"User '{user_id}' already exists in Cognito.",
                {"userId": user_id},
            )
        raise InternalError(f"Failed to create Cognito user: {exc}")


def _add_user_to_group(
    user_pool_id: str, user_id: str, group_name: str
) -> None:
    """Add a Cognito user to a group (e.g. Administrators)."""
    try:
        cognito.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=user_id,
            GroupName=group_name,
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to add user '{user_id}' to group '{group_name}': {exc}"
        )


def _delete_cognito_user(user_pool_id: str, user_id: str) -> None:
    """Delete a Cognito user (cleanup on DynamoDB duplicate)."""
    try:
        cognito.admin_delete_user(
            UserPoolId=user_pool_id,
            Username=user_id,
        )
    except ClientError:
        logger.warning("Failed to clean up Cognito user %s", user_id)


def _sanitise_record(item: dict[str, Any]) -> dict[str, Any]:
    """Remove DynamoDB key attributes from a user record for API response."""
    return {
        k: v for k, v in item.items()
        if k not in ("PK", "SK")
    }
