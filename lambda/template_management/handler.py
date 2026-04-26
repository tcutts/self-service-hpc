"""Cluster Template Management Lambda handler.

Handles CRUD operations for cluster templates and default template
seeding for initial platform deployment.

Environment variables:
    TEMPLATES_TABLE_NAME: DynamoDB ClusterTemplates table name
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

from auth import get_caller_identity, is_administrator, is_authenticated
from errors import (
    ApiError,
    AuthorisationError,
    DuplicateError,
    InternalError,
    NotFoundError,
    ValidationError,
    build_error_response,
)
from templates import create_template, delete_template, get_template, list_templates

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TEMPLATES_TABLE_NAME = os.environ.get("TEMPLATES_TABLE_NAME", "ClusterTemplates")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route API Gateway proxy events to the appropriate template operation."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_parameters = event.get("pathParameters") or {}

    logger.info(
        "Template management request: %s %s",
        http_method,
        resource,
    )

    try:
        if resource == "/templates" and http_method == "POST":
            response = _handle_create_template(event)
        elif resource == "/templates" and http_method == "GET":
            response = _handle_list_templates(event)
        elif resource == "/templates/{templateId}" and http_method == "GET":
            template_id = path_parameters.get("templateId", "")
            response = _handle_get_template(event, template_id)
        elif resource == "/templates/{templateId}" and http_method == "DELETE":
            template_id = path_parameters.get("templateId", "")
            response = _handle_delete_template(event, template_id)
        else:
            response = _response(
                404,
                {"error": {"code": "NOT_FOUND", "message": "Route not found", "details": {}}},
            )

    except (
        AuthorisationError,
        ValidationError,
        DuplicateError,
        NotFoundError,
        InternalError,
    ) as exc:
        response = build_error_response(exc)
    except Exception:
        logger.exception("Unhandled error in template management handler")
        response = _response(
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "details": {}}},
        )

    # Log the API action to CloudWatch (Requirement 13.3)
    log_api_action(event, response)
    return response


def _handle_create_template(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /templates — create a new cluster template."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can create cluster templates.")

    body = _parse_body(event)
    template_id = body.get("templateId", "").strip() if isinstance(body.get("templateId"), str) else ""
    template_name = body.get("templateName", "").strip() if isinstance(body.get("templateName"), str) else ""
    description = body.get("description", "").strip() if isinstance(body.get("description"), str) else ""
    instance_types = body.get("instanceTypes", [])
    login_instance_type = body.get("loginInstanceType", "").strip() if isinstance(body.get("loginInstanceType"), str) else ""
    min_nodes = body.get("minNodes", 0)
    max_nodes = body.get("maxNodes", 0)
    ami_id = body.get("amiId", "").strip() if isinstance(body.get("amiId"), str) else ""
    software_stack = body.get("softwareStack", {})

    template_record = create_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_id,
        template_name=template_name,
        description=description,
        instance_types=instance_types,
        login_instance_type=login_instance_type,
        min_nodes=min_nodes,
        max_nodes=max_nodes,
        ami_id=ami_id,
        software_stack=software_stack,
    )
    logger.info("Template created: %s by %s", template_id, caller)
    return _response(201, template_record)


def _handle_list_templates(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /templates — list all cluster templates.

    Any authenticated user can list templates.
    """
    if not is_authenticated(event):
        raise AuthorisationError("Authentication is required to list templates.")

    templates = list_templates(table_name=TEMPLATES_TABLE_NAME)
    return _response(200, {"templates": templates})


def _handle_get_template(event: dict[str, Any], template_id: str) -> dict[str, Any]:
    """Handle GET /templates/{templateId} — get a single template.

    Any authenticated user can view template details.
    """
    if not is_authenticated(event):
        raise AuthorisationError("Authentication is required to view templates.")

    template_record = get_template(table_name=TEMPLATES_TABLE_NAME, template_id=template_id)
    return _response(200, template_record)


def _handle_delete_template(event: dict[str, Any], template_id: str) -> dict[str, Any]:
    """Handle DELETE /templates/{templateId} — delete a cluster template."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can delete cluster templates.")

    delete_template(table_name=TEMPLATES_TABLE_NAME, template_id=template_id)
    logger.info("Template deleted: %s by %s", template_id, caller)
    return _response(200, {"message": f"Template '{template_id}' has been deleted."})


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
