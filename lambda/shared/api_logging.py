"""API action logging utility for Lambda handlers.

Provides a ``log_api_action`` function that emits a structured JSON log
entry to CloudWatch Logs for every API action.  Each entry includes:

- ``userId``: the caller's identity extracted from Cognito claims
- ``actionType``: a human-readable label derived from the HTTP method
  and resource path (e.g. ``"POST /users"``)
- ``timestamp``: ISO 8601 UTC timestamp of the action
- ``httpMethod``: the raw HTTP method
- ``resource``: the API Gateway resource path
- ``statusCode``: the HTTP status code of the response

Usage from any handler::

    from api_logging import log_api_action

    def handler(event, context):
        ...
        response = _response(200, body)
        log_api_action(event, response)
        return response
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("api_action_log")
logger.setLevel(logging.INFO)


def get_user_id_from_event(event: dict[str, Any]) -> str:
    """Extract the caller's user identifier from Cognito claims.

    Falls back to ``"anonymous"`` when claims are absent (e.g. health
    checks or unauthenticated requests).
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )
    return claims.get("cognito:username", claims.get("sub", "anonymous"))


def build_action_type(http_method: str, resource: str) -> str:
    """Derive a concise action type label from the HTTP method and resource.

    Returns a string like ``"POST /users"`` or ``"DELETE /projects/{projectId}"``.
    """
    return f"{http_method} {resource}"


def log_api_action(
    event: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    """Emit a structured JSON log entry for an API action.

    Parameters
    ----------
    event:
        The API Gateway proxy event.
    response:
        The handler's response dict (must contain ``statusCode``).

    Returns
    -------
    dict
        The log entry that was emitted, useful for testing.
    """
    http_method = event.get("httpMethod", "UNKNOWN")
    resource = event.get("resource", "UNKNOWN")
    user_id = get_user_id_from_event(event)
    action_type = build_action_type(http_method, resource)
    status_code = response.get("statusCode", 0)
    timestamp = datetime.now(timezone.utc).isoformat()

    log_entry: dict[str, Any] = {
        "userId": user_id,
        "actionType": action_type,
        "timestamp": timestamp,
        "httpMethod": http_method,
        "resource": resource,
        "statusCode": status_code,
    }

    logger.info(json.dumps(log_entry))
    return log_entry
