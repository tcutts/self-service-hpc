"""Property-based tests for storage configuration validation.

**Property 1: Invalid storageMode rejected** — any string not in
{"lustre", "mountpoint"} is rejected.
**Validates: Requirements 1.6**

**Property 2: Invalid lustreCapacityGiB rejected** — values < 1200 or
not multiples of 1200 are rejected.
**Validates: Requirements 2.3, 2.4**

**Property 3: Valid capacity flows to FSx** — multiples of 1200 >= 1200
are accepted and used as StorageCapacity.
**Validates: Requirements 2.6**

**Property 7: Invalid node scaling rejected** — minNodes > maxNodes or
out-of-range values are rejected.
**Validates: Requirements 5.3, 5.4, 5.5**

**Property 8: resolve_template preserves overrides** — user-provided
minNodes/maxNodes/storageMode are never overwritten.
**Validates: Requirements 5.6, 5.7, 9.2, 9.3**

**Property 9: Cluster record round-trip** — storageMode and capacity
are persisted and retrievable.
**Validates: Requirements 6.1, 6.2**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck, assume
import hypothesis.strategies as st
from moto import mock_aws

# Add conftest helpers to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    TEMPLATES_TABLE_NAME,
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    create_projects_table,
    create_clusters_table,
    create_templates_table,
    create_cluster_name_registry_table,
    reload_cluster_ops_handler_modules,
    _CLUSTER_OPS_DIR,
    _load_module_from,
    _ensure_shared_modules,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Any text that is NOT "lustre" or "mountpoint"
invalid_storage_mode_strategy = st.text(min_size=1, max_size=50).filter(
    lambda s: s not in ("lustre", "mountpoint")
)

# Integers that are < 1200 OR not multiples of 1200
invalid_lustre_capacity_strategy = st.one_of(
    st.integers(max_value=1199),
    st.integers(min_value=1201).filter(lambda x: x % 1200 != 0),
)

# Valid lustre capacities: multiples of 1200 >= 1200
valid_lustre_capacity_strategy = st.integers(min_value=1, max_value=50).map(
    lambda x: x * 1200
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_env():
    """Set environment variables for mocked AWS."""
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
        "CLUSTERS_TABLE_NAME": CLUSTERS_TABLE_NAME,
        "CLUSTER_NAME_REGISTRY_TABLE_NAME": CLUSTER_NAME_REGISTRY_TABLE_NAME,
        "CREATION_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
        "DESTRUCTION_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test-destroy",
    })


def _seed_project(projects_table, project_id="test-proj"):
    """Insert a minimal project record with infrastructure."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "budgetBreached": False,
        "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "s3BucketName": f"hpc-{project_id}-storage",
        "vpcId": f"vpc-{project_id}",
        "efsFileSystemId": f"fs-{project_id}",
        "publicSubnetIds": ["subnet-pub-1"],
        "privateSubnetIds": ["subnet-priv-1"],
        "securityGroupIds": {
            "headNode": "sg-head",
            "computeNode": "sg-compute",
            "efs": "sg-efs",
            "fsx": "sg-fsx",
        },
    })


def _build_create_event(project_id, body):
    """Build an API Gateway event for cluster creation."""
    return {
        "httpMethod": "POST",
        "resource": "/projects/{projectId}/clusters",
        "pathParameters": {"projectId": project_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "test-user",
                    "sub": "sub-test-user",
                    "cognito:groups": f"ProjectUser-{project_id}",
                }
            }
        },
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Property 1: Invalid storageMode rejected
# ---------------------------------------------------------------------------


@given(storage_mode=invalid_storage_mode_strategy)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_invalid_storage_mode_rejected(storage_mode):
    """Any storageMode value not in {"lustre", "mountpoint"} SHALL be
    rejected with a VALIDATION_ERROR and HTTP 400.

    **Validates: Requirements 1.6**
    """
    _setup_env()

    projects_table = create_projects_table()
    create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_project(projects_table)

    event = _build_create_event("test-proj", {
        "clusterName": "test-cluster",
        "templateId": "tpl-1",
        "storageMode": storage_mode,
    })

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "storageMode" in body["error"]["message"].lower() or "storagemode" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Property 2: Invalid lustreCapacityGiB rejected
# ---------------------------------------------------------------------------


