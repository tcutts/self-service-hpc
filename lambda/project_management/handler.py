"""Project Management Lambda handler.

Handles CRUD operations for projects, project membership management,
project budget configuration, and project lifecycle operations
(deploy, destroy, edit).

Environment variables:
    PROJECTS_TABLE_NAME: DynamoDB Projects table name
    CLUSTERS_TABLE_NAME: DynamoDB Clusters table name
    USERS_TABLE_NAME: DynamoDB PlatformUsers table name
    USER_POOL_ID: Cognito User Pool ID
    BUDGET_SNS_TOPIC_ARN: SNS topic ARN for budget notifications
    PROJECT_DEPLOY_STATE_MACHINE_ARN: Step Functions state machine ARN for project deployment
    PROJECT_DESTROY_STATE_MACHINE_ARN: Step Functions state machine ARN for project destruction
    PROJECT_UPDATE_STATE_MACHINE_ARN: Step Functions state machine ARN for project update
"""

import json
import logging
import os
import sys
from typing import Any

import boto3

# Add shared utilities to the module search path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from api_logging import log_api_action  # noqa: E402

from auth import get_caller_identity, is_administrator, is_project_admin
from errors import (
    ApiError,
    AuthorisationError,
    ConflictError,
    DuplicateError,
    InternalError,
    NotFoundError,
    ValidationError,
    build_error_response,
)
from projects import create_project, delete_project, get_foundation_timestamp, get_project, list_projects, _get_active_clusters
from members import add_member, remove_member
from budget import set_budget
import lifecycle

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn_client = boto3.client("stepfunctions")

PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "PlatformUsers")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
BUDGET_SNS_TOPIC_ARN = os.environ.get("BUDGET_SNS_TOPIC_ARN", "")
PROJECT_DEPLOY_STATE_MACHINE_ARN = os.environ.get("PROJECT_DEPLOY_STATE_MACHINE_ARN", "")
PROJECT_DESTROY_STATE_MACHINE_ARN = os.environ.get("PROJECT_DESTROY_STATE_MACHINE_ARN", "")
PROJECT_UPDATE_STATE_MACHINE_ARN = os.environ.get("PROJECT_UPDATE_STATE_MACHINE_ARN", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route API Gateway proxy events to the appropriate project operation."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_parameters = event.get("pathParameters") or {}

    logger.info(
        "Project management request: %s %s",
        http_method,
        resource,
    )

    try:
        # Project CRUD routes
        if resource == "/projects" and http_method == "POST":
            response = _handle_create_project(event)
        elif resource == "/projects" and http_method == "GET":
            response = _handle_list_projects(event)
        elif resource == "/projects/{projectId}" and http_method == "GET":
            project_id = path_parameters.get("projectId", "")
            response = _handle_get_project(event, project_id)
        elif resource == "/projects/{projectId}" and http_method == "DELETE":
            project_id = path_parameters.get("projectId", "")
            response = _handle_delete_project(event, project_id)

        # Membership routes
        elif resource == "/projects/{projectId}/members" and http_method == "POST":
            project_id = path_parameters.get("projectId", "")
            response = _handle_add_member(event, project_id)
        elif (
            resource == "/projects/{projectId}/members/{userId}"
            and http_method == "DELETE"
        ):
            project_id = path_parameters.get("projectId", "")
            user_id = path_parameters.get("userId", "")
            response = _handle_remove_member(event, project_id, user_id)

        # Budget route
        elif resource == "/projects/{projectId}/budget" and http_method == "PUT":
            project_id = path_parameters.get("projectId", "")
            response = _handle_set_budget(event, project_id)

        # Deploy route
        elif resource == "/projects/{projectId}/deploy" and http_method == "POST":
            project_id = path_parameters.get("projectId", "")
            response = _handle_deploy_project(event, project_id)

        # Destroy route
        elif resource == "/projects/{projectId}/destroy" and http_method == "POST":
            project_id = path_parameters.get("projectId", "")
            response = _handle_destroy_project_infra(event, project_id)

        # Update route
        elif resource == "/projects/{projectId}/update" and http_method == "POST":
            project_id = path_parameters.get("projectId", "")
            response = _handle_update_project(event, project_id)

        # Batch routes
        elif resource == "/projects/batch/update" and http_method == "POST":
            response = _handle_batch_update(event)
        elif resource == "/projects/batch/deploy" and http_method == "POST":
            response = _handle_batch_deploy(event)
        elif resource == "/projects/batch/destroy" and http_method == "POST":
            response = _handle_batch_destroy(event)

        # Edit route
        elif resource == "/projects/{projectId}" and http_method == "PUT":
            project_id = path_parameters.get("projectId", "")
            response = _handle_edit_project(event, project_id)

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
        InternalError,
    ) as exc:
        response = build_error_response(exc)
    except Exception:
        logger.exception("Unhandled error in project management handler")
        response = _response(
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "details": {}}},
        )

    # Log the API action to CloudWatch (Requirement 13.3)
    log_api_action(event, response)
    return response


