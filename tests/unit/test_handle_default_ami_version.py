"""Unit tests for _handle_default_ami() version parameter handling.

**Validates: Requirements 2.3**

Tests that the /templates/default-ami endpoint correctly passes the optional
`version` query parameter through to `get_latest_pcs_ami()`, and that it
continues to work without the parameter (backward compatibility).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

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
load_lambda_module("shared", "api_logging")
load_lambda_module("template_management", "errors")
load_lambda_module("template_management", "auth")
load_lambda_module("template_management", "ami_lookup")
load_lambda_module("template_management", "templates")
template_handler = load_lambda_module("template_management", "handler")

_handle_default_ami = template_handler._handle_default_ami


def _authenticated_event(query_params=None):
    """Build a minimal API Gateway event that passes is_authenticated."""
    return {
        "httpMethod": "GET",
        "resource": "/templates/default-ami",
        "queryStringParameters": query_params,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": "testuser",
                    "sub": "abc-123",
                },
            },
        },
    }


class TestHandleDefaultAmiVersion:
    """_handle_default_ami() passes version to get_latest_pcs_ami when provided."""

    def test_passes_version_to_get_latest_pcs_ami(self):
        """When version query param is provided, it is forwarded as slurm_version."""
        mock_get_ami = MagicMock(return_value={
            "amiId": "ami-abc123",
            "name": "aws-pcs-sample_ami-amzn2-x86_64-slurm-24.11-20250101",
            "architecture": "x86_64",
            "creationDate": "2025-01-01T00:00:00Z",
        })

        with patch.object(template_handler, "get_latest_pcs_ami", mock_get_ami):
            event = _authenticated_event({"arch": "x86_64", "version": "24.11"})
            response = _handle_default_ami(event)

        mock_get_ami.assert_called_once_with("x86_64", slurm_version="24.11")
        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["amiId"] == "ami-abc123"

    def test_passes_version_25_05(self):
        """Version 25.05 is forwarded correctly."""
        mock_get_ami = MagicMock(return_value={
            "amiId": "ami-def456",
            "name": "aws-pcs-sample_ami-amzn2-x86_64-slurm-25.05-20250601",
            "architecture": "x86_64",
            "creationDate": "2025-06-01T00:00:00Z",
        })

        with patch.object(template_handler, "get_latest_pcs_ami", mock_get_ami):
            event = _authenticated_event({"arch": "x86_64", "version": "25.05"})
            response = _handle_default_ami(event)

        mock_get_ami.assert_called_once_with("x86_64", slurm_version="25.05")
        assert response["statusCode"] == 200

    def test_no_version_param_backward_compatibility(self):
        """When version is absent, get_latest_pcs_ami is called without slurm_version."""
        mock_get_ami = MagicMock(return_value={
            "amiId": "ami-latest",
            "name": "aws-pcs-sample_ami-al2023-x86_64-slurm-25.11-20250701",
            "architecture": "x86_64",
            "creationDate": "2025-07-01T00:00:00Z",
        })

        with patch.object(template_handler, "get_latest_pcs_ami", mock_get_ami):
            event = _authenticated_event({"arch": "x86_64"})
            response = _handle_default_ami(event)

        # Called with arch only — no slurm_version keyword
        mock_get_ami.assert_called_once_with("x86_64")
        assert response["statusCode"] == 200

    def test_no_query_params_at_all(self):
        """When queryStringParameters is None, defaults apply (arch=x86_64, no version)."""
        mock_get_ami = MagicMock(return_value={
            "amiId": "ami-default",
            "name": "aws-pcs-sample_ami-al2023-x86_64-slurm-25.11-20250701",
            "architecture": "x86_64",
            "creationDate": "2025-07-01T00:00:00Z",
        })

        with patch.object(template_handler, "get_latest_pcs_ami", mock_get_ami):
            event = _authenticated_event(None)
            response = _handle_default_ami(event)

        mock_get_ami.assert_called_once_with("x86_64")
        assert response["statusCode"] == 200

    def test_version_with_whitespace_is_stripped(self):
        """Leading/trailing whitespace in version param is stripped."""
        mock_get_ami = MagicMock(return_value={
            "amiId": "ami-stripped",
            "name": "aws-pcs-sample_ami-al2023-x86_64-slurm-25.11-20250701",
            "architecture": "x86_64",
            "creationDate": "2025-07-01T00:00:00Z",
        })

        with patch.object(template_handler, "get_latest_pcs_ami", mock_get_ami):
            event = _authenticated_event({"arch": "x86_64", "version": "  25.11  "})
            response = _handle_default_ami(event)

        mock_get_ami.assert_called_once_with("x86_64", slurm_version="25.11")
        assert response["statusCode"] == 200

    def test_arm64_with_version(self):
        """arm64 architecture combined with version param works correctly."""
        mock_get_ami = MagicMock(return_value={
            "amiId": "ami-arm",
            "name": "aws-pcs-sample_ami-amzn2-arm64-slurm-24.11-20250101",
            "architecture": "arm64",
            "creationDate": "2025-01-01T00:00:00Z",
        })

        with patch.object(template_handler, "get_latest_pcs_ami", mock_get_ami):
            event = _authenticated_event({"arch": "arm64", "version": "24.11"})
            response = _handle_default_ami(event)

        mock_get_ami.assert_called_once_with("arm64", slurm_version="24.11")
        assert response["statusCode"] == 200
