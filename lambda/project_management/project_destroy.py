"""Project destruction workflow step handlers.

Each function in this module implements a single step of the project
destroy Step Functions state machine.  Every handler receives an
``event`` dict (the state machine payload) and returns a dict that
Step Functions passes to the next step.

Destruction steps:
    1. Validate project state (must be DESTROYING) and check no active clusters
    2. Start CDK destroy via CodeBuild
    3. Poll CodeBuild build status
    4. Clear infrastructure IDs from the project record
    5. Archive the project (transition to ARCHIVED)

On failure the state machine invokes ``handle_destroy_failure`` which
transitions the project back to ACTIVE and stores the error message.

Environment variables
---------------------
PROJECTS_TABLE_NAME     DynamoDB Projects table
CLUSTERS_TABLE_NAME     DynamoDB Clusters table
CODEBUILD_PROJECT_NAME  CodeBuild project for CDK deploy/destroy
"""

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

import lifecycle
from errors import ConflictError, InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
codebuild_client = boto3.client("codebuild")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")
CODEBUILD_PROJECT_NAME = os.environ.get("CODEBUILD_PROJECT_NAME", "")

# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
TOTAL_STEPS = 5

STEP_LABELS: dict[int, str] = {
    1: "Validating project state",
    2: "Starting CDK destruction",
    3: "Destroying infrastructure",
    4: "Clearing infrastructure records",
    5: "Archiving project",
}


def _update_project_progress(
    table_name: str,
    project_id: str,
    step: int,
    total: int,
    description: str,
) -> None:
    """Write the current step progress to the DynamoDB Projects record.

    Updates ``currentStep``, ``totalSteps``, and ``stepDescription``
    so the GET endpoint can report progress to the UI.
    """
    table = dynamodb.Table(table_name)
    try:
        table.update_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET currentStep = :step, totalSteps = :total, "
                "stepDescription = :desc"
            ),
            ExpressionAttributeValues={
                ":step": step,
                ":total": total,
                ":desc": description,
            },
        )
        logger.info(
            "Progress updated for project '%s': step %d/%d — %s",
            project_id,
            step,
            total,
            description,
        )
    except ClientError as exc:
        # Progress tracking failure is non-fatal — log and continue
        logger.warning(
            "Failed to update progress for project '%s': %s",
            project_id,
            exc,
        )


# ===================================================================
# Step 1 — Validate project state and check for active clusters
# ===================================================================

def validate_and_check_clusters(event: dict[str, Any]) -> dict[str, Any]:
    """Verify the project status is DESTROYING and no active clusters remain.

    Updates progress to step 1.

    Raises ``ConflictError`` if active clusters still exist.
    """
    project_id: str = event.get("projectId", "")
    if not project_id:
        raise ValidationError("projectId is required.", {"field": "projectId"})

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 1, TOTAL_STEPS, STEP_LABELS[1],
    )

    # Verify project exists and status is DESTROYING
    projects_table = dynamodb.Table(PROJECTS_TABLE_NAME)
    response = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )

    item = response.get("Item")
    if not item:
        raise NotFoundError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )

    status = item.get("status", "")
    if status != "DESTROYING":
        raise ValidationError(
            f"Project '{project_id}' is in status '{status}', expected 'DESTROYING'.",
            {"projectId": project_id, "currentStatus": status},
        )

    # Check for active clusters
    clusters_table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    clusters_response = clusters_table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
            & boto3.dynamodb.conditions.Key("SK").begins_with("CLUSTER#")
        ),
    )

    active_clusters = [
        c for c in clusters_response.get("Items", [])
        if c.get("status") in ("ACTIVE", "CREATING")
    ]

    if active_clusters:
        cluster_names = [c.get("clusterName", "") for c in active_clusters]
        raise ConflictError(
            f"Cannot destroy project '{project_id}': active clusters remain.",
            {"projectId": project_id, "activeClusters": cluster_names},
        )

    logger.info(
        "Project '%s' validated — status is DESTROYING, no active clusters",
        project_id,
    )
    return event


# ===================================================================
# Step 2 — Start CDK destroy via CodeBuild
# ===================================================================

def start_cdk_destroy(event: dict[str, Any]) -> dict[str, Any]:
    """Start a CodeBuild project that runs ``npx cdk destroy``.

    Updates progress to step 2.

    Adds ``buildId`` to the returned event.
    """
    project_id: str = event["projectId"]

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 2, TOTAL_STEPS, STEP_LABELS[2],
    )

    if not CODEBUILD_PROJECT_NAME:
        raise InternalError(
            "CODEBUILD_PROJECT_NAME environment variable is not set.",
        )

    try:
        response = codebuild_client.start_build(
            projectName=CODEBUILD_PROJECT_NAME,
            environmentVariablesOverride=[
                {
                    "name": "PROJECT_ID",
                    "value": project_id,
                    "type": "PLAINTEXT",
                },
                {
                    "name": "CDK_COMMAND",
                    "value": f"npx cdk destroy HpcProject-{project_id} --force",
                    "type": "PLAINTEXT",
                },
            ],
        )
    except ClientError as exc:
        raise InternalError(f"Failed to start CodeBuild destroy: {exc}")

    build_id = response["build"]["id"]
    logger.info(
        "CodeBuild destroy started for project '%s': build %s",
        project_id,
        build_id,
    )

    return {**event, "buildId": build_id}


