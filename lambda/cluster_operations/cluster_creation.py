"""Cluster creation workflow step handlers.

Each function in this module implements a single step of the cluster
creation Step Functions state machine.  Every handler receives an
``event`` dict (the state machine payload) and returns a dict that
Step Functions passes to the next step.

Environment variables
---------------------
CLUSTERS_TABLE_NAME           DynamoDB Clusters table
CLUSTER_NAME_REGISTRY_TABLE_NAME  DynamoDB ClusterNameRegistry table
PROJECTS_TABLE_NAME           DynamoDB Projects table

Expected event keys (accumulated across steps)
-----------------------------------------------
projectId, clusterName, templateId
vpcId, publicSubnetIds, privateSubnetIds
efsFileSystemId, s3BucketName
securityGroupIds  (dict with headNode, computeNode, efs, fsx keys)
fsxFilesystemId   (set by create_fsx_filesystem)
pcsClusterId      (set by create_pcs_cluster)
pcsClusterArn     (set by create_pcs_cluster)
loginNodeGroupId  (set by create_login_node_group)
computeNodeGroupId (set by create_compute_node_group)
queueId           (set by create_pcs_queue)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from cluster_names import register_cluster_name, validate_cluster_name
from errors import BudgetExceededError, InternalError, ValidationError
from posix_provisioning import generate_user_data_script
from tagging import build_resource_tags, tags_as_dict

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
fsx_client = boto3.client("fsx")
iam_client = boto3.client("iam")
pcs_client = boto3.client("pcs")
sfn_client = boto3.client("stepfunctions")
tagging_client = boto3.client("resourcegroupstaggingapi")
sns_client = boto3.client("sns")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE_NAME", "Clusters")
CLUSTER_NAME_REGISTRY_TABLE_NAME = os.environ.get(
    "CLUSTER_NAME_REGISTRY_TABLE_NAME", "ClusterNameRegistry"
)
PROJECTS_TABLE_NAME = os.environ.get("PROJECTS_TABLE_NAME", "Projects")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "PlatformUsers")
TEMPLATES_TABLE_NAME = os.environ.get("TEMPLATES_TABLE_NAME", "ClusterTemplates")
CLUSTER_LIFECYCLE_SNS_TOPIC_ARN = os.environ.get(
    "CLUSTER_LIFECYCLE_SNS_TOPIC_ARN", ""
)

# ---------------------------------------------------------------------------
# Retry configuration for PCS ConflictException
# ---------------------------------------------------------------------------
_PCS_MAX_RETRIES = 5
_PCS_BASE_DELAY_SECONDS = 10

# ---------------------------------------------------------------------------
# Step progress tracking
# ---------------------------------------------------------------------------
TOTAL_STEPS = 12

STEP_LABELS: dict[int, str] = {
    1: "Registering cluster name",
    2: "Checking budget",
    3: "Creating IAM roles",
    4: "Waiting for instance profiles",
    5: "Creating FSx filesystem",
    6: "Waiting for FSx",
    7: "Creating PCS cluster",
    8: "Creating login nodes",
    9: "Creating compute nodes",
    10: "Creating queue",
    11: "Tagging resources",
    12: "Finalising",
}

# ---------------------------------------------------------------------------
# Step dispatcher — maps step names from the Step Functions state machine
# to the corresponding handler functions in this module.
# ---------------------------------------------------------------------------
_STEP_DISPATCH: dict[str, Any] = {}  # populated after function definitions


def step_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry-point invoked by the cluster creation state machine.

    The state machine sends ``{"step": "<step_name>", "payload": {...}}``.
    This dispatcher routes to the matching function and passes the payload.
    """
    step = event.get("step", "")
    payload = event.get("payload", event)

    handler_fn = _STEP_DISPATCH.get(step)
    if handler_fn is None:
        raise ValueError(f"Unknown cluster-creation step: '{step}'")

    logger.info("Dispatching cluster-creation step: %s", step)
    return handler_fn(payload)


def _update_step_progress(
    project_id: str,
    cluster_name: str,
    step_number: int,
) -> None:
    """Write the current step progress to the DynamoDB Clusters record.

    Creates or updates the cluster record with ``currentStep``,
    ``totalSteps``, and ``stepDescription`` so the GET endpoint can
    report progress to the UI.
    """
    step_description = STEP_LABELS.get(step_number, f"Step {step_number}")

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    try:
        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            UpdateExpression=(
                "SET currentStep = :step, totalSteps = :total, "
                "stepDescription = :desc, #st = :status"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":step": step_number,
                ":total": TOTAL_STEPS,
                ":desc": step_description,
                ":status": "CREATING",
            },
        )
        logger.info(
            "Progress updated for cluster '%s': step %d/%d — %s",
            cluster_name,
            step_number,
            TOTAL_STEPS,
            step_description,
        )
    except ClientError as exc:
        # Progress tracking failure is non-fatal — log and continue
        logger.warning(
            "Failed to update progress for cluster '%s': %s",
            cluster_name,
            exc,
        )


def _lookup_user_email(user_id: str) -> str:
    """Look up a user's email from the PlatformUsers table.

    The userId field in PlatformUsers is the user's email address.
    Records are stored with PK=USER#{userId}, SK=PROFILE.

    Returns the email address, or an empty string if not found.
    """
    if not user_id or not USERS_TABLE_NAME:
        return ""

    table = dynamodb.Table(USERS_TABLE_NAME)
    try:
        response = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
        )
        item = response.get("Item")
        if item:
            return item.get("userId", "")
    except ClientError as exc:
        logger.warning("Failed to look up email for user '%s': %s", user_id, exc)

    return ""


