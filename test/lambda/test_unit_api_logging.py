"""Unit tests for the shared API action logging middleware.

Validates that ``log_api_action`` emits structured JSON log entries
containing the required fields: userId, actionType, timestamp,
httpMethod, resource, and statusCode.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

# Load the shared module directly from its file path to avoid
# collisions with other Lambda packages.
_SHARED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "shared",
)
sys.path.insert(0, _SHARED_DIR)

from api_logging import (  # noqa: E402
    build_action_type,
    get_user_id_from_event,
    log_api_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_event(
    method: str = "GET",
    resource: str = "/users",
    username: str = "alice",
    sub: str = "sub-alice",
    groups: str = "Administrators",
    path_parameters: dict | None = None,
    body: str | None = None,
) -> dict:
    """Build a minimal API Gateway proxy event."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_parameters,
        "body": body,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": username,
                    "sub": sub,
                    "cognito:groups": groups,
                }
            }
        },
    }


def _build_response(status_code: int = 200) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True}),
    }


# ---------------------------------------------------------------------------
# Tests — get_user_id_from_event
# ---------------------------------------------------------------------------

class TestGetUserIdFromEvent:
    """Tests for extracting user identity from Cognito claims."""

    def test_extracts_cognito_username(self):
        event = _build_event(username="bob")
        assert get_user_id_from_event(event) == "bob"

    def test_falls_back_to_sub(self):
        event = _build_event()
        # Remove cognito:username, keep sub
        del event["requestContext"]["authorizer"]["claims"]["cognito:username"]
        assert get_user_id_from_event(event) == "sub-alice"

    def test_returns_anonymous_when_no_claims(self):
        event = {"requestContext": {}}
        assert get_user_id_from_event(event) == "anonymous"

    def test_returns_anonymous_for_empty_event(self):
        assert get_user_id_from_event({}) == "anonymous"


# ---------------------------------------------------------------------------
# Tests — build_action_type
# ---------------------------------------------------------------------------

class TestBuildActionType:
    """Tests for deriving action type labels."""

    def test_simple_resource(self):
        assert build_action_type("POST", "/users") == "POST /users"

    def test_parameterised_resource(self):
        result = build_action_type("DELETE", "/projects/{projectId}")
        assert result == "DELETE /projects/{projectId}"

    def test_nested_resource(self):
        result = build_action_type("PUT", "/projects/{projectId}/budget")
        assert result == "PUT /projects/{projectId}/budget"


# ---------------------------------------------------------------------------
# Tests — log_api_action
# ---------------------------------------------------------------------------

class TestLogApiAction:
    """Tests for the main logging function."""

    def test_returns_log_entry_with_all_required_fields(self):
        event = _build_event(method="POST", resource="/users", username="admin-user")
        response = _build_response(201)

        entry = log_api_action(event, response)

        assert entry["userId"] == "admin-user"
        assert entry["actionType"] == "POST /users"
        assert entry["httpMethod"] == "POST"
        assert entry["resource"] == "/users"
        assert entry["statusCode"] == 201
        # Timestamp should be a valid ISO 8601 string
        datetime.fromisoformat(entry["timestamp"])

    def test_logs_error_responses(self):
        event = _build_event(method="DELETE", resource="/users/{userId}", username="hacker")
        response = _build_response(403)

        entry = log_api_action(event, response)

        assert entry["userId"] == "hacker"
        assert entry["statusCode"] == 403
        assert entry["actionType"] == "DELETE /users/{userId}"

    def test_handles_missing_http_method(self):
        event = _build_event()
        del event["httpMethod"]
        response = _build_response(200)

        entry = log_api_action(event, response)

        assert entry["httpMethod"] == "UNKNOWN"
        assert "UNKNOWN" in entry["actionType"]

    def test_handles_missing_resource(self):
        event = _build_event()
        del event["resource"]
        response = _build_response(200)

        entry = log_api_action(event, response)

        assert entry["resource"] == "UNKNOWN"

    def test_timestamp_is_utc(self):
        event = _build_event()
        response = _build_response(200)

        entry = log_api_action(event, response)

        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.tzinfo is not None
        assert ts.tzinfo == timezone.utc

    def test_emits_json_to_logger(self, caplog):
        event = _build_event(method="GET", resource="/templates", username="viewer")
        response = _build_response(200)

        with caplog.at_level(logging.INFO, logger="api_action_log"):
            log_api_action(event, response)

        # Verify the log record contains valid JSON with the required fields
        assert len(caplog.records) >= 1
        log_record = caplog.records[-1]
        parsed = json.loads(log_record.message)
        assert parsed["userId"] == "viewer"
        assert parsed["actionType"] == "GET /templates"
        assert "timestamp" in parsed

    def test_anonymous_user_when_unauthenticated(self):
        event = {"httpMethod": "GET", "resource": "/health", "requestContext": {}}
        response = _build_response(200)

        entry = log_api_action(event, response)

        assert entry["userId"] == "anonymous"


# ---------------------------------------------------------------------------
# Tests — Integration with handler pattern
# ---------------------------------------------------------------------------

class TestHandlerIntegration:
    """Verify that the logging function works with real handler responses."""

    def test_works_with_typical_success_response(self):
        event = _build_event(method="GET", resource="/projects", username="admin")
        response = {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({"projects": []}),
        }

        entry = log_api_action(event, response)

        assert entry["statusCode"] == 200
        assert entry["userId"] == "admin"

    def test_works_with_error_response(self):
        event = _build_event(method="POST", resource="/users", username="regular")
        response = {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "error": {
                    "code": "AUTHORISATION_ERROR",
                    "message": "Only administrators can create users.",
                    "details": {},
                }
            }),
        }

        entry = log_api_action(event, response)

        assert entry["statusCode"] == 403
        assert entry["userId"] == "regular"
        assert entry["actionType"] == "POST /users"