def _handle_create_project(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /projects — create a new project."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can create projects.")

    body = _parse_body(event)
    project_id = body.get("projectId", "").strip()
    project_name = body.get("projectName", "").strip()
    cost_allocation_tag = body.get("costAllocationTag", "").strip() or None

    project_record = create_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        project_name=project_name,
        cost_allocation_tag=cost_allocation_tag,
    )
    logger.info("Project created: %s by %s", project_id, caller)
    return _response(201, project_record)


def _handle_list_projects(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /projects — list all projects."""
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can list all projects.")

    projects = list_projects(table_name=PROJECTS_TABLE_NAME)
    foundation_timestamp = get_foundation_timestamp(table_name=PROJECTS_TABLE_NAME)
    return _response(200, {"projects": projects, "foundationStackTimestamp": foundation_timestamp})


def _handle_get_project(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle GET /projects/{projectId} — get a single project."""
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can view project details.")

    project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=project_id)

    # Include progress fields for transitional statuses
    if project_record.get("status") in ("DEPLOYING", "DESTROYING", "UPDATING"):
        project_record["progress"] = {
            "currentStep": int(project_record.get("currentStep", 0)),
            "totalSteps": int(project_record.get("totalSteps", 0)),
            "stepDescription": project_record.get("stepDescription", ""),
        }

    return _response(200, project_record)


def _handle_delete_project(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle DELETE /projects/{projectId} — delete a project."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can delete projects.")

    delete_project(
        table_name=PROJECTS_TABLE_NAME,
        clusters_table_name=CLUSTERS_TABLE_NAME,
        project_id=project_id,
    )
    logger.info("Project deleted: %s by %s", project_id, caller)
    return _response(200, {"message": f"Project '{project_id}' has been deleted."})


def _handle_add_member(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/members — add a member."""
    caller = get_caller_identity(event)
    if not is_project_admin(event, project_id):
        raise AuthorisationError(
            "Only project administrators can manage membership."
        )

    body = _parse_body(event)
    user_id = body.get("userId", "").strip()
    role = body.get("role", "PROJECT_USER").strip()

    membership = add_member(
        projects_table_name=PROJECTS_TABLE_NAME,
        users_table_name=USERS_TABLE_NAME,
        user_pool_id=USER_POOL_ID,
        project_id=project_id,
        user_id=user_id,
        role=role,
    )
    logger.info(
        "Member added: %s to project %s by %s", user_id, project_id, caller
    )
    return _response(201, membership)


def _handle_remove_member(
    event: dict[str, Any], project_id: str, user_id: str
) -> dict[str, Any]:
    """Handle DELETE /projects/{projectId}/members/{userId} — remove a member."""
    caller = get_caller_identity(event)
    if not is_project_admin(event, project_id):
        raise AuthorisationError(
            "Only project administrators can manage membership."
        )

    remove_member(
        projects_table_name=PROJECTS_TABLE_NAME,
        user_pool_id=USER_POOL_ID,
        project_id=project_id,
        user_id=user_id,
    )
    logger.info(
        "Member removed: %s from project %s by %s", user_id, project_id, caller
    )
    return _response(
        200, {"message": f"User '{user_id}' removed from project '{project_id}'."}
    )


def _handle_set_budget(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle PUT /projects/{projectId}/budget — set project budget."""
    caller = get_caller_identity(event)
    if not is_project_admin(event, project_id):
        raise AuthorisationError(
            "Only project administrators can manage budgets."
        )

    body = _parse_body(event)
    budget_limit = body.get("budgetLimit")
    if budget_limit is None:
        raise ValidationError("budgetLimit is required.", {"field": "budgetLimit"})
    try:
        budget_limit = float(budget_limit)
    except (TypeError, ValueError):
        raise ValidationError(
            "budgetLimit must be a number.", {"field": "budgetLimit"}
        )

    result = set_budget(
        projects_table_name=PROJECTS_TABLE_NAME,
        budget_sns_topic_arn=BUDGET_SNS_TOPIC_ARN,
        project_id=project_id,
        budget_limit=budget_limit,
    )
    logger.info(
        "Budget set: %s for project %s by %s", budget_limit, project_id, caller
    )
    return _response(200, result)


def _handle_deploy_project(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/deploy — start infrastructure deployment."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can deploy project infrastructure.")

    # Verify project exists and status is CREATED
    project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=project_id)
    if project_record.get("status") != "CREATED":
        raise ConflictError(
            f"Cannot deploy project '{project_id}': project status is "
            f"'{project_record.get('status')}', expected 'CREATED'.",
            {
                "projectId": project_id,
                "currentStatus": project_record.get("status"),
                "requiredStatus": "CREATED",
            },
        )

    # Transition to DEPLOYING
    lifecycle.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="DEPLOYING",
    )

    # Set initial progress tracking
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    table.update_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        UpdateExpression="SET currentStep = :step, totalSteps = :total",
        ExpressionAttributeValues={
            ":step": 0,
            ":total": 5,
        },
    )

    # Start Step Functions execution
    if PROJECT_DEPLOY_STATE_MACHINE_ARN:
        try:
            sfn_client.start_execution(
                stateMachineArn=PROJECT_DEPLOY_STATE_MACHINE_ARN,
                input=json.dumps({"projectId": project_id}),
            )
            logger.info(
                "Started deploy execution for project '%s' by %s",
                project_id,
                caller,
            )
        except Exception:
            logger.warning(
                "Failed to start deploy Step Functions execution for project '%s'. "
                "The state machine may not exist yet.",
                project_id,
                exc_info=True,
            )
    else:
        logger.warning(
            "PROJECT_DEPLOY_STATE_MACHINE_ARN is not set. "
            "Skipping Step Functions execution for project '%s'.",
            project_id,
        )

    return _response(202, {
        "message": f"Project '{project_id}' deployment started.",
        "projectId": project_id,
        "status": "DEPLOYING",
    })


def _handle_destroy_project_infra(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/destroy — start infrastructure destruction."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can destroy project infrastructure.")

    # Verify project exists and status is ACTIVE
    project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=project_id)
    if project_record.get("status") != "ACTIVE":
        raise ConflictError(
            f"Cannot destroy project '{project_id}': project status is "
            f"'{project_record.get('status')}', expected 'ACTIVE'.",
            {
                "projectId": project_id,
                "currentStatus": project_record.get("status"),
                "requiredStatus": "ACTIVE",
            },
        )

    # Check for active/creating clusters
    active_clusters = _get_active_clusters(CLUSTERS_TABLE_NAME, project_id)
    if active_clusters:
        cluster_names = [c["clusterName"] for c in active_clusters]
        raise ConflictError(
            f"Cannot destroy project '{project_id}': active clusters exist. "
            f"Destroy all clusters first.",
            {"projectId": project_id, "activeClusters": cluster_names},
        )

    # Transition to DESTROYING
    lifecycle.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="DESTROYING",
    )

    # Set initial progress tracking
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    table.update_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        UpdateExpression="SET currentStep = :step, totalSteps = :total",
        ExpressionAttributeValues={
            ":step": 0,
            ":total": 5,
        },
    )

    # Start Step Functions execution
    if PROJECT_DESTROY_STATE_MACHINE_ARN:
        try:
            sfn_client.start_execution(
                stateMachineArn=PROJECT_DESTROY_STATE_MACHINE_ARN,
                input=json.dumps({"projectId": project_id}),
            )
            logger.info(
                "Started destroy execution for project '%s' by %s",
                project_id,
                caller,
            )
        except Exception:
            logger.warning(
                "Failed to start destroy Step Functions execution for project '%s'. "
                "The state machine may not exist yet.",
                project_id,
                exc_info=True,
            )
    else:
        logger.warning(
            "PROJECT_DESTROY_STATE_MACHINE_ARN is not set. "
            "Skipping Step Functions execution for project '%s'.",
            project_id,
        )

    return _response(202, {
        "message": f"Project '{project_id}' destruction started.",
        "projectId": project_id,
        "status": "DESTROYING",
    })


