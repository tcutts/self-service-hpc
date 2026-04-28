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
dynamodb = boto3.resource("dynamodb")


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

        elif (
            resource == "/projects/{projectId}/clusters/{clusterName}/fail"
            and http_method == "POST"
        ):
            project_id = path_parameters.get("projectId", "")
            cluster_name = path_parameters.get("clusterName", "")
            response = _handle_force_fail_cluster(event, project_id, cluster_name)

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


def _lookup_project_infrastructure(project_id: str) -> dict[str, Any]:
    """Retrieve infrastructure details for a project from DynamoDB.

    Returns a dict with vpcId, efsFileSystemId, s3BucketName,
    publicSubnetIds, privateSubnetIds, and securityGroupIds.

    Raises ``NotFoundError`` if the project does not exist.
    Raises ``ValidationError`` if required infrastructure fields are missing.
    """
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )

    infra = {
        "vpcId": item.get("vpcId", ""),
        "efsFileSystemId": item.get("efsFileSystemId", ""),
        "s3BucketName": item.get("s3BucketName", ""),
        "publicSubnetIds": item.get("publicSubnetIds", []),
        "privateSubnetIds": item.get("privateSubnetIds", []),
        "securityGroupIds": item.get("securityGroupIds", {}),
    }

    if not infra["s3BucketName"]:
        raise ValidationError(
            f"Project '{project_id}' infrastructure is incomplete — "
            "s3BucketName is missing. Has the project been deployed?",
            {"projectId": project_id},
        )
    if not infra["privateSubnetIds"]:
        raise ValidationError(
            f"Project '{project_id}' infrastructure is incomplete — "
            "privateSubnetIds are missing. Has the project been deployed?",
            {"projectId": project_id},
        )
    if not infra["securityGroupIds"]:
        raise ValidationError(
            f"Project '{project_id}' infrastructure is incomplete — "
            "securityGroupIds are missing. Has the project been deployed?",
            {"projectId": project_id},
        )

    return infra


def _validate_storage_and_scaling(
    body: dict[str, Any],
    storage_mode_default: str = "mountpoint",
) -> tuple[str, int, int | None, int | None]:
    """Validate storage mode, Lustre capacity, and node scaling from a request body.

    Returns ``(storage_mode, lustre_capacity_gib, min_nodes, max_nodes)``.
    ``min_nodes`` and ``max_nodes`` are ``None`` when not supplied (meaning
    "use template default").
    """
    storage_mode = body.get("storageMode", storage_mode_default)
    if storage_mode not in ("lustre", "mountpoint"):
        raise ValidationError(
            f"Invalid storageMode '{storage_mode}'. Must be 'lustre' or 'mountpoint'.",
            {"field": "storageMode"},
        )

    lustre_capacity_gib = body.get("lustreCapacityGiB", 1200)
    if storage_mode == "lustre":
        if not isinstance(lustre_capacity_gib, int) or lustre_capacity_gib < 1200:
            raise ValidationError(
                "Lustre capacity must be at least 1200 GiB.",
                {"field": "lustreCapacityGiB"},
            )
        if lustre_capacity_gib % 1200 != 0:
            raise ValidationError(
                "Lustre capacity must be a multiple of 1200 GiB.",
                {"field": "lustreCapacityGiB"},
            )

    min_nodes = body.get("minNodes")
    max_nodes = body.get("maxNodes")
    if min_nodes is not None:
        if not isinstance(min_nodes, int) or min_nodes < 0:
            raise ValidationError(
                "minNodes must be a non-negative integer.",
                {"field": "minNodes"},
            )
    if max_nodes is not None:
        if not isinstance(max_nodes, int) or max_nodes < 1:
            raise ValidationError(
                "maxNodes must be a positive integer.",
                {"field": "maxNodes"},
            )
    if min_nodes is not None and max_nodes is not None and min_nodes > max_nodes:
        raise ValidationError(
            "minNodes cannot exceed maxNodes.",
            {"fields": ["minNodes", "maxNodes"]},
        )

    return storage_mode, lustre_capacity_gib, min_nodes, max_nodes


def _start_cluster_creation(
    *,
    project_id: str,
    cluster_name: str,
    template_id: str,
    caller: str,
    storage_mode: str,
    lustre_capacity_gib: int,
    min_nodes: int | None,
    max_nodes: int | None,
    action_label: str = "creation",
) -> dict[str, Any]:
    """Write the initial CREATING record and start the creation Step Functions execution.

    Shared by both create and recreate flows.  Returns the 202 API response.
    """
    infra = _lookup_project_infrastructure(project_id)

    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    clusters_table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    clusters_table.put_item(
        Item={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
            "clusterName": cluster_name,
            "projectId": project_id,
            "templateId": template_id,
            "storageMode": storage_mode,
            "createdBy": caller,
            "status": "CREATING",
            "currentStep": 0,
            "totalSteps": 12,
            "stepDescription": "Starting cluster creation",
            "createdAt": now,
        },
    )

    timestamp = int(time.time())
    payload = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": template_id,
        "createdBy": caller,
        "storageMode": storage_mode,
        "lustreCapacityGiB": lustre_capacity_gib if storage_mode == "lustre" else None,
        "minNodes": min_nodes,
        "maxNodes": max_nodes,
        "vpcId": infra["vpcId"],
        "efsFileSystemId": infra["efsFileSystemId"],
        "s3BucketName": infra["s3BucketName"],
        "publicSubnetIds": infra["publicSubnetIds"],
        "privateSubnetIds": infra["privateSubnetIds"],
        "securityGroupIds": infra["securityGroupIds"],
    }

    sfn_client.start_execution(
        stateMachineArn=CREATION_STATE_MACHINE_ARN,
        name=f"{project_id}-{cluster_name}-{timestamp}",
        input=json.dumps(payload),
    )

    logger.info(
        "Cluster %s started: %s in project %s by %s",
        action_label,
        cluster_name,
        project_id,
        caller,
    )
    return _response(
        202,
        {
            "message": f"Cluster '{cluster_name}' {action_label} started.",
            "projectId": project_id,
            "clusterName": cluster_name,
            "templateId": template_id,
        },
    )


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

    storage_mode, lustre_capacity_gib, min_nodes, max_nodes = (
        _validate_storage_and_scaling(body)
    )

    if check_budget_breach(PROJECTS_TABLE_NAME, project_id):
        raise BudgetExceededError(
            f"Project '{project_id}' budget has been exceeded. "
            "Cluster creation is blocked until the budget is resolved.",
            {"projectId": project_id},
        )

    return _start_cluster_creation(
        project_id=project_id,
        cluster_name=cluster_name,
        template_id=template_id,
        caller=caller,
        storage_mode=storage_mode,
        lustre_capacity_gib=lustre_capacity_gib,
        min_nodes=min_nodes,
        max_nodes=max_nodes,
        action_label="creation",
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

    # Ensure storageMode is always present (default for legacy records)
    if "storageMode" not in cluster:
        cluster["storageMode"] = "mountpoint"

    # Only include lustreCapacityGiB when storageMode is lustre
    if cluster["storageMode"] != "lustre":
        cluster.pop("lustreCapacityGiB", None)
    elif "lustreCapacityGiB" in cluster:
        cluster["lustreCapacityGiB"] = int(cluster["lustreCapacityGiB"])

    # Ensure node scaling fields are integers (DynamoDB returns Decimal)
    if "minNodes" in cluster:
        cluster["minNodes"] = int(cluster["minNodes"])
    if "maxNodes" in cluster:
        cluster["maxNodes"] = int(cluster["maxNodes"])

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

    # For storage mode, fall back to the stored value (or "mountpoint") when
    # the request body doesn't supply one.
    storage_mode_default = cluster.get("storageMode", "mountpoint")
    storage_mode, lustre_capacity_gib, min_nodes, max_nodes = (
        _validate_storage_and_scaling(body, storage_mode_default=storage_mode_default)
    )

    if check_budget_breach(PROJECTS_TABLE_NAME, project_id):
        raise BudgetExceededError(
            f"Project '{project_id}' budget has been exceeded. "
            "Cluster recreation is blocked until the budget is resolved.",
            {"projectId": project_id},
        )

    return _start_cluster_creation(
        project_id=project_id,
        cluster_name=cluster_name,
        template_id=template_id,
        caller=caller,
        storage_mode=storage_mode,
        lustre_capacity_gib=lustre_capacity_gib,
        min_nodes=min_nodes,
        max_nodes=max_nodes,
        action_label="recreation",
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
        "storageMode": cluster.get("storageMode", "mountpoint"),
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


def _handle_force_fail_cluster(
    event: dict[str, Any], project_id: str, cluster_name: str
) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/clusters/{clusterName}/fail.

    Allows a user to manually transition a stuck CREATING cluster to
    FAILED status so they can take corrective action (destroy or recreate).
    """
    caller = get_caller_identity(event)
    if not is_project_user(event, project_id):
        raise AuthorisationError(
            "You must be a project member to force-fail clusters."
        )

    # Retrieve the cluster record — raises NotFoundError if missing
    cluster = get_cluster(
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
        cluster_name=cluster_name,
    )

    # Only CREATING clusters can be force-failed
    if cluster.get("status") != "CREATING":
        raise ConflictError(
            f"Cluster '{cluster_name}' cannot be marked as failed in its current state "
            f"(status: {cluster.get('status')}). Only CREATING clusters can be force-failed.",
            {"clusterName": cluster_name, "status": cluster.get("status")},
        )

    # Update the DynamoDB record to FAILED
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    clusters_table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    clusters_table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        },
        UpdateExpression=(
            "SET #st = :status, errorMessage = :msg, updatedAt = :now"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":status": "FAILED",
            ":msg": "Manually marked as failed by user",
            ":now": now,
        },
    )

    logger.info(
        "Cluster force-failed: %s in project %s by %s",
        cluster_name,
        project_id,
        caller,
    )
    return _response(
        200,
        {
            "message": f"Cluster '{cluster_name}' has been marked as failed.",
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
