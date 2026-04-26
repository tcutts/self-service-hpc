# Feature: project-update, Property 7: CDK command format
"""Property-based tests verifying update workflow step handlers.

Property 7: For any valid project ID string, the `start_cdk_update` step
passes a CodeBuild environment variable CDK_COMMAND whose value equals
`npx cdk deploy HpcProject-{projectId} --exclusively --require-approval never`.
**Validates: Requirements 3.2, 3.3**
"""

import os
from unittest.mock import MagicMock

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    _PROJECT_MGMT_DIR,
    _load_module_from,
    create_projects_table,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Project IDs: alphanumeric with hyphens, matching realistic project ID patterns
project_id_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-"),
).filter(lambda s: s.strip() and not s.startswith("-") and not s.endswith("-"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_updating_project(projects_table, project_id):
    """Insert a project in UPDATING status into the mocked DynamoDB table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": f"Project {project_id}",
        "costAllocationTag": project_id,
        "status": "UPDATING",
        "errorMessage": "",
        "currentStep": 0,
        "totalSteps": 5,
        "stepDescription": "",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
    })


# ---------------------------------------------------------------------------
# Property 7: CDK command format is correct for any project ID
# ---------------------------------------------------------------------------

@given(project_id=project_id_strategy)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_cdk_command_format_correct_for_any_project_id(project_id):
    """For any valid project ID string, start_cdk_update passes CDK_COMMAND
    equal to `npx cdk deploy HpcProject-{projectId} --exclusively --require-approval never`.

    **Validates: Requirements 3.2, 3.3**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
    })

    projects_table = create_projects_table()

    # Load modules inside mock context so boto3 binds to moto
    _load_module_from(_PROJECT_MGMT_DIR, "errors")
    _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")
    update_mod = _load_module_from(_PROJECT_MGMT_DIR, "project_update")

    # Mock CodeBuild client (not supported by moto)
    mock_codebuild = MagicMock()
    mock_codebuild.start_build.return_value = {
        "build": {"id": f"build-{project_id}-001"}
    }
    update_mod.codebuild_client = mock_codebuild
    update_mod.CODEBUILD_PROJECT_NAME = "test-codebuild-project"

    # Seed an UPDATING project
    _seed_updating_project(projects_table, project_id)

    # Execute start_cdk_update
    result = update_mod.start_cdk_update({"projectId": project_id})

    # Verify CodeBuild was called
    mock_codebuild.start_build.assert_called_once()

    # Extract the environment variables passed to CodeBuild
    call_args = mock_codebuild.start_build.call_args
    env_vars = call_args.kwargs["environmentVariablesOverride"]

    # Find the CDK_COMMAND variable
    cdk_cmd_var = next(
        (v for v in env_vars if v["name"] == "CDK_COMMAND"), None
    )
    assert cdk_cmd_var is not None, "CDK_COMMAND environment variable not found"

    expected_command = (
        f"npx cdk deploy HpcProject-{project_id} "
        f"--exclusively --require-approval never"
    )
    assert cdk_cmd_var["value"] == expected_command, (
        f"Expected CDK_COMMAND '{expected_command}' but got '{cdk_cmd_var['value']}'"
    )

    # Also verify PROJECT_ID is passed correctly
    project_id_var = next(
        (v for v in env_vars if v["name"] == "PROJECT_ID"), None
    )
    assert project_id_var is not None, "PROJECT_ID environment variable not found"
    assert project_id_var["value"] == project_id, (
        f"Expected PROJECT_ID '{project_id}' but got '{project_id_var['value']}'"
    )

    # Verify buildId is returned in the result
    assert "buildId" in result


# ---------------------------------------------------------------------------
# Strategies for Property 8
# ---------------------------------------------------------------------------

# VPC IDs: vpc- followed by hex characters
vpc_id_strategy = st.from_regex(r"vpc-[0-9a-f]{8,17}", fullmatch=True)

# EFS filesystem IDs: fs- followed by hex characters
efs_id_strategy = st.from_regex(r"fs-[0-9a-f]{8,17}", fullmatch=True)

# S3 bucket names: lowercase alphanumeric with hyphens, 3-20 chars
s3_bucket_strategy = st.from_regex(r"[a-z][a-z0-9\-]{2,19}", fullmatch=True)

# Subnet IDs: subnet- followed by hex characters
subnet_id_strategy = st.from_regex(r"subnet-[0-9a-f]{8,17}", fullmatch=True)

# Security group IDs: sg- followed by hex characters
sg_id_strategy = st.from_regex(r"sg-[0-9a-f]{8,17}", fullmatch=True)

# Lists of subnet IDs (1-3 items to keep tests fast)
subnet_list_strategy = st.lists(subnet_id_strategy, min_size=1, max_size=3)

# Security group map with the four expected keys
security_groups_strategy = st.fixed_dictionaries({
    "headNode": sg_id_strategy,
    "computeNode": sg_id_strategy,
    "efs": sg_id_strategy,
    "fsx": sg_id_strategy,
})


# ---------------------------------------------------------------------------
# Property 8: Infrastructure outputs round-trip through DynamoDB
# ---------------------------------------------------------------------------

