"""POSIX user provisioning for HPC cluster nodes.

Provides utilities for:
- Generating EC2 launch template user data scripts that create POSIX
  user accounts on cluster nodes at boot time.
- Propagating new users to active cluster nodes via SSM Run Command.
- Generating bash commands for individual user creation and generic
  account disabling.

Environment variables
---------------------
USERS_TABLE_NAME       DynamoDB PlatformUsers table name
PROJECTS_TABLE_NAME    DynamoDB Projects table name
CLUSTERS_TABLE_NAME    DynamoDB Clusters table name

DynamoDB key schemas
--------------------
Projects table membership records:
    PK = PROJECT#{projectId}, SK = MEMBER#{userId}

PlatformUsers table user profiles:
    PK = USER#{userId}, SK = PROFILE  (contains posixUid, posixGid)

Clusters table:
    PK = PROJECT#{projectId}, SK = CLUSTER#{clusterName}
    (contains status, loginNodeIp)
"""

import logging
import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GENERIC_ACCOUNTS = ["ec2-user", "centos", "ubuntu"]

_SSM_MAX_RETRIES = 3
_SSM_BASE_DELAY_SECONDS = 1

# Propagation status constants
PROPAGATION_SUCCESS = "SUCCESS"
PROPAGATION_PENDING = "PENDING_PROPAGATION"


# ===================================================================
# User data script generation
# ===================================================================


def wrap_user_data_mime(script: str) -> str:
    """Wrap a bash script in MIME multipart format for EC2 user data.

    AWS Parallel Computing Service (PCS) requires launch template user
    data to be in MIME multipart format.  This function wraps a plain
    bash script in a ``multipart/mixed`` MIME message with a single
    ``text/x-shellscript`` part.

    Parameters
    ----------
    script : str
        The bash script content (should start with ``#!/bin/bash``).

    Returns
    -------
    str
        The MIME-wrapped user data string, starting with the
        ``Content-Type: multipart/mixed`` header.
    """
    mime_msg = MIMEMultipart()
    part = MIMEText(script, "x-shellscript")
    mime_msg.attach(part)
    return mime_msg.as_string()

def generate_user_creation_commands(user_id: str, uid: int, gid: int) -> list[str]:
    """Generate bash commands to create a single POSIX user account.

    Creates the user with the specified UID/GID, a home directory,
    and sets ownership on the home directory.

    Parameters
    ----------
    user_id : str
        The platform user identifier (used as the Linux username).
    uid : int
        The POSIX UID to assign.
    gid : int
        The POSIX GID to assign.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    if not user_id:
        return []
    commands = [
        f"groupadd -g {gid} {user_id} 2>/dev/null || true",
        f"useradd -u {uid} -g {gid} -m -d /home/{user_id} {user_id} 2>/dev/null || true",
        f"chown {uid}:{gid} /home/{user_id}",
    ]
    return commands


def generate_disable_generic_accounts_commands() -> list[str]:
    """Generate bash commands to disable interactive login for generic accounts.

    Disables ec2-user, centos, and ubuntu accounts by locking the
    password and setting the shell to /sbin/nologin.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    commands = []
    for account in GENERIC_ACCOUNTS:
        commands.append(
            f"if id {account} &>/dev/null; then "
            f"usermod -L -s /sbin/nologin {account}; "
            f"fi"
        )
    return commands


def generate_pam_exec_logging_commands(log_file: str = "/var/log/hpc-access.log") -> list[str]:
    """Generate bash commands to configure pam_exec for SSH/DCV login event logging.

    Creates a pam_exec hook script that logs user login events
    (user, remote host, timestamp) to a dedicated log file, then
    configures PAM to invoke the script on session open.

    Parameters
    ----------
    log_file : str
        Path to the access log file. Defaults to ``/var/log/hpc-access.log``.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    hook_script = "/usr/local/bin/hpc-access-log.sh"
    commands = [
        f"# --- Configure pam_exec for SSH/DCV login event logging ---",
        f"cat > {hook_script} << 'PAMEOF'",
        "#!/bin/bash",
        f'echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) user=$PAM_USER remote_host=$PAM_RHOST service=$PAM_SERVICE type=$PAM_TYPE" >> {log_file}',
        "PAMEOF",
        f"chmod 755 {hook_script}",
        f"touch {log_file}",
        f"chmod 644 {log_file}",
        # Add pam_exec to sshd PAM config (idempotent — only if not already present)
        f"if ! grep -q '{hook_script}' /etc/pam.d/sshd 2>/dev/null; then",
        f"  echo 'session optional pam_exec.so {hook_script}' >> /etc/pam.d/sshd",
        "fi",
        # Add pam_exec to DCV PAM config if it exists
        f"if [ -f /etc/pam.d/dcv ] && ! grep -q '{hook_script}' /etc/pam.d/dcv 2>/dev/null; then",
        f"  echo 'session optional pam_exec.so {hook_script}' >> /etc/pam.d/dcv",
        "fi",
    ]
    return commands


def generate_cloudwatch_agent_commands(
    project_id: str,
    log_file: str = "/var/log/hpc-access.log",
) -> list[str]:
    """Generate bash commands to configure the CloudWatch agent for access log forwarding.

    Writes a CloudWatch agent configuration that ships the access log
    file to the project's CloudWatch Log Group, then starts (or
    restarts) the agent.

    Parameters
    ----------
    project_id : str
        The project identifier, used to construct the log group name.
    log_file : str
        Path to the access log file. Defaults to ``/var/log/hpc-access.log``.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    log_group = f"/hpc-platform/clusters/{project_id}/access-logs"
    config_path = "/opt/aws/amazon-cloudwatch-agent/etc/hpc-access-log.json"
    commands = [
        "# --- Configure CloudWatch agent for access log forwarding ---",
        f"cat > {config_path} << 'CWEOF'",
        "{",
        '  "logs": {',
        '    "logs_collected": {',
        '      "files": {',
        '        "collect_list": [',
        "          {",
        f'            "file_path": "{log_file}",',
        f'            "log_group_name": "{log_group}",',
        '            "log_stream_name": "{instance_id}/access-log",',
        '            "timezone": "UTC"',
        "          }",
        "        ]",
        "      }",
        "    }",
        "  }",
        "}",
        "CWEOF",
        # Start or restart the CloudWatch agent with the new config
        f"if command -v amazon-cloudwatch-agent-ctl &>/dev/null; then",
        f"  amazon-cloudwatch-agent-ctl -a append-config -m ec2 -s -c file:{config_path}",
        "fi",
    ]
    return commands


def generate_efs_mount_commands(efs_filesystem_id: str, mount_path: str = "/home") -> list[str]:
    """Generate bash commands to mount an EFS filesystem.

    Installs ``amazon-efs-utils``, creates the mount point, adds an
    fstab entry for TLS-encrypted mounting, and runs ``mount -a -t efs``.

    Parameters
    ----------
    efs_filesystem_id : str
        The EFS filesystem ID (e.g. ``fs-abc123``).
    mount_path : str
        The local mount path. Defaults to ``/home``.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    return [
        "# --- Mount EFS filesystem ---",
        "yum install -y amazon-efs-utils || apt-get install -y amazon-efs-utils",
        f"mkdir -p {mount_path}",
        f"echo '{efs_filesystem_id}:/ {mount_path} efs _netdev,tls 0 0' >> /etc/fstab",
        "mount -a -t efs",
    ]


def generate_mountpoint_s3_commands(s3_bucket_name: str, mount_path: str = "/data") -> list[str]:
    """Generate bash commands to install and mount S3 via Mountpoint.

    Parameters
    ----------
    s3_bucket_name : str
        The S3 bucket name to mount.
    mount_path : str
        The local mount path. Defaults to ``/data``.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    return [
        "# --- Mount project S3 bucket via Mountpoint for Amazon S3 ---",
        "yum install -y mountpoint-s3 || apt-get install -y mountpoint-s3",
        f"mkdir -p {mount_path}",
        f"mount-s3 {s3_bucket_name} {mount_path} --allow-delete --allow-overwrite",
        f"echo 'mount-s3 {s3_bucket_name} {mount_path} --allow-delete --allow-overwrite' >> /etc/rc.local",
        "chmod +x /etc/rc.local",
    ]


def generate_fsx_lustre_mount_commands(
    fsx_dns_name: str, fsx_mount_name: str, mount_path: str = "/data"
) -> list[str]:
    """Generate bash commands to mount FSx for Lustre.

    Parameters
    ----------
    fsx_dns_name : str
        The DNS name of the FSx for Lustre filesystem.
    fsx_mount_name : str
        The mount name of the FSx for Lustre filesystem.
    mount_path : str
        The local mount path. Defaults to ``/data``.

    Returns
    -------
    list[str]
        A list of bash command strings.
    """
    return [
        "# --- Mount FSx for Lustre filesystem ---",
        "amazon-linux-extras install -y lustre || yum install -y lustre-client",
        f"mkdir -p {mount_path}",
        f"mount -t lustre {fsx_dns_name}@tcp:/{fsx_mount_name} {mount_path}",
        f"echo '{fsx_dns_name}@tcp:/{fsx_mount_name} {mount_path} lustre defaults,noatime,flock,_netdev 0 0' >> /etc/fstab",
    ]


def generate_user_data_script(
    project_id: str,
    users_table_name: str,
    projects_table_name: str,
    storage_mode: str = "",
    s3_bucket_name: str = "",
    fsx_dns_name: str = "",
    fsx_mount_name: str = "",
    efs_filesystem_id: str = "",
) -> str:
    """Generate a bash user data script for EC2 launch templates.

    The script:
    1. Fetches project members from the Projects DynamoDB table.
    2. Looks up each member's POSIX UID/GID from the PlatformUsers table.
    3. Mounts the EFS filesystem at ``/home`` (if *efs_filesystem_id* is provided).
    4. Creates POSIX user accounts with the correct UID/GID.
    5. Sets home directory ownership.
    6. Disables interactive login for generic accounts.
    7. Mounts project storage at ``/data`` based on the storage mode.

    Parameters
    ----------
    project_id : str
        The project identifier to fetch members for.
    users_table_name : str
        The DynamoDB PlatformUsers table name.
    projects_table_name : str
        The DynamoDB Projects table name.
    storage_mode : str
        ``"mountpoint"`` for Mountpoint for S3, ``"lustre"`` for FSx for
        Lustre.  When empty, no storage mount commands are generated.
    s3_bucket_name : str
        The S3 bucket name (required when *storage_mode* is ``"mountpoint"``).
    fsx_dns_name : str
        The FSx DNS name (required when *storage_mode* is ``"lustre"``).
    fsx_mount_name : str
        The FSx mount name (required when *storage_mode* is ``"lustre"``).
    efs_filesystem_id : str
        The EFS filesystem ID to mount at ``/home``.  When empty, EFS
        mount commands are omitted (preserving existing behaviour).

    Returns
    -------
    str
        A complete bash script suitable for EC2 user data.
    """
    members = _fetch_project_members(projects_table_name, project_id)
    users = _fetch_user_posix_identities(users_table_name, members)

    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        f"# POSIX user provisioning for project: {project_id}",
        f"# Generated for {len(users)} user(s)",
        "",
    ]

    # --- EFS mount (before user creation so /home is available) ---
    if efs_filesystem_id:
        for cmd in generate_efs_mount_commands(efs_filesystem_id):
            lines.append(cmd)
        lines.append("")

    lines.append("# --- Create project user accounts ---")

    for user in users:
        user_id = user["userId"]
        uid = user["posixUid"]
        gid = user["posixGid"]
        lines.append(f"# User: {user_id}")
        for cmd in generate_user_creation_commands(user_id, uid, gid):
            lines.append(cmd)
        lines.append("")

    lines.append("# --- Disable generic accounts ---")
    for cmd in generate_disable_generic_accounts_commands():
        lines.append(cmd)

    lines.append("")
    lines.append("# --- Configure access logging ---")
    for cmd in generate_pam_exec_logging_commands():
        lines.append(cmd)

    lines.append("")
    for cmd in generate_cloudwatch_agent_commands(project_id):
        lines.append(cmd)

    # --- Storage mount ---
    if storage_mode == "mountpoint" and s3_bucket_name:
        lines.append("")
        for cmd in generate_mountpoint_s3_commands(s3_bucket_name):
            lines.append(cmd)
    elif storage_mode == "lustre" and fsx_dns_name and fsx_mount_name:
        lines.append("")
        for cmd in generate_fsx_lustre_mount_commands(fsx_dns_name, fsx_mount_name):
            lines.append(cmd)

    lines.append("")
    lines.append("echo 'POSIX user provisioning complete.'")

    return "\n".join(lines)


