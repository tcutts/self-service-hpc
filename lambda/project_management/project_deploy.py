"""Project deployment workflow step handlers.

Each function in this module implements a single step of the project
deploy Step Functions state machine.  Every handler receives an
``event`` dict (the state machine payload) and returns a dict that
Step Functions passes to the next step.

Deployment steps:
    1. Validate project state (must be DEPLOYING)
    2. Start CDK deploy via CodeBuild
    3. Poll CodeBuild build status
    4. Extract CloudFormation stack outputs
    5. Record infrastructure IDs in DynamoDB, transition to ACTIVE

On failure the state machine invokes ``handle_deploy_failure`` which
transitions the project back to CREATED and stores the error message.

Environment variables
---------------------
PROJECTS_TABLE_NAME     DynamoDB Projects table
CODEBUILD_PROJECT_NAME  CodeBuild project for CDK deploy/destroy
"""

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

import lifecycle
from errors import InternalError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
codebuild_client = boto3.client("codebuild")
cfn_client = boto3.client("cloudformation")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
CODEBUILD_PROJECT_NAME = os.environ.get("CODEBUILD_PROJECT_NAME", "")

# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
TOTAL_STEPS = 5

STEP_LABELS: dict[int, str] = {
    1: "Validating project state",
    2: "Starting CDK deployment",
    3: "Deploying infrastructure",
    4: "Extracting stack outputs",
    5: "Recording infrastructure",
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
# Step 1 — Validate project state
# ===================================================================

def validate_project_state(event: dict[str, Any]) -> dict[str, Any]:
    """Verify the project exists and its status is DEPLOYING.

    Updates progress to step 1.

    Returns the event dict enriched with project metadata on success.
    """
    project_id: str = event.get("projectId", "")
    if not project_id:
        raise ValidationError("projectId is required.", {"field": "projectId"})

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 1, TOTAL_STEPS, STEP_LABELS[1],
    )

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

    status = item.get("status", "")
    if status != "DEPLOYING":
        raise ValidationError(
            f"Project '{project_id}' is in status '{status}', expected 'DEPLOYING'.",
            {"projectId": project_id, "currentStatus": status},
        )

    logger.info("Project '%s' validated — status is DEPLOYING", project_id)
    return event


# ===================================================================
# Step 2 — Start CDK deploy via CodeBuild
# ===================================================================

def start_cdk_deploy(event: dict[str, Any]) -> dict[str, Any]:
    """Start a CodeBuild project that runs ``npx cdk deploy``.

    Passes project parameters as environment variables to CodeBuild.
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
                    "value": f"npx cdk deploy HpcProject-{project_id} --exclusively --require-approval never",
                    "type": "PLAINTEXT",
                },
            ],
        )
    except ClientError as exc:
        raise InternalError(f"Failed to start CodeBuild deploy: {exc}")

    build_id = response["build"]["id"]
    logger.info(
        "CodeBuild deploy started for project '%s': build %s",
        project_id,
        build_id,
    )

    return {**event, "buildId": build_id}


# ===================================================================
# Step 3 — Check CodeBuild deploy status
# ===================================================================

def check_deploy_status(event: dict[str, Any]) -> dict[str, Any]:
    """Poll CodeBuild build status.

    Returns the event with ``deployComplete: True/False``.
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
        return {**event, "deployComplete": True}

    if build_status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
        raise InternalError(
            f"CodeBuild deploy failed for project '{project_id}': "
            f"build status is {build_status}.",
        )

    # Still in progress
    return {**event, "deployComplete": False}


# ===================================================================
# Step 4 — Extract CloudFormation stack outputs
# ===================================================================

