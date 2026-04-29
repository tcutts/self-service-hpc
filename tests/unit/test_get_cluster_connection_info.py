"""Unit tests for _handle_get_cluster connection info in handler.py.

Validates that the API handler returns correct connectionInfo with SSH, DCV,
and SSM fields for ACTIVE clusters, and omits connectionInfo for non-ACTIVE clusters.

Requirements: 3.1, 3.2, 3.3, 3.4
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

# Add the cluster_operations module to the path
_CLUSTER_OPS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "cluster_operations",
)
sys.path.insert(0, _CLUSTER_OPS_DIR)

# Add the shared module path (imported by handler)
_SHARED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "shared",
)
sys.path.insert(0, _SHARED_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api_event(project_id: str = "proj-123", cluster_name: str = "my-cluster") -> dict:
    """Build a minimal API Gateway proxy event for GET cluster."""
    return {
        "httpMethod": "GET",
        "resource": "/projects/{projectId}/clusters/{clusterName}",
        "pathParameters": {
            "projectId": project_id,
            "clusterName": cluster_name,
        },
        "requestContext": {
            "authorizer": {"claims": {"sub": "user-1", "email": "user@example.com"}},
        },
        "headers": {},
        "body": None,
    }


def _active_cluster(**overrides) -> dict:
    """Build a minimal ACTIVE cluster record."""
    cluster = {
        "PK": "PROJECT#proj-123",
        "SK": "CLUSTER#my-cluster",
        "clusterName": "my-cluster",
        "projectId": "proj-123",
        "status": "ACTIVE",
        "loginNodeIp": "54.123.45.67",
        "loginNodeInstanceId": "i-0abc123def456789a",
        "sshPort": 22,
        "dcvPort": 8443,
        "storageMode": "mountpoint",
    }
    cluster.update(overrides)
    return cluster


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetClusterConnectionInfo:
    """Validates: Requirements 3.1, 3.2, 3.3, 3.4"""

    @patch("handler.is_project_user", return_value=True)
    @patch("handler.check_budget_breach", return_value=False)
    @patch("handler.get_cluster")
    def test_active_cluster_with_ip_and_instance_id(
        self, mock_get_cluster, mock_budget, mock_auth
    ):
        """ACTIVE cluster with IP and instance ID → all three connectionInfo fields populated.

        Validates: Requirements 3.1, 3.2, 3.3
        """
        mock_get_cluster.return_value = _active_cluster()

        from handler import handler

        response = handler(_make_api_event(), None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        conn = body["connectionInfo"]
        assert conn["ssh"] == "ssh -p 22 <username>@54.123.45.67"
        assert conn["dcv"] == "https://54.123.45.67:8443"
        assert conn["ssm"] == "aws ssm start-session --target i-0abc123def456789a"

    @patch("handler.is_project_user", return_value=True)
    @patch("handler.check_budget_breach", return_value=False)
    @patch("handler.get_cluster")
    def test_active_cluster_with_empty_ip_and_empty_instance_id(
        self, mock_get_cluster, mock_budget, mock_auth
    ):
        """ACTIVE cluster with empty IP and empty instance ID → all fields are empty strings.

        Validates: Requirement 3.4
        """
        mock_get_cluster.return_value = _active_cluster(
            loginNodeIp="",
            loginNodeInstanceId="",
        )

        from handler import handler

        response = handler(_make_api_event(), None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        conn = body["connectionInfo"]
        assert conn["ssh"] == ""
        assert conn["dcv"] == ""
        assert conn["ssm"] == ""

    @patch("handler.is_project_user", return_value=True)
    @patch("handler.check_budget_breach", return_value=False)
    @patch("handler.get_cluster")
    def test_active_cluster_with_ip_but_no_instance_id(
        self, mock_get_cluster, mock_budget, mock_auth
    ):
        """ACTIVE cluster with IP but no instance ID → ssh and dcv populated, ssm empty.

        Validates: Requirements 3.2, 3.3, 3.1
        """
        mock_get_cluster.return_value = _active_cluster(
            loginNodeIp="10.0.1.5",
            loginNodeInstanceId="",
            sshPort=2222,
            dcvPort=9443,
        )

        from handler import handler

        response = handler(_make_api_event(), None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        conn = body["connectionInfo"]
        assert conn["ssh"] == "ssh -p 2222 <username>@10.0.1.5"
        assert conn["dcv"] == "https://10.0.1.5:9443"
        assert conn["ssm"] == ""

    @patch("handler.is_project_user", return_value=True)
    @patch("handler.check_budget_breach", return_value=False)
    @patch("handler.get_cluster")
    def test_non_active_cluster_has_no_connection_info(
        self, mock_get_cluster, mock_budget, mock_auth
    ):
        """Non-ACTIVE cluster → connectionInfo not in response.

        Validates: Requirement 3.4 (connectionInfo only for ACTIVE clusters)
        """
        mock_get_cluster.return_value = _active_cluster(status="CREATING")

        from handler import handler

        response = handler(_make_api_event(), None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert "connectionInfo" not in body