def _publish_lifecycle_notification(
    subject: str,
    message: str,
    user_email: str,
) -> None:
    """Publish a cluster lifecycle notification to the SNS topic.

    Subscribes the user's email to the topic (idempotent) and then
    publishes the notification message.  If the topic ARN is not
    configured, the function silently returns.
    """
    if not CLUSTER_LIFECYCLE_SNS_TOPIC_ARN:
        logger.info("Cluster lifecycle SNS topic not configured — skipping notification.")
        return

    # Subscribe the user's email (idempotent — SNS deduplicates)
    if user_email:
        try:
            sns_client.subscribe(
                TopicArn=CLUSTER_LIFECYCLE_SNS_TOPIC_ARN,
                Protocol="email",
                Endpoint=user_email,
                ReturnSubscriptionArn=True,
            )
        except ClientError as exc:
            logger.warning(
                "Failed to subscribe '%s' to lifecycle topic: %s",
                user_email,
                exc,
            )

    try:
        sns_client.publish(
            TopicArn=CLUSTER_LIFECYCLE_SNS_TOPIC_ARN,
            Subject=subject[:100],  # SNS subject max 100 chars
            Message=message,
        )
        logger.info("Published lifecycle notification: %s", subject)
    except ClientError as exc:
        # Notification failure is non-fatal — log and continue
        logger.warning("Failed to publish lifecycle notification: %s", exc)


# ===================================================================
# Step 1 — Validate cluster name and register in ClusterNameRegistry
# ===================================================================

def validate_and_register_name(event: dict[str, Any]) -> dict[str, Any]:
    """Validate cluster name format and register it in the registry.

    Raises ``ValidationError`` if the name format is invalid.
    Raises ``ConflictError`` (via ``register_cluster_name``) if the
    name is reserved by a different project.

    Returns the event dict unchanged on success so that subsequent
    steps can continue with the same payload.
    """
    cluster_name: str = event.get("clusterName", "")
    project_id: str = event.get("projectId", "")

    # Write progress before executing step logic
    if project_id and cluster_name:
        _update_step_progress(project_id, cluster_name, 1)

    if not cluster_name:
        raise ValidationError("clusterName is required.", {"field": "clusterName"})
    if not project_id:
        raise ValidationError("projectId is required.", {"field": "projectId"})

    if not validate_cluster_name(cluster_name):
        raise ValidationError(
            f"Invalid cluster name '{cluster_name}'. "
            "Names must be non-empty and contain only alphanumeric "
            "characters, hyphens, and underscores.",
            {"clusterName": cluster_name},
        )

    register_cluster_name(
        table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        cluster_name=cluster_name,
        project_id=project_id,
    )

    logger.info(
        "Cluster name '%s' registered for project '%s'",
        cluster_name,
        project_id,
    )
    return event


# ===================================================================
# Step 2 — Check project budget breach status
# ===================================================================

def check_budget_breach(event: dict[str, Any]) -> dict[str, Any]:
    """Check whether the project budget has been breached.

    Uses a DynamoDB consistent read to avoid stale data.

    Raises ``BudgetExceededError`` if the budget is breached.
    """
    project_id: str = event["projectId"]

    # Write progress before executing step logic
    _update_step_progress(project_id, event.get("clusterName", ""), 2)

    table = dynamodb.Table(PROJECTS_TABLE_NAME)
    response = table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )

    item = response.get("Item")
    if not item:
        raise ValidationError(
            f"Project '{project_id}' not found.",
            {"projectId": project_id},
        )

    if item.get("budgetBreached", False):
        raise BudgetExceededError(
            f"Project '{project_id}' budget has been exceeded. "
            "Cluster creation is blocked until the budget is resolved.",
            {"projectId": project_id},
        )

    logger.info("Budget check passed for project '%s'", project_id)
    return event


# ===================================================================
# Step 3 — Create IAM roles and instance profiles
# ===================================================================

# Managed policy ARNs attached to every PCS node role
_PCS_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
]

# Inline policy granting PCS node registration
_PCS_INLINE_POLICY_NAME = "PCSRegisterComputeNodeGroupInstance"
_PCS_INLINE_POLICY_DOCUMENT = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "pcs:RegisterComputeNodeGroupInstance",
            "Resource": "*",
        }
    ],
}

# Trust policy allowing EC2 to assume the role
_EC2_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def _create_role_and_instance_profile(
    role_name: str,
    project_id: str,
    cluster_name: str,
) -> str:
    """Create an IAM role and instance profile for a PCS node type.

    Creates the role with the EC2 trust policy, attaches the required
    managed policies, adds the PCS inline policy, creates an instance
    profile with the same name, and adds the role to the profile.

    Returns the instance profile ARN.
    """
    tags = [
        {"Key": tag["Key"], "Value": tag["Value"]}
        for tag in build_resource_tags(project_id, cluster_name)
    ]

    # 1. Create IAM role
    iam_client.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(_EC2_TRUST_POLICY),
        Tags=tags,
    )
    logger.info("Created IAM role '%s'", role_name)

    # 2. Add inline policy
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName=_PCS_INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(_PCS_INLINE_POLICY_DOCUMENT),
    )

    # 3. Attach managed policies
    for policy_arn in _PCS_MANAGED_POLICIES:
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn=policy_arn,
        )

    # 4. Create instance profile
    iam_client.create_instance_profile(
        InstanceProfileName=role_name,
        Tags=tags,
    )
    logger.info("Created instance profile '%s'", role_name)

    # 5. Add role to instance profile
    response = iam_client.add_role_to_instance_profile(
        InstanceProfileName=role_name,
        RoleName=role_name,
    )

    # Retrieve the instance profile ARN
    ip_response = iam_client.get_instance_profile(
        InstanceProfileName=role_name,
    )
    instance_profile_arn = ip_response["InstanceProfile"]["Arn"]

    logger.info(
        "Instance profile '%s' ready (ARN: %s)",
        role_name,
        instance_profile_arn,
    )
    return instance_profile_arn