def _handle_update_project(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle POST /projects/{projectId}/update — start infrastructure update."""
    caller = get_caller_identity(event)
    if not is_administrator(event):
        raise AuthorisationError("Only administrators can update project infrastructure.")

    # Verify project exists and status is ACTIVE
    project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=project_id)
    if project_record.get("status") != "ACTIVE":
        raise ConflictError(
            f"Cannot update project '{project_id}': project status is "
            f"'{project_record.get('status')}', expected 'ACTIVE'.",
            {
                "projectId": project_id,
                "currentStatus": project_record.get("status"),
                "requiredStatus": "ACTIVE",
            },
        )

    # Transition to UPDATING
    lifecycle.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="UPDATING",
    )

    # Set initial progress tracking
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    table.update_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        UpdateExpression="SET currentStep = :step, totalSteps = :total",
        ExpressionAttributeValues={
            ":step": 0,
            ":total": 5,
        },
    )

    # Start Step Functions execution
    if PROJECT_UPDATE_STATE_MACHINE_ARN:
        try:
            sfn_client.start_execution(
                stateMachineArn=PROJECT_UPDATE_STATE_MACHINE_ARN,
                input=json.dumps({"projectId": project_id}),
            )
            logger.info(
                "Started update execution for project '%s' by %s",
                project_id,
                caller,
            )
        except Exception:
            logger.warning(
                "Failed to start update Step Functions execution for project '%s'. "
                "The state machine may not exist yet.",
                project_id,
                exc_info=True,
            )
    else:
        logger.warning(
            "PROJECT_UPDATE_STATE_MACHINE_ARN is not set. "
            "Skipping Step Functions execution for project '%s'.",
            project_id,
        )

    return _response(202, {
        "message": f"Project '{project_id}' update started.",
        "projectId": project_id,
        "status": "UPDATING",
    })


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


def _handle_batch_update(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /projects/batch/update — batch update multiple projects."""
    project_ids = _validate_batch_request(event, "projectIds")
    caller = get_caller_identity(event)
    results: list[dict[str, Any]] = []

    for pid in project_ids:
        try:
            project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=pid)
            if project_record.get("status") != "ACTIVE":
                raise ConflictError(
                    f"Cannot update project '{pid}': project status is "
                    f"'{project_record.get('status')}', expected 'ACTIVE'.",
                    {"projectId": pid, "currentStatus": project_record.get("status"), "requiredStatus": "ACTIVE"},
                )

            lifecycle.transition_project(
                table_name=PROJECTS_TABLE_NAME,
                project_id=pid,
                target_status="UPDATING",
            )

            dynamodb_resource = boto3.resource("dynamodb")
            table = dynamodb_resource.Table(PROJECTS_TABLE_NAME)
            table.update_item(
                Key={"PK": f"PROJECT#{pid}", "SK": "METADATA"},
                UpdateExpression="SET currentStep = :step, totalSteps = :total",
                ExpressionAttributeValues={":step": 0, ":total": 5},
            )

            if PROJECT_UPDATE_STATE_MACHINE_ARN:
                sfn_client.start_execution(
                    stateMachineArn=PROJECT_UPDATE_STATE_MACHINE_ARN,
                    input=json.dumps({"projectId": pid}),
                )

            logger.info("Batch update started for project '%s' by %s", pid, caller)
            results.append({"id": pid, "status": "success", "message": "Update started"})
        except ApiError as exc:
            logger.warning("Batch update failed for project '%s': %s", pid, str(exc))
            results.append({"id": pid, "status": "error", "message": str(exc)})

    return _build_batch_response(results)


