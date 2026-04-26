"""User Management Lambda handler.

Handles CRUD operations for platform users including POSIX UID/GID
assignment, Cognito user lifecycle, and DynamoDB persistence.

Environment variables:
    USERS_TABLE_NAME: DynamoDB PlatformUsers table name
    USER_POOL_ID: Cognito User Pool ID
"""

import json
import logging
import os
import sys
from typing import Any

# Add shared utilities to the module search path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from api_logging import log_api_action  # noqa: E402

from auth import is_administrator, get_caller_identity
from errors import (
    AuthorisationError,
    DuplicateError,
    NotFoundError,
    ValidationError,
    InternalError,
    build_error_response,
)
from users import create_user, deactivate_user, get_user, list_users, reactivate_user

logger = logging.getLogger()
logger.setLevel(logging.INFO)

USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "PlatformUsers")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route API Gateway proxy events to the appropriate user operation."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_parameters = event.get("pathParameters") or {}

    logger.info(
        "User management request: %s %s",
        http_method,
        resource,
    )

    try:
        if resource == "/users" and http_method == "POST":
            response = _handle_create_user(event)
        elif resource == "/users" and http_method == "GET":
            response = _handle_list_users(event)
        elif resource == "/users/{userId}" and http_method == "GET":
            user_id = path_parameters.get("userId", "")
            response = _handle_get_user(event, user_id)
        elif resource == "/users/{userId}" and http_method == "DELETE":
            user_id = path_parameters.get("userId", "")
            response = _handle_delete_user(event, user_id)
        elif resource == "/users/{userId}/reactivate" and http_method == "POST":
            user_id = path_parameters.get("userId", "")
            response = _handle_reactivate_user(event, user_id)
        else:
            response = _response(404, {"error": {"code": "NOT_FOUND", "message": "Route not found", "details": {}}})

    except AuthorisationError as exc:
        response = build_error_response(exc)
    except ValidationError as exc:
        response = build_error_response(exc)
    except DuplicateError as exc:
        response = build_error_response(exc)
    except NotFoundError as exc:
        response = build_error_response(exc)
    except InternalError as exc:
        response = build_error_response(exc)
    except Exception:
        logger.exception("Unhandled error in user management handler")
        response = _response(500, {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "details": {}}})

    # Log the API action to CloudWatch (Requirement 13.3)
    log_api_action(event, response)
    return response


def _handle_create_user(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /users — create a new platform user."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can create users.")

    body = _parse_body(event)
    user_id = body.get("userId", "").strip()
    display_name = body.get("displayName", "").strip()
    email = body.get("email", "").strip()
    role = body.get("role", "User").strip()

    if not user_id:
        raise ValidationError("userId is required.", {"field": "userId"})
    if not display_name:
        raise ValidationError("displayName is required.", {"field": "displayName"})
    if not email:
        raise ValidationError("email is required.", {"field": "email"})

    user_record = create_user(
        table_name=USERS_TABLE_NAME,
        user_pool_id=USER_POOL_ID,
        user_id=user_id,
        display_name=display_name,
        email=email,
        role=role,
    )
    logger.info("User created: %s by %s", user_id, caller)
    return _response(201, user_record)


def _handle_list_users(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /users — list all platform users."""
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can list users.")

    users = list_users(table_name=USERS_TABLE_NAME)
    return _response(200, {"users": users})


def _handle_get_user(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Handle GET /users/{userId} — get a single user's details."""
    caller = get_caller_identity(event)
    # Admins can view any user; non-admins can only view themselves
    if not is_administrator(event) and caller != user_id:
        raise AuthorisationError("You can only view your own profile.")

    user_record = get_user(table_name=USERS_TABLE_NAME, user_id=user_id)
    return _response(200, user_record)


def _handle_delete_user(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Handle DELETE /users/{userId} — deactivate a platform user."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can deactivate users.")

    deactivate_user(
        table_name=USERS_TABLE_NAME,
        user_pool_id=USER_POOL_ID,
        user_id=user_id,
    )
    logger.info("User deactivated: %s by %s", user_id, caller)
    return _response(200, {"message": f"User {user_id} has been deactivated."})


def _handle_reactivate_user(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Handle POST /users/{userId}/reactivate — reactivate a deactivated user."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can reactivate users.")

    user_record = reactivate_user(
        table_name=USERS_TABLE_NAME,
        user_pool_id=USER_POOL_ID,
        user_id=user_id,
    )
    logger.info("User reactivated: %s by %s", user_id, caller)
    return _response(200, user_record)


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON request body from an API Gateway event."""
    body = event.get("body")
    if not body:
        raise ValidationError("Request body is required.", {})
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise ValidationError("Request body must be valid JSON.", {})


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, default=str),
    }