# ===================================================================
# SSM Run Command propagation
# ===================================================================

def propagate_user_to_clusters(
    user_id: str,
    uid: int,
    gid: int,
    project_id: str,
    clusters_table_name: str,
    projects_table_name: str | None = None,
) -> str:
    """Propagate a new POSIX user to all active cluster nodes via SSM.

    Queries the Clusters DynamoDB table for active clusters in the
    project, then sends an SSM Run Command to each cluster's login
    node to create the user account.

    Before sending any SSM commands, verifies that the user holds a
    Membership_Record for the target project.  If no membership record
    exists, the propagation is skipped and PROPAGATION_SUCCESS is
    returned (the user should not have a Linux account on the cluster).

    Retries up to 3 times with exponential backoff on SSM failures.

    Parameters
    ----------
    user_id : str
        The platform user identifier.
    uid : int
        The POSIX UID to assign.
    gid : int
        The POSIX GID to assign.
    project_id : str
        The project identifier.
    clusters_table_name : str
        The DynamoDB Clusters table name.
    projects_table_name : str | None
        The DynamoDB Projects table name.  When provided, the function
        verifies the user holds a Membership_Record before proceeding.
        Falls back to the ``PROJECTS_TABLE_NAME`` environment variable
        when *None*.

    Returns
    -------
    str
        PROPAGATION_SUCCESS if all clusters were updated, or
        PROPAGATION_PENDING if any cluster failed after retries.
    """
    # Resolve the projects table name from the parameter or environment
    resolved_projects_table = projects_table_name or os.environ.get("PROJECTS_TABLE_NAME", "")

    # Verify membership before propagating (Requirement 9.3)
    if resolved_projects_table:
        if not _verify_membership(resolved_projects_table, project_id, user_id):
            logger.warning(
                "User '%s' has no membership record for project '%s' "
                "— skipping POSIX propagation.",
                user_id,
                project_id,
            )
            return PROPAGATION_SUCCESS

    active_clusters = _fetch_active_clusters(clusters_table_name, project_id)

    if not active_clusters:
        logger.info(
            "No active clusters for project '%s' — skipping propagation.",
            project_id,
        )
        return PROPAGATION_SUCCESS

    commands = generate_user_creation_commands(user_id, uid, gid)
    if not commands:
        return PROPAGATION_SUCCESS

    script = "\n".join(commands)
    all_succeeded = True

    for cluster in active_clusters:
        cluster_name = cluster.get("clusterName", "")
        instance_id = cluster.get("loginNodeInstanceId", "")

        if not instance_id:
            logger.warning(
                "Cluster '%s' has no loginNodeInstanceId — skipping.",
                cluster_name,
            )
            all_succeeded = False
            continue

        success = _send_ssm_command_with_retry(
            instance_id=instance_id,
            script=script,
            cluster_name=cluster_name,
            user_id=user_id,
        )
        if not success:
            all_succeeded = False

    status = PROPAGATION_SUCCESS if all_succeeded else PROPAGATION_PENDING
    logger.info(
        "User '%s' propagation to project '%s' clusters: %s",
        user_id,
        project_id,
        status,
    )
    return status


# ===================================================================
# Internal helpers
# ===================================================================

def _fetch_project_members(
    projects_table_name: str,
    project_id: str,
) -> list[str]:
    """Fetch the list of user IDs that are members of a project.

    Queries the Projects table for items with
    PK=PROJECT#{projectId} and SK begins_with MEMBER#.

    Returns a list of user ID strings.
    """
    table = dynamodb.Table(projects_table_name)
    try:
        response = table.query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
                & boto3.dynamodb.conditions.Key("SK").begins_with("MEMBER#")
            ),
        )
    except ClientError as exc:
        logger.error(
            "Failed to fetch members for project '%s': %s",
            project_id,
            exc,
        )
        return []

    members = []
    for item in response.get("Items", []):
        user_id = item.get("userId", "")
        if user_id:
            members.append(user_id)

    return members


