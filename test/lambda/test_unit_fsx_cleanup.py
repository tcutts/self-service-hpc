"""Unit tests for the FSx Cleanup Lambda I/O functions and handler.

Covers:
- scan_fsx_filesystems handles pagination correctly
- DRA deletion before filesystem deletion ordering
- DRA failure skips filesystem deletion
- SNS notification sent when deletions occur
- No notification when no orphans found
- Notification includes error details when failures occur
- Handler returns correct summary dict
- Fail-fast when DynamoDB is unreachable
- Fail-fast when FSx API is unreachable during initial scan

Requirements: 1.2, 2.1–2.5, 3.1–3.3, 4.1–4.3, 5.1–5.3, 6.1–6.4, 8.1–8.3

Infrastructure is set up once per test class via the ``fsx_cleanup_env``
fixture defined below. DRA operations are not supported by moto, so those
tests patch ``delete_filesystem_dras`` on the handler module.
"""

import json
import os
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws
import pytest

from conftest import (
    AWS_REGION,
    CLUSTERS_TABLE_NAME,
    create_clusters_table,
    reload_fsx_cleanup_modules,
)

# ---------------------------------------------------------------------------
# SNS topic name used across tests
# ---------------------------------------------------------------------------
SNS_TOPIC_NAME = "hpc-cluster-lifecycle"


# ---------------------------------------------------------------------------
# Fixture — FSx cleanup environment (class-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def fsx_cleanup_env():
    """Provide a single moto mock_aws context for FSx cleanup tests.

    Yields a dict with:
        clusters_table: the Clusters DynamoDB Table resource
        sns_topic_arn:  the SNS topic ARN
        fsx_client:     a boto3 FSx client bound to the mock
        sns_client:     a boto3 SNS client bound to the mock
        ec2_client:     a boto3 EC2 client bound to the mock
        subnet_id:      a VPC subnet ID for creating filesystems
        modules:        (handler_mod, cleanup_mod)
    """
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"

        # Create DynamoDB Clusters table
        clusters_table = create_clusters_table()

        # Create SNS topic
        sns_client = boto3.client("sns", region_name=AWS_REGION)
        topic_resp = sns_client.create_topic(Name=SNS_TOPIC_NAME)
        sns_topic_arn = topic_resp["TopicArn"]

        # Create VPC + subnet for FSx filesystems
        ec2_client = boto3.client("ec2", region_name=AWS_REGION)
        vpc = ec2_client.create_vpc(CidrBlock="10.0.0.0/16")
        vpc_id = vpc["Vpc"]["VpcId"]
        subnet = ec2_client.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")
        subnet_id = subnet["Subnet"]["SubnetId"]

        # Set environment variables
        os.environ["CLUSTERS_TABLE_NAME"] = CLUSTERS_TABLE_NAME
        os.environ["SNS_TOPIC_ARN"] = sns_topic_arn
        os.environ["AWS_REGION"] = AWS_REGION

        # Reload modules so boto3 clients bind to moto mocks
        handler_mod, cleanup_mod = reload_fsx_cleanup_modules()

        fsx_client = boto3.client("fsx", region_name=AWS_REGION)

        yield {
            "clusters_table": clusters_table,
            "sns_topic_arn": sns_topic_arn,
            "fsx_client": fsx_client,
            "sns_client": sns_client,
            "ec2_client": ec2_client,
            "subnet_id": subnet_id,
            "modules": (handler_mod, cleanup_mod),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_lustre_filesystem(fsx_client, subnet_id, project_id, cluster_name, extra_tags=None):
    """Create a tagged FSx Lustre filesystem in moto and return its ID."""
    tags = [
        {"Key": "Project", "Value": project_id},
        {"Key": "ClusterName", "Value": cluster_name},
    ]
    if extra_tags:
        tags.extend(extra_tags)

    resp = fsx_client.create_file_system(
        FileSystemType="LUSTRE",
        StorageCapacity=1200,
        SubnetIds=[subnet_id],
        LustreConfiguration={"DeploymentType": "SCRATCH_1"},
        Tags=tags,
    )
    return resp["FileSystem"]["FileSystemId"]


def _create_untagged_filesystem(fsx_client, subnet_id):
    """Create an FSx Lustre filesystem without Project/ClusterName tags."""
    resp = fsx_client.create_file_system(
        FileSystemType="LUSTRE",
        StorageCapacity=1200,
        SubnetIds=[subnet_id],
        LustreConfiguration={"DeploymentType": "SCRATCH_1"},
        Tags=[{"Key": "Environment", "Value": "test"}],
    )
    return resp["FileSystem"]["FileSystemId"]


def _seed_cluster_record(clusters_table, project_id, cluster_name, status):
    """Insert a cluster record into the Clusters DynamoDB table."""
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": f"CLUSTER#{cluster_name}",
        "projectId": project_id,
        "clusterName": cluster_name,
        "status": status,
    })


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("fsx_cleanup_env")
class TestFsxCleanupUnit:
    """Unit tests for FSx cleanup I/O functions and handler."""

    # -- scan_fsx_filesystems pagination -----------------------------------

    def test_scan_handles_pagination(self, fsx_cleanup_env):
        """scan_fsx_filesystems retrieves all Lustre filesystems across pages.

        Validates: Requirement 1.2
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]

        # Create 3 filesystems
        fs_ids = []
        for i in range(3):
            fs_id = _create_lustre_filesystem(
                fsx_client, subnet_id, f"pag-proj-{i}", f"pag-cluster-{i}"
            )
            fs_ids.append(fs_id)

        # scan_fsx_filesystems should find all of them
        result = cleanup_mod.scan_fsx_filesystems()

        found_ids = {fs["FileSystemId"] for fs in result}
        for fs_id in fs_ids:
            assert fs_id in found_ids, f"Expected {fs_id} in scan results"

        # Clean up
        for fs_id in fs_ids:
            fsx_client.delete_file_system(FileSystemId=fs_id)

    # -- DRA deletion before filesystem deletion ---------------------------

    def test_dra_deleted_before_filesystem(self, fsx_cleanup_env):
        """Handler deletes DRAs before the filesystem for orphaned resources.

        Validates: Requirements 3.1, 3.2
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]
        clusters_table = fsx_cleanup_env["clusters_table"]

        fs_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "dra-proj", "dra-cluster"
        )
        # No cluster record → orphaned

        # Track call ordering
        call_order = []

        original_delete_dras = cleanup_mod.delete_filesystem_dras
        original_delete_fs = cleanup_mod.delete_filesystem

        def tracking_delete_dras(filesystem_id):
            call_order.append(("dra", filesystem_id))
            return True  # Pretend DRAs deleted successfully

        def tracking_delete_fs(filesystem_id):
            call_order.append(("fs", filesystem_id))
            # Actually delete so moto state is clean
            return original_delete_fs(filesystem_id)

        with patch.object(handler_mod, "delete_filesystem_dras", side_effect=tracking_delete_dras), \
             patch.object(handler_mod, "delete_filesystem", side_effect=tracking_delete_fs):
            handler_mod.handler({}, None)

        # Verify DRA deletion was called before filesystem deletion
        dra_calls = [c for c in call_order if c[0] == "dra" and c[1] == fs_id]
        fs_calls = [c for c in call_order if c[0] == "fs" and c[1] == fs_id]

        assert len(dra_calls) >= 1, "DRA deletion should be called"
        assert len(fs_calls) >= 1, "Filesystem deletion should be called"

        # DRA call should come before FS call
        dra_idx = call_order.index(dra_calls[0])
        fs_idx = call_order.index(fs_calls[0])
        assert dra_idx < fs_idx, "DRA deletion must happen before filesystem deletion"

    # -- DRA failure skips filesystem deletion ------------------------------

    def test_dra_failure_skips_filesystem_deletion(self, fsx_cleanup_env):
        """When DRA deletion fails, the filesystem is not deleted.

        Validates: Requirement 3.3
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]

        fs_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "dra-fail-proj", "dra-fail-cluster"
        )
        # No cluster record → orphaned

        fs_delete_called = []

        def failing_delete_dras(filesystem_id):
            return False  # DRA deletion fails

        def tracking_delete_fs(filesystem_id):
            fs_delete_called.append(filesystem_id)
            return True

        with patch.object(handler_mod, "delete_filesystem_dras", side_effect=failing_delete_dras), \
             patch.object(handler_mod, "delete_filesystem", side_effect=tracking_delete_fs):
            result = handler_mod.handler({}, None)

        # Filesystem deletion should NOT have been called for this fs
        assert fs_id not in fs_delete_called, (
            "Filesystem deletion should be skipped when DRA deletion fails"
        )
        assert result["total_failed"] >= 1

        # Clean up the filesystem that wasn't deleted
        fsx_client.delete_file_system(FileSystemId=fs_id)

    # -- SNS notification sent when deletions occur ------------------------

    def test_sns_notification_sent_on_deletions(self, fsx_cleanup_env):
        """SNS notification is published when orphaned filesystems are deleted.

        Validates: Requirements 6.1, 6.2
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]
        sns_client = fsx_cleanup_env["sns_client"]
        sns_topic_arn = fsx_cleanup_env["sns_topic_arn"]

        fs_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "sns-proj", "sns-cluster"
        )
        # No cluster record → orphaned

        publish_calls = []

        original_publish = cleanup_mod.publish_notification

        def tracking_publish(subject, message):
            publish_calls.append({"subject": subject, "message": message})
            original_publish(subject, message)

        def mock_delete_dras(filesystem_id):
            return True

        with patch.object(handler_mod, "delete_filesystem_dras", side_effect=mock_delete_dras), \
             patch.object(handler_mod, "publish_notification", side_effect=tracking_publish):
            handler_mod.handler({}, None)

        assert len(publish_calls) == 1, "Exactly one notification should be published"
        assert "sns-proj" in publish_calls[0]["message"]
        assert "sns-cluster" in publish_calls[0]["message"]

    # -- No notification when no orphans found -----------------------------

    def test_no_notification_when_no_orphans(self, fsx_cleanup_env):
        """No SNS notification is published when all filesystems are active.

        Validates: Requirement 6.3
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]
        clusters_table = fsx_cleanup_env["clusters_table"]

        fs_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "active-proj", "active-cluster"
        )
        _seed_cluster_record(clusters_table, "active-proj", "active-cluster", "ACTIVE")

        publish_calls = []

        def tracking_publish(subject, message):
            publish_calls.append(True)

        with patch.object(handler_mod, "publish_notification", side_effect=tracking_publish):
            handler_mod.handler({}, None)

        assert len(publish_calls) == 0, "No notification should be sent when no orphans found"

        # Clean up
        fsx_client.delete_file_system(FileSystemId=fs_id)

    # -- Notification includes error details when failures occur -----------

    def test_notification_includes_error_details(self, fsx_cleanup_env):
        """Notification message includes error details for failed deletions.

        Validates: Requirement 6.4
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]

        fs_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "err-proj", "err-cluster"
        )
        # No cluster record → orphaned

        publish_calls = []

        def mock_delete_dras(filesystem_id):
            return False  # DRA fails → error recorded

        def tracking_publish(subject, message):
            publish_calls.append({"subject": subject, "message": message})

        with patch.object(handler_mod, "delete_filesystem_dras", side_effect=mock_delete_dras), \
             patch.object(handler_mod, "publish_notification", side_effect=tracking_publish):
            result = handler_mod.handler({}, None)

        assert len(publish_calls) == 1, "Notification should be sent when failures occur"
        msg = publish_calls[0]["message"]
        assert "err-proj" in msg
        assert "err-cluster" in msg
        assert "DRA deletion failed" in msg or "Error" in msg or "error" in msg.lower()

        # Clean up
        fsx_client.delete_file_system(FileSystemId=fs_id)

    # -- Handler returns correct summary dict ------------------------------

    def test_handler_returns_correct_summary(self, fsx_cleanup_env):
        """Handler returns a summary dict with correct counts.

        Validates: Requirements 5.1, 5.3
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]
        clusters_table = fsx_cleanup_env["clusters_table"]

        # Create one orphaned and one active filesystem
        orphan_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "sum-orphan-proj", "sum-orphan-cluster"
        )
        active_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "sum-active-proj", "sum-active-cluster"
        )
        _seed_cluster_record(
            clusters_table, "sum-active-proj", "sum-active-cluster", "ACTIVE"
        )
        # No cluster record for orphan → orphaned

        def mock_delete_dras(filesystem_id):
            return True

        with patch.object(handler_mod, "delete_filesystem_dras", side_effect=mock_delete_dras):
            result = handler_mod.handler({}, None)

        assert result["total_scanned"] >= 2
        assert result["total_orphaned"] >= 1
        assert result["total_deleted"] >= 1
        assert result["total_failed"] >= 0
        assert "errors" in result
        assert result["total_orphaned"] == result["total_deleted"] + result["total_failed"]

        # Clean up active filesystem
        fsx_client.delete_file_system(FileSystemId=active_id)

    # -- Fail-fast when DynamoDB is unreachable ----------------------------

    def test_fail_fast_dynamodb_unreachable(self, fsx_cleanup_env):
        """Handler terminates without deletions when DynamoDB is unreachable.

        Validates: Requirement 8.2
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]
        fsx_client = fsx_cleanup_env["fsx_client"]
        subnet_id = fsx_cleanup_env["subnet_id"]

        fs_id = _create_lustre_filesystem(
            fsx_client, subnet_id, "dynamo-fail-proj", "dynamo-fail-cluster"
        )

        def failing_lookup(project_id, cluster_name):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "Service unavailable"}},
                "GetItem",
            )

        fs_delete_called = []

        def tracking_delete_fs(filesystem_id):
            fs_delete_called.append(filesystem_id)
            return True

        with patch.object(handler_mod, "lookup_cluster_record", side_effect=failing_lookup), \
             patch.object(handler_mod, "delete_filesystem", side_effect=tracking_delete_fs):
            result = handler_mod.handler({}, None)

        # Should abort without deleting anything
        assert len(fs_delete_called) == 0, "No filesystems should be deleted when DynamoDB is unreachable"
        assert result["total_deleted"] == 0
        assert any("DynamoDB unreachable" in e.get("error", "") for e in result.get("errors", []))

        # Clean up
        fsx_client.delete_file_system(FileSystemId=fs_id)

    # -- Fail-fast when FSx API is unreachable during initial scan ---------

    def test_fail_fast_fsx_api_unreachable(self, fsx_cleanup_env):
        """Handler terminates when FSx API is unreachable during initial scan.

        Validates: Requirement 8.3
        """
        handler_mod, cleanup_mod = fsx_cleanup_env["modules"]

        def failing_scan():
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "FSx unavailable"}},
                "DescribeFileSystems",
            )

        with patch.object(handler_mod, "scan_fsx_filesystems", side_effect=failing_scan):
            result = handler_mod.handler({}, None)

        assert result["total_scanned"] == 0
        assert result["total_deleted"] == 0
        assert any("FSx API unreachable" in e.get("error", "") for e in result.get("errors", []))
