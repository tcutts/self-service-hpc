"""Bug condition exploration test — Slurm version mismatch in cluster creation pipeline.

**Validates: Requirements 1.1, 1.2, 1.3, 1.6**

This test encodes the EXPECTED (correct) behavior. It is designed to FAIL
on unfixed code, proving the bug exists. After the fix is applied, the same
test should PASS, confirming the bug is resolved.

Bug condition: For any template with a supported schedulerVersion value,
resolve_template() SHALL inject schedulerVersion into the event,
create_pcs_cluster() SHALL use that version in the PCS scheduler config,
and get_latest_pcs_ami() SHALL use the correct OS prefix for that version.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis.strategies import sampled_from

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# cluster_operations must come FIRST so its errors.py (which has ConflictError)
# is found before template_management's errors.py.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_TEMPLATE_MGMT_DIR = os.path.join(_LAMBDA_DIR, "template_management")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

# Order matters: cluster_operations first (has ConflictError in errors.py)
for _d in [_SHARED_DIR, _TEMPLATE_MGMT_DIR, _CLUSTER_OPS_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

# Expected version-to-OS mapping (the correct behavior we want after fix)
EXPECTED_VERSION_OS_MAP = {
    "24.11": "amzn2",
    "25.05": "amzn2",
    "25.11": "al2023",
}

SUPPORTED_VERSIONS = list(EXPECTED_VERSION_OS_MAP.keys())


class TestBugConditionResolveTemplate:
    """resolve_template() should inject schedulerVersion from the template."""

    @settings(max_examples=5)
    @given(version=sampled_from(SUPPORTED_VERSIONS))
    def test_resolve_template_injects_scheduler_version(self, version):
        """For any supported version in the template's softwareStack,
        resolve_template() must inject schedulerVersion into the event.

        **Validates: Requirements 1.2**

        On UNFIXED code this FAILS because resolve_template() never
        extracts softwareStack.schedulerVersion from the template record.
        """
        from cluster_creation import resolve_template

        template_record = {
            "PK": "TEMPLATE#test-tpl",
            "SK": "METADATA",
            "templateId": "test-tpl",
            "loginInstanceType": "c7g.medium",
            "instanceTypes": ["c7g.medium"],
            "purchaseOption": "ONDEMAND",
            "amiId": "ami-12345678",
            "loginAmiId": "ami-12345678",
            "minNodes": 1,
            "maxNodes": 10,
            "softwareStack": {
                "scheduler": "slurm",
                "schedulerVersion": version,
            },
        }

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": template_record}

        event = {"templateId": "test-tpl", "projectId": "proj1", "clusterName": "c1"}

        with patch("cluster_creation.dynamodb") as mock_dynamodb:
            mock_dynamodb.Table.return_value = mock_table
            result = resolve_template(event)

        # EXPECTED: schedulerVersion is present and matches the template
        assert "schedulerVersion" in result, (
            f"resolve_template() did not inject schedulerVersion into the event. "
            f"Template had schedulerVersion='{version}' but it was not extracted."
        )
        assert result["schedulerVersion"] == version, (
            f"Expected schedulerVersion='{version}', got '{result['schedulerVersion']}'"
        )


class TestBugConditionCreatePcsCluster:
    """create_pcs_cluster() should use event's schedulerVersion, not hardcoded."""

    @settings(max_examples=5)
    @given(version=sampled_from(SUPPORTED_VERSIONS))
    def test_create_pcs_cluster_uses_event_version(self, version):
        """For any supported version in the event, create_pcs_cluster()
        must pass that version to the PCS create_cluster API call.

        **Validates: Requirements 1.1**

        On UNFIXED code this FAILS because create_pcs_cluster() hardcodes
        "version": "24.11" regardless of event["schedulerVersion"].
        """
        from cluster_creation import create_pcs_cluster

        event = {
            "clusterName": "test-cluster",
            "projectId": "proj1",
            "privateSubnetIds": ["subnet-abc"],
            "securityGroupIds": {"computeNode": "sg-123"},
            "schedulerVersion": version,
        }

        mock_pcs = MagicMock()
        mock_pcs.create_cluster.return_value = {
            "cluster": {"id": "pcs-123", "arn": "arn:aws:pcs:us-east-1:123:cluster/pcs-123"},
        }

        with (
            patch("cluster_creation.pcs_client", mock_pcs),
            patch("cluster_creation._update_step_progress"),
        ):
            create_pcs_cluster(event)

        # Inspect the scheduler dict passed to PCS
        call_kwargs = mock_pcs.create_cluster.call_args
        scheduler_arg = call_kwargs.kwargs.get("scheduler") or call_kwargs[1].get("scheduler")
        actual_version = scheduler_arg["version"]

        assert actual_version == version, (
            f"create_pcs_cluster() passed version='{actual_version}' to PCS "
            f"but event had schedulerVersion='{version}'. "
            f"The function is hardcoding the version instead of reading from the event."
        )


