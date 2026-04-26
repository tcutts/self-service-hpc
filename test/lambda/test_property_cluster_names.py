# Feature: self-service-hpc, Properties 9, 10, 11, 17: Cluster name validation and registry
"""Property-based tests for cluster name validation, cross-project uniqueness,
same-project reuse, and registry association preservation.

**Validates: Requirements 6.7, 6.8, 18.1, 18.2, 18.3, 18.4**
"""

import os
import re

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
import pytest

from conftest import (
    AWS_REGION,
    CLUSTER_NAME_REGISTRY_TABLE_NAME,
    create_cluster_name_registry_table,
    reload_cluster_ops_modules,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

valid_cluster_name = (
    st.from_regex(r"[a-zA-Z0-9_-]+", fullmatch=True)
    .filter(lambda s: len(s) > 0 and len(s) <= 30)
)

project_id_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

_VALID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


# ---------------------------------------------------------------------------
# Property 9: Cluster name validation
# ---------------------------------------------------------------------------


@given(name=st.text())
@settings(max_examples=10, deadline=None)
def test_cluster_name_validation(name):
    """For any string, validate_cluster_name accepts iff the string is
    non-empty and matches ^[a-zA-Z0-9_-]+$.

    **Validates: Requirements 18.1**
    """
    cluster_names_mod, _ = reload_cluster_ops_modules()

    expected = bool(name) and _VALID_PATTERN.match(name) is not None
    assert cluster_names_mod.validate_cluster_name(name) == expected


# ---------------------------------------------------------------------------
# Property 10: Cluster name cross-project uniqueness
# ---------------------------------------------------------------------------


@given(
    data=st.tuples(valid_cluster_name, project_id_strategy, project_id_strategy).filter(
        lambda t: t[1] != t[2]
    )
)
@settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_cluster_name_cross_project_uniqueness(data):
    """For any cluster name registered to project A, a request from project B
    (B != A) SHALL be rejected with a ConflictError.

    **Validates: Requirements 6.7, 18.3**
    """
    cluster_name, project_a, project_b = data

    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    create_cluster_name_registry_table()
    cluster_names_mod, errors_mod = reload_cluster_ops_modules()

    # Register with project A — should succeed
    cluster_names_mod.register_cluster_name(
        table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        cluster_name=cluster_name,
        project_id=project_a,
    )

    # Attempt from project B — should fail
    with pytest.raises(errors_mod.ConflictError):
        cluster_names_mod.register_cluster_name(
            table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
            cluster_name=cluster_name,
            project_id=project_b,
        )


# ---------------------------------------------------------------------------
# Property 11: Cluster name same-project reuse
# ---------------------------------------------------------------------------


@given(cluster_name=valid_cluster_name, project_id=project_id_strategy)
@settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_cluster_name_same_project_reuse(cluster_name, project_id):
    """For any cluster name previously used within a project, re-registration
    within the same project SHALL be accepted (no error).

    **Validates: Requirements 6.8, 18.4**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    create_cluster_name_registry_table()
    cluster_names_mod, _ = reload_cluster_ops_modules()

    # First registration
    cluster_names_mod.register_cluster_name(
        table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        cluster_name=cluster_name,
        project_id=project_id,
    )

    # Same-project re-registration — should succeed without error
    result = cluster_names_mod.register_cluster_name(
        table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        cluster_name=cluster_name,
        project_id=project_id,
    )
    assert result["clusterName"] == cluster_name
    assert result["projectId"] == project_id


# ---------------------------------------------------------------------------
# Property 17: Cluster name registry preserves association
# ---------------------------------------------------------------------------


@given(cluster_name=valid_cluster_name, project_id=project_id_strategy)
@settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_cluster_name_registry_preserves_association(cluster_name, project_id):
    """For any cluster name registered with a project ID, looking up that
    cluster name SHALL return the associated project ID.

    **Validates: Requirements 18.2**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    create_cluster_name_registry_table()
    cluster_names_mod, _ = reload_cluster_ops_modules()

    # Register
    cluster_names_mod.register_cluster_name(
        table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        cluster_name=cluster_name,
        project_id=project_id,
    )

    # Lookup
    record = cluster_names_mod.lookup_cluster_name(
        table_name=CLUSTER_NAME_REGISTRY_TABLE_NAME,
        cluster_name=cluster_name,
    )
    assert record is not None
    assert record["projectId"] == project_id
    assert record["clusterName"] == cluster_name