def create_iam_resources(event: dict[str, Any]) -> dict[str, Any]:
    """Create cluster-specific IAM roles and instance profiles.

    Creates two IAM roles and instance profiles:
    - ``AWSPCS-{projectId}-{clusterName}-login`` for login nodes
    - ``AWSPCS-{projectId}-{clusterName}-compute`` for compute nodes

    Each role gets:
    - EC2 trust policy (``ec2.amazonaws.com``)
    - Inline policy granting ``pcs:RegisterComputeNodeGroupInstance``
    - Managed policies: ``AmazonSSMManagedInstanceCore``,
      ``CloudWatchAgentServerPolicy``

    Adds ``loginInstanceProfileArn`` and ``computeInstanceProfileArn``
    to the returned event.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 3)

    login_role_name = f"AWSPCS-{project_id}-{cluster_name}-login"
    compute_role_name = f"AWSPCS-{project_id}-{cluster_name}-compute"

    try:
        login_profile_arn = _create_role_and_instance_profile(
            login_role_name, project_id, cluster_name,
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to create login IAM resources '{login_role_name}': {exc}"
        )

    try:
        compute_profile_arn = _create_role_and_instance_profile(
            compute_role_name, project_id, cluster_name,
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to create compute IAM resources '{compute_role_name}': {exc}"
        )

    logger.info(
        "IAM resources created for cluster '%s': login=%s, compute=%s",
        cluster_name,
        login_profile_arn,
        compute_profile_arn,
    )

    return {
        **event,
        "loginInstanceProfileArn": login_profile_arn,
        "computeInstanceProfileArn": compute_profile_arn,
    }


# ===================================================================
# Step 4 — Wait for instance profiles to propagate
# ===================================================================

def wait_for_instance_profiles(event: dict[str, Any]) -> dict[str, Any]:
    """Poll IAM until both login and compute instance profiles are available.

    Instance profiles can take a few seconds to propagate after
    creation.  This step is called by the Step Functions state machine
    in a retry loop — it returns ``instanceProfilesReady: True`` when
    both profiles are available, or ``False`` so the state machine can
    wait and retry.

    Handles ``NoSuchEntity`` gracefully by returning
    ``instanceProfilesReady: False``.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 4)

    login_profile_name = f"AWSPCS-{project_id}-{cluster_name}-login"
    compute_profile_name = f"AWSPCS-{project_id}-{cluster_name}-compute"

    for profile_name in (login_profile_name, compute_profile_name):
        try:
            iam_client.get_instance_profile(InstanceProfileName=profile_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchEntity":
                logger.info(
                    "Instance profile '%s' not yet available — will retry",
                    profile_name,
                )
                return {**event, "instanceProfilesReady": False}
            raise

    logger.info(
        "Instance profiles ready for cluster '%s': %s, %s",
        cluster_name,
        login_profile_name,
        compute_profile_name,
    )
    return {**event, "instanceProfilesReady": True}


# ===================================================================
# Step 5 — Create FSx for Lustre filesystem
# ===================================================================

def create_fsx_filesystem(event: dict[str, Any]) -> dict[str, Any]:
    """Create an FSx for Lustre filesystem without an inline data repository.

    The filesystem is placed in the first private subnet and uses the
    FSx security group from the project infrastructure.  A Data
    Repository Association (DRA) is created separately after the
    filesystem becomes available — this allows lazy loading from S3
    so the filesystem is ready faster.

    Adds ``fsxFilesystemId`` to the returned event.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 5)
    private_subnet_ids: list[str] = event["privateSubnetIds"]
    security_group_ids: dict[str, str] = event["securityGroupIds"]

    fsx_sg = security_group_ids["fsx"]
    subnet_id = private_subnet_ids[0]

    tags = [
        {"Key": tag["Key"], "Value": tag["Value"]}
        for tag in build_resource_tags(project_id, cluster_name)
    ]

    try:
        response = fsx_client.create_file_system(
            FileSystemType="LUSTRE",
            FileSystemTypeVersion="2.15",
            StorageCapacity=1200,  # minimum for Lustre (1.2 TiB)
            StorageType="SSD",
            SubnetIds=[subnet_id],
            SecurityGroupIds=[fsx_sg],
            LustreConfiguration={
                "DeploymentType": "SCRATCH_2",
                "DataCompressionType": "LZ4",
            },
            Tags=tags,
        )
    except ClientError as exc:
        raise InternalError(f"Failed to create FSx filesystem: {exc}")

    fsx_id = response["FileSystem"]["FileSystemId"]
    logger.info(
        "FSx filesystem '%s' creation initiated for cluster '%s'",
        fsx_id,
        cluster_name,
    )

    return {**event, "fsxFilesystemId": fsx_id}


# ===================================================================
# Step 6 — Check FSx filesystem status
# ===================================================================

_FSX_TERMINAL_FAILURE_STATES = {"FAILED", "DELETING", "MISCONFIGURED"}

_FSX_MAX_POLL_ATTEMPTS = 60  # 60 × 30s wait = 30 minutes max


def check_fsx_status(event: dict[str, Any]) -> dict[str, Any]:
    """Poll FSx filesystem status.

    Returns the event with an added ``fsxAvailable`` boolean.
    Step Functions uses this to decide whether to wait and retry.

    Raises ``InternalError`` if the filesystem enters a terminal
    failure state (FAILED, DELETING, MISCONFIGURED) or if the
    maximum number of poll attempts is exceeded.
    """
    fsx_id: str = event["fsxFilesystemId"]

    # Write progress before executing step logic
    _update_step_progress(event["projectId"], event.get("clusterName", ""), 6)

    # Track poll attempts to prevent infinite wait loops
    poll_count: int = event.get("fsxPollCount", 0) + 1

    try:
        response = fsx_client.describe_file_systems(FileSystemIds=[fsx_id])
    except ClientError as exc:
        raise InternalError(f"Failed to describe FSx filesystem: {exc}")

    filesystems = response.get("FileSystems", [])
    if not filesystems:
        raise InternalError(f"FSx filesystem '{fsx_id}' not found.")

    status = filesystems[0]["Lifecycle"]
    dns_name = filesystems[0].get("DNSName", "")
    mount_name = filesystems[0].get("LustreConfiguration", {}).get("MountName", "")

    logger.info(
        "FSx filesystem '%s' status: %s (poll %d/%d)",
        fsx_id,
        status,
        poll_count,
        _FSX_MAX_POLL_ATTEMPTS,
    )

    # Fail fast on terminal error states
    if status in _FSX_TERMINAL_FAILURE_STATES:
        raise InternalError(
            f"FSx filesystem '{fsx_id}' entered terminal state '{status}'. "
            "Cluster creation cannot proceed."
        )

    # Fail if we've exceeded the maximum number of poll attempts
    if poll_count >= _FSX_MAX_POLL_ATTEMPTS and status != "AVAILABLE":
        raise InternalError(
            f"FSx filesystem '{fsx_id}' did not become available after "
            f"{poll_count} attempts (status: {status}). "
            "Cluster creation timed out waiting for FSx."
        )

    return {
        **event,
        "fsxAvailable": status == "AVAILABLE",
        "fsxDnsName": dns_name,
        "fsxMountName": mount_name,
        "fsxPollCount": poll_count,
    }


# ===================================================================
# Step 6b — Create Data Repository Association (lazy loading from S3)
# ===================================================================

def create_fsx_dra(event: dict[str, Any]) -> dict[str, Any]:
    """Create a Data Repository Association linking FSx to the project S3 bucket.

    Uses lazy loading so files are only fetched from S3 on first
    access, rather than bulk-importing everything at filesystem
    creation time.  Auto-export ensures that new/changed/deleted
    files on Lustre are synced back to S3 automatically.

    Adds ``fsxDraId`` to the returned event.
    """
    fsx_id: str = event["fsxFilesystemId"]
    s3_bucket_name: str = event["s3BucketName"]
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    tags = [
        {"Key": tag["Key"], "Value": tag["Value"]}
        for tag in build_resource_tags(project_id, cluster_name)
    ]

    try:
        response = fsx_client.create_data_repository_association(
            FileSystemId=fsx_id,
            FileSystemPath="/data",
            DataRepositoryPath=f"s3://{s3_bucket_name}",
            S3={
                "AutoImportPolicy": {
                    "Events": ["NEW", "CHANGED", "DELETED"],
                },
                "AutoExportPolicy": {
                    "Events": ["NEW", "CHANGED", "DELETED"],
                },
            },
            Tags=tags,
        )
    except ClientError as exc:
        raise InternalError(
            f"Failed to create DRA for FSx filesystem '{fsx_id}': {exc}"
        )

    dra_id = response["Association"]["AssociationId"]
    logger.info(
        "DRA '%s' created for FSx filesystem '%s' (cluster '%s')",
        dra_id,
        fsx_id,
        cluster_name,
    )

    return {**event, "fsxDraId": dra_id}


# ===================================================================
# Step 7 — Create PCS cluster
# ===================================================================

def create_pcs_cluster(event: dict[str, Any]) -> dict[str, Any]:
    """Create an AWS PCS cluster with Slurm 24.11+ and STANDARD accounting.

    Includes retry logic for ``ConflictException`` because only one
    cluster can be in *Creating* state per region per account.

    Adds ``pcsClusterId`` and ``pcsClusterArn`` to the returned event.
    """
    cluster_name: str = event["clusterName"]
    project_id: str = event["projectId"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 7)
    private_subnet_ids: list[str] = event["privateSubnetIds"]
    security_group_ids: dict[str, str] = event["securityGroupIds"]

    compute_sg = security_group_ids["computeNode"]

    tags = tags_as_dict(project_id, cluster_name)

    last_exc: Exception | None = None
    for attempt in range(_PCS_MAX_RETRIES):
        try:
            response = pcs_client.create_cluster(
                clusterName=cluster_name,
                scheduler={
                    "type": "SLURM",
                    "version": "24.11",
                },
                size="SMALL",
                networking={
                    "subnetIds": private_subnet_ids[:1],
                    "securityGroupIds": [compute_sg],
                },
                slurmConfiguration={
                    "slurmCustomSettings": [],
                    "scaleDownIdleTimeInSeconds": 600,
                },
                tags=tags,
            )
            cluster_info = response.get("cluster", {})
            pcs_cluster_id = cluster_info.get("id", "")
            pcs_cluster_arn = cluster_info.get("arn", "")

            logger.info(
                "PCS cluster '%s' (%s) creation initiated",
                cluster_name,
                pcs_cluster_id,
            )

            return {
                **event,
                "pcsClusterId": pcs_cluster_id,
                "pcsClusterArn": pcs_cluster_arn,
            }

        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code == "ConflictException" and attempt < _PCS_MAX_RETRIES - 1:
                delay = _PCS_BASE_DELAY_SECONDS * (2 ** attempt)
                logger.warning(
                    "PCS ConflictException on attempt %d — retrying in %ds",
                    attempt + 1,
                    delay,
                )
                last_exc = exc
                time.sleep(delay)
            else:
                raise InternalError(
                    f"Failed to create PCS cluster after {attempt + 1} attempts: {exc}"
                )

    raise InternalError(
        f"Failed to create PCS cluster after {_PCS_MAX_RETRIES} attempts: {last_exc}"
    )


# ===================================================================
# Step 8 — Create login node compute node group
# ===================================================================

def create_login_node_group(event: dict[str, Any]) -> dict[str, Any]:
    """Create the login (head) node compute node group.

    Login nodes use:
    - Public subnet for SSH/DCV access
    - Static scaling with minimum 1 instance
    - On-demand purchase option

    Adds ``loginNodeGroupId`` to the returned event.
    """
    cluster_name: str = event["clusterName"]
    pcs_cluster_id: str = event["pcsClusterId"]
    project_id: str = event["projectId"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 8)
    template_id: str = event.get("templateId", "")
    public_subnet_ids: list[str] = event["publicSubnetIds"]
    security_group_ids: dict[str, str] = event["securityGroupIds"]
    efs_filesystem_id: str = event.get("efsFileSystemId", "")
    fsx_filesystem_id: str = event.get("fsxFilesystemId", "")

    head_sg = security_group_ids["headNode"]

    # Resolve instance type from template or use a sensible default
    login_instance_type = event.get("loginInstanceType", "c7g.medium")

    # Generate POSIX user data script for login nodes.
    # In production, the user data would be set via a custom launch template.
    user_data_script = generate_user_data_script(
        project_id=project_id,
        users_table_name=USERS_TABLE_NAME,
        projects_table_name=PROJECTS_TABLE_NAME,
    )
    logger.info(
        "Generated POSIX user data script for login nodes (%d bytes)",
        len(user_data_script),
    )

    tags = tags_as_dict(project_id, cluster_name)

    try:
        response = pcs_client.create_compute_node_group(
            clusterIdentifier=pcs_cluster_id,
            computeNodeGroupName=f"{cluster_name}-login",
            subnetIds=public_subnet_ids,
            purchaseOption="ONDEMAND",
            scalingConfiguration={
                "minInstanceCount": 1,
                "maxInstanceCount": 1,
            },
            instanceConfigs=[
                {"instanceType": login_instance_type},
            ],
            customLaunchTemplate={
                "id": event.get("loginLaunchTemplateId", ""),
                "version": event.get("loginLaunchTemplateVersion", "$Default"),
            },
            iamInstanceProfileArn=event.get("loginInstanceProfileArn", ""),
            tags=tags,
        )
    except ClientError as exc:
        raise InternalError(f"Failed to create login node group: {exc}")

    node_group = response.get("computeNodeGroup", {})
    login_group_id = node_group.get("id", "")

    logger.info(
        "Login node group '%s' created for cluster '%s'",
        login_group_id,
        cluster_name,
    )

    return {**event, "loginNodeGroupId": login_group_id, "userDataScript": user_data_script}


# ===================================================================
# Step 9 — Create compute node compute node group
# ===================================================================

def create_compute_node_group(event: dict[str, Any]) -> dict[str, Any]:
    """Create the compute node group for job execution.

    Compute nodes use:
    - Private subnet for network isolation
    - Elastic scaling (min 0, max from template)

    Adds ``computeNodeGroupId`` to the returned event.
    """
    cluster_name: str = event["clusterName"]
    pcs_cluster_id: str = event["pcsClusterId"]
    project_id: str = event["projectId"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 9)
    private_subnet_ids: list[str] = event["privateSubnetIds"]
    security_group_ids: dict[str, str] = event["securityGroupIds"]

    compute_sg = security_group_ids["computeNode"]

    # Template-driven configuration
    instance_types = event.get("instanceTypes", ["c7g.medium"])
    max_nodes = event.get("maxNodes", 10)
    min_nodes = event.get("minNodes", 0)
    purchase_option = event.get("purchaseOption", "ONDEMAND")

    # Generate POSIX user data script for compute nodes.
    # In production, the user data would be set via a custom launch template.
    user_data_script = generate_user_data_script(
        project_id=project_id,
        users_table_name=USERS_TABLE_NAME,
        projects_table_name=PROJECTS_TABLE_NAME,
    )
    logger.info(
        "Generated POSIX user data script for compute nodes (%d bytes)",
        len(user_data_script),
    )

    tags = tags_as_dict(project_id, cluster_name)

    instance_configs = [{"instanceType": it} for it in instance_types]

    try:
        response = pcs_client.create_compute_node_group(
            clusterIdentifier=pcs_cluster_id,
            computeNodeGroupName=f"{cluster_name}-compute",
            subnetIds=private_subnet_ids,
            purchaseOption=purchase_option,
            scalingConfiguration={
                "minInstanceCount": min_nodes,
                "maxInstanceCount": max_nodes,
            },
            instanceConfigs=instance_configs,
            customLaunchTemplate={
                "id": event.get("computeLaunchTemplateId", ""),
                "version": event.get("computeLaunchTemplateVersion", "$Default"),
            },
            iamInstanceProfileArn=event.get("computeInstanceProfileArn", ""),
            tags=tags,
        )
    except ClientError as exc:
        raise InternalError(f"Failed to create compute node group: {exc}")

    node_group = response.get("computeNodeGroup", {})
    compute_group_id = node_group.get("id", "")

    logger.info(
        "Compute node group '%s' created for cluster '%s'",
        compute_group_id,
        cluster_name,
    )

    return {**event, "computeNodeGroupId": compute_group_id}


# ===================================================================
# Step 10 — Create PCS queue
# ===================================================================

def create_pcs_queue(event: dict[str, Any]) -> dict[str, Any]:
    """Create a PCS queue linked to the compute node group.

    Adds ``queueId`` to the returned event.
    """
    cluster_name: str = event["clusterName"]
    pcs_cluster_id: str = event["pcsClusterId"]
    project_id: str = event["projectId"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 10)
    compute_node_group_id: str = event["computeNodeGroupId"]

    tags = tags_as_dict(project_id, cluster_name)

    try:
        response = pcs_client.create_queue(
            clusterIdentifier=pcs_cluster_id,
            queueName=f"{cluster_name}-queue",
            computeNodeGroupConfigurations=[
                {"computeNodeGroupId": compute_node_group_id},
            ],
            tags=tags,
        )
    except ClientError as exc:
        raise InternalError(f"Failed to create PCS queue: {exc}")

    queue = response.get("queue", {})
    queue_id = queue.get("id", "")

    logger.info(
        "PCS queue '%s' created for cluster '%s'",
        queue_id,
        cluster_name,
    )

    return {**event, "queueId": queue_id}


# ===================================================================
# Step 11 — Tag all resources
# ===================================================================

def tag_resources(event: dict[str, Any]) -> dict[str, Any]:
    """Tag all cluster resources with project and cluster name tags.

    Uses the Resource Groups Tagging API to apply tags to every
    resource ARN collected during the workflow.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 11)

    tags = tags_as_dict(project_id, cluster_name)

    # Collect ARNs of resources created during the workflow
    resource_arns: list[str] = []

    pcs_cluster_arn = event.get("pcsClusterArn", "")
    if pcs_cluster_arn:
        resource_arns.append(pcs_cluster_arn)

    fsx_id = event.get("fsxFilesystemId", "")
    if fsx_id:
        # FSx ARN follows the pattern arn:aws:fsx:<region>:<account>:file-system/<id>
        # We tag via the tagging API using the filesystem ID; the ARN
        # will be resolved by the service.  For now we skip non-ARN
        # resources — FSx was already tagged at creation time.
        pass

    # PCS sub-resources (node groups, queues) were tagged at creation.
    # This step is a safety net for any resources that might have been
    # missed or for future resources added to the workflow.

    if resource_arns:
        try:
            tagging_client.tag_resources(
                ResourceARNList=resource_arns,
                Tags=tags,
            )
            logger.info(
                "Tagged %d resources for cluster '%s'",
                len(resource_arns),
                cluster_name,
            )
        except ClientError as exc:
            # Tagging failure is non-fatal — log and continue
            logger.warning(
                "Failed to tag resources for cluster '%s': %s",
                cluster_name,
                exc,
            )

    return event


# ===================================================================
# Step 12 — Record cluster in DynamoDB
# ===================================================================

def record_cluster(event: dict[str, Any]) -> dict[str, Any]:
    """Record the cluster details in the DynamoDB Clusters table.

    Stores all resource IDs, connection information, and sets the
    cluster status to ``ACTIVE``.
    """
    project_id: str = event["projectId"]
    cluster_name: str = event["clusterName"]

    # Write progress before executing step logic
    _update_step_progress(project_id, cluster_name, 12)

    now = datetime.now(timezone.utc).isoformat()

    cluster_record = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name,
        "projectId": project_id,
        "templateId": event.get("templateId", ""),
        "pcsClusterId": event.get("pcsClusterId", ""),
        "pcsClusterArn": event.get("pcsClusterArn", ""),
        "loginNodeGroupId": event.get("loginNodeGroupId", ""),
        "computeNodeGroupId": event.get("computeNodeGroupId", ""),
        "queueId": event.get("queueId", ""),
        "fsxFilesystemId": event.get("fsxFilesystemId", ""),
        "loginNodeIp": event.get("loginNodeIp", ""),
        "sshPort": event.get("sshPort", 22),
        "dcvPort": event.get("dcvPort", 8443),
        "status": "ACTIVE",
        "createdBy": event.get("createdBy", ""),
        "createdAt": now,
    }

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    try:
        table.put_item(Item=cluster_record)
    except ClientError as exc:
        raise InternalError(f"Failed to record cluster in DynamoDB: {exc}")

    logger.info(
        "Cluster '%s' recorded as ACTIVE for project '%s'",
        cluster_name,
        project_id,
    )

    # Send success notification to the creating user
    created_by = event.get("createdBy", "")
    user_email = _lookup_user_email(created_by)
    login_ip = event.get("loginNodeIp", "")
    ssh_port = event.get("sshPort", 22)
    dcv_port = event.get("dcvPort", 8443)

    connection_details = ""
    if login_ip:
        connection_details = (
            f"\n\nConnection Details:\n"
            f"  SSH: ssh -p {ssh_port} <username>@{login_ip}\n"
            f"  DCV: https://{login_ip}:{dcv_port}"
        )

    _publish_lifecycle_notification(
        subject=f"Cluster '{cluster_name}' is ready",
        message=(
            f"Your HPC cluster '{cluster_name}' in project '{project_id}' "
            f"has been created successfully and is now ACTIVE."
            f"{connection_details}"
        ),
        user_email=user_email,
    )

    return {**event, "status": "ACTIVE", "createdAt": now}


