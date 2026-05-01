"""Preservation property tests — capture existing correct behavior BEFORE fix.

**Validates: Requirements 3.2, 3.3, 3.5, 3.6**

These tests encode behavior that MUST remain unchanged after the bugfix.
They are written against UNFIXED code and must PASS, confirming the baseline.
After the fix, they are re-run to confirm no regressions.
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

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
_cluster_creation = load_lambda_module("cluster_operations", "cluster_creation")
load_lambda_module("template_management", "errors")
_ami_lookup = load_lambda_module("template_management", "ami_lookup")


# ---------------------------------------------------------------------------
# Strategies for generating valid template data
# ---------------------------------------------------------------------------
instance_type_st = st.sampled_from([
    "c7g.medium", "c7g.large", "m6i.xlarge", "g4dn.xlarge", "hpc7g.4xlarge",
])
instance_types_st = st.lists(instance_type_st, min_size=1, max_size=3)
ami_id_st = st.from_regex(r"ami-[a-f0-9]{8,17}", fullmatch=True)
node_count_st = st.integers(min_value=0, max_value=100)
purchase_option_st = st.sampled_from(["ONDEMAND", "SPOT"])


class TestResolveTemplateFieldExtraction:
    """resolve_template() extracts template fields into the event payload.

    **Validates: Requirements 3.2**
    """

    @settings(max_examples=5, deadline=None)
    @given(
        login_type=instance_type_st,
        inst_types=instance_types_st,
        purchase=purchase_option_st,
        ami=ami_id_st,
        login_ami=ami_id_st,
    )
    def test_extracts_template_fields(self, login_type, inst_types, purchase, ami, login_ami):
        """For all valid template records, resolve_template() extracts
        loginInstanceType, instanceTypes, purchaseOption, amiId, loginAmiId
        matching the template values.

        **Validates: Requirements 3.2**
        """
        resolve_template = _cluster_creation.resolve_template

        template_record = {
            "PK": "TEMPLATE#test-tpl",
            "SK": "METADATA",
            "templateId": "test-tpl",
            "loginInstanceType": login_type,
            "instanceTypes": inst_types,
            "purchaseOption": purchase,
            "amiId": ami,
            "loginAmiId": login_ami,
            "minNodes": 1,
            "maxNodes": 10,
        }

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": template_record}

        event = {"templateId": "test-tpl", "projectId": "proj1", "clusterName": "c1"}

        with patch.object(_cluster_creation, "dynamodb") as mock_dynamodb:
            mock_dynamodb.Table.return_value = mock_table
            result = resolve_template(event)

        assert result["loginInstanceType"] == login_type
        assert result["instanceTypes"] == inst_types
        assert result["purchaseOption"] == purchase
        assert result["amiId"] == ami
        assert result["loginAmiId"] == login_ami


class TestResolveTemplateUserOverrides:
    """resolve_template() preserves user-provided minNodes/maxNodes overrides.

    **Validates: Requirements 3.5**
    """

    @settings(max_examples=5, deadline=None)
    @given(
        user_min=st.integers(min_value=0, max_value=50),
        user_max=st.integers(min_value=1, max_value=100),
    )
    def test_preserves_user_node_overrides(self, user_min, user_max):
        """For all events with user-provided minNodes/maxNodes,
        those overrides are preserved in the result.

        **Validates: Requirements 3.5**
        """
        resolve_template = _cluster_creation.resolve_template

        template_record = {
            "PK": "TEMPLATE#test-tpl",
            "SK": "METADATA",
            "templateId": "test-tpl",
            "loginInstanceType": "c7g.medium",
            "instanceTypes": ["c7g.medium"],
            "purchaseOption": "ONDEMAND",
            "amiId": "ami-12345678",
            "loginAmiId": "ami-12345678",
            "minNodes": 99,
            "maxNodes": 999,
        }

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": template_record}

        event = {
            "templateId": "test-tpl",
            "projectId": "proj1",
            "clusterName": "c1",
            "minNodes": user_min,
            "maxNodes": user_max,
            "storageMode": "FSX",
            "lustreCapacityGiB": 2400,
        }

        with patch.object(_cluster_creation, "dynamodb") as mock_dynamodb:
            mock_dynamodb.Table.return_value = mock_table
            result = resolve_template(event)

        # User overrides preserved — not replaced by template values
        assert result["minNodes"] == user_min
        assert result["maxNodes"] == user_max
        # Pass-through fields unchanged
        assert result["storageMode"] == "FSX"
        assert result["lustreCapacityGiB"] == 2400


class TestResolveTemplateDefaults:
    """resolve_template() with no templateId applies sensible defaults.

    **Validates: Requirements 3.5**
    """

    def test_no_template_applies_defaults(self):
        """When no templateId is provided, defaults are applied.

        **Validates: Requirements 3.5**
        """
        resolve_template = _cluster_creation.resolve_template

        event = {"projectId": "proj1", "clusterName": "c1"}

        result = resolve_template(event)

        assert result["loginInstanceType"] == "c7g.medium"
        assert result["instanceTypes"] == ["c7g.medium"]
        assert result["purchaseOption"] == "ONDEMAND"


class TestGetLatestPcsAmiSortOrder:
    """get_latest_pcs_ami() sorts AMIs by CreationDate descending.

    **Validates: Requirements 3.3**
    """

    @settings(max_examples=5, deadline=None)
    @given(
        num_images=st.integers(min_value=2, max_value=5),
    )
    def test_returns_latest_ami_by_creation_date(self, num_images):
        """For all lists of AMI images with distinct CreationDate values,
        get_latest_pcs_ami() returns the one with the latest date.

        **Validates: Requirements 3.3**
        """
        get_latest_pcs_ami = _ami_lookup.get_latest_pcs_ami

        # Build images with distinct, ordered creation dates
        images = []
        for i in range(num_images):
            images.append({
                "ImageId": f"ami-{i:08d}",
                "Name": f"aws-pcs-sample_ami-al2023-x86_64-slurm-25.11-2025010{i}",
                "Architecture": "x86_64",
                "CreationDate": f"2025-01-{10 + i:02d}T00:00:00Z",
            })

        # The latest image is the last one (highest date)
        expected_ami_id = images[-1]["ImageId"]

        # Shuffle to ensure sort is actually tested
        import random
        shuffled = images.copy()
        random.shuffle(shuffled)

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {"Images": shuffled}

        with patch.object(_ami_lookup, "ec2_client", mock_ec2):
            result = get_latest_pcs_ami(arch="x86_64")

        assert result["amiId"] == expected_ami_id
        # Verify return format has expected keys
        assert "amiId" in result
        assert "name" in result
        assert "architecture" in result
        assert "creationDate" in result