@given(capacity=invalid_lustre_capacity_strategy)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_invalid_lustre_capacity_rejected(capacity):
    """Any lustreCapacityGiB value that is < 1200 or not a multiple of 1200
    SHALL be rejected with a VALIDATION_ERROR and HTTP 400 when
    storageMode is "lustre".

    **Validates: Requirements 2.3, 2.4**
    """
    _setup_env()

    projects_table = create_projects_table()
    create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_project(projects_table)

    event = _build_create_event("test-proj", {
        "clusterName": "test-cluster",
        "templateId": "tpl-1",
        "storageMode": "lustre",
        "lustreCapacityGiB": capacity,
    })

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "capacity" in body["error"]["message"].lower() or "lustre" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Property 3: Valid capacity flows to FSx
# ---------------------------------------------------------------------------


@given(capacity=valid_lustre_capacity_strategy)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_valid_capacity_flows_to_fsx(capacity):
    """Valid lustreCapacityGiB values (multiples of 1200 >= 1200) SHALL be
    accepted and passed as the StorageCapacity parameter to
    create_fsx_filesystem.

    **Validates: Requirements 2.6**
    """
    _setup_env()
    os.environ["TEMPLATES_TABLE_NAME"] = "ClusterTemplates"

    # Load the cluster_creation module inside the mock context
    _ensure_shared_modules()
    errors_mod = _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

    mock_response = {
        "FileSystem": {
            "FileSystemId": "fs-test-123",
            "Lifecycle": "CREATING",
        }
    }

    event = {
        "projectId": "test-proj",
        "clusterName": "cap-test",
        "lustreCapacityGiB": capacity,
        "privateSubnetIds": ["subnet-priv-1"],
        "securityGroupIds": {"fsx": "sg-fsx"},
    }

    with patch.object(creation_mod.fsx_client, "create_file_system", return_value=mock_response) as mock_create, \
         patch.object(creation_mod, "_update_step_progress"):
        result = creation_mod.create_fsx_filesystem(event)

    # Verify the capacity was passed through
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["StorageCapacity"] == capacity

    # Verify the filesystem ID is returned
    assert result["fsxFilesystemId"] == "fs-test-123"


# ---------------------------------------------------------------------------
# Additional strategies for Properties 4–6
# ---------------------------------------------------------------------------

import string
import boto3

# S3 bucket name strategy (valid bucket-name-like strings)
s3_bucket_name_strategy = st.from_regex(
    r'[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]', fullmatch=True
)

# DNS name strategy (non-empty text)
fsx_dns_name_strategy = st.text(min_size=1, max_size=100).filter(lambda s: s.strip())

# Mount name strategy (lowercase ascii, non-empty)
fsx_mount_name_strategy = st.text(
    min_size=1, max_size=50, alphabet=string.ascii_lowercase
)


# ---------------------------------------------------------------------------
# Property 4: Mountpoint S3 commands correct
# ---------------------------------------------------------------------------


@given(bucket_name=s3_bucket_name_strategy)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_mountpoint_s3_commands_contain_bucket_and_path(bucket_name):
    """Generated Mountpoint S3 commands SHALL contain the bucket name
    and the mount path.

    **Validates: Requirements 3.4**
    """
    posix_mod = _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")

    mount_path = "/data"
    commands = posix_mod.generate_mountpoint_s3_commands(bucket_name, mount_path)

    joined = "\n".join(commands)

    # The bucket name must appear in the commands
    assert bucket_name in joined, (
        f"Bucket name '{bucket_name}' not found in commands"
    )

    # The mount path must appear in the commands
    assert mount_path in joined, (
        f"Mount path '{mount_path}' not found in commands"
    )


# ---------------------------------------------------------------------------
# Property 5: FSx mount commands correct
# ---------------------------------------------------------------------------


@given(
    dns_name=fsx_dns_name_strategy,
    mount_name=fsx_mount_name_strategy,
)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_fsx_mount_commands_contain_dns_and_mount_name(dns_name, mount_name):
    """Generated FSx Lustre mount commands SHALL contain the DNS name
    and the mount name.

    **Validates: Requirements 3.5**
    """
    posix_mod = _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")

    mount_path = "/data"
    commands = posix_mod.generate_fsx_lustre_mount_commands(dns_name, mount_name, mount_path)

    joined = "\n".join(commands)

    # The DNS name must appear in the commands
    assert dns_name in joined, (
        f"DNS name '{dns_name}' not found in commands"
    )

    # The mount name must appear in the commands
    assert mount_name in joined, (
        f"Mount name '{mount_name}' not found in commands"
    )


# ---------------------------------------------------------------------------
# Property 6: S3 IAM policy scoped correctly
# ---------------------------------------------------------------------------