def _fetch_user_posix_identities(
    users_table_name: str,
    user_ids: list[str],
) -> list[dict[str, Any]]:
    """Look up POSIX UID/GID for a list of user IDs.

    Queries the PlatformUsers table for each user's profile record
    (PK=USER#{userId}, SK=PROFILE) and extracts posixUid and posixGid.

    Returns a list of dicts with userId, posixUid, posixGid.
    """
    table = dynamodb.Table(users_table_name)
    users = []

    for user_id in user_ids:
        try:
            response = table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
            )
        except ClientError as exc:
            logger.warning(
                "Failed to fetch POSIX identity for user '%s': %s",
                user_id,
                exc,
            )
            continue

        item = response.get("Item")
        if item and "posixUid" in item and "posixGid" in item:
            users.append({
                "userId": user_id,
                "posixUid": int(item["posixUid"]),
                "posixGid": int(item["posixGid"]),
            })
        else:
            logger.warning(
                "User '%s' has no POSIX identity — skipping.",
                user_id,
            )

    return users


def _fetch_active_clusters(
    clusters_table_name: str,
    project_id: str,
) -> list[dict[str, Any]]:
    """Fetch active clusters for a project from DynamoDB.

    Queries for items with PK=PROJECT#{projectId} and
    SK begins_with CLUSTER#, then filters for status=ACTIVE.

    Returns a list of cluster record dicts.
    """
    table = dynamodb.Table(clusters_table_name)
    try:
        response = table.query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("PK").eq(f"PROJECT#{project_id}")
                & boto3.dynamodb.conditions.Key("SK").begins_with("CLUSTER#")
            ),
        )
    except ClientError as exc:
        logger.error(
            "Failed to fetch clusters for project '%s': %s",
            project_id,
            exc,
        )
        return []

    return [
        item for item in response.get("Items", [])
        if item.get("status") == "ACTIVE"
    ]


def _verify_membership(
    projects_table_name: str,
    project_id: str,
    user_id: str,
) -> bool:
    """Check whether a user holds a Membership_Record for a project.

    Queries the Projects table for the item with
    PK=PROJECT#{projectId}, SK=MEMBER#{userId}.

    Returns True if the record exists, False otherwise.
    """
    table = dynamodb.Table(projects_table_name)
    try:
        response = table.get_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": f"MEMBER#{user_id}"},
        )
    except ClientError as exc:
        logger.error(
            "Failed to verify membership for user '%s' in project '%s': %s",
            user_id,
            project_id,
            exc,
        )
        return False

    return "Item" in response


def _send_ssm_command_with_retry(
    instance_id: str,
    script: str,
    cluster_name: str,
    user_id: str,
) -> bool:
    """Send an SSM Run Command with retry logic.

    Retries up to _SSM_MAX_RETRIES times with exponential backoff.

    Parameters
    ----------
    instance_id : str
        The EC2 instance ID to target.
    script : str
        The bash script to execute.
    cluster_name : str
        The cluster name (for logging).
    user_id : str
        The user being provisioned (for logging).

    Returns
    -------
    bool
        True if the command was sent successfully, False otherwise.
    """
    for attempt in range(_SSM_MAX_RETRIES):
        try:
            ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [script]},
                Comment=f"Provision POSIX user '{user_id}' on cluster '{cluster_name}'",
            )
            logger.info(
                "SSM command sent to instance '%s' for user '%s' on cluster '%s'",
                instance_id,
                user_id,
                cluster_name,
            )
            return True

        except ClientError as exc:
            delay = _SSM_BASE_DELAY_SECONDS * (2 ** attempt)
            logger.warning(
                "SSM command failed for instance '%s' (attempt %d/%d): %s — "
                "retrying in %ds",
                instance_id,
                attempt + 1,
                _SSM_MAX_RETRIES,
                exc,
                delay,
            )
            if attempt < _SSM_MAX_RETRIES - 1:
                time.sleep(delay)

    logger.error(
        "SSM command failed after %d retries for instance '%s' "
        "(user '%s', cluster '%s')",
        _SSM_MAX_RETRIES,
        instance_id,
        user_id,
        cluster_name,
    )
    return False
