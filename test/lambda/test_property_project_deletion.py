# Feature: self-service-hpc, Property 6: Project deletion is blocked by active clusters
"""Property-based test verifying that project deletion is blocked when active
clusters exist and allowed when no active clusters exist.

**Validates: Requirements 2.2, 2.3**
"""

import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
import pytest

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    CLUSTERS_TABLE_NAME,
    create_projects_table,
    create_clusters_table,
    reload_project_mgmt_modules,
)

ACTIVE_STATUSES = ["ACTIVE", "CREATING"]
INACTIVE_STATUSES = ["DESTROYING", "DESTROYED"]
ALL_STATUSES = ACTIVE_STATUSES + INACTIVE_STATUSES

project_id_strategy = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
cluster_name_strategy = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")) | st.just("-") | st.just("_"))
cluster_strategy = st.lists(st.tuples(cluster_name_strategy, st.sampled_from(ALL_STATUSES)), min_size=0, max_size=10, unique_by=lambda x: x[0])


def _seed_project(projects_table, project_id):
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}", "SK": "METADATA",
        "projectId": project_id, "projectName": f"Project {project_id}",
        "costAllocationTag": project_id, "status": "ACTIVE",
        "createdAt": "2024-01-01T00:00:00+00:00", "updatedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_cluster(clusters_table, project_id, cluster_name, status):
    clusters_table.put_item(Item={
        "PK": f"PROJECT#{project_id}", "SK": f"CLUSTER#{cluster_name}",
        "clusterName": cluster_name, "projectId": project_id,
        "status": status, "createdAt": "2024-01-01T00:00:00+00:00",
    })


@given(project_id=project_id_strategy, clusters=cluster_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@mock_aws
def test_project_deletion_blocked_by_active_clusters(project_id, clusters):
    """For any project with active clusters, deletion SHALL be rejected.
    For any project with zero active clusters, deletion SHALL succeed.

    **Validates: Requirements 2.2, 2.3**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION, "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    projects_table = create_projects_table()
    clusters_table = create_clusters_table()
    _, projects_mod, _, errors_mod = reload_project_mgmt_modules()

    _seed_project(projects_table, project_id)
    for cluster_name, status in clusters:
        _seed_cluster(clusters_table, project_id, cluster_name, status)

    active_cluster_names = sorted(name for name, status in clusters if status in ACTIVE_STATUSES)

    if active_cluster_names:
        with pytest.raises(errors_mod.ConflictError) as exc_info:
            projects_mod.delete_project(table_name=PROJECTS_TABLE_NAME, clusters_table_name=CLUSTERS_TABLE_NAME, project_id=project_id)
        assert "active clusters" in exc_info.value.message.lower()
        assert sorted(exc_info.value.details["activeClusters"]) == active_cluster_names
    else:
        projects_mod.delete_project(table_name=PROJECTS_TABLE_NAME, clusters_table_name=CLUSTERS_TABLE_NAME, project_id=project_id)
        response = projects_table.get_item(Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"})
        assert "Item" not in response