def extract_stack_outputs(event: dict[str, Any]) -> dict[str, Any]:
    """Describe the CloudFormation stack to extract infrastructure IDs.

    Extracts VpcId, EfsFileSystemId, S3BucketName, and security group
    IDs from the stack outputs.
    Updates progress to step 4.

    Adds infrastructure IDs to the returned event.
    """
    project_id: str = event["projectId"]
    stack_name = f"HpcProject-{project_id}"

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 4, TOTAL_STEPS, STEP_LABELS[4],
    )

    try:
        response = cfn_client.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        raise InternalError(
            f"Failed to describe CloudFormation stack '{stack_name}': {exc}"
        )

    stacks = response.get("Stacks", [])
    if not stacks:
        raise InternalError(f"CloudFormation stack '{stack_name}' not found.")

    outputs = stacks[0].get("Outputs", [])
    output_map: dict[str, str] = {
        o["OutputKey"]: o["OutputValue"] for o in outputs
    }

    vpc_id = output_map.get("VpcId", "")
    efs_filesystem_id = output_map.get("EfsFileSystemId", "")
    s3_bucket_name = output_map.get("S3BucketName", "")
    head_node_sg = output_map.get("HeadNodeSecurityGroupId", "")
    compute_node_sg = output_map.get("ComputeNodeSecurityGroupId", "")
    efs_sg = output_map.get("EfsSecurityGroupId", "")
    fsx_sg = output_map.get("FsxSecurityGroupId", "")

    # Subnet IDs are stored as comma-separated strings in stack outputs
    public_subnet_ids_raw = output_map.get("PublicSubnetIds", "")
    private_subnet_ids_raw = output_map.get("PrivateSubnetIds", "")
    public_subnet_ids = [s for s in public_subnet_ids_raw.split(",") if s]
    private_subnet_ids = [s for s in private_subnet_ids_raw.split(",") if s]

    logger.info(
        "Stack outputs for project '%s': VPC=%s, EFS=%s, S3=%s",
        project_id,
        vpc_id,
        efs_filesystem_id,
        s3_bucket_name,
    )

    login_launch_template_id = output_map.get("LoginLaunchTemplateId", "")
    compute_launch_template_id = output_map.get("ComputeLaunchTemplateId", "")

    return {
        **event,
        "cdkStackName": stack_name,
        "vpcId": vpc_id,
        "efsFileSystemId": efs_filesystem_id,
        "s3BucketName": s3_bucket_name,
        "publicSubnetIds": public_subnet_ids,
        "privateSubnetIds": private_subnet_ids,
        "securityGroupIds": {
            "headNode": head_node_sg,
            "computeNode": compute_node_sg,
            "efs": efs_sg,
            "fsx": fsx_sg,
        },
        "loginLaunchTemplateId": login_launch_template_id,
        "computeLaunchTemplateId": compute_launch_template_id,
    }


# ===================================================================
# Step 5 — Record infrastructure IDs and transition to ACTIVE
# ===================================================================

def record_infrastructure(event: dict[str, Any]) -> dict[str, Any]:
    """Write infrastructure IDs to the project DynamoDB record.

    Transitions the project status to ACTIVE via
    ``lifecycle.transition_project()``.
    Updates progress to step 5.
    """
    project_id: str = event["projectId"]

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 5, TOTAL_STEPS, STEP_LABELS[5],
    )

    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    try:
        table.update_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET vpcId = :vpc, efsFileSystemId = :efs, "
                "s3BucketName = :s3, cdkStackName = :stack, "
                "publicSubnetIds = :pubsubs, privateSubnetIds = :privsubs, "
                "securityGroupIds = :sgs, "
                "loginLaunchTemplateId = :llt, "
                "computeLaunchTemplateId = :clt"
            ),
            ExpressionAttributeValues={
                ":vpc": event.get("vpcId", ""),
                ":efs": event.get("efsFileSystemId", ""),
                ":s3": event.get("s3BucketName", ""),
                ":stack": event.get("cdkStackName", ""),
                ":pubsubs": event.get("publicSubnetIds", []),
                ":privsubs": event.get("privateSubnetIds", []),
                ":sgs": event.get("securityGroupIds", {}),
                ":llt": event.get("loginLaunchTemplateId", ""),
                ":clt": event.get("computeLaunchTemplateId", ""),
            },
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to record infrastructure for project '{project_id}': {exc}"
        )

    # Transition to ACTIVE
    lifecycle.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="ACTIVE",
    )

    logger.info(
        "Project '%s' infrastructure recorded and transitioned to ACTIVE",
        project_id,
    )

    return {**event, "status": "ACTIVE"}


# ===================================================================
# Failure handler — transition back to CREATED
# ===================================================================

def handle_deploy_failure(event: dict[str, Any]) -> dict[str, Any]:
    """Handle deployment failure by transitioning the project back to CREATED.

    Stores the error message in the project record.
    """
    project_id: str = event.get("projectId", "")
    error_info = event.get("error", {})
    error_message = error_info.get("Cause", event.get("errorMessage", "Unknown error"))

    logger.error(
        "Deployment failed for project '%s': %s",
        project_id,
        error_message,
    )

    if project_id:
        try:
            lifecycle.transition_project(
                table_name=PROJECTS_TABLE_NAME,
                project_id=project_id,
                target_status="CREATED",
                error_message=error_message,
            )
            logger.info(
                "Project '%s' transitioned back to CREATED after deploy failure",
                project_id,
            )
        except Exception:
            logger.exception(
                "Failed to transition project '%s' back to CREATED",
                project_id,
            )

    return {
        **event,
        "status": "CREATED",
        "errorMessage": error_message,
    }


# ===================================================================
# Step Functions Lambda entry point
# ===================================================================

STEP_DISPATCH: dict[str, Any] = {
    "validate_project_state": validate_project_state,
    "start_cdk_deploy": start_cdk_deploy,
    "check_deploy_status": check_deploy_status,
    "extract_stack_outputs": extract_stack_outputs,
    "record_infrastructure": record_infrastructure,
    "handle_deploy_failure": handle_deploy_failure,
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
        raise ValueError(f"Unknown deploy step: '{step}'")

    return handler_fn(payload)
