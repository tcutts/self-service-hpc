"""Property-based tests for SFN transition optimization consolidated handlers.

# Feature: sfn-transition-optimization, Property 1: Cluster creation pre-parallel output equivalence

For any valid cluster creation event payload, calling consolidated_pre_parallel(event)
produces the same output dict as calling validate_and_register_name, check_budget_breach,
resolve_template, and create_iam_resources sequentially, where each step receives the
output of the previous step.

**Validates: Requirements 1.1, 1.3, 14.1**

# Feature: sfn-transition-optimization, Property 2: Cluster creation post-parallel output equivalence

For any valid post-parallel cluster creation event payload (containing fields from all
three parallel branches), calling consolidated_post_parallel(event) produces the same
output dict as calling resolve_login_node_details, create_pcs_queue, tag_resources,
and record_cluster sequentially, where each step receives the output of the previous step.

**Validates: Requirements 2.1, 2.3, 14.1**
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from conftest import load_lambda_module, _ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "posix_provisioning")
load_lambda_module("cluster_operations", "tagging")
cluster_creation = load_lambda_module("cluster_operations", "cluster_creation")

consolidated_pre_parallel = cluster_creation.consolidated_pre_parallel
consolidated_post_parallel = cluster_creation.consolidated_post_parallel
validate_and_register_name = cluster_creation.validate_and_register_name
check_budget_breach = cluster_creation.check_budget_breach
resolve_template = cluster_creation.resolve_template
create_iam_resources = cluster_creation.create_iam_resources
resolve_login_node_details = cluster_creation.resolve_login_node_details
create_pcs_queue = cluster_creation.create_pcs_queue
tag_resources = cluster_creation.tag_resources
record_cluster = cluster_creation.record_cluster


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

project_id_strategy = st.from_regex(
    r"proj-[a-z0-9]{3,8}", fullmatch=True,
)
cluster_name_strategy = st.from_regex(
    r"[a-zA-Z][a-zA-Z0-9_-]{2,12}", fullmatch=True,
)
template_id_strategy = st.from_regex(
    r"tmpl-[a-z0-9]{4,8}", fullmatch=True,
)
ami_id_strategy = st.from_regex(
    r"ami-[a-f0-9]{8,17}", fullmatch=True,
)
instance_type_strategy = st.sampled_from([
    "c7g.medium", "c7g.large", "c7g.xlarge",
    "m7g.medium", "m7g.large", "t3.micro",
])
purchase_option_strategy = st.sampled_from(["ONDEMAND", "SPOT"])
scheduler_version_strategy = st.sampled_from([
    "23.11.10", "24.05.4", "23.02.7",
])


@st.composite
def cluster_creation_event(draw):
    """Generate a valid cluster creation event payload.

    Produces the fields needed by the four pre-parallel steps:
    validate_and_register_name, check_budget_breach, resolve_template,
    and create_iam_resources.
    """
    project_id = draw(project_id_strategy)
    cluster_name = draw(cluster_name_strategy)
    template_id = draw(template_id_strategy)

    return {
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": template_id,
    }


def _build_mock_dynamodb(event, ami_id, instance_types,
                         login_instance_type, purchase_option,
                         scheduler_version, min_nodes, max_nodes):
    """Build a mock DynamoDB resource that returns deterministic data.

    Mocks three tables:
    - ClusterNameRegistry (for register_cluster_name)
    - Projects (for check_budget_breach)
    - ClusterTemplates (for resolve_template)
    - Clusters (for _update_step_progress)
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name

        if table_name == "ClusterNameRegistry":
            # register_cluster_name uses put_item with ConditionExpression
            mock_table.put_item.return_value = {}
        elif table_name == "Projects":
            # check_budget_breach uses get_item
            mock_table.get_item.return_value = {
                "Item": {
                    "PK": f"PROJECT#{event['projectId']}",
                    "SK": "METADATA",
                    "budgetBreached": False,
                }
            }
        elif table_name == "ClusterTemplates":
            # resolve_template uses get_item
            mock_table.get_item.return_value = {
                "Item": {
                    "PK": f"TEMPLATE#{event['templateId']}",
                    "SK": "METADATA",
                    "loginInstanceType": login_instance_type,
                    "instanceTypes": instance_types,
                    "purchaseOption": purchase_option,
                    "amiId": ami_id,
                    "loginAmiId": "",
                    "softwareStack": {
                        "schedulerVersion": scheduler_version,
                    },
                    "minNodes": min_nodes,
                    "maxNodes": max_nodes,
                }
            }
        elif table_name == "Clusters":
            # _update_step_progress uses update_item
            mock_table.update_item.return_value = {}
        else:
            mock_table.get_item.return_value = {"Item": None}
            mock_table.update_item.return_value = {}

        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_mock_iam(project_id, cluster_name):
    """Build a mock IAM client that returns deterministic responses."""
    mock_iam = MagicMock()

    login_role = f"AWSPCS-{project_id}-{cluster_name}-login"
    compute_role = f"AWSPCS-{project_id}-{cluster_name}-compute"

    login_arn = f"arn:aws:iam::123456789012:instance-profile/{login_role}"
    compute_arn = f"arn:aws:iam::123456789012:instance-profile/{compute_role}"

    def get_instance_profile(InstanceProfileName):
        if InstanceProfileName == login_role:
            return {"InstanceProfile": {"Arn": login_arn}}
        elif InstanceProfileName == compute_role:
            return {"InstanceProfile": {"Arn": compute_arn}}
        return {"InstanceProfile": {"Arn": f"arn:aws:iam::123456789012:instance-profile/{InstanceProfileName}"}}

    mock_iam.create_role.return_value = {}
    mock_iam.put_role_policy.return_value = {}
    mock_iam.attach_role_policy.return_value = {}
    mock_iam.create_instance_profile.return_value = {}
    mock_iam.add_role_to_instance_profile.return_value = {}
    mock_iam.get_instance_profile.side_effect = get_instance_profile

    return mock_iam


