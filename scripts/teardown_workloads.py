#!/usr/bin/env python3
"""Teardown workloads helper script.

Destroys all clusters and projects while retaining the foundation
infrastructure (Cognito, DynamoDB tables, API Gateway, CloudFront).

Usage:
    python scripts/teardown_workloads.py --profile thecutts

Steps:
    1. Scan Clusters table for ACTIVE/CREATING clusters and destroy them
       (PCS resources, FSx filesystems, DynamoDB status update).
    2. Scan Projects table for all project records and destroy each
       project CDK stack (``npx cdk destroy HpcProject-{projectId} --force``).
    3. Remove all cluster, project, membership, and cluster name registry
       records from DynamoDB.
    4. Report any failures encountered during teardown.

Requirements: 20.2, 20.3, 20.5, 20.6, 20.7
"""

import argparse
import logging
import subprocess
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLUSTERS_TABLE = "Clusters"
PROJECTS_TABLE = "Projects"
CLUSTER_NAME_REGISTRY_TABLE = "ClusterNameRegistry"

CDK_DESTROY_RETRY_DELAY_SECONDS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

def _scan_all(table, **kwargs) -> list[dict[str, Any]]:
    """Paginated DynamoDB scan that returns all items."""
    items: list[dict[str, Any]] = []
    response = table.scan(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
    return items


# ---------------------------------------------------------------------------
# Step 1 — Destroy active clusters
# ---------------------------------------------------------------------------

def _destroy_cluster(
    pcs_client,
    fsx_client,
    clusters_table,
    cluster: dict[str, Any],
) -> str | None:
    """Best-effort destruction of a single cluster's cloud resources.

    Deletes PCS resources (compute node groups, queue, cluster) and
    the FSx filesystem, then marks the cluster as DESTROYED in DynamoDB.

    Returns an error message string on failure, or None on success.
    """
    project_id = cluster.get("projectId", "")
    cluster_name = cluster.get("clusterName", "unknown")
    label = f"cluster '{cluster_name}' in project '{project_id}'"

    pcs_cluster_id = cluster.get("pcsClusterId", "")
    compute_ng_id = cluster.get("computeNodeGroupId", "")
    login_ng_id = cluster.get("loginNodeGroupId", "")
    queue_id = cluster.get("queueId", "")
    fsx_fs_id = cluster.get("fsxFilesystemId", "")

    errors: list[str] = []

    # --- PCS cleanup (best-effort, order matters) ---
    for ng_id, ng_label in [(compute_ng_id, "compute"), (login_ng_id, "login")]:
        if ng_id and pcs_cluster_id:
            try:
                pcs_client.delete_compute_node_group(
                    clusterIdentifier=pcs_cluster_id,
                    computeNodeGroupIdentifier=ng_id,
                )
                logger.info("Deleted PCS %s node group '%s' for %s", ng_label, ng_id, label)
            except ClientError as exc:
                msg = f"Failed to delete PCS {ng_label} node group '{ng_id}' for {label}: {exc}"
                logger.warning(msg)
                errors.append(msg)

    if queue_id and pcs_cluster_id:
        try:
            pcs_client.delete_queue(
                clusterIdentifier=pcs_cluster_id,
                queueIdentifier=queue_id,
            )
            logger.info("Deleted PCS queue '%s' for %s", queue_id, label)
        except ClientError as exc:
            msg = f"Failed to delete PCS queue '{queue_id}' for {label}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    if pcs_cluster_id:
        try:
            pcs_client.delete_cluster(clusterIdentifier=pcs_cluster_id)
            logger.info("Deleted PCS cluster '%s' for %s", pcs_cluster_id, label)
        except ClientError as exc:
            msg = f"Failed to delete PCS cluster '{pcs_cluster_id}' for {label}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    # --- FSx cleanup ---
    if fsx_fs_id:
        try:
            fsx_client.delete_file_system(FileSystemId=fsx_fs_id)
            logger.info("Deleted FSx filesystem '%s' for %s", fsx_fs_id, label)
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code in ("FileSystemNotFound", "BadRequest"):
                logger.info("FSx filesystem '%s' already gone for %s", fsx_fs_id, label)
            else:
                msg = f"Failed to delete FSx filesystem '{fsx_fs_id}' for {label}: {exc}"
                logger.warning(msg)
                errors.append(msg)

    # --- Mark cluster as DESTROYED in DynamoDB ---
    try:
        clusters_table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"CLUSTER#{cluster_name}",
            },
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "DESTROYED"},
        )
    except ClientError as exc:
        msg = f"Failed to update DynamoDB status for {label}: {exc}"
        logger.warning(msg)
        errors.append(msg)

    if errors:
        return "; ".join(errors)
    return None


def destroy_active_clusters(session: boto3.Session) -> list[str]:
    """Scan for ACTIVE/CREATING clusters and destroy them.

    Returns a list of error messages (empty on full success).
    """
    dynamodb = session.resource("dynamodb")
    pcs_client = session.client("pcs")
    fsx_client = session.client("fsx")
    clusters_table = dynamodb.Table(CLUSTERS_TABLE)

    all_clusters = _scan_all(
        clusters_table,
        FilterExpression=(
            boto3.dynamodb.conditions.Attr("status").is_in(["ACTIVE", "CREATING"])
        ),
    )

    if not all_clusters:
        logger.info("No ACTIVE or CREATING clusters found.")
        return []

    logger.info("Found %d cluster(s) to destroy.", len(all_clusters))
    failures: list[str] = []

    for cluster in all_clusters:
        error = _destroy_cluster(pcs_client, fsx_client, clusters_table, cluster)
        if error:
            failures.append(error)

    return failures