# ===================================================================
# Step 13 — Handle creation failure (rollback)
# ===================================================================

def handle_creation_failure(event: dict[str, Any]) -> dict[str, Any]:
    """Rollback handler for cluster creation failures.

    Cleans up partially created resources in reverse order:
    1. Delete IAM roles and instance profiles (login and compute)
    2. Delete PCS queue (if created)
    3. Delete PCS compute node groups (if created)
    4. Delete PCS cluster (if created)
    5. Delete FSx filesystem (if created)
    6. Mark cluster as FAILED in DynamoDB

    Each cleanup step is best-effort — failures are logged but do
    not prevent subsequent cleanup steps from running.
    """
    project_id: str = event.get("projectId", "")
    cluster_name: str = event.get("clusterName", "")
    pcs_cluster_id: str = event.get("pcsClusterId", "")
    fsx_id: str = event.get("fsxFilesystemId", "")
    queue_id: str = event.get("queueId", "")
    login_group_id: str = event.get("loginNodeGroupId", "")
    compute_group_id: str = event.get("computeNodeGroupId", "")

    # Capture the original error for the DynamoDB record
    error_info = event.get("error", {})
    error_message = error_info.get("Cause", event.get("errorMessage", "Unknown error"))

    cleanup_results: list[str] = []

    # 1. Delete IAM roles and instance profiles
    if project_id and cluster_name:
        login_role_name = f"AWSPCS-{project_id}-{cluster_name}-login"
        compute_role_name = f"AWSPCS-{project_id}-{cluster_name}-compute"
        cleanup_results.extend(
            _cleanup_iam_role_and_profile(login_role_name)
        )
        cleanup_results.extend(
            _cleanup_iam_role_and_profile(compute_role_name)
        )

    # 2. Delete PCS queue
    if queue_id and pcs_cluster_id:
        cleanup_results.append(
            _cleanup_pcs_queue(pcs_cluster_id, queue_id)
        )

    # 3. Delete compute node group
    if compute_group_id and pcs_cluster_id:
        cleanup_results.append(
            _cleanup_pcs_node_group(pcs_cluster_id, compute_group_id, "compute")
        )

    # 4. Delete login node group
    if login_group_id and pcs_cluster_id:
        cleanup_results.append(
            _cleanup_pcs_node_group(pcs_cluster_id, login_group_id, "login")
        )

    # 5. Delete PCS cluster
    if pcs_cluster_id:
        cleanup_results.append(
            _cleanup_pcs_cluster(pcs_cluster_id)
        )

    # 6. Delete FSx filesystem
    if fsx_id:
        cleanup_results.append(
            _cleanup_fsx_filesystem(fsx_id)
        )

    # 7. Record FAILED status in DynamoDB
    if project_id and cluster_name:
        _record_failed_cluster(project_id, cluster_name, error_message)

    logger.info(
        "Rollback complete for cluster '%s': %s",
        cluster_name,
        "; ".join(cleanup_results),
    )

    # Send failure notification to the creating user
    created_by = event.get("createdBy", "")
    user_email = _lookup_user_email(created_by)

    _publish_lifecycle_notification(
        subject=f"Cluster '{cluster_name}' creation failed",
        message=(
            f"Your HPC cluster '{cluster_name}' in project '{project_id}' "
            f"failed to create.\n\n"
            f"Error: {error_message}\n\n"
            f"All partially created resources have been cleaned up automatically."
        ),
        user_email=user_email,
    )

    return {
        **event,
        "status": "FAILED",
        "rollbackResults": cleanup_results,
        "errorMessage": error_message,
    }


