"""Project update workflow step handlers.

Each function in this module implements a single step of the project
update Step Functions state machine.  Every handler receives an
``event`` dict (the state machine payload) and returns a dict that
Step Functions passes to the next step.

Update steps:
    1. Validate project state (must be UPDATING), snapshot current outputs
    2. Start CDK deploy via CodeBuild
    3. Poll CodeBuild build status
    4. Extract CloudFormation stack outputs
    5. Compare old vs new outputs, record updated infrastructure, transition to ACTIVE

On failure the state machine invokes ``handle_update_failure`` which
transitions the project back to ACTIVE (not CREATED, since infrastructure
already exists) and stores the error message.

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
    2: "Starting CDK update",
    3: "Updating infrastructure",
    4: "Extracting stack outputs",
    5: "Recording updated infrastructure",
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
# Step 1 — Validate project state and snapshot current outputs
# ===================================================================

def validate_update_state(event: dict[str, Any]) -> dict[str, Any]:
    """Verify the project exists and its status is UPDATING.

    Snapshots the current infrastructure outputs into ``previousOutputs``
    in the event payload so that Step 5 can detect changes.
    Updates progress to step 1.
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
    if status != "UPDATING":
        raise ValidationError(
            f"Project '{project_id}' is in status '{status}', expected 'UPDATING'.",
            {"projectId": project_id, "currentStatus": status},
        )

    # Snapshot current infrastructure outputs for diff detection in Step 5
    previous_outputs = {
        "vpcId": item.get("vpcId", ""),
        "efsFileSystemId": item.get("efsFileSystemId", ""),
        "s3BucketName": item.get("s3BucketName", ""),
        "publicSubnetIds": item.get("publicSubnetIds", []),
        "privateSubnetIds": item.get("privateSubnetIds", []),
        "securityGroupIds": item.get("securityGroupIds", {}),
    }

    logger.info(
        "Project '%s' validated — status is UPDATING, previous outputs snapshotted",
        project_id,
    )
    return {**event, "previousOutputs": previous_outputs}


# ===================================================================
# Step 2 — Start CDK update via CodeBuild
# ===================================================================

def start_cdk_update(event: dict[str, Any]) -> dict[str, Any]:
    """Start a CodeBuild project that runs ``npx cdk deploy`` for update.

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
        raise InternalError(f"Failed to start CodeBuild update: {exc}")

    build_id = response["build"]["id"]
    logger.info(
        "CodeBuild update started for project '%s': build %s",
        project_id,
        build_id,
    )

    return {**event, "buildId": build_id}


# ===================================================================
# Step 3 — Check CodeBuild update status
# ===================================================================

def check_update_status(event: dict[str, Any]) -> dict[str, Any]:
    """Poll CodeBuild build status.

    Returns the event with ``updateComplete: True/False``.
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
        return {**event, "updateComplete": True}

    if build_status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
        raise InternalError(
            f"CodeBuild update failed for project '{project_id}': "
            f"build status is {build_status}.",
        )

    # Still in progress
    return {**event, "updateComplete": False}


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
    }


# ===================================================================
# Step 5 — Compare outputs, record infrastructure, transition to ACTIVE
# ===================================================================

# Critical fields to compare between old and new infrastructure outputs.
# Changes to these fields are logged as warnings because existing clusters
# reference the previous resource IDs.
_CRITICAL_SCALAR_FIELDS = ("vpcId", "efsFileSystemId", "s3BucketName")
_CRITICAL_SG_KEYS = ("headNode", "computeNode", "efs", "fsx")


def _log_infrastructure_changes(
    project_id: str,
    previous_outputs: dict[str, Any],
    new_event: dict[str, Any],
) -> None:
    """Compare old vs new infrastructure outputs and log warnings for changes.

    This is informational only — the update proceeds regardless.
    """
    for field in _CRITICAL_SCALAR_FIELDS:
        old_val = previous_outputs.get(field, "")
        new_val = new_event.get(field, "")
        if old_val and new_val and old_val != new_val:
            logger.warning(
                "Project '%s': %s changed from '%s' to '%s'",
                project_id,
                field,
                old_val,
                new_val,
            )

    # Compare security group IDs
    old_sgs = previous_outputs.get("securityGroupIds", {})
    new_sgs = new_event.get("securityGroupIds", {})
    for sg_key in _CRITICAL_SG_KEYS:
        old_sg = old_sgs.get(sg_key, "")
        new_sg = new_sgs.get(sg_key, "")
        if old_sg and new_sg and old_sg != new_sg:
            logger.warning(
                "Project '%s': securityGroupIds.%s changed from '%s' to '%s'",
                project_id,
                sg_key,
                old_sg,
                new_sg,
            )

    # Compare subnet IDs
    old_pub = sorted(previous_outputs.get("publicSubnetIds", []))
    new_pub = sorted(new_event.get("publicSubnetIds", []))
    if old_pub and new_pub and old_pub != new_pub:
        logger.warning(
            "Project '%s': publicSubnetIds changed from %s to %s",
            project_id,
            old_pub,
            new_pub,
        )

    old_priv = sorted(previous_outputs.get("privateSubnetIds", []))
    new_priv = sorted(new_event.get("privateSubnetIds", []))
    if old_priv and new_priv and old_priv != new_priv:
        logger.warning(
            "Project '%s': privateSubnetIds changed from %s to %s",
            project_id,
            old_priv,
            new_priv,
        )


