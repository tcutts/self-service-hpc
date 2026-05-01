"""Unit tests for resolve_template() scheduler version extraction.

**Validates: Requirements 2.1, 2.2, 3.5**
"""

from unittest.mock import MagicMock, patch

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(os.path.dirname(__file__), "..", "conftest.py"))
_tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_tc)
load_lambda_module = _tc.load_lambda_module
_ensure_shared_modules = _tc._ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
load_lambda_module("cluster_operations", "errors")
load_lambda_module("cluster_operations", "cluster_names")
load_lambda_module("cluster_operations", "pcs_sizing")
load_lambda_module("cluster_operations", "tagging")
load_lambda_module("cluster_operations", "posix_provisioning")
cluster_creation = load_lambda_module("cluster_operations", "cluster_creation")
resolve_template = cluster_creation.resolve_template

pcs_versions = load_lambda_module("shared", "pcs_versions")
DEFAULT_SLURM_VERSION = pcs_versions.DEFAULT_SLURM_VERSION


def _make_template_record(template_id="test-tpl", software_stack=None):
    """Build a minimal template DynamoDB record."""
    record = {
        "PK": f"TEMPLATE#{template_id}",
        "SK": "METADATA",
        "templateId": template_id,
        "loginInstanceType": "c7g.medium",
        "instanceTypes": ["c7g.medium"],
        "purchaseOption": "ONDEMAND",
        "amiId": "ami-12345678",
        "loginAmiId": "ami-12345678",
        "minNodes": 1,
        "maxNodes": 10,
    }
    if software_stack is not None:
        record["softwareStack"] = software_stack
    return record


def _mock_table_with_item(item):
    """Return a MagicMock DynamoDB table that returns *item* on get_item."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": item}
    return mock_table


class TestResolveTemplateSchedulerVersion:
    """resolve_template() injects schedulerVersion from the template's softwareStack."""

    def test_injects_version_from_software_stack(self):
        """When the template has softwareStack.schedulerVersion, that value
        is injected into the returned event.

        **Validates: Requirements 2.1**
        """
        record = _make_template_record(
            software_stack={"scheduler": "slurm", "schedulerVersion": "24.11"},
        )
        mock_table = _mock_table_with_item(record)
        event = {"templateId": "test-tpl", "projectId": "proj1", "clusterName": "c1"}

        with patch("cluster_creation.dynamodb") as mock_ddb:
            mock_ddb.Table.return_value = mock_table
            result = resolve_template(event)

        assert result["schedulerVersion"] == "24.11"

    def test_injects_each_supported_version(self):
        """Verify extraction works for every supported version string.

        **Validates: Requirements 2.1**
        """
        for version in ("24.11", "25.05", "25.11"):
            record = _make_template_record(
                software_stack={"scheduler": "slurm", "schedulerVersion": version},
            )
            mock_table = _mock_table_with_item(record)
            event = {"templateId": "test-tpl", "projectId": "proj1", "clusterName": "c1"}

            with patch("cluster_creation.dynamodb") as mock_ddb:
                mock_ddb.Table.return_value = mock_table
                result = resolve_template(event)

            assert result["schedulerVersion"] == version, (
                f"Expected schedulerVersion='{version}', got '{result.get('schedulerVersion')}'"
            )


class TestResolveTemplateDefaultsVersion:
    """resolve_template() defaults schedulerVersion to DEFAULT_SLURM_VERSION."""

    def test_defaults_when_no_software_stack(self):
        """When the template record has no softwareStack key at all,
        schedulerVersion defaults to DEFAULT_SLURM_VERSION.

        **Validates: Requirements 2.2, 3.5**
        """
        record = _make_template_record(software_stack=None)
        mock_table = _mock_table_with_item(record)
        event = {"templateId": "test-tpl", "projectId": "proj1", "clusterName": "c1"}

        with patch("cluster_creation.dynamodb") as mock_ddb:
            mock_ddb.Table.return_value = mock_table
            result = resolve_template(event)

        assert result["schedulerVersion"] == DEFAULT_SLURM_VERSION

    def test_defaults_when_software_stack_missing_version(self):
        """When softwareStack exists but has no schedulerVersion key,
        schedulerVersion defaults to DEFAULT_SLURM_VERSION.

        **Validates: Requirements 2.2, 3.5**
        """
        record = _make_template_record(software_stack={"scheduler": "slurm"})
        mock_table = _mock_table_with_item(record)
        event = {"templateId": "test-tpl", "projectId": "proj1", "clusterName": "c1"}

        with patch("cluster_creation.dynamodb") as mock_ddb:
            mock_ddb.Table.return_value = mock_table
            result = resolve_template(event)

        assert result["schedulerVersion"] == DEFAULT_SLURM_VERSION

    def test_defaults_when_no_template_id(self):
        """When no templateId is provided, resolve_template() applies
        defaults including schedulerVersion = DEFAULT_SLURM_VERSION.

        **Validates: Requirements 2.2, 3.5**
        """
        event = {"projectId": "proj1", "clusterName": "c1"}
        result = resolve_template(event)

        assert result["schedulerVersion"] == DEFAULT_SLURM_VERSION
