"""Unit tests for resolve_template user override preservation.

Verifies that resolve_template preserves user-provided minNodes/maxNodes
overrides and passes storageMode/lustreCapacityGiB through unchanged.

**Validates: Requirements 5.6, 5.7, 9.2, 9.3**
"""

import os
import sys

import boto3
from moto import mock_aws
import pytest

# ---------------------------------------------------------------------------
# Environment variables required by the cluster_creation module at import
# ---------------------------------------------------------------------------
os.environ.setdefault("CLUSTERS_TABLE_NAME", "Clusters")
os.environ.setdefault("CLUSTER_NAME_REGISTRY_TABLE_NAME", "ClusterNameRegistry")
os.environ.setdefault("PROJECTS_TABLE_NAME", "Projects")
os.environ.setdefault("USERS_TABLE_NAME", "PlatformUsers")
os.environ.setdefault("TEMPLATES_TABLE_NAME", "ClusterTemplates")

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import _CLUSTER_OPS_DIR, _load_module_from, create_templates_table


def _load_cluster_creation_module():
    _load_module_from(_CLUSTER_OPS_DIR, "errors")
    _load_module_from(_CLUSTER_OPS_DIR, "auth")
    _load_module_from(_CLUSTER_OPS_DIR, "cluster_names")
    _load_module_from(_CLUSTER_OPS_DIR, "clusters")
    _load_module_from(_CLUSTER_OPS_DIR, "tagging")
    _load_module_from(_CLUSTER_OPS_DIR, "posix_provisioning")
    return _load_module_from(_CLUSTER_OPS_DIR, "cluster_creation")


class TestResolveTemplateOverrides:
    """Tests for resolve_template preserving user overrides."""

    @pytest.fixture(autouse=True)
    def setup(self):
        with mock_aws():
            table = create_templates_table()
            table.put_item(Item={
                "PK": "TEMPLATE#cpu-general",
                "SK": "METADATA",
                "loginInstanceType": "c7g.large",
                "instanceTypes": ["c7g.xlarge"],
                "maxNodes": 20,
                "minNodes": 2,
                "purchaseOption": "ONDEMAND",
            })
            self.mod = _load_cluster_creation_module()
            yield

    def test_preserves_user_min_nodes(self):
        """User-provided minNodes is not overwritten by template value."""
        event = {"templateId": "cpu-general", "minNodes": 5}
        result = self.mod.resolve_template(event)
        assert result["minNodes"] == 5

    def test_preserves_user_max_nodes(self):
        """User-provided maxNodes is not overwritten by template value."""
        event = {"templateId": "cpu-general", "maxNodes": 50}
        result = self.mod.resolve_template(event)
        assert result["maxNodes"] == 50

    def test_preserves_both_overrides(self):
        """Both minNodes and maxNodes user overrides are preserved."""
        event = {"templateId": "cpu-general", "minNodes": 3, "maxNodes": 30}
        result = self.mod.resolve_template(event)
        assert result["minNodes"] == 3
        assert result["maxNodes"] == 30

    def test_falls_back_to_template_when_absent(self):
        """Template values used when minNodes/maxNodes are absent."""
        event = {"templateId": "cpu-general"}
        result = self.mod.resolve_template(event)
        assert result["minNodes"] == 2
        assert result["maxNodes"] == 20

    def test_falls_back_to_template_when_none(self):
        """Template values used when minNodes/maxNodes are None."""
        event = {"templateId": "cpu-general", "minNodes": None, "maxNodes": None}
        result = self.mod.resolve_template(event)
        assert result["minNodes"] == 2
        assert result["maxNodes"] == 20

    def test_mixed_override_and_fallback(self):
        """minNodes from user, maxNodes from template."""
        event = {"templateId": "cpu-general", "minNodes": 7, "maxNodes": None}
        result = self.mod.resolve_template(event)
        assert result["minNodes"] == 7
        assert result["maxNodes"] == 20

    def test_preserves_storage_mode(self):
        """storageMode passes through unchanged."""
        event = {"templateId": "cpu-general", "storageMode": "lustre"}
        result = self.mod.resolve_template(event)
        assert result["storageMode"] == "lustre"

    def test_preserves_mountpoint_storage_mode(self):
        """storageMode=mountpoint passes through unchanged."""
        event = {"templateId": "cpu-general", "storageMode": "mountpoint"}
        result = self.mod.resolve_template(event)
        assert result["storageMode"] == "mountpoint"

    def test_preserves_lustre_capacity(self):
        """lustreCapacityGiB passes through unchanged."""
        event = {"templateId": "cpu-general", "storageMode": "lustre", "lustreCapacityGiB": 2400}
        result = self.mod.resolve_template(event)
        assert result["lustreCapacityGiB"] == 2400

    def test_preserves_null_lustre_capacity(self):
        """lustreCapacityGiB=None passes through unchanged."""
        event = {"templateId": "cpu-general", "storageMode": "mountpoint", "lustreCapacityGiB": None}
        result = self.mod.resolve_template(event)
        assert result["lustreCapacityGiB"] is None


class TestResolveTemplateNoTemplate:
    """Tests for resolve_template without a templateId (defaults path)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        with mock_aws():
            create_templates_table()
            self.mod = _load_cluster_creation_module()
            yield

    def test_defaults_when_no_template(self):
        """Default minNodes=0, maxNodes=10 when no template provided."""
        result = self.mod.resolve_template({})
        assert result["minNodes"] == 0
        assert result["maxNodes"] == 10

    def test_preserves_overrides_without_template(self):
        """User overrides preserved even without a template."""
        event = {"minNodes": 4, "maxNodes": 40}
        result = self.mod.resolve_template(event)
        assert result["minNodes"] == 4
        assert result["maxNodes"] == 40

    def test_preserves_storage_mode_without_template(self):
        """storageMode passes through even without a template."""
        event = {"storageMode": "lustre", "lustreCapacityGiB": 3600}
        result = self.mod.resolve_template(event)
        assert result["storageMode"] == "lustre"
        assert result["lustreCapacityGiB"] == 3600
