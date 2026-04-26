"""Cluster Operations Lambda handler.

Handles cluster creation, destruction, listing, and detail retrieval
for projects. Orchestrates long-running operations via Step Functions.

Environment variables:
    CLUSTERS_TABLE_NAME: DynamoDB Clusters table name
    PROJECTS_TABLE_NAME: DynamoDB Projects table name
    CLUSTER_NAME_REGISTRY_TABLE_NAME: DynamoDB ClusterNameRegistry table name
    CREATION_STATE_MACHINE_ARN: Step Functions state machine ARN for cluster creation
    DESTRUCTION_STATE_MACHINE_ARN: Step Functions state machine ARN for cluster destruction
    USER_POOL_ID: Cognito User Pool ID
"""

import json
import logging
import os
import sys
import time
from typing import Any

import boto3

# Add shared utilities to the module search path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from api_logging import log_api_action  # noqa: E402

from auth import get_caller_identity, is_project_user
from cluster_names import validate_cluster_name
from clusters import check_budget_breach, get_cluster, list_clusters
from errors import (
    ApiError,
    AuthorisationError,
    BudgetExceededError,
    ConflictError,
    DuplicateError,
    InternalError,
    NotFoundError,
    ValidationError,
    build_error_response,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
CLUSTER_NAME_REGISTRY_TABLE_NAME = os.environ.get(
    "CLUSTER_NAME_REGISTRY_TABLE_NAME", "ClusterNameRegistry"
)
CREATION_STATE_MACHINE_ARN = os.environ.get("CREATION_STATE_MACHINE_ARN", "")
DESTRUCTION_STATE_MACHINE_ARN = os.environ.get("DESTRUCTION_STATE_MACHINE_ARN", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")

sfn_client = boto3.client("stepfunctions")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route API Gateway proxy events to the appropriate cluster operation."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_parameters = event.get("pathParameters") or {}

    logger.info(
        "Cluster operations request: %s %s",
        http_method,
        resource,
    )

    try:
        if resource == "/projects/{projectId}/clusters" and http_method == "POST":
            project_id = path_parameters.get("projectId", "")
            response = _handle_create_cluster(event, project_id)

        elif resource == "/projects/{projectId}/clusters" and http_method == "GET":
            project_id = path_parameters.get("projectId", "")
            response = _handle_list_clusters(event, project_id)

        elif (
            resource == "/projects/{projectId}/clusters/{clusterName}"
            and http_method == "GET"
        ):
            project_id = path_parameters.get("projectId", "")
            cluster_name = path_parameters.get("clusterName", "")
            response = _handle_get_cluster(event, project_id, cluster_name)

        elif (
            resource == "/projects/{projectId}/clusters/{clusterName}"
            and http_method == "DELETE"
        ):
            project_id = path_parameters.get("projectId", "")
            cluster_name = path_parameters.get("clusterName", "")
            response = _handle_delete_cluster(event, project_id, cluster_name)

        elif (
            resource == "/projects/{projectId}/clusters/{clusterName}/recreate"
            and http_method == "POST"
        ):
            project_id = path_parameters.get("projectId", "")
            cluster_name = path_parameters.get("clusterName", "")
            response = _handle_recreate_cluster(event, project_id, cluster_name)

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
        ConflictError,
        BudgetExceededError,
        InternalError,
    ) as exc:
        response = build_error_response(exc)
    except Exception:
        logger.exception("Unhandled error in cluster operations handler")
        response = _response(
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "details": {}}},
        )

    # Log the API action to CloudWatch (Requirement 13.3)
    log_api_action(event, response)
    return response


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _handle_create_cluster(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/clusters — create a new cluster.

    Validates input, checks authorisation and budget, then starts the
    cluster creation Step Functions execution.
    """
    caller = get_caller_identity(event)
    if not is_project_user(event, project_id):
        raise AuthorisationError(
            "You must be a project member to create clusters."
        )

    body = _parse_body(event)
    cluster_name = body.get("clusterName", "").strip()
    template_id = body.get("templateId", "").strip()

    if not cluster_name:
        raise ValidationError("clusterName is required.", {"field": "clusterName"})
    if not template_id:
        raise ValidationError("templateId is required.", {"field": "templateId"})
    if not validate_cluster_name(cluster_name):
        raise ValidationError(
            f"Invalid cluster name '{cluster_name}'. "
            "Names must be non-empty and contain only alphanumeric characters, "
            "hyphens, and underscores.",
            {"clusterName": cluster_name},
        )

    # Check budget breach before starting creation
    if check_budget_breach(PROJECTS_TABLE_NAME, project_id):
        raise BudgetExceededError(
            f"Project '{project_id}' budget has been exceeded. "
            "Cluster creation is blocked until the budget is resolved.",
            {"projectId": project_id},
        )

    # Start the creation Step Functions execution
    timestamp = int(time.time())
    payload = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": template_id,
        "createdBy": caller,
    }

    sfn_client.start_execution(
        stateMachineArn=CREATION_STATE_MACHINE_ARN,
        name=f"{project_id}-{cluster_name}-{timestamp}",
        input=json.dumps(payload),
    )

    logger.info(
        "Cluster creation started: %s in project %s by %s",
        cluster_name,
        project_id,
        caller,
    )
    return _response(
        202,
        {
            "message": f"Cluster '{cluster_name}' creation started.",
            "projectId": project_id,
            "clusterName": cluster_name,
            "templateId": template_id,
        },
    )


def _handle_list_clusters(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle GET /projects/{projectId}/clusters — list clusters for a project."""
    if not is_project_user(event, project_id):
        raise AuthorisationError(
            "You must be a project member to list clusters."
        )

    clusters = list_clusters(
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
    )
    return _response(200, {"clusters": clusters})


def _handle_get_cluster(
    event: dict[str, Any], project_id: str, cluster_name: str
) -> dict[str, Any]:
    """Handle GET /projects/{projectId}/clusters/{clusterName} — get cluster details.

    Returns cluster details including SSH/DCV connection info for
    active clusters. Checks budget breach and denies access if breached.
    """
    if not is_project_user(event, project_id):
        raise AuthorisationError(
            "You must be a project member to view cluster details."
        )

    # Check budget breach — deny access to cluster details if breached
    if check_budget_breach(PROJECTS_TABLE_NAME, project_id):
        raise BudgetExceededError(
            f"Project '{project_id}' budget has been exceeded. "
            "Cluster access is denied until the budget is resolved.",
            {"projectId": project_id},
        )

    cluster = get_cluster(
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
        cluster_name=cluster_name,
    )

    # Enrich active clusters with connection info
    if cluster.get("status") == "ACTIVE":
        login_ip = cluster.get("loginNodeIp", "")
        ssh_port = cluster.get("sshPort", 22)
        dcv_port = cluster.get("dcvPort", 8443)
        cluster["connectionInfo"] = {
            "ssh": f"ssh -p {ssh_port} <username>@{login_ip}" if login_ip else "",
            "dcv": f"https://{login_ip}:{dcv_port}" if login_ip else "",
        }

    # Include progress fields for clusters in CREATING status
    if cluster.get("status") == "CREATING":
        cluster["progress"] = {
            "currentStep": int(cluster.get("currentStep", 0)),
            "totalSteps": int(cluster.get("totalSteps", 0)),
            "stepDescription": cluster.get("stepDescription", ""),
        }

    return _response(200, cluster)


def _handle_recreate_cluster(
    event: dict[str, Any], project_id: str, cluster_name: str
) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/clusters/{clusterName}/recreate.

    Validates authorisation, retrieves the destroyed cluster record,
    resolves the template ID (with optional override), checks budget,
    then starts the creation Step Functions execution.
    """
    caller = get_caller_identity(event)
    if not is_project_user(event, project_id):
        raise AuthorisationError(
            "You must be a project member to recreate clusters."
        )

    # Retrieve the cluster record — raises NotFoundError if missing
    cluster = get_cluster(
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
        cluster_name=cluster_name,
    )

    # Only DESTROYED clusters can be recreated
    if cluster.get("status") != "DESTROYED":
        raise ConflictError(
            f"Cluster '{cluster_name}' cannot be recreated in its current state "
            f"(status: {cluster.get('status')}). Only DESTROYED clusters can be recreated.",
            {"clusterName": cluster_name, "status": cluster.get("status")},
        )

    # Resolve templateId: use request body override, or fall back to stored value
    body = _parse_optional_body(event)
    template_id = body.get("templateId", "").strip() if body else ""
    if not template_id:
        template_id = cluster.get("templateId", "")

    # Check budget breach before starting recreation
    if check_budget_breach(PROJECTS_TABLE_NAME, project_id):
        raise BudgetExceededError(
            f"Project '{project_id}' budget has been exceeded. "
            "Cluster recreation is blocked until the budget is resolved.",
            {"projectId": project_id},
        )

    # Start the creation Step Functions execution
    timestamp = int(time.time())
    payload = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": template_id,
        "createdBy": caller,
    }

    sfn_client.start_execution(
        stateMachineArn=CREATION_STATE_MACHINE_ARN,
        name=f"{project_id}-{cluster_name}-{timestamp}",
        input=json.dumps(payload),
    )

    logger.info(
        "Cluster recreation started: %s in project %s by %s",
        cluster_name,
        project_id,
        caller,
    )
    return _response(
        202,
        {
            "message": f"Cluster '{cluster_name}' recreation started.",
            "projectId": project_id,
            "clusterName": cluster_name,
            "templateId": template_id,
        },
    )


def _handle_delete_cluster(
    event: dict[str, Any], project_id: str, cluster_name: str
) -> dict[str, Any]:
    """Handle DELETE /projects/{projectId}/clusters/{clusterName} — destroy a cluster.

    Validates authorisation, retrieves the cluster record, then starts
    the destruction Step Functions execution.
    """
    caller = get_caller_identity(event)
    if not is_project_user(event, project_id):
        raise AuthorisationError(
            "You must be a project member to destroy clusters."
        )

    # Retrieve the cluster to get resource IDs for the destruction workflow
    cluster = get_cluster(
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
        cluster_name=cluster_name,
    )

    if cluster.get("status") not in ("ACTIVE", "FAILED"):
        raise ConflictError(
            f"Cluster '{cluster_name}' cannot be destroyed in its current state "
            f"(status: {cluster.get('status')}).",
            {"clusterName": cluster_name, "status": cluster.get("status")},
        )

    # Start the destruction Step Functions execution
    timestamp = int(time.time())
    payload = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "pcsClusterId": cluster.get("pcsClusterId", ""),
        "pcsClusterArn": cluster.get("pcsClusterArn", ""),
        "loginNodeGroupId": cluster.get("loginNodeGroupId", ""),
        "computeNodeGroupId": cluster.get("computeNodeGroupId", ""),
        "queueId": cluster.get("queueId", ""),
        "fsxFilesystemId": cluster.get("fsxFilesystemId", ""),
        "destroyedBy": caller,
    }

    sfn_client.start_execution(
        stateMachineArn=DESTRUCTION_STATE_MACHINE_ARN,
        name=f"{project_id}-{cluster_name}-destroy-{timestamp}",
        input=json.dumps(payload),
    )

    logger.info(
        "Cluster destruction started: %s in project %s by %s",
        cluster_name,
        project_id,
        caller,
    )
    return _response(
        202,
        {
            "message": f"Cluster '{cluster_name}' destruction started.",
            "projectId": project_id,
            "clusterName": cluster_name,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON request body from an API Gateway event."""
    body = event.get("body")
    if not body:
        raise ValidationError("Request body is required.", {})
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise ValidationError("Request body must be valid JSON.", {})


def _parse_optional_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse an optional JSON request body from an API Gateway event.

    Returns an empty dict when the body is absent or empty.
    Raises ``ValidationError`` if the body is present but not valid JSON.
    """
    body = event.get("body")
    if not body:
        return {}
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
