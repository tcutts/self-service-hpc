"""Accounting query business logic.

Uses SSM Run Command to execute ``sacct`` queries on login nodes of
active clusters and aggregates results into a unified response.

Environment variables are injected by the handler module.
"""

import json
import logging
import time
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

# Timeout for waiting on SSM command results (seconds)
SSM_COMMAND_TIMEOUT = 30
SSM_POLL_INTERVAL = 2


def get_active_clusters(
    clusters_table_name: str,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return active clusters, optionally filtered by project.

    When *project_id* is ``None`` all projects are scanned.  When a
    project ID is provided only clusters belonging to that project are
    returned.
    """
    table = dynamodb.Table(clusters_table_name)

    if project_id:
        response = table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"PROJECT#{project_id}")
                & Key("SK").begins_with("CLUSTER#")
            ),
        )
        items = response.get("Items", [])
    else:
        # Scan all clusters across projects
        items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": Key("SK").begins_with("CLUSTER#"),
        }
        while True:
            response = table.scan(**scan_kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

    return [
        item for item in items if item.get("status") == "ACTIVE"
    ]


def query_sacct_on_cluster(
    cluster: dict[str, Any],
) -> dict[str, Any]:
    """Execute ``sacct -p`` on the login node of *cluster* via SSM.

    Returns a dict with ``clusterName``, ``projectId``, ``jobs`` (list
    of parsed sacct records), and optionally ``error`` if the command
    failed.
    """
    cluster_name = cluster.get("clusterName", "unknown")
    project_id = cluster.get("projectId", cluster.get("PK", "").replace("PROJECT#", ""))
    login_instance_id = cluster.get("loginNodeInstanceId", "")

    result: dict[str, Any] = {
        "clusterName": cluster_name,
        "projectId": project_id,
        "jobs": [],
    }

    if not login_instance_id:
        result["error"] = "No login node instance ID available for this cluster."
        return result

    try:
        send_response = ssm_client.send_command(
            InstanceIds=[login_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": ["sacct -p --allusers --noheader"],
            },
            TimeoutSeconds=SSM_COMMAND_TIMEOUT,
        )
        command_id = send_response["Command"]["CommandId"]
    except ClientError as exc:
        logger.warning(
            "SSM send_command failed for cluster %s: %s",
            cluster_name,
            exc,
        )
        result["error"] = f"Failed to send SSM command: {exc}"
        return result

    # Poll for command completion
    output = _wait_for_command(command_id, login_instance_id)
    if output is None:
        result["error"] = "SSM command timed out or failed."
        return result

    result["jobs"] = _parse_sacct_output(output)
    return result


def query_accounting_jobs(
    clusters_table_name: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Query sacct across active clusters and aggregate results.

    Parameters
    ----------
    clusters_table_name:
        Name of the DynamoDB Clusters table.
    project_id:
        If provided, restrict to clusters in this project.

    Returns
    -------
    dict with ``jobs`` (aggregated list), ``clusterResults`` (per-cluster
    detail), and ``totalJobs`` count.
    """
    clusters = get_active_clusters(clusters_table_name, project_id)

    all_jobs: list[dict[str, Any]] = []
    cluster_results: list[dict[str, Any]] = []

    for cluster in clusters:
        cluster_result = query_sacct_on_cluster(cluster)
        cluster_results.append(cluster_result)
        all_jobs.extend(cluster_result.get("jobs", []))

    return {
        "jobs": all_jobs,
        "clusterResults": cluster_results,
        "totalJobs": len(all_jobs),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _wait_for_command(
    command_id: str,
    instance_id: str,
) -> str | None:
    """Poll SSM until the command completes or times out.

    Returns the standard output on success, or ``None`` on failure/timeout.
    """
    deadline = time.time() + SSM_COMMAND_TIMEOUT
    while time.time() < deadline:
        try:
            invocation = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ClientError:
            # Command may not be ready yet
            time.sleep(SSM_POLL_INTERVAL)
            continue

        status = invocation.get("Status", "")
        if status == "Success":
            return invocation.get("StandardOutputContent", "")
        if status in ("Failed", "Cancelled", "TimedOut"):
            logger.warning(
                "SSM command %s finished with status %s: %s",
                command_id,
                status,
                invocation.get("StandardErrorContent", ""),
            )
            return None

        time.sleep(SSM_POLL_INTERVAL)

    logger.warning("SSM command %s timed out waiting for result.", command_id)
    return None


def _parse_sacct_output(output: str) -> list[dict[str, str]]:
    """Parse pipe-delimited ``sacct -p`` output into a list of dicts.

    ``sacct -p`` produces lines like::

        JobID|JobName|Partition|Account|AllocCPUS|State|ExitCode|

    Each trailing pipe is stripped before splitting.
    """
    if not output or not output.strip():
        return []

    # sacct -p with --noheader omits the header, but we define our own
    # field names based on the default sacct output columns.
    field_names = [
        "JobID",
        "JobName",
        "Partition",
        "Account",
        "AllocCPUS",
        "State",
        "ExitCode",
    ]

    jobs: list[dict[str, str]] = []
    for line in output.strip().splitlines():
        # Remove trailing pipe if present
        line = line.rstrip("|")
        parts = line.split("|")
        record: dict[str, str] = {}
        for i, field in enumerate(field_names):
            record[field] = parts[i].strip() if i < len(parts) else ""
        # Include any extra fields as numbered keys
        for i in range(len(field_names), len(parts)):
            record[f"field_{i}"] = parts[i].strip()
        jobs.append(record)

    return jobs