# ---------------------------------------------------------------------------
# Step 2 — Destroy project CDK stacks
# ---------------------------------------------------------------------------

def _run_cdk_destroy(project_id: str, profile: str) -> str | None:
    """Run ``npx cdk destroy`` for a project stack with one retry.

    Returns an error message on failure, or None on success.
    """
    stack_name = f"HpcProject-{project_id}"
    cmd = [
        "npx", "cdk", "destroy", stack_name,
        "--force",
        "--profile", profile,
    ]

    for attempt in range(1, 3):
        logger.info(
            "Destroying CDK stack '%s' (attempt %d/2)...",
            stack_name,
            attempt,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                logger.info("CDK stack '%s' destroyed successfully.", stack_name)
                return None

            stderr = result.stderr.strip()
            logger.warning(
                "CDK destroy '%s' failed (attempt %d): %s",
                stack_name,
                attempt,
                stderr or result.stdout.strip(),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "CDK destroy '%s' timed out (attempt %d).",
                stack_name,
                attempt,
            )
        except OSError as exc:
            logger.warning(
                "CDK destroy '%s' OS error (attempt %d): %s",
                stack_name,
                attempt,
                exc,
            )

        if attempt == 1:
            logger.info(
                "Retrying CDK destroy for '%s' in %ds...",
                stack_name,
                CDK_DESTROY_RETRY_DELAY_SECONDS,
            )
            time.sleep(CDK_DESTROY_RETRY_DELAY_SECONDS)

    return f"CDK stack '{stack_name}' destroy failed after 2 attempts."


def destroy_project_stacks(session: boto3.Session, profile: str) -> list[str]:
    """Scan for all projects and destroy their CDK stacks.

    Returns a list of error messages (empty on full success).
    """
    dynamodb = session.resource("dynamodb")
    projects_table = dynamodb.Table(PROJECTS_TABLE)

    all_projects = _scan_all(
        projects_table,
        FilterExpression=(
            boto3.dynamodb.conditions.Attr("SK").eq("METADATA")
            & boto3.dynamodb.conditions.Attr("PK").begins_with("PROJECT#")
        ),
    )

    if not all_projects:
        logger.info("No project records found.")
        return []

    logger.info("Found %d project(s) to tear down.", len(all_projects))
    failures: list[str] = []

    for project in all_projects:
        project_id = project.get("projectId", "")
        if not project_id:
            continue
        error = _run_cdk_destroy(project_id, profile)
        if error:
            failures.append(error)

    return failures


# ---------------------------------------------------------------------------
# Step 3 — Clean up DynamoDB records
# ---------------------------------------------------------------------------

def cleanup_dynamodb_records(session: boto3.Session) -> list[str]:
    """Remove all cluster, project, membership, and cluster name registry
    records from DynamoDB.

    Returns a list of error messages (empty on full success).
    """
    dynamodb = session.resource("dynamodb")
    failures: list[str] = []

    # --- Clusters table: delete all items ---
    failures.extend(_delete_all_items(dynamodb.Table(CLUSTERS_TABLE), "Clusters"))

    # --- Projects table: delete METADATA and MEMBER# records ---
    failures.extend(_delete_all_items(dynamodb.Table(PROJECTS_TABLE), "Projects"))

    # --- ClusterNameRegistry table: delete all items ---
    failures.extend(
        _delete_all_items(
            dynamodb.Table(CLUSTER_NAME_REGISTRY_TABLE),
            "ClusterNameRegistry",
        )
    )

    return failures


def _delete_all_items(table, table_label: str) -> list[str]:
    """Delete every item from a DynamoDB table using batch_writer.

    Returns a list of error messages.
    """
    failures: list[str] = []
    try:
        items = _scan_all(table, ProjectionExpression="PK, SK")
    except ClientError as exc:
        msg = f"Failed to scan {table_label} table: {exc}"
        logger.warning(msg)
        return [msg]

    if not items:
        logger.info("No records to delete in %s table.", table_label)
        return []

    logger.info("Deleting %d record(s) from %s table.", len(items), table_label)

    try:
        with table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        logger.info("Deleted all records from %s table.", table_label)
    except ClientError as exc:
        msg = f"Failed to delete records from {table_label} table: {exc}"
        logger.warning(msg)
        failures.append(msg)

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tear down all HPC workloads (clusters and projects).",
    )
    parser.add_argument(
        "--profile",
        default="thecutts",
        help="AWS CLI profile to use (default: thecutts)",
    )
    args = parser.parse_args()

    logger.info("=== HPC Workload Teardown ===")
    logger.info("Using AWS profile: %s", args.profile)

    session = boto3.Session(profile_name=args.profile)
    all_failures: list[str] = []

    # Step 1: Destroy active clusters
    logger.info("--- Step 1: Destroying active clusters ---")
    all_failures.extend(destroy_active_clusters(session))

    # Step 2: Destroy project CDK stacks
    logger.info("--- Step 2: Destroying project CDK stacks ---")
    all_failures.extend(destroy_project_stacks(session, args.profile))

    # Step 3: Clean up DynamoDB records
    logger.info("--- Step 3: Cleaning up DynamoDB records ---")
    all_failures.extend(cleanup_dynamodb_records(session))

    # Report results
    if all_failures:
        logger.error("=== Teardown completed with %d failure(s): ===", len(all_failures))
        for i, failure in enumerate(all_failures, 1):
            logger.error("  %d. %s", i, failure)
        return 1

    logger.info("=== Teardown completed successfully ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