def _handle_batch_deploy(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /projects/batch/deploy — batch deploy multiple projects."""
    project_ids = _validate_batch_request(event, "projectIds")
    caller = get_caller_identity(event)
    results: list[dict[str, Any]] = []

    for pid in project_ids:
        try:
            project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=pid)
            if project_record.get("status") != "CREATED":
                raise ConflictError(
                    f"Cannot deploy project '{pid}': project status is "
                    f"'{project_record.get('status')}', expected 'CREATED'.",
                    {"projectId": pid, "currentStatus": project_record.get("status"), "requiredStatus": "CREATED"},
                )

            lifecycle.transition_project(
                table_name=PROJECTS_TABLE_NAME,
                project_id=pid,
                target_status="DEPLOYING",
            )

            dynamodb_resource = boto3.resource("dynamodb")
            table = dynamodb_resource.Table(PROJECTS_TABLE_NAME)
            table.update_item(
                Key={"PK": f"PROJECT#{pid}", "SK": "METADATA"},
                UpdateExpression="SET currentStep = :step, totalSteps = :total",
                ExpressionAttributeValues={":step": 0, ":total": 5},
            )

            if PROJECT_DEPLOY_STATE_MACHINE_ARN:
                sfn_client.start_execution(
                    stateMachineArn=PROJECT_DEPLOY_STATE_MACHINE_ARN,
                    input=json.dumps({"projectId": pid}),
                )

            logger.info("Batch deploy started for project '%s' by %s", pid, caller)
            results.append({"id": pid, "status": "success", "message": "Deploy started"})
        except ApiError as exc:
            logger.warning("Batch deploy failed for project '%s': %s", pid, str(exc))
            results.append({"id": pid, "status": "error", "message": str(exc)})

    return _build_batch_response(results)


def _handle_batch_destroy(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /projects/batch/destroy — batch destroy multiple projects."""
    project_ids = _validate_batch_request(event, "projectIds")
    caller = get_caller_identity(event)
    results: list[dict[str, Any]] = []

    for pid in project_ids:
        try:
            project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=pid)
            if project_record.get("status") != "ACTIVE":
                raise ConflictError(
                    f"Cannot destroy project '{pid}': project status is "
                    f"'{project_record.get('status')}', expected 'ACTIVE'.",
                    {"projectId": pid, "currentStatus": project_record.get("status"), "requiredStatus": "ACTIVE"},
                )

            active_clusters = _get_active_clusters(CLUSTERS_TABLE_NAME, pid)
            if active_clusters:
                cluster_names = [c["clusterName"] for c in active_clusters]
                raise ConflictError(
                    f"Cannot destroy project '{pid}': active clusters exist. "
                    f"Destroy all clusters first.",
                    {"projectId": pid, "activeClusters": cluster_names},
                )

            lifecycle.transition_project(
                table_name=PROJECTS_TABLE_NAME,
                project_id=pid,
                target_status="DESTROYING",
            )

            dynamodb_resource = boto3.resource("dynamodb")
            table = dynamodb_resource.Table(PROJECTS_TABLE_NAME)
            table.update_item(
                Key={"PK": f"PROJECT#{pid}", "SK": "METADATA"},
                UpdateExpression="SET currentStep = :step, totalSteps = :total",
                ExpressionAttributeValues={":step": 0, ":total": 5},
            )

            if PROJECT_DESTROY_STATE_MACHINE_ARN:
                sfn_client.start_execution(
                    stateMachineArn=PROJECT_DESTROY_STATE_MACHINE_ARN,
                    input=json.dumps({"projectId": pid}),
                )

            logger.info("Batch destroy started for project '%s' by %s", pid, caller)
            results.append({"id": pid, "status": "success", "message": "Destroy started"})
        except ApiError as exc:
            logger.warning("Batch destroy failed for project '%s': %s", pid, str(exc))
            results.append({"id": pid, "status": "error", "message": str(exc)})

    return _build_batch_response(results)


def _handle_edit_project(event: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Handle PUT /projects/{projectId} — update editable project fields."""
    caller = get_caller_identity(event)
    if not is_project_admin(event, project_id):
        raise AuthorisationError(
            "Only project administrators can edit projects."
        )

    # Verify project exists and status is ACTIVE
    project_record = get_project(table_name=PROJECTS_TABLE_NAME, project_id=project_id)
    if project_record.get("status") != "ACTIVE":
        raise ConflictError(
            f"Cannot edit project '{project_id}': project status is "
            f"'{project_record.get('status')}', expected 'ACTIVE'.",
            {
                "projectId": project_id,
                "currentStatus": project_record.get("status"),
                "requiredStatus": "ACTIVE",
            },
        )

    body = _parse_body(event)

    # Validate budgetLimit
    budget_limit = body.get("budgetLimit")
    if budget_limit is None:
        raise ValidationError("budgetLimit is required.", {"field": "budgetLimit"})
    try:
        budget_limit = float(budget_limit)
    except (TypeError, ValueError):
        raise ValidationError(
            "budgetLimit must be a number.", {"field": "budgetLimit"}
        )
    if budget_limit <= 0:
        raise ValidationError(
            "budgetLimit must be a positive number.",
            {"field": "budgetLimit"},
        )

    # Validate budgetType
    budget_type = body.get("budgetType", "MONTHLY")
    if budget_type not in ("MONTHLY", "TOTAL"):
        raise ValidationError(
            "budgetType must be 'MONTHLY' or 'TOTAL'.",
            {"field": "budgetType"},
        )

    # Update budget via set_budget (now supports budget_type and breach clearing)
    result = set_budget(
        projects_table_name=PROJECTS_TABLE_NAME,
        budget_sns_topic_arn=BUDGET_SNS_TOPIC_ARN,
        project_id=project_id,
        budget_limit=budget_limit,
        budget_type=budget_type,
        caller_identity=caller,
    )

    logger.info(
        "Project edited: %s by %s (budgetLimit=%s, budgetType=%s)",
        project_id,
        caller,
        budget_limit,
        budget_type,
    )

    # Return updated project
    updated_project = get_project(table_name=PROJECTS_TABLE_NAME, project_id=project_id)
    return _response(200, updated_project)


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