# ===================================================================
# Step 3 — Check CodeBuild destroy status
# ===================================================================

def check_destroy_status(event: dict[str, Any]) -> dict[str, Any]:
    """Poll CodeBuild build status.

    Returns the event with ``destroyComplete: True/False``.
    Step Functions uses this to decide whether to wait and retry.
    Updates progress to step 3.
    """
    project_id: str = event["projectId"]
    build_id: str = event.get("buildId", "")

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 3, TOTAL_STEPS, STEP_LABELS[3],
    )

    if not build_id:
        raise InternalError("buildId is missing from the event.")

    try:
        response = codebuild_client.batch_get_builds(ids=[build_id])
    except ClientError as exc:
        raise InternalError(f"Failed to describe CodeBuild build: {exc}")

    builds = response.get("builds", [])
    if not builds:
        raise InternalError(f"CodeBuild build '{build_id}' not found.")

    build = builds[0]
    build_status = build.get("buildStatus", "IN_PROGRESS")

    logger.info(
        "CodeBuild build '%s' for project '%s' status: %s",
        build_id,
        project_id,
        build_status,
    )

    if build_status == "SUCCEEDED":
        return {**event, "destroyComplete": True}

    if build_status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
        raise InternalError(
            f"CodeBuild destroy failed for project '{project_id}': "
            f"build status is {build_status}.",
        )

    # Still in progress
    return {**event, "destroyComplete": False}


# ===================================================================
# Step 4 — Clear infrastructure IDs from the project record
# ===================================================================

def clear_infrastructure(event: dict[str, Any]) -> dict[str, Any]:
    """Clear infrastructure IDs from the project DynamoDB record.

    Removes vpcId, efsFileSystemId, s3BucketName, and cdkStackName
    by setting them to empty strings.
    Updates progress to step 4.
    """
    project_id: str = event["projectId"]

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 4, TOTAL_STEPS, STEP_LABELS[4],
    )

    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    try:
        table.update_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET vpcId = :empty, efsFileSystemId = :empty, "
                "s3BucketName = :empty, cdkStackName = :empty"
            ),
            ExpressionAttributeValues={
                ":empty": "",
            },
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to clear infrastructure for project '{project_id}': {exc}"
        )

    logger.info(
        "Infrastructure IDs cleared for project '%s'",
        project_id,
    )

    return event


# ===================================================================
# Step 5 — Archive the project
# ===================================================================

def archive_project(event: dict[str, Any]) -> dict[str, Any]:
    """Transition the project status to ARCHIVED.

    Uses ``lifecycle.transition_project()`` for atomic state transition.
    Updates progress to step 5.
    """
    project_id: str = event["projectId"]

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 5, TOTAL_STEPS, STEP_LABELS[5],
    )

    lifecycle.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="ARCHIVED",
    )

    logger.info(
        "Project '%s' transitioned to ARCHIVED",
        project_id,
    )

    return {**event, "status": "ARCHIVED"}


# ===================================================================
# Failure handler — transition back to ACTIVE
# ===================================================================

def handle_destroy_failure(event: dict[str, Any]) -> dict[str, Any]:
    """Handle destruction failure by transitioning the project back to ACTIVE.

    Stores the error message in the project record.
    """
    project_id: str = event.get("projectId", "")
    error_info = event.get("error", {})
    error_message = error_info.get("Cause", event.get("errorMessage", "Unknown error"))

    logger.error(
        "Destruction failed for project '%s': %s",
        project_id,
        error_message,
    )

    if project_id:
        try:
            lifecycle.transition_project(
                table_name=PROJECTS_TABLE_NAME,
                project_id=project_id,
                target_status="ACTIVE",
                error_message=error_message,
            )
            logger.info(
                "Project '%s' transitioned back to ACTIVE after destroy failure",
                project_id,
            )
        except Exception:
            logger.exception(
                "Failed to transition project '%s' back to ACTIVE",
                project_id,
            )

    return {
        **event,
        "status": "ACTIVE",
        "errorMessage": error_message,
    }


# ===================================================================
# Step Functions Lambda entry point
# ===================================================================

STEP_DISPATCH: dict[str, Any] = {
    "validate_and_check_clusters": validate_and_check_clusters,
    "start_cdk_destroy": start_cdk_destroy,
    "check_destroy_status": check_destroy_status,
    "clear_infrastructure": clear_infrastructure,
    "archive_project": archive_project,
    "handle_destroy_failure": handle_destroy_failure,
}


def step_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dispatch Step Functions invocations to the appropriate step function.

    The state machine passes ``{ "step": "<step_name>", "payload": { ... } }``
    and this handler routes to the matching function.
    """
    step = event.get("step", "")
    payload = event.get("payload", event)

    handler_fn = STEP_DISPATCH.get(step)
    if handler_fn is None:
        raise ValueError(f"Unknown destroy step: '{step}'")

    return handler_fn(payload)
