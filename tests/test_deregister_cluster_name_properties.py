"""Property-based test: register then deregister leaves no item in the table.

**Validates: Requirements 2.4, 2.5, 2.6**

[PBT: Property 3] For any valid cluster name string,
``deregister_cluster_name`` after ``register_cluster_name`` should result
in the item no longer existing in the table.
"""

import os
import sys

import boto3
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from moto import mock_aws

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

for _d in [_SHARED_DIR, _CLUSTER_OPS_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import cluster_names  # noqa: E402

TABLE_NAME = "ClusterNameRegistry"

# ---------------------------------------------------------------------------
# Strategy: valid cluster names (alphanumeric, hyphens, underscores, non-empty)
# ---------------------------------------------------------------------------
valid_cluster_name_strategy = st.from_regex(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,29}", fullmatch=True)

project_id_strategy = st.from_regex(r"proj-[a-z0-9]{3,8}", fullmatch=True)


def _create_registry_table():
    """Create the ClusterNameRegistry DynamoDB table in the moto mock."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class TestRegisterThenDeregisterProperty:
    """[PBT: Property 3] Register then deregister leaves no item.

    **Validates: Requirements 2.4, 2.5, 2.6**
    """

    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        cluster_name=valid_cluster_name_strategy,
        project_id=project_id_strategy,
    )
    def test_register_then_deregister_removes_item(self, cluster_name, project_id):
        """For any valid cluster name, registering then deregistering should
        result in the item no longer existing in the table.

        **Validates: Requirements 2.4, 2.5, 2.6**
        """
        with mock_aws():
            _create_registry_table()
            # Point the module's dynamodb resource at the moto mock
            mock_dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
            original_dynamodb = cluster_names.dynamodb
            cluster_names.dynamodb = mock_dynamodb

            try:
                # Register the cluster name
                cluster_names.register_cluster_name(TABLE_NAME, cluster_name, project_id)

                # Verify it exists
                lookup = cluster_names.lookup_cluster_name(TABLE_NAME, cluster_name)
                assert lookup is not None, (
                    f"register_cluster_name did not create an entry for '{cluster_name}'"
                )

                # Deregister the cluster name
                result = cluster_names.deregister_cluster_name(TABLE_NAME, cluster_name)
                assert result is True, (
                    f"deregister_cluster_name returned False for '{cluster_name}' "
                    "which was just registered"
                )

                # Verify it no longer exists
                lookup_after = cluster_names.lookup_cluster_name(TABLE_NAME, cluster_name)
                assert lookup_after is None, (
                    f"Item for '{cluster_name}' still exists after deregister_cluster_name. "
                    f"Found: {lookup_after}"
                )
            finally:
                cluster_names.dynamodb = original_dynamodb