class TestBugConditionGetLatestPcsAmi:
    """get_latest_pcs_ami() should accept slurm_version and use correct OS prefix."""

    @settings(max_examples=5)
    @given(version=sampled_from(SUPPORTED_VERSIONS))
    def test_get_latest_pcs_ami_uses_version_param(self, version):
        """For any supported version, get_latest_pcs_ami(slurm_version=v)
        must build the AMI name pattern with the correct version and OS prefix.

        **Validates: Requirements 1.3**

        On UNFIXED code this FAILS because get_latest_pcs_ami() does not
        accept a slurm_version parameter — it always uses hardcoded
        _PCS_SLURM_VERSION="25.11" and _PCS_AMI_OS="al2023".
        """
        from ami_lookup import get_latest_pcs_ami

        expected_os = EXPECTED_VERSION_OS_MAP[version]

        mock_ec2 = MagicMock()
        mock_ec2.describe_images.return_value = {
            "Images": [
                {
                    "ImageId": "ami-test123",
                    "Name": f"aws-pcs-sample_ami-{expected_os}-x86_64-slurm-{version}-20250101",
                    "Architecture": "x86_64",
                    "CreationDate": "2025-01-01T00:00:00Z",
                },
            ],
        }

        with patch("ami_lookup.ec2_client", mock_ec2):
            # This call should accept slurm_version parameter
            # On unfixed code, this will raise TypeError because the
            # function doesn't accept slurm_version
            result = get_latest_pcs_ami(arch="x86_64", slurm_version=version)

        # Verify the filter used the correct version and OS prefix
        call_kwargs = mock_ec2.describe_images.call_args
        filters = call_kwargs.kwargs.get("Filters") or call_kwargs[1].get("Filters")
        name_filter = [f for f in filters if f["Name"] == "name"][0]
        name_pattern = name_filter["Values"][0]

        assert version in name_pattern, (
            f"AMI name pattern '{name_pattern}' does not contain version '{version}'"
        )
        assert expected_os in name_pattern, (
            f"AMI name pattern '{name_pattern}' does not contain OS prefix '{expected_os}' "
            f"for version '{version}'"
        )


class TestBugConditionValidateTemplateFields:
    """_validate_template_fields() should reject invalid schedulerVersion values."""

    def test_rejects_invalid_scheduler_version(self):
        """_validate_template_fields() must reject schedulerVersion="99.99".

        **Validates: Requirements 1.6**

        On UNFIXED code this FAILS because _validate_template_fields()
        does not validate schedulerVersion at all.
        """
        from templates import _validate_template_fields
        # Get ValidationError from templates' own globals to guarantee
        # class identity matches what _validate_template_fields raises.
        import templates as _templates_mod
        ValidationError = _templates_mod.ValidationError

        # This should raise ValidationError for an invalid version
        with pytest.raises(ValidationError):
            _validate_template_fields(
                template_id="test-tpl",
                template_name="Test Template",
                instance_types=["c7g.medium"],
                login_instance_type="c7g.medium",
                min_nodes=1,
                max_nodes=10,
                ami_id="ami-12345678",
                software_stack={"schedulerVersion": "99.99"},
            )