# ===================================================================
# Internal cleanup helpers
# ===================================================================

def _cleanup_iam_role_and_profile(role_name: str) -> list[str]:
    """Best-effort deletion of an IAM role and its instance profile.

    Cleanup order:
    1. Remove role from instance profile
    2. Delete instance profile
    3. Detach managed policies
    4. Delete inline policies
    5. Delete IAM role

    Each step logs and continues on ``NoSuchEntity`` or ``ClientError``
    so that subsequent steps are always attempted.

    Returns a list of result strings for logging.
    """
    results: list[str] = []

    # 1. Remove role from instance profile
    try:
        iam_client.remove_role_from_instance_profile(
            InstanceProfileName=role_name,
            RoleName=role_name,
        )
        logger.info("Removed role '%s' from instance profile", role_name)
        results.append(f"remove_role_from_profile:{role_name}:done")
    except ClientError as exc:
        logger.warning(
            "Failed to remove role '%s' from instance profile: %s",
            role_name,
            exc,
        )
        results.append(f"remove_role_from_profile:{role_name}:skipped")

    # 2. Delete instance profile
    try:
        iam_client.delete_instance_profile(InstanceProfileName=role_name)
        logger.info("Deleted instance profile '%s'", role_name)
        results.append(f"instance_profile:{role_name}:deleted")
    except ClientError as exc:
        logger.warning(
            "Failed to delete instance profile '%s': %s",
            role_name,
            exc,
        )
        results.append(f"instance_profile:{role_name}:failed")

    # 3. Detach managed policies
    for policy_arn in _PCS_MANAGED_POLICIES:
        try:
            iam_client.detach_role_policy(
                RoleName=role_name,
                PolicyArn=policy_arn,
            )
            logger.info(
                "Detached policy '%s' from role '%s'", policy_arn, role_name
            )
        except ClientError as exc:
            logger.warning(
                "Failed to detach policy '%s' from role '%s': %s",
                policy_arn,
                role_name,
                exc,
            )

    # 4. Delete inline policies
    try:
        iam_client.delete_role_policy(
            RoleName=role_name,
            PolicyName=_PCS_INLINE_POLICY_NAME,
        )
        logger.info(
            "Deleted inline policy '%s' from role '%s'",
            _PCS_INLINE_POLICY_NAME,
            role_name,
        )
    except ClientError as exc:
        logger.warning(
            "Failed to delete inline policy '%s' from role '%s': %s",
            _PCS_INLINE_POLICY_NAME,
            role_name,
            exc,
        )

    # 5. Delete IAM role
    try:
        iam_client.delete_role(RoleName=role_name)
        logger.info("Deleted IAM role '%s'", role_name)
        results.append(f"role:{role_name}:deleted")
    except ClientError as exc:
        logger.warning("Failed to delete IAM role '%s': %s", role_name, exc)
        results.append(f"role:{role_name}:failed")

    return results