@given(bucket_name=s3_bucket_name_strategy)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_s3_iam_policy_scoped_to_bucket(bucket_name):
    """The MountpointS3Access IAM policy resource ARNs SHALL contain
    only the specific bucket name — no wildcards in the bucket portion.

    **Validates: Requirements 4.1, 4.3**
    """
    _setup_env()

    # Load modules inside mock context
    _ensure_shared_modules()
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

    project_id = "test-proj"
    cluster_name = "iam-test"

    # Create the IAM roles that configure_mountpoint_s3_iam expects
    iam = boto3.client("iam", region_name=AWS_REGION)
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    for suffix in ["login", "compute"]:
        iam.create_role(
            RoleName=f"AWSPCS-{project_id}-{cluster_name}-{suffix}",
            AssumeRolePolicyDocument=trust_policy,
        )

    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "s3BucketName": bucket_name,
    }

    creation_mod.configure_mountpoint_s3_iam(event)

    # Verify the policy on both roles
    for suffix in ["login", "compute"]:
        role_name = f"AWSPCS-{project_id}-{cluster_name}-{suffix}"
        policy_resp = iam.get_role_policy(
            RoleName=role_name,
            PolicyName="MountpointS3Access",
        )
        policy_doc = policy_resp["PolicyDocument"]
        # moto may return the document as a dict or a JSON string
        if isinstance(policy_doc, str):
            policy_doc = json.loads(policy_doc)

        resources = policy_doc["Statement"][0]["Resource"]

        # Exactly two resource ARNs: bucket and bucket/*
        assert len(resources) == 2, (
            f"Expected 2 resource ARNs, got {len(resources)}: {resources}"
        )

        expected_bucket_arn = f"arn:aws:s3:::{bucket_name}"
        expected_objects_arn = f"arn:aws:s3:::{bucket_name}/*"

        assert expected_bucket_arn in resources, (
            f"Bucket ARN '{expected_bucket_arn}' not in resources: {resources}"
        )
        assert expected_objects_arn in resources, (
            f"Objects ARN '{expected_objects_arn}' not in resources: {resources}"
        )

        # No wildcard bucket names — every resource must reference the specific bucket
        for arn in resources:
            # Strip the s3 prefix to get the bucket portion
            bucket_portion = arn.replace("arn:aws:s3:::", "")
            assert bucket_portion.startswith(bucket_name), (
                f"Resource ARN '{arn}' does not start with bucket name '{bucket_name}'"
            )



# ---------------------------------------------------------------------------
# Property 7: Invalid node scaling rejected
# ---------------------------------------------------------------------------

# Strategy: generate minNodes/maxNodes pairs that violate at least one rule:
#   - minNodes < 0
#   - maxNodes < 1
#   - minNodes > maxNodes (when both individually valid)
invalid_node_scaling_strategy = st.one_of(
    # Case 1: minNodes negative
    st.tuples(
        st.integers(max_value=-1),
        st.integers(min_value=1),
    ),
    # Case 2: maxNodes < 1
    st.tuples(
        st.integers(min_value=0, max_value=100),
        st.integers(max_value=0),
    ),
    # Case 3: minNodes > maxNodes (both individually valid)
    st.integers(min_value=1, max_value=1000).flatmap(
        lambda max_n: st.tuples(
            st.integers(min_value=max_n + 1, max_value=max_n + 1000),
            st.just(max_n),
        )
    ),
)


@given(data=invalid_node_scaling_strategy)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_invalid_node_scaling_rejected(data):
    """Any minNodes/maxNodes combination where minNodes > maxNodes, minNodes < 0,
    or maxNodes < 1 SHALL be rejected with a VALIDATION_ERROR and HTTP 400.

    **Validates: Requirements 5.3, 5.4, 5.5**
    """
    min_nodes, max_nodes = data

    _setup_env()

    projects_table = create_projects_table()
    create_clusters_table()
    create_cluster_name_registry_table()

    handler_mod, _, _, _, _ = reload_cluster_ops_handler_modules()

    _seed_project(projects_table)

    event = _build_create_event("test-proj", {
        "clusterName": "scale-test",
        "templateId": "tpl-1",
        "storageMode": "mountpoint",
        "minNodes": min_nodes,
        "maxNodes": max_nodes,
    })

    response = handler_mod.handler(event, {})

    assert response["statusCode"] == 400, (
        f"Expected 400 for minNodes={min_nodes}, maxNodes={max_nodes}, "
        f"got {response['statusCode']}"
    )
    body = json.loads(response["body"])
    assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Property 8: resolve_template preserves overrides
# ---------------------------------------------------------------------------