def record_updated_infrastructure(event: dict[str, Any]) -> dict[str, Any]:
    """Compare old vs new outputs, write updated infrastructure IDs, transition to ACTIVE.

    Compares the ``previousOutputs`` snapshot (from Step 1) against the
    new outputs (from Step 4) and logs warnings for any changed critical
    resource IDs.  Then writes the updated infrastructure IDs to DynamoDB
    and transitions the project to ACTIVE.
    Updates progress to step 5.
    """
    project_id: str = event["projectId"]

    _update_project_progress(
        PROJECTS_TABLE_NAME, project_id, 5, TOTAL_STEPS, STEP_LABELS[5],
    )

    # Compare old vs new outputs and log warnings
    previous_outputs = event.get("previousOutputs", {})
    _log_infrastructure_changes(project_id, previous_outputs, event)

    # Write updated infrastructure IDs to DynamoDB
    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    try:
        table.update_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET vpcId = :vpc, efsFileSystemId = :efs, "
                "s3BucketName = :s3, cdkStackName = :stack, "
                "publicSubnetIds = :pubsubs, privateSubnetIds = :privsubs, "
                "securityGroupIds = :sgs"
            ),
            ExpressionAttributeValues={
                ":vpc": event.get("vpcId", ""),
                ":efs": event.get("efsFileSystemId", ""),
                ":s3": event.get("s3BucketName", ""),
                ":stack": event.get("cdkStackName", ""),
                ":pubsubs": event.get("publicSubnetIds", []),
                ":privsubs": event.get("privateSubnetIds", []),
                ":sgs": event.get("securityGroupIds", {}),
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
        "Project '%s' updated infrastructure recorded and transitioned to ACTIVE",
        project_id,
    )

    return {**event, "status": "ACTIVE"}


# ===================================================================
# Failure handler — transition back to ACTIVE
# ===================================================================

def handle_update_failure(event: dict[str, Any]) -> dict[str, Any]:
    """Handle update failure by transitioning the project back to ACTIVE.

    Unlike the deploy failure handler (which rolls back to CREATED),
    the update failure handler rolls back to ACTIVE because the
    infrastructure already exists and remains functional —
    CloudFormation automatically rolls back failed updates.

    Stores the error message in the project record.
    """
    project_id: str = event.get("projectId", "")
    error_info = event.get("error", {})
    error_message = error_info.get("Cause", event.get("errorMessage", "Unknown error"))

    logger.error(
        "Update failed for project '%s': %s",
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
                "Project '%s' transitioned back to ACTIVE after update failure",
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
# Consolidated step handlers
# ===================================================================

def consolidated_pre_loop(event: dict[str, Any]) -> dict[str, Any]:
    """Execute pre-loop steps sequentially in a single invocation.

    Calls validate_update_state and start_cdk_update in order.
    Each step receives the accumulated payload from prior steps.

    Raises the original error from whichever sub-step fails,
    preserving the error type and message for the catch block.

    Returns the merged payload with all fields from both steps.
    """
    steps = [
        validate_update_state,
        start_cdk_update,
    ]
    result: dict[str, Any] = {}
    for step_fn in steps:
        payload = {**event, **result}
        result = {**result, **step_fn(payload)}
    return result


def consolidated_post_loop(event: dict[str, Any]) -> dict[str, Any]:
    """Execute post-loop steps sequentially in a single invocation.

    Calls extract_stack_outputs and record_updated_infrastructure in order.
    Each step receives the accumulated payload from prior steps.

    Raises the original error from whichever sub-step fails,
    preserving the error type and message for the catch block.

    Returns the merged payload with all fields from both steps.
    """
    steps = [
        extract_stack_outputs,
        record_updated_infrastructure,
    ]
    result: dict[str, Any] = {}
    for step_fn in steps:
        payload = {**event, **result}
        result = {**result, **step_fn(payload)}
    return result


# ===================================================================
# Step Functions Lambda entry point
# ===================================================================

STEP_DISPATCH: dict[str, Any] = {
    "validate_update_state": validate_update_state,
    "start_cdk_update": start_cdk_update,
    "check_update_status": check_update_status,
    "extract_stack_outputs": extract_stack_outputs,
    "record_updated_infrastructure": record_updated_infrastructure,
    "handle_update_failure": handle_update_failure,
    "consolidated_pre_loop": consolidated_pre_loop,
    "consolidated_post_loop": consolidated_post_loop,
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
        raise ValueError(f"Unknown update step: '{step}'")

    return handler_fn(payload)