@given(
    project_id=project_id_strategy,
    vpc_id=vpc_id_strategy,
    efs_id=efs_id_strategy,
    s3_bucket=s3_bucket_strategy,
    public_subnets=subnet_list_strategy,
    private_subnets=subnet_list_strategy,
    security_groups=security_groups_strategy,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_infrastructure_outputs_roundtrip_through_dynamodb(
    project_id, vpc_id, efs_id, s3_bucket,
    public_subnets, private_subnets, security_groups,
):
    """For any set of valid infrastructure output values, after
    record_updated_infrastructure writes them to DynamoDB, reading the
    project record back returns the same values.

    **Validates: Requirements 3.5, 3.6, 4.3**
    """
    import boto3 as _boto3

    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
    })

    projects_table = create_projects_table()

    # Load modules inside mock context so boto3 binds to moto
    _load_module_from(_PROJECT_MGMT_DIR, "errors")
    _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")
    update_mod = _load_module_from(_PROJECT_MGMT_DIR, "project_update")

    # Seed a project in UPDATING status (required for transition to ACTIVE)
    _seed_updating_project(projects_table, project_id)

    # Build the event payload as record_updated_infrastructure expects it
    stack_name = f"HpcProject-{project_id}"
    event = {
        "projectId": project_id,
        "cdkStackName": stack_name,
        "vpcId": vpc_id,
        "efsFileSystemId": efs_id,
        "s3BucketName": s3_bucket,
        "publicSubnetIds": public_subnets,
        "privateSubnetIds": private_subnets,
        "securityGroupIds": security_groups,
        "previousOutputs": {
            "vpcId": "",
            "efsFileSystemId": "",
            "s3BucketName": "",
            "publicSubnetIds": [],
            "privateSubnetIds": [],
            "securityGroupIds": {},
        },
    }

    # Execute record_updated_infrastructure
    result = update_mod.record_updated_infrastructure(event)

    # Verify the function reports ACTIVE status
    assert result["status"] == "ACTIVE"

    # Read the project record back from DynamoDB
    response = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
        ConsistentRead=True,
    )
    item = response["Item"]

    # Verify all infrastructure outputs round-tripped correctly
    assert item["vpcId"] == vpc_id, (
        f"vpcId mismatch: expected '{vpc_id}', got '{item['vpcId']}'"
    )
    assert item["efsFileSystemId"] == efs_id, (
        f"efsFileSystemId mismatch: expected '{efs_id}', got '{item['efsFileSystemId']}'"
    )
    assert item["s3BucketName"] == s3_bucket, (
        f"s3BucketName mismatch: expected '{s3_bucket}', got '{item['s3BucketName']}'"
    )
    assert item["cdkStackName"] == stack_name, (
        f"cdkStackName mismatch: expected '{stack_name}', got '{item['cdkStackName']}'"
    )
    assert list(item["publicSubnetIds"]) == public_subnets, (
        f"publicSubnetIds mismatch: expected {public_subnets}, got {list(item['publicSubnetIds'])}"
    )
    assert list(item["privateSubnetIds"]) == private_subnets, (
        f"privateSubnetIds mismatch: expected {private_subnets}, got {list(item['privateSubnetIds'])}"
    )

    # DynamoDB returns maps as dicts — compare security group IDs
    stored_sgs = dict(item["securityGroupIds"])
    assert stored_sgs == security_groups, (
        f"securityGroupIds mismatch: expected {security_groups}, got {stored_sgs}"
    )

    # Verify the project transitioned to ACTIVE
    assert item["status"] == "ACTIVE", (
        f"Expected status 'ACTIVE', got '{item['status']}'"
    )


# ---------------------------------------------------------------------------
# Strategies for Property 9
# ---------------------------------------------------------------------------

# Strategy that generates a pair of distinct values for a critical field
# to guarantee at least one field differs between old and new outputs.
distinct_vpc_pair = st.tuples(vpc_id_strategy, vpc_id_strategy).filter(
    lambda pair: pair[0] != pair[1]
)
distinct_efs_pair = st.tuples(efs_id_strategy, efs_id_strategy).filter(
    lambda pair: pair[0] != pair[1]
)
distinct_sg_pair = st.tuples(sg_id_strategy, sg_id_strategy).filter(
    lambda pair: pair[0] != pair[1]
)

# Which critical field(s) to change — at least one must differ
CRITICAL_FIELD_NAMES = [
    "vpcId",
    "efsFileSystemId",
    "s3BucketName",
    "sg_headNode",
    "sg_computeNode",
    "sg_efs",
    "sg_fsx",
]

# Strategy: pick a non-empty subset of critical fields to change
changed_fields_strategy = st.lists(
    st.sampled_from(CRITICAL_FIELD_NAMES),
    min_size=1,
    max_size=len(CRITICAL_FIELD_NAMES),
    unique=True,
)


# ---------------------------------------------------------------------------
# Property 9: Changed infrastructure IDs trigger warnings
# ---------------------------------------------------------------------------

@given(
    project_id=project_id_strategy,
    changed_fields=changed_fields_strategy,
    # Generate pairs of distinct values for each possible field type
    vpc_pair=distinct_vpc_pair,
    efs_pair=distinct_efs_pair,
    s3_old=s3_bucket_strategy,
    s3_new=s3_bucket_strategy,
    sg_pairs=st.tuples(
        distinct_sg_pair,  # headNode
        distinct_sg_pair,  # computeNode
        distinct_sg_pair,  # efs
        distinct_sg_pair,  # fsx
    ),
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_changed_infrastructure_ids_trigger_warnings(
    project_id, changed_fields, vpc_pair, efs_pair,
    s3_old, s3_new, sg_pairs,
):
    """For any pair of old and new infrastructure output maps where at least
    one critical field differs, record_updated_infrastructure emits a
    WARNING-level log entry identifying each changed resource.

    **Validates: Requirements 4.4**
    """
    import logging

    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
    })

    projects_table = create_projects_table()

    # Load modules inside mock context so boto3 binds to moto
    _load_module_from(_PROJECT_MGMT_DIR, "errors")
    _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")
    update_mod = _load_module_from(_PROJECT_MGMT_DIR, "project_update")

    # Seed an UPDATING project
    _seed_updating_project(projects_table, project_id)

    # Build base values — start with identical old/new, then diverge selected fields
    base_vpc = vpc_pair[0]
    base_efs = efs_pair[0]
    base_s3 = s3_old
    base_sgs = {
        "headNode": sg_pairs[0][0],
        "computeNode": sg_pairs[1][0],
        "efs": sg_pairs[2][0],
        "fsx": sg_pairs[3][0],
    }

    old_outputs = {
        "vpcId": base_vpc,
        "efsFileSystemId": base_efs,
        "s3BucketName": base_s3,
        "publicSubnetIds": [],
        "privateSubnetIds": [],
        "securityGroupIds": dict(base_sgs),
    }

    # New values start as copies of old, then change selected fields
    new_vpc = base_vpc
    new_efs = base_efs
    new_s3 = base_s3
    new_sgs = dict(base_sgs)

    # Track which fields we actually changed (only when old != new value)
    expected_changed = []

    for field in changed_fields:
        if field == "vpcId":
            new_vpc = vpc_pair[1]
            if base_vpc != new_vpc:
                expected_changed.append("vpcId")
        elif field == "efsFileSystemId":
            new_efs = efs_pair[1]
            if base_efs != new_efs:
                expected_changed.append("efsFileSystemId")
        elif field == "s3BucketName":
            new_s3 = s3_new
            if base_s3 != new_s3:
                expected_changed.append("s3BucketName")
        elif field == "sg_headNode":
            new_sgs["headNode"] = sg_pairs[0][1]
            if base_sgs["headNode"] != new_sgs["headNode"]:
                expected_changed.append("securityGroupIds.headNode")
        elif field == "sg_computeNode":
            new_sgs["computeNode"] = sg_pairs[1][1]
            if base_sgs["computeNode"] != new_sgs["computeNode"]:
                expected_changed.append("securityGroupIds.computeNode")
        elif field == "sg_efs":
            new_sgs["efs"] = sg_pairs[2][1]
            if base_sgs["efs"] != new_sgs["efs"]:
                expected_changed.append("securityGroupIds.efs")
        elif field == "sg_fsx":
            new_sgs["fsx"] = sg_pairs[3][1]
            if base_sgs["fsx"] != new_sgs["fsx"]:
                expected_changed.append("securityGroupIds.fsx")

    # Build the event payload
    event = {
        "projectId": project_id,
        "cdkStackName": f"HpcProject-{project_id}",
        "vpcId": new_vpc,
        "efsFileSystemId": new_efs,
        "s3BucketName": new_s3,
        "publicSubnetIds": [],
        "privateSubnetIds": [],
        "securityGroupIds": new_sgs,
        "previousOutputs": old_outputs,
    }

    # Capture log output at WARNING level from the project_update module
    logger = logging.getLogger("project_update")
    logger.setLevel(logging.WARNING)

    # Use a handler to capture log records
    captured_records = []
    handler = logging.Handler()
    handler.emit = lambda record: captured_records.append(record)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)

    try:
        # Execute record_updated_infrastructure
        result = update_mod.record_updated_infrastructure(event)

        # Verify the function completed successfully
        assert result["status"] == "ACTIVE"

        # Extract warning messages
        warning_messages = [
            r.getMessage() for r in captured_records if r.levelno == logging.WARNING
        ]

        # For each expected changed field, verify a warning was emitted
        for changed_field in expected_changed:
            matching = [
                msg for msg in warning_messages if changed_field in msg
            ]
            assert len(matching) >= 1, (
                f"Expected a WARNING log entry for changed field '{changed_field}' "
                f"but found none. Warning messages: {warning_messages}"
            )

        # If we expected at least one change, verify at least one warning was emitted
        if expected_changed:
            assert len(warning_messages) >= 1, (
                f"Expected at least one WARNING log entry for changed fields "
                f"{expected_changed} but found none."
            )
    finally:
        logger.removeHandler(handler)
