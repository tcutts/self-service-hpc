"""Property-based tests for SFN transition optimization — project lifecycle consolidated handlers.

# Feature: sfn-transition-optimization, Property 5: Project lifecycle consolidated pre-loop output equivalence

For any valid project event payload and for any project lifecycle workflow (deploy, update,
destroy), calling the respective consolidated_pre_loop(event) produces the same output dict
as calling the two constituent pre-loop steps sequentially (validate then start).

**Validates: Requirements 4.1, 5.1, 6.1, 14.2, 14.3, 14.4**
"""

import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_PROJECT_MGMT_DIR = os.path.join(_LAMBDA_DIR, "project_management")

sys.path.insert(0, _PROJECT_MGMT_DIR)

# Clear cached modules to ensure correct imports
for _mod in [
    "errors", "lifecycle",
    "project_deploy", "project_update", "project_destroy",
]:
    if _mod in sys.modules:
        del sys.modules[_mod]

import lifecycle  # noqa: E402
import project_deploy  # noqa: E402
import project_update  # noqa: E402
import project_destroy  # noqa: E402

from project_deploy import (  # noqa: E402
    consolidated_pre_loop as deploy_consolidated_pre_loop,
    consolidated_post_loop as deploy_consolidated_post_loop,
    validate_project_state as deploy_validate,
    start_cdk_deploy,
    extract_stack_outputs as deploy_extract_stack_outputs,
    record_infrastructure,
)
from project_update import (  # noqa: E402
    consolidated_pre_loop as update_consolidated_pre_loop,
    consolidated_post_loop as update_consolidated_post_loop,
    validate_update_state as update_validate,
    start_cdk_update,
    extract_stack_outputs as update_extract_stack_outputs,
    record_updated_infrastructure,
)
from project_destroy import (  # noqa: E402
    consolidated_pre_loop as destroy_consolidated_pre_loop,
    consolidated_post_loop as destroy_consolidated_post_loop,
    validate_and_check_clusters as destroy_validate,
    start_cdk_destroy,
    clear_infrastructure,
    archive_project,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

project_id_strategy = st.from_regex(
    r"proj-[a-z0-9]{3,8}", fullmatch=True,
)

build_id_strategy = st.from_regex(
    r"build-[a-f0-9]{8,12}", fullmatch=True,
)


@st.composite
def deploy_event(draw):
    """Generate a valid project deploy event payload."""
    return {
        "projectId": draw(project_id_strategy),
    }


@st.composite
def update_event(draw):
    """Generate a valid project update event payload."""
    return {
        "projectId": draw(project_id_strategy),
    }


@st.composite
def destroy_event(draw):
    """Generate a valid project destroy event payload."""
    return {
        "projectId": draw(project_id_strategy),
    }


# ---------------------------------------------------------------------------
# Mock builders — Deploy
# ---------------------------------------------------------------------------

def _build_deploy_mock_dynamodb(project_id):
    """Build a mock DynamoDB resource for deploy pre-loop steps.

    Mocks the Projects table for validate_project_state (get_item)
    and _update_project_progress (update_item).
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name

        if table_name == "Projects":
            mock_table.get_item.return_value = {
                "Item": {
                    "PK": f"PROJECT#{project_id}",
                    "SK": "METADATA",
                    "status": "DEPLOYING",
                }
            }
            mock_table.update_item.return_value = {}
        else:
            mock_table.get_item.return_value = {"Item": None}
            mock_table.update_item.return_value = {}

        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_deploy_mock_codebuild(build_id):
    """Build a mock CodeBuild client for start_cdk_deploy."""
    mock_cb = MagicMock()
    mock_cb.start_build.return_value = {
        "build": {"id": build_id},
    }
    return mock_cb


# ---------------------------------------------------------------------------
# Mock builders — Update
# ---------------------------------------------------------------------------

def _build_update_mock_dynamodb(project_id):
    """Build a mock DynamoDB resource for update pre-loop steps.

    Mocks the Projects table for validate_update_state (get_item with
    infrastructure snapshot fields) and _update_project_progress.
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name

        if table_name == "Projects":
            mock_table.get_item.return_value = {
                "Item": {
                    "PK": f"PROJECT#{project_id}",
                    "SK": "METADATA",
                    "status": "UPDATING",
                    "vpcId": "vpc-abc123",
                    "efsFileSystemId": "fs-abc123",
                    "s3BucketName": "my-bucket",
                    "publicSubnetIds": ["subnet-pub1"],
                    "privateSubnetIds": ["subnet-priv1"],
                    "securityGroupIds": {
                        "headNode": "sg-head",
                        "computeNode": "sg-compute",
                        "efs": "sg-efs",
                        "fsx": "sg-fsx",
                    },
                }
            }
            mock_table.update_item.return_value = {}
        else:
            mock_table.get_item.return_value = {"Item": None}
            mock_table.update_item.return_value = {}

        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_update_mock_codebuild(build_id):
    """Build a mock CodeBuild client for start_cdk_update."""
    mock_cb = MagicMock()
    mock_cb.start_build.return_value = {
        "build": {"id": build_id},
    }
    return mock_cb


# ---------------------------------------------------------------------------
# Mock builders — Destroy
# ---------------------------------------------------------------------------

def _build_destroy_mock_dynamodb(project_id):
    """Build a mock DynamoDB resource for destroy pre-loop steps.

    Mocks the Projects table (validate_and_check_clusters) and the
    Clusters table (query for active clusters — returns none).
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name

        if table_name == "Projects":
            mock_table.get_item.return_value = {
                "Item": {
                    "PK": f"PROJECT#{project_id}",
                    "SK": "METADATA",
                    "status": "DESTROYING",
                }
            }
            mock_table.update_item.return_value = {}
        elif table_name == "Clusters":
            mock_table.query.return_value = {"Items": []}
        else:
            mock_table.get_item.return_value = {"Item": None}
            mock_table.update_item.return_value = {}

        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_destroy_mock_codebuild(build_id):
    """Build a mock CodeBuild client for start_cdk_destroy."""
    mock_cb = MagicMock()
    mock_cb.start_build.return_value = {
        "build": {"id": build_id},
    }
    return mock_cb


# ===================================================================
# [PBT: Property 5] Project lifecycle consolidated pre-loop output
#                    equivalence — Deploy workflow
# ===================================================================

class TestProjectDeployPreLoopEquivalence:
    """[PBT: Property 5 — Deploy] For any valid project deploy event,
    consolidated_pre_loop(event) produces the same output dict as calling
    validate_project_state → start_cdk_deploy sequentially.

    # Feature: sfn-transition-optimization, Property 5: Project lifecycle consolidated pre-loop output equivalence

    **Validates: Requirements 4.1, 14.2**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=deploy_event(),
        build_id=build_id_strategy,
    )
    def test_consolidated_matches_sequential(self, event, build_id):
        """consolidated_pre_loop(event) == sequential execution of
        validate_project_state → start_cdk_deploy.

        **Validates: Requirements 4.1, 14.2**
        """
        project_id = event["projectId"]
        mock_dynamodb = _build_deploy_mock_dynamodb(project_id)
        mock_codebuild = _build_deploy_mock_codebuild(build_id)

        patches = [
            patch.object(project_deploy, "dynamodb", mock_dynamodb),
            patch.object(project_deploy, "codebuild_client", mock_codebuild),
            patch.object(
                project_deploy, "CODEBUILD_PROJECT_NAME", "test-project",
            ),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = deploy_validate(event)
            r2 = start_cdk_deploy({**event, **r1})
            sequential_result = {}
            for r in [r1, r2]:
                sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = deploy_consolidated_pre_loop(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )


# ===================================================================
# [PBT: Property 5] Project lifecycle consolidated pre-loop output
#                    equivalence — Update workflow
# ===================================================================

class TestProjectUpdatePreLoopEquivalence:
    """[PBT: Property 5 — Update] For any valid project update event,
    consolidated_pre_loop(event) produces the same output dict as calling
    validate_update_state → start_cdk_update sequentially.

    # Feature: sfn-transition-optimization, Property 5: Project lifecycle consolidated pre-loop output equivalence

    **Validates: Requirements 5.1, 14.3**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=update_event(),
        build_id=build_id_strategy,
    )
    def test_consolidated_matches_sequential(self, event, build_id):
        """consolidated_pre_loop(event) == sequential execution of
        validate_update_state → start_cdk_update.

        **Validates: Requirements 5.1, 14.3**
        """
        project_id = event["projectId"]
        mock_dynamodb = _build_update_mock_dynamodb(project_id)
        mock_codebuild = _build_update_mock_codebuild(build_id)

        patches = [
            patch.object(project_update, "dynamodb", mock_dynamodb),
            patch.object(project_update, "codebuild_client", mock_codebuild),
            patch.object(
                project_update, "CODEBUILD_PROJECT_NAME", "test-project",
            ),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = update_validate(event)
            r2 = start_cdk_update({**event, **r1})
            sequential_result = {}
            for r in [r1, r2]:
                sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = update_consolidated_pre_loop(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )


# ===================================================================
# [PBT: Property 5] Project lifecycle consolidated pre-loop output
#                    equivalence — Destroy workflow
# ===================================================================

class TestProjectDestroyPreLoopEquivalence:
    """[PBT: Property 5 — Destroy] For any valid project destroy event,
    consolidated_pre_loop(event) produces the same output dict as calling
    validate_and_check_clusters → start_cdk_destroy sequentially.

    # Feature: sfn-transition-optimization, Property 5: Project lifecycle consolidated pre-loop output equivalence

    **Validates: Requirements 6.1, 14.4**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=destroy_event(),
        build_id=build_id_strategy,
    )
    def test_consolidated_matches_sequential(self, event, build_id):
        """consolidated_pre_loop(event) == sequential execution of
        validate_and_check_clusters → start_cdk_destroy.

        **Validates: Requirements 6.1, 14.4**
        """
        project_id = event["projectId"]
        mock_dynamodb = _build_destroy_mock_dynamodb(project_id)
        mock_codebuild = _build_destroy_mock_codebuild(build_id)

        patches = [
            patch.object(project_destroy, "dynamodb", mock_dynamodb),
            patch.object(
                project_destroy, "codebuild_client", mock_codebuild,
            ),
            patch.object(
                project_destroy, "CODEBUILD_PROJECT_NAME", "test-project",
            ),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = destroy_validate(event)
            r2 = start_cdk_destroy({**event, **r1})
            sequential_result = {}
            for r in [r1, r2]:
                sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = destroy_consolidated_pre_loop(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )


# ===================================================================
# Property 6 — Post-loop strategies and mock builders
# ===================================================================

# Strategies for CloudFormation stack output fields used by deploy/update
# post-loop handlers.

vpc_id_strategy = st.from_regex(r"vpc-[a-f0-9]{8,12}", fullmatch=True)
efs_id_strategy = st.from_regex(r"fs-[a-f0-9]{8,12}", fullmatch=True)
s3_bucket_strategy = st.from_regex(
    r"hpc-bucket-[a-z0-9]{4,8}", fullmatch=True,
)
sg_id_strategy = st.from_regex(r"sg-[a-f0-9]{8,12}", fullmatch=True)
subnet_id_strategy = st.from_regex(
    r"subnet-[a-f0-9]{8,12}", fullmatch=True,
)


@st.composite
def deploy_post_loop_event(draw):
    """Generate a valid event for the deploy post-loop steps.

    The post-loop event arrives after the CodeBuild polling loop
    completes, so it contains projectId and buildId (plus
    deployComplete=True).
    """
    return {
        "projectId": draw(project_id_strategy),
        "buildId": draw(build_id_strategy),
        "deployComplete": True,
    }


@st.composite
def update_post_loop_event(draw):
    """Generate a valid event for the update post-loop steps.

    The post-loop event arrives after the CodeBuild polling loop
    completes, so it contains projectId, buildId, updateComplete,
    and the previousOutputs snapshot from validate_update_state.
    """
    return {
        "projectId": draw(project_id_strategy),
        "buildId": draw(build_id_strategy),
        "updateComplete": True,
        "previousOutputs": {
            "vpcId": draw(vpc_id_strategy),
            "efsFileSystemId": draw(efs_id_strategy),
            "s3BucketName": draw(s3_bucket_strategy),
            "publicSubnetIds": draw(
                st.lists(subnet_id_strategy, min_size=1, max_size=2),
            ),
            "privateSubnetIds": draw(
                st.lists(subnet_id_strategy, min_size=1, max_size=2),
            ),
            "securityGroupIds": {
                "headNode": draw(sg_id_strategy),
                "computeNode": draw(sg_id_strategy),
                "efs": draw(sg_id_strategy),
                "fsx": draw(sg_id_strategy),
            },
        },
    }


@st.composite
def destroy_post_loop_event(draw):
    """Generate a valid event for the destroy post-loop steps.

    The post-loop event arrives after the CodeBuild polling loop
    completes, so it contains projectId and destroyComplete=True.
    """
    return {
        "projectId": draw(project_id_strategy),
        "buildId": draw(build_id_strategy),
        "destroyComplete": True,
    }


@st.composite
def cfn_stack_outputs(draw):
    """Generate a set of CloudFormation stack outputs.

    Returns a dict of OutputKey → OutputValue pairs matching the
    fields extracted by extract_stack_outputs.
    """
    public_subnets = draw(
        st.lists(subnet_id_strategy, min_size=1, max_size=3),
    )
    private_subnets = draw(
        st.lists(subnet_id_strategy, min_size=1, max_size=3),
    )
    return {
        "VpcId": draw(vpc_id_strategy),
        "EfsFileSystemId": draw(efs_id_strategy),
        "S3BucketName": draw(s3_bucket_strategy),
        "HeadNodeSecurityGroupId": draw(sg_id_strategy),
        "ComputeNodeSecurityGroupId": draw(sg_id_strategy),
        "EfsSecurityGroupId": draw(sg_id_strategy),
        "FsxSecurityGroupId": draw(sg_id_strategy),
        "PublicSubnetIds": ",".join(public_subnets),
        "PrivateSubnetIds": ",".join(private_subnets),
    }


# ---------------------------------------------------------------------------
# Mock builders — Deploy post-loop
# ---------------------------------------------------------------------------

def _build_deploy_post_mock_dynamodb():
    """Build a mock DynamoDB resource for deploy post-loop steps.

    Mocks the Projects table for _update_project_progress (update_item)
    and record_infrastructure (update_item + lifecycle.transition_project).
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name
        mock_table.update_item.return_value = {}
        mock_table.get_item.return_value = {"Item": None}
        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_mock_cfn(project_id, output_map):
    """Build a mock CloudFormation client for extract_stack_outputs.

    Returns a describe_stacks response with the given output_map
    formatted as CloudFormation Outputs.
    """
    mock_cfn = MagicMock()
    outputs = [
        {"OutputKey": k, "OutputValue": v}
        for k, v in output_map.items()
    ]
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackName": f"HpcProject-{project_id}",
                "Outputs": outputs,
            }
        ]
    }
    return mock_cfn


# ---------------------------------------------------------------------------
# Mock builders — Update post-loop
# ---------------------------------------------------------------------------

def _build_update_post_mock_dynamodb():
    """Build a mock DynamoDB resource for update post-loop steps.

    Mocks the Projects table for _update_project_progress (update_item)
    and record_updated_infrastructure (update_item +
    lifecycle.transition_project).
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name
        mock_table.update_item.return_value = {}
        mock_table.get_item.return_value = {"Item": None}
        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


# ---------------------------------------------------------------------------
# Mock builders — Destroy post-loop
# ---------------------------------------------------------------------------

def _build_destroy_post_mock_dynamodb():
    """Build a mock DynamoDB resource for destroy post-loop steps.

    Mocks the Projects table for _update_project_progress (update_item),
    clear_infrastructure (update_item), and archive_project
    (lifecycle.transition_project).
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name
        mock_table.update_item.return_value = {}
        mock_table.get_item.return_value = {"Item": None}
        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


# ===================================================================
# [PBT: Property 6] Project lifecycle consolidated post-loop output
#                    equivalence — Deploy workflow
# ===================================================================

class TestProjectDeployPostLoopEquivalence:
    """[PBT: Property 6 — Deploy] For any valid project deploy event,
    consolidated_post_loop(event) produces the same output dict as calling
    extract_stack_outputs → record_infrastructure sequentially.

    # Feature: sfn-transition-optimization, Property 6: Project lifecycle consolidated post-loop output equivalence

    **Validates: Requirements 4.2, 14.2**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=deploy_post_loop_event(),
        stack_outputs=cfn_stack_outputs(),
    )
    def test_consolidated_matches_sequential(self, event, stack_outputs):
        """consolidated_post_loop(event) == sequential execution of
        extract_stack_outputs → record_infrastructure.

        **Validates: Requirements 4.2, 14.2**
        """
        project_id = event["projectId"]
        mock_dynamodb = _build_deploy_post_mock_dynamodb()
        mock_cfn = _build_mock_cfn(project_id, stack_outputs)
        mock_lifecycle = MagicMock()

        patches = [
            patch.object(project_deploy, "dynamodb", mock_dynamodb),
            patch.object(project_deploy, "cfn_client", mock_cfn),
            patch.object(project_deploy, "lifecycle", mock_lifecycle),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = deploy_extract_stack_outputs(event)
            r2 = record_infrastructure({**event, **r1})
            sequential_result = {}
            for r in [r1, r2]:
                sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = deploy_consolidated_post_loop(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )


# ===================================================================
# [PBT: Property 6] Project lifecycle consolidated post-loop output
#                    equivalence — Update workflow
# ===================================================================

class TestProjectUpdatePostLoopEquivalence:
    """[PBT: Property 6 — Update] For any valid project update event,
    consolidated_post_loop(event) produces the same output dict as calling
    extract_stack_outputs → record_updated_infrastructure sequentially.

    # Feature: sfn-transition-optimization, Property 6: Project lifecycle consolidated post-loop output equivalence

    **Validates: Requirements 5.2, 14.3**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=update_post_loop_event(),
        stack_outputs=cfn_stack_outputs(),
    )
    def test_consolidated_matches_sequential(self, event, stack_outputs):
        """consolidated_post_loop(event) == sequential execution of
        extract_stack_outputs → record_updated_infrastructure.

        **Validates: Requirements 5.2, 14.3**
        """
        project_id = event["projectId"]
        mock_dynamodb = _build_update_post_mock_dynamodb()
        mock_cfn = _build_mock_cfn(project_id, stack_outputs)
        mock_lifecycle = MagicMock()

        patches = [
            patch.object(project_update, "dynamodb", mock_dynamodb),
            patch.object(project_update, "cfn_client", mock_cfn),
            patch.object(project_update, "lifecycle", mock_lifecycle),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = update_extract_stack_outputs(event)
            r2 = record_updated_infrastructure({**event, **r1})
            sequential_result = {}
            for r in [r1, r2]:
                sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = update_consolidated_post_loop(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )


# ===================================================================
# [PBT: Property 6] Project lifecycle consolidated post-loop output
#                    equivalence — Destroy workflow
# ===================================================================

class TestProjectDestroyPostLoopEquivalence:
    """[PBT: Property 6 — Destroy] For any valid project destroy event,
    consolidated_post_loop(event) produces the same output dict as calling
    clear_infrastructure → archive_project sequentially.

    # Feature: sfn-transition-optimization, Property 6: Project lifecycle consolidated post-loop output equivalence

    **Validates: Requirements 6.2, 14.4**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=destroy_post_loop_event(),
    )
    def test_consolidated_matches_sequential(self, event):
        """consolidated_post_loop(event) == sequential execution of
        clear_infrastructure → archive_project.

        **Validates: Requirements 6.2, 14.4**
        """
        mock_dynamodb = _build_destroy_post_mock_dynamodb()
        mock_lifecycle = MagicMock()

        patches = [
            patch.object(project_destroy, "dynamodb", mock_dynamodb),
            patch.object(project_destroy, "lifecycle", mock_lifecycle),
        ]

        for p in patches:
            p.start()

        try:
            # --- Sequential execution ---
            r1 = clear_infrastructure(event)
            r2 = archive_project({**event, **r1})
            sequential_result = {}
            for r in [r1, r2]:
                sequential_result = {**sequential_result, **r}

            # --- Consolidated execution ---
            consolidated_result = destroy_consolidated_post_loop(event)
        finally:
            for p in patches:
                p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )
