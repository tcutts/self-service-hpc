# Feature: self-service-hpc, Property 15: Resource tagging correctness
"""Property-based tests for resource tagging correctness.

Verifies that for any project identifier and cluster name, the tag set
constructed for cluster resources includes a tag with key ``Project``
equal to the project identifier and a tag with key ``ClusterName``
equal to the cluster name.

**Validates: Requirements 14.2, 14.3**
"""

import os
import sys

from hypothesis import given, settings
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Module loading — use the same file-path approach as conftest.py
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_ROOT, "cluster_operations")


def _load_tagging_module():
    """Load the tagging module directly from its file path."""
    import importlib.util

    filepath = os.path.join(_CLUSTER_OPS_DIR, "tagging.py")
    spec = importlib.util.spec_from_file_location("tagging", filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Project identifiers: non-empty alphanumeric strings (realistic IDs)
project_id_strategy = st.text(
    min_size=1,
    max_size=40,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
)

# Cluster names: non-empty strings with alphanumeric, hyphens, underscores
cluster_name_strategy = st.from_regex(r"[a-zA-Z0-9_-]+", fullmatch=True).filter(
    lambda s: 0 < len(s) <= 60
)


# ---------------------------------------------------------------------------
# Property 15: Resource tagging correctness
# ---------------------------------------------------------------------------


@given(project_id=project_id_strategy, cluster_name=cluster_name_strategy)
@settings(max_examples=100, deadline=None)
def test_build_resource_tags_includes_project_and_cluster_name(project_id, cluster_name):
    """For any project identifier and cluster name, build_resource_tags
    SHALL return a tag set containing Project = projectId and
    ClusterName = clusterName.

    **Validates: Requirements 14.2, 14.3**
    """
    tagging = _load_tagging_module()
    tags = tagging.build_resource_tags(project_id, cluster_name)

    tag_dict = {t["Key"]: t["Value"] for t in tags}
    assert "Project" in tag_dict, "Tag set must include a 'Project' tag"
    assert tag_dict["Project"] == project_id
    assert "ClusterName" in tag_dict, "Tag set must include a 'ClusterName' tag"
    assert tag_dict["ClusterName"] == cluster_name


@given(project_id=project_id_strategy, cluster_name=cluster_name_strategy)
@settings(max_examples=100, deadline=None)
def test_tags_as_dict_includes_project_and_cluster_name(project_id, cluster_name):
    """For any project identifier and cluster name, tags_as_dict SHALL
    return a dictionary containing Project = projectId and
    ClusterName = clusterName.

    **Validates: Requirements 14.2, 14.3**
    """
    tagging = _load_tagging_module()
    tag_map = tagging.tags_as_dict(project_id, cluster_name)

    assert "Project" in tag_map, "Tag dict must include a 'Project' key"
    assert tag_map["Project"] == project_id
    assert "ClusterName" in tag_map, "Tag dict must include a 'ClusterName' key"
    assert tag_map["ClusterName"] == cluster_name