# ===================================================================
# [PBT: Property 1] Cluster creation pre-parallel output equivalence
# ===================================================================

class TestClusterCreationPreParallelEquivalence:
    """[PBT: Property 1] For any valid cluster creation event payload,
    consolidated_pre_parallel(event) produces the same output dict as
    calling the four steps sequentially.

    # Feature: sfn-transition-optimization, Property 1: Cluster creation pre-parallel output equivalence

    **Validates: Requirements 1.1, 1.3, 14.1**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=cluster_creation_event(),
        ami_id=ami_id_strategy,
        instance_types=st.lists(
            instance_type_strategy, min_size=1, max_size=3,
        ),
        login_instance_type=instance_type_strategy,
        purchase_option=purchase_option_strategy,
        scheduler_version=scheduler_version_strategy,
        min_nodes=st.integers(min_value=0, max_value=10),
        max_nodes=st.integers(min_value=1, max_value=100),
    )
    def test_consolidated_matches_sequential(
        self, event, ami_id, instance_types,
        login_instance_type, purchase_option,
        scheduler_version, min_nodes, max_nodes,
    ):
        """consolidated_pre_parallel(event) == sequential execution of
        validate_and_register_name → check_budget_breach →
        resolve_template → create_iam_resources.

        **Validates: Requirements 1.1, 1.3, 14.1**
        """
        mock_dynamodb = _build_mock_dynamodb(
            event, ami_id, instance_types,
            login_instance_type, purchase_option,
            scheduler_version, min_nodes, max_nodes,
        )
        mock_iam = _build_mock_iam(
            event["projectId"], event["clusterName"],
        )

        patches = [
            patch.object(cluster_creation, "dynamodb", mock_dynamodb),
            patch.object(cluster_creation, "iam_client", mock_iam),
            patch("cluster_names.boto3"),
        ]

        for p in patches:
            p.start()

        # Also mock register_cluster_name at the module level since
        # it's imported directly in cluster_creation
        with patch.object(
            cluster_creation, "register_cluster_name"
        ) as mock_register:
            mock_register.return_value = None

            try:
                # --- Sequential execution ---
                r1 = validate_and_register_name(event)
                r2 = check_budget_breach({**event, **r1})
                r3 = resolve_template({**event, **r1, **r2})
                r4 = create_iam_resources(
                    {**event, **r1, **r2, **r3}
                )
                sequential_result = {}
                for r in [r1, r2, r3, r4]:
                    sequential_result = {**sequential_result, **r}

                # --- Consolidated execution ---
                consolidated_result = consolidated_pre_parallel(event)
            finally:
                for p in patches:
                    p.stop()

        # The consolidated result must match the sequential result
        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )


# ---------------------------------------------------------------------------
# Strategies for post-parallel event payloads
# ---------------------------------------------------------------------------

ip_address_strategy = st.from_regex(
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}", fullmatch=True,
)
instance_id_strategy = st.from_regex(
    r"i-[a-f0-9]{8,17}", fullmatch=True,
)
pcs_cluster_id_strategy = st.from_regex(
    r"pcs-[a-z0-9]{6,12}", fullmatch=True,
)
node_group_id_strategy = st.from_regex(
    r"ng-[a-z0-9]{6,12}", fullmatch=True,
)
queue_id_strategy = st.from_regex(
    r"q-[a-z0-9]{6,12}", fullmatch=True,
)
fsx_id_strategy = st.from_regex(
    r"fs-[a-f0-9]{8,17}", fullmatch=True,
)
pcs_cluster_arn_strategy = st.from_regex(
    r"arn:aws:pcs:us-east-1:123456789012:cluster/pcs-[a-z0-9]{6}",
    fullmatch=True,
)
storage_mode_strategy = st.sampled_from(["lustre", "mountpoint"])


@st.composite
def post_parallel_event(draw):
    """Generate a valid post-parallel event payload.

    Contains fields accumulated from the pre-parallel steps and all
    three parallel branches (storage, PCS cluster, launch templates).
    """
    project_id = draw(project_id_strategy)
    cluster_name = draw(cluster_name_strategy)
    pcs_cluster_id = draw(pcs_cluster_id_strategy)
    login_node_group_id = draw(node_group_id_strategy)
    compute_node_group_id = draw(node_group_id_strategy)
    pcs_cluster_arn = draw(pcs_cluster_arn_strategy)
    storage_mode = draw(storage_mode_strategy)
    ssh_port = draw(st.sampled_from([22, 2222]))
    dcv_port = draw(st.sampled_from([8443, 9443]))

    event = {
        # Pre-parallel fields
        "projectId": project_id,
        "clusterName": cluster_name,
        "templateId": draw(template_id_strategy),
        "storageMode": storage_mode,
        "minNodes": draw(st.integers(min_value=0, max_value=10)),
        "maxNodes": draw(st.integers(min_value=1, max_value=100)),
        "sshPort": ssh_port,
        "dcvPort": dcv_port,
        "createdBy": draw(st.from_regex(
            r"user-[a-z0-9]{4,8}", fullmatch=True,
        )),
        # Storage branch output
        "fsxFilesystemId": draw(fsx_id_strategy),
        # PCS cluster branch output
        "pcsClusterId": pcs_cluster_id,
        "pcsClusterArn": pcs_cluster_arn,
        "loginNodeGroupId": login_node_group_id,
        "computeNodeGroupId": compute_node_group_id,
    }

    if storage_mode == "lustre":
        event["lustreCapacityGiB"] = draw(
            st.sampled_from([1200, 2400, 4800])
        )

    return event


# ---------------------------------------------------------------------------
# Mock builders for post-parallel tests
# ---------------------------------------------------------------------------

def _build_post_parallel_mock_dynamodb():
    """Build a mock DynamoDB resource for post-parallel steps.

    Mocks the Clusters table (for _update_step_progress and
    record_cluster) and the PlatformUsers table (for _lookup_user_email).
    """
    mock_dynamodb = MagicMock()

    def table_factory(table_name):
        mock_table = MagicMock()
        mock_table.table_name = table_name
        mock_table.update_item.return_value = {}
        mock_table.put_item.return_value = {}
        mock_table.get_item.return_value = {"Item": None}
        return mock_table

    mock_dynamodb.Table.side_effect = table_factory
    return mock_dynamodb


def _build_mock_ec2_for_login(instance_id, public_ip):
    """Build a mock EC2 client that returns a login node instance."""
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": instance_id,
                        "PublicIpAddress": public_ip,
                    }
                ]
            }
        ]
    }
    return mock_ec2


def _build_mock_pcs(queue_id):
    """Build a mock PCS client that returns a deterministic queue ID."""
    mock_pcs = MagicMock()
    mock_pcs.create_queue.return_value = {
        "queue": {"id": queue_id},
    }
    return mock_pcs


# ===================================================================
# [PBT: Property 2] Cluster creation post-parallel output equivalence
# ===================================================================

class TestClusterCreationPostParallelEquivalence:
    """[PBT: Property 2] For any valid post-parallel cluster creation
    event payload, consolidated_post_parallel(event) produces the same
    output dict as calling the four tail steps sequentially.

    # Feature: sfn-transition-optimization, Property 2: Cluster creation post-parallel output equivalence

    **Validates: Requirements 2.1, 2.3, 14.1**
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        event=post_parallel_event(),
        login_instance_id=instance_id_strategy,
        login_ip=ip_address_strategy,
        queue_id=queue_id_strategy,
    )
    def test_consolidated_matches_sequential(
        self, event, login_instance_id, login_ip, queue_id,
    ):
        """consolidated_post_parallel(event) == sequential execution of
        resolve_login_node_details → create_pcs_queue →
        tag_resources → record_cluster.

        **Validates: Requirements 2.1, 2.3, 14.1**
        """
        mock_dynamodb = _build_post_parallel_mock_dynamodb()
        mock_ec2 = _build_mock_ec2_for_login(
            login_instance_id, login_ip,
        )
        mock_pcs = _build_mock_pcs(queue_id)
        mock_tagging = MagicMock()
        mock_tagging.tag_resources.return_value = {}
        mock_sns = MagicMock()
        mock_sns.subscribe.return_value = {}
        mock_sns.publish.return_value = {}

        # Fix datetime.now so both runs produce the same timestamp
        fixed_now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        patches = [
            patch.object(cluster_creation, "dynamodb", mock_dynamodb),
            patch.object(cluster_creation, "ec2_client", mock_ec2),
            patch.object(cluster_creation, "pcs_client", mock_pcs),
            patch.object(
                cluster_creation, "tagging_client", mock_tagging,
            ),
            patch.object(cluster_creation, "sns_client", mock_sns),
            patch(
                "cluster_creation.datetime",
                wraps=datetime,
            ),
        ]

        for p in patches:
            p.start()

        # Override datetime.now to return fixed value
        with patch(
            "cluster_creation.datetime",
        ) as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            try:
                # --- Sequential execution ---
                r1 = resolve_login_node_details(event)
                r2 = create_pcs_queue({**event, **r1})
                r3 = tag_resources({**event, **r1, **r2})
                r4 = record_cluster({**event, **r1, **r2, **r3})
                sequential_result = {}
                for r in [r1, r2, r3, r4]:
                    sequential_result = {**sequential_result, **r}

                # --- Consolidated execution ---
                consolidated_result = consolidated_post_parallel(event)
            finally:
                for p in patches:
                    p.stop()

        assert consolidated_result == sequential_result, (
            f"Consolidated output differs from sequential.\n"
            f"Consolidated: {consolidated_result}\n"
            f"Sequential:   {sequential_result}\n"
            f"Event:        {event}"
        )