@given(
    user_min=st.integers(min_value=0, max_value=500),
    user_max=st.integers(min_value=1, max_value=500),
    storage_mode=st.sampled_from(["lustre", "mountpoint"]),
)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_resolve_template_preserves_overrides(user_min, user_max, storage_mode):
    """User-provided minNodes, maxNodes, and storageMode SHALL never be
    overwritten by resolve_template — the template values are only used
    as fallback when the user does not provide overrides.

    **Validates: Requirements 5.6, 5.7, 9.2, 9.3**
    """
    assume(user_min <= user_max)

    _setup_env()
    os.environ["TEMPLATES_TABLE_NAME"] = TEMPLATES_TABLE_NAME

    # Create the templates table and seed a template with different values
    templates_table = create_templates_table()
    templates_table.put_item(Item={
        "PK": "TEMPLATE#tpl-override",
        "SK": "METADATA",
        "templateId": "tpl-override",
        "templateName": "Override Test Template",
        "loginInstanceType": "c7g.medium",
        "instanceTypes": ["c7g.medium"],
        "purchaseOption": "ONDEMAND",
        "minNodes": 99,   # different from user values
        "maxNodes": 999,  # different from user values
    })

    # Load modules inside mock context
    _ensure_shared_modules()
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

    event = {
        "projectId": "test-proj",
        "clusterName": "override-test",
        "templateId": "tpl-override",
        "storageMode": storage_mode,
        "lustreCapacityGiB": 2400 if storage_mode == "lustre" else None,
        "minNodes": user_min,
        "maxNodes": user_max,
    }

    result = creation_mod.resolve_template(event)

    # User-provided values must be preserved, not overwritten by template
    assert result["minNodes"] == user_min, (
        f"minNodes was overwritten: expected {user_min}, got {result['minNodes']}"
    )
    assert result["maxNodes"] == user_max, (
        f"maxNodes was overwritten: expected {user_max}, got {result['maxNodes']}"
    )
    assert result["storageMode"] == storage_mode, (
        f"storageMode was overwritten: expected {storage_mode}, got {result['storageMode']}"
    )
    if storage_mode == "lustre":
        assert result.get("lustreCapacityGiB") == 2400, (
            f"lustreCapacityGiB was overwritten: expected 2400, got {result.get('lustreCapacityGiB')}"
        )


# ---------------------------------------------------------------------------
# Property 9: Cluster record round-trip
# ---------------------------------------------------------------------------


@given(
    storage_mode=st.sampled_from(["lustre", "mountpoint"]),
    capacity=st.integers(min_value=1, max_value=10).map(lambda x: x * 1200),
)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_cluster_record_round_trip(storage_mode, capacity):
    """storageMode and lustreCapacityGiB (when lustre) SHALL be persisted
    by record_cluster and retrievable from DynamoDB.

    **Validates: Requirements 6.1, 6.2**
    """
    _setup_env()
    os.environ["TEMPLATES_TABLE_NAME"] = TEMPLATES_TABLE_NAME
    os.environ["CLUSTER_LIFECYCLE_SNS_TOPIC_ARN"] = ""

    clusters_table = create_clusters_table()

    # Load modules inside mock context
    _ensure_shared_modules()
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    creation_mod = _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")

    project_id = "roundtrip-proj"
    cluster_name = "roundtrip-cluster"

    event = {
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": "tpl-1",
        "storageMode": storage_mode,
        "minNodes": 2,
        "maxNodes": 20,
        "pcsClusterId": "pcs-123",
        "pcsClusterArn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs-123",
        "loginNodeGroupId": "lng-1",
        "computeNodeGroupId": "cng-1",
        "queueId": "q-1",
        "fsxFilesystemId": "fs-123" if storage_mode == "lustre" else "",
        "loginNodeIp": "10.0.0.1",
        "createdBy": "test-user",
    }
    if storage_mode == "lustre":
        event["lustreCapacityGiB"] = capacity

    # Patch _update_step_progress to avoid needing the full cluster record
    from unittest.mock import patch as mock_patch
    with mock_patch.object(creation_mod, "_update_step_progress"):
        creation_mod.record_cluster(event)

    # Read back from DynamoDB
    response = clusters_table.get_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
        }
    )
    item = response["Item"]

    # storageMode must be persisted
    assert item["storageMode"] == storage_mode, (
        f"storageMode not persisted: expected {storage_mode}, got {item['storageMode']}"
    )

    # lustreCapacityGiB must be persisted when lustre, absent when mountpoint
    if storage_mode == "lustre":
        assert int(item["lustreCapacityGiB"]) == capacity, (
            f"lustreCapacityGiB not persisted: expected {capacity}, got {item.get('lustreCapacityGiB')}"
        )
    else:
        assert "lustreCapacityGiB" not in item, (
            f"lustreCapacityGiB should not be present for mountpoint, but found {item.get('lustreCapacityGiB')}"
        )
