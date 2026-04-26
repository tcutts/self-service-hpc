"""Structured error types and response builder for the Template Management API.

Error codes:
    AUTHORISATION_ERROR (403) — caller lacks required role/permission
    VALIDATION_ERROR (400) — invalid input
    DUPLICATE_ERROR (409) — resource already exists
    NOT_FOUND (404) — resource does not exist
    INTERNAL_ERROR (500) — unexpected server error
"""

import json
from typing import Any


class ApiError(Exception):
    """Base class for structured API errors."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthorisationError(ApiError):
    """Caller lacks required role or permission."""

    code = "AUTHORISATION_ERROR"
    status_code = 403


class ValidationError(ApiError):
    """Invalid input data."""

    code = "VALIDATION_ERROR"
    status_code = 400


class DuplicateError(ApiError):
    """Resource already exists."""

    code = "DUPLICATE_ERROR"
    status_code = 409


class NotFoundError(ApiError):
    """Resource does not exist."""

    code = "NOT_FOUND"
    status_code = 404


class InternalError(ApiError):
    """Unexpected server error."""

    code = "INTERNAL_ERROR"
    status_code = 500


def build_error_response(error: ApiError) -> dict[str, Any]:
    """Build an API Gateway proxy response from an ApiError."""
    return {
        "statusCode": error.status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            }
        ),
    }