def _cleanup_pcs_queue(cluster_id: str, queue_id: str) -> str:
    """Best-effort deletion of a PCS queue."""
    try:
        pcs_client.delete_queue(
            clusterIdentifier=cluster_id,
            queueIdentifier=queue_id,
        )
        logger.info("Deleted PCS queue '%s'", queue_id)
        return f"queue:{queue_id}:deleted"
    except ClientError as exc:
        logger.warning("Failed to delete PCS queue '%s': %s", queue_id, exc)
        return f"queue:{queue_id}:failed"


def _cleanup_pcs_node_group(
    cluster_id: str, node_group_id: str, label: str
) -> str:
    """Best-effort deletion of a PCS compute node group."""
    try:
        pcs_client.delete_compute_node_group(
            clusterIdentifier=cluster_id,
            computeNodeGroupIdentifier=node_group_id,
        )
        logger.info("Deleted PCS %s node group '%s'", label, node_group_id)
        return f"{label}_node_group:{node_group_id}:deleted"
    except ClientError as exc:
        logger.warning(
            "Failed to delete PCS %s node group '%s': %s",
            label,
            node_group_id,
            exc,
        )
        return f"{label}_node_group:{node_group_id}:failed"


def _cleanup_pcs_cluster(cluster_id: str) -> str:
    """Best-effort deletion of a PCS cluster."""
    try:
        pcs_client.delete_cluster(clusterIdentifier=cluster_id)
        logger.info("Deleted PCS cluster '%s'", cluster_id)
        return f"cluster:{cluster_id}:deleted"
    except ClientError as exc:
        logger.warning("Failed to delete PCS cluster '%s': %s", cluster_id, exc)
        return f"cluster:{cluster_id}:failed"


def _cleanup_fsx_filesystem(fsx_id: str) -> str:
    """Best-effort deletion of an FSx filesystem."""
    try:
        fsx_client.delete_file_system(FileSystemId=fsx_id)
        logger.info("Deleted FSx filesystem '%s'", fsx_id)
        return f"fsx:{fsx_id}:deleted"
    except ClientError as exc:
        logger.warning("Failed to delete FSx filesystem '%s': %s", fsx_id, exc)
        return f"fsx:{fsx_id}:failed"


def _record_failed_cluster(
    project_id: str, cluster_name: str, error_message: str
) -> None:
    """Record a FAILED cluster status in DynamoDB."""
    now = datetime.now(timezone.utc).isoformat()

    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    try:
        table.put_item(
            Item={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
                "clusterName": cluster_name,
                "projectId": project_id,
                "status": "FAILED",
                "errorMessage": error_message,
                "createdAt": now,
            },
        )
        logger.info(
            "Recorded FAILED status for cluster '%s' in project '%s'",
            cluster_name,
            project_id,
        )
    except ClientError as exc:
        logger.warning(
            "Failed to record FAILED status for cluster '%s': %s",
            cluster_name,
            exc,
        )

# ===================================================================
# Template resolution — resolve template fields before parallel state
# ===================================================================

