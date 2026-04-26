"""Accounting Query Lambda handler.

Provides cross-cluster Slurm job accounting data by querying ``sacct``
on login nodes of active clusters via SSM Run Command.

Environment variables:
    CLUSTERS_TABLE_NAME: DynamoDB Clusters table name
    PROJECTS_TABLE_NAME: DynamoDB Projects table name
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

from accounting import query_accounting_jobs
from auth import get_caller_identity, is_administrator, is_project_admin
from errors import (
    ApiError,
    AuthorisationError,
    InternalError,
    NotFoundError,
    ValidationError,
    build_error_response,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route API Gateway proxy events to the appropriate accounting operation."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    logger.info(
        "Accounting query request: %s %s",
        http_method,
        resource,
    )

    try:
        if resource == "/accounting/jobs" and http_method == "GET":
            response = _handle_get_jobs(event)
        else:
            response = _response(
                404,
                {"error": {"code": "NOT_FOUND", "message": "Route not found", "details": {}}},
            )

    except (AuthorisationError, ValidationError, NotFoundError, InternalError) as exc:
        response = build_error_response(exc)
    except Exception:
        logger.exception("Unhandled error in accounting query handler")
        response = _response(
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "details": {}}},
        )

    # Log the API action to CloudWatch (Requirement 13.3)
    log_api_action(event, response)
    return response


def _handle_get_jobs(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /accounting/jobs — query job records across clusters.

    Query parameters:
        projectId (optional): restrict results to a single project.

    Authorisation:
        - Without projectId: Admin only (cross-cluster query).
        - With projectId: Admin or Project Admin for that project.
    """
    caller = get_caller_identity(event)
    query_params = event.get("queryStringParameters") or {}
    project_id = query_params.get("projectId", "").strip() if query_params.get("projectId") else None

    if project_id:
        # Project-scoped query: Admin or Project Admin
        if not is_project_admin(event, project_id):
            raise AuthorisationError(
                "Only administrators or project administrators can query project accounting data."
            )
    else:
        # Cross-cluster query: Admin only
        if not is_administrator(event):
            raise AuthorisationError(
                "Only administrators can query accounting data across all clusters."
            )

    logger.info(
        "Accounting query by %s, projectId=%s",
        caller,
        project_id or "(all)",
    )

    result = query_accounting_jobs(
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
    )

    return _response(200, result)


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
