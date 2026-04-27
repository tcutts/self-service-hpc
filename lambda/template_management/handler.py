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
from templates import create_template, delete_template, get_template, list_templates, update_template
from ami_lookup import get_latest_pcs_ami

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
        elif resource == "/templates/{templateId}" and http_method == "PUT":
            template_id = path_parameters.get("templateId", "")
            response = _handle_update_template(event, template_id)
        elif resource == "/templates/batch/delete" and http_method == "POST":
            response = _handle_batch_delete(event)
        elif resource == "/templates/default-ami" and http_method == "GET":
            response = _handle_default_ami(event)
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


def _handle_update_template(event: dict[str, Any], template_id: str) -> dict[str, Any]:
    """Handle PUT /templates/{templateId} — update a cluster template."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can update cluster templates.")

    body = _parse_body(event)

    body_template_id = body.get("templateId")
    if body_template_id is not None and body_template_id != template_id:
        raise ValidationError(
            "Body templateId does not match path parameter.",
            {"field": "templateId"},
        )

    template_name = body.get("templateName", "").strip() if isinstance(body.get("templateName"), str) else ""
    description = body.get("description", "").strip() if isinstance(body.get("description"), str) else ""
    instance_types = body.get("instanceTypes", [])
    login_instance_type = body.get("loginInstanceType", "").strip() if isinstance(body.get("loginInstanceType"), str) else ""
    min_nodes = body.get("minNodes", 0)
    max_nodes = body.get("maxNodes", 0)
    ami_id = body.get("amiId", "").strip() if isinstance(body.get("amiId"), str) else ""
    software_stack = body.get("softwareStack", {})

    updated_record = update_template(
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
    logger.info("Template updated: %s by %s", template_id, caller)
    return _response(200, updated_record)


def _handle_default_ami(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /templates/default-ami — look up the latest PCS sample AMI.

    Query parameters:
        arch: CPU architecture — "x86_64" (default) or "arm64"
    """
    if not is_authenticated(event):
        raise AuthorisationError("Authentication is required.")

    params = event.get("queryStringParameters") or {}
    arch = params.get("arch", "x86_64").strip().lower()
    if arch not in ("x86_64", "arm64"):
        raise ValidationError(
            "arch must be 'x86_64' or 'arm64'.",
            {"field": "arch", "validValues": ["x86_64", "arm64"]},
        )

    ami = get_latest_pcs_ami(arch)
    return _response(200, ami)


def _validate_batch_request(event: dict[str, Any], id_field: str) -> list[str]:
    """Validate a batch request: check admin auth, parse body, validate ID array.

    Returns the list of IDs on success. Raises ValidationError on failure.
    """
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can perform batch operations.")

    body = _parse_body(event)
    ids = body.get(id_field)

    if not isinstance(ids, list) or len(ids) == 0:
        raise ValidationError(
            "Batch request must contain between 1 and 25 identifiers.",
            {"field": id_field},
        )
    if len(ids) > 25:
        raise ValidationError(
            "Batch request must contain between 1 and 25 identifiers.",
            {"field": id_field},
        )

    return ids


def _build_batch_response(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a BatchResult response from a list of per-item results."""
    succeeded = sum(1 for r in results if r["status"] == "success")
    failed = len(results) - succeeded
    return _response(200, {
        "results": results,
        "summary": {
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
        },
    })


def _handle_batch_delete(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /templates/batch/delete — batch delete multiple templates."""
    template_ids = _validate_batch_request(event, "templateIds")
    caller = get_caller_identity(event)
    results: list[dict[str, Any]] = []

    for tid in template_ids:
        try:
            delete_template(table_name=TEMPLATES_TABLE_NAME, template_id=tid)
            logger.info("Batch delete succeeded for template '%s' by %s", tid, caller)
            results.append({"id": tid, "status": "success", "message": "Template deleted"})
        except ApiError as exc:
            logger.warning("Batch delete failed for template '%s': %s", tid, str(exc))
            results.append({"id": tid, "status": "error", "message": str(exc)})

    return _build_batch_response(results)


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