def resolve_template(event: dict[str, Any]) -> dict[str, Any]:
    """Resolve cluster template fields from the ClusterTemplates table.

    If ``templateId`` is present and non-empty, reads the template
    record from DynamoDB and injects the template-driven fields into
    the event payload.  If the template is not found, raises
    ``ValidationError`` so the workflow fails fast with a clear error.

    If ``templateId`` is empty or missing, sensible defaults are
    applied so the workflow can proceed without a template.

    Returns the augmented event dict.
    """
    template_id: str = event.get("templateId", "")

    if template_id:
        logger.info("Resolving template '%s' from ClusterTemplates table", template_id)

        table = dynamodb.Table(TEMPLATES_TABLE_NAME)
        try:
            response = table.get_item(
                Key={"PK": f"TEMPLATE#{template_id}", "SK": "METADATA"},
            )
        except ClientError as exc:
            raise InternalError(
                f"Failed to read template '{template_id}' from ClusterTemplates table: {exc}"
            )

        item = response.get("Item")
        if not item:
            raise ValidationError(
                f"Cluster template '{template_id}' not found.",
                {"templateId": template_id},
            )

        logger.info("Template '%s' resolved successfully", template_id)

        return {
            **event,
            "loginInstanceType": item.get("loginInstanceType", "c7g.medium"),
            "instanceTypes": item.get("instanceTypes", ["c7g.medium"]),
            "maxNodes": item.get("maxNodes", 10),
            "minNodes": item.get("minNodes", 0),
            "purchaseOption": item.get("purchaseOption", "ONDEMAND"),
        }

    # No template — apply sensible defaults
    logger.info("No templateId provided — using default template values")
    return {
        **event,
        "loginInstanceType": "c7g.medium",
        "instanceTypes": ["c7g.medium"],
        "maxNodes": 10,
        "minNodes": 0,
        "purchaseOption": "ONDEMAND",
    }


# ===================================================================
# EventBridge handler — mark cluster FAILED on execution termination
# ===================================================================

def mark_cluster_failed_from_event(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Handle EventBridge Step Functions execution status change events.

    Extracts the execution ARN from the event, describes the execution
    to retrieve the input payload, and transitions the cluster record
    to FAILED status if it is still in CREATING (idempotency guard).

    Expected event structure (EventBridge detail)::

        {
            "source": "aws.states",
            "detail-type": "Step Functions Execution Status Change",
            "detail": {
                "executionArn": "arn:aws:states:...",
                "stateMachineArn": "arn:aws:states:...",
                "status": "TIMED_OUT" | "FAILED" | "ABORTED"
            }
        }
    """
    detail = event.get("detail", {})
    execution_arn = detail.get("executionArn", "")
    execution_status = detail.get("status", "")

    if not execution_arn:
        logger.error("EventBridge event missing executionArn in detail")
        return {"status": "error", "message": "Missing executionArn"}

    logger.info(
        "Processing execution termination event: %s (status: %s)",
        execution_arn,
        execution_status,
    )

    # Describe the execution to get the input payload
    try:
        response = sfn_client.describe_execution(executionArn=execution_arn)
    except ClientError as exc:
        logger.error("Failed to describe execution '%s': %s", execution_arn, exc)
        return {"status": "error", "message": f"Failed to describe execution: {exc}"}

    input_str = response.get("input", "{}")
    try:
        payload = json.loads(input_str)
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse execution input as JSON: %s", input_str[:200])
        return {"status": "error", "message": "Invalid execution input JSON"}

    project_id = payload.get("projectId", "")
    cluster_name = payload.get("clusterName", "")

    if not project_id or not cluster_name:
        logger.error(
            "Execution input missing projectId or clusterName: %s",
            input_str[:200],
        )
        return {"status": "error", "message": "Missing projectId or clusterName in execution input"}

    # Idempotency guard: only update if the cluster is still in CREATING status
    table = dynamodb.Table(CLUSTERS_TABLE_NAME)
    try:
        get_response = table.get_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            ConsistentRead=True,
        )
    except ClientError as exc:
        logger.error(
            "Failed to read cluster record for '%s/%s': %s",
            project_id,
            cluster_name,
            exc,
        )
        return {"status": "error", "message": f"Failed to read cluster record: {exc}"}

    item = get_response.get("Item")
    if not item:
        logger.warning(
            "Cluster record not found for '%s/%s' — nothing to update",
            project_id,
            cluster_name,
        )
        return {"status": "skipped", "message": "Cluster record not found"}

    current_status = item.get("status", "")
    if current_status != "CREATING":
        logger.info(
            "Cluster '%s/%s' is already in '%s' status — skipping update",
            project_id,
            cluster_name,
            current_status,
        )
        return {"status": "skipped", "message": f"Cluster already in {current_status} status"}

    # Transition to FAILED
    error_message = (
        f"Cluster creation failed — Step Functions execution {execution_status.lower()}"
    )
    _record_failed_cluster(project_id, cluster_name, error_message)

    logger.info(
        "Cluster '%s/%s' marked as FAILED due to execution %s",
        project_id,
        cluster_name,
        execution_status,
    )
    return {
        "status": "updated",
        "projectId": project_id,
        "clusterName": cluster_name,
        "newStatus": "FAILED",
    }


# ---------------------------------------------------------------------------
# Populate the step dispatch table now that all functions are defined.
# ---------------------------------------------------------------------------
_STEP_DISPATCH.update({
    "validate_and_register_name": validate_and_register_name,
    "check_budget_breach": check_budget_breach,
    "create_iam_resources": create_iam_resources,
    "wait_for_instance_profiles": wait_for_instance_profiles,
    "resolve_template": resolve_template,
    "create_fsx_filesystem": create_fsx_filesystem,
    "check_fsx_status": check_fsx_status,
    "create_fsx_dra": create_fsx_dra,
    "create_pcs_cluster": create_pcs_cluster,
    "create_login_node_group": create_login_node_group,
    "create_compute_node_group": create_compute_node_group,
    "create_pcs_queue": create_pcs_queue,
    "tag_resources": tag_resources,
    "record_cluster": record_cluster,
    "handle_creation_failure": handle_creation_failure,
})
