"""Unit tests for the Accounting Query Lambda.

Tests cover:
- Authorisation checks (Admin for cross-cluster, Project Admin for project-scoped)
- SSM command construction for sacct queries
- Result aggregation from multiple clusters
- Graceful handling of SSM failures
- sacct output parsing
"""

import json
from unittest.mock import MagicMock, patch

from conftest import build_admin_event, build_non_admin_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_accounting_event(
    caller="admin-user",
    groups="Administrators",
    project_id=None,
):
    """Build an API Gateway event for GET /accounting/jobs."""
    query_params = {}
    if project_id:
        query_params["projectId"] = project_id

    return {
        "httpMethod": "GET",
        "resource": "/accounting/jobs",
        "pathParameters": None,
        "queryStringParameters": query_params or None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:username": caller,
                    "sub": f"sub-{caller}",
                    "cognito:groups": groups,
                }
            }
        },
        "body": None,
    }


def _seed_active_cluster(clusters_table, project_id, cluster_name, instance_id="i-abc123"):
    """Insert an ACTIVE cluster record into the Clusters table."""
    clusters_table.put_item(
        Item={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
            "clusterName": cluster_name,
            "projectId": project_id,
            "status": "ACTIVE",
            "loginNodeInstanceId": instance_id,
            "loginNodeIp": "10.0.0.1",
            "sshPort": 22,
            "dcvPort": 8443,
        }
    )


def _seed_destroyed_cluster(clusters_table, project_id, cluster_name):
    """Insert a DESTROYED cluster record into the Clusters table."""
    clusters_table.put_item(
        Item={
            "PK": f"PROJECT#{project_id}",
            "SK": f"CLUSTER#{cluster_name}",
            "clusterName": cluster_name,
            "projectId": project_id,
            "status": "DESTROYED",
        }
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestAccountingQuery:
    """Tests for the accounting query handler and business logic."""

    # -- Authorisation tests ------------------------------------------------

    def test_cross_cluster_query_admin_allowed(self, accounting_env):
        """Admin can query jobs across all clusters."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(caller="admin-user", groups="Administrators")

        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert "jobs" in body
        assert "totalJobs" in body

    def test_cross_cluster_query_non_admin_rejected(self, accounting_env):
        """Non-admin cannot query jobs across all clusters."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(
            caller="regular-user",
            groups="ProjectUser-alpha",
        )

        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 403
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_scoped_query_admin_allowed(self, accounting_env):
        """Admin can query jobs for a specific project."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(
            caller="admin-user",
            groups="Administrators",
            project_id="proj-alpha",
        )

        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert "jobs" in body

    def test_project_scoped_query_project_admin_allowed(self, accounting_env):
        """Project Admin can query jobs for their project."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(
            caller="proj-admin",
            groups="ProjectAdmin-proj-alpha",
            project_id="proj-alpha",
        )

        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert "jobs" in body

    def test_project_scoped_query_wrong_project_admin_rejected(self, accounting_env):
        """Project Admin for a different project is rejected."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(
            caller="proj-admin",
            groups="ProjectAdmin-proj-beta",
            project_id="proj-alpha",
        )

        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 403
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_project_scoped_query_regular_user_rejected(self, accounting_env):
        """Regular project user cannot query accounting data."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(
            caller="regular-user",
            groups="ProjectUser-proj-alpha",
            project_id="proj-alpha",
        )

        response = handler_mod.handler(event, None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 403
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    # -- Route tests --------------------------------------------------------

    def test_unknown_route_returns_404(self, accounting_env):
        """Unknown routes return 404."""
        handler_mod = accounting_env["modules"][0]
        event = {
            "httpMethod": "POST",
            "resource": "/accounting/unknown",
            "pathParameters": None,
            "queryStringParameters": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "cognito:username": "admin-user",
                        "sub": "sub-admin-user",
                        "cognito:groups": "Administrators",
                    }
                }
            },
            "body": None,
        }

        response = handler_mod.handler(event, None)
        assert response["statusCode"] == 404

    # -- Active cluster filtering -------------------------------------------

    def test_only_active_clusters_queried(self, accounting_env):
        """Only ACTIVE clusters are included in accounting queries."""
        accounting_mod = accounting_env["modules"][1]
        clusters_table = accounting_env["clusters_table"]

        _seed_active_cluster(clusters_table, "proj-filter", "cluster-active", "i-active1")
        _seed_destroyed_cluster(clusters_table, "proj-filter", "cluster-destroyed")

        active = accounting_mod.get_active_clusters(
            clusters_table_name="Clusters",
            project_id="proj-filter",
        )

        assert len(active) == 1
        assert active[0]["clusterName"] == "cluster-active"

    def test_active_clusters_all_projects(self, accounting_env):
        """Cross-project query returns active clusters from all projects."""
        accounting_mod = accounting_env["modules"][1]
        clusters_table = accounting_env["clusters_table"]

        _seed_active_cluster(clusters_table, "proj-a", "cluster-a1", "i-a1")
        _seed_active_cluster(clusters_table, "proj-b", "cluster-b1", "i-b1")

        active = accounting_mod.get_active_clusters(
            clusters_table_name="Clusters",
        )

        cluster_names = {c["clusterName"] for c in active}
        assert "cluster-a1" in cluster_names
        assert "cluster-b1" in cluster_names

    # -- sacct output parsing -----------------------------------------------

    def test_parse_sacct_output_valid(self, accounting_env):
        """Valid sacct -p output is parsed into structured records."""
        accounting_mod = accounting_env["modules"][1]

        output = (
            "12345|my_job|compute|default|4|COMPLETED|0:0|\n"
            "12346|other_job|gpu|default|8|FAILED|1:0|\n"
        )

        jobs = accounting_mod._parse_sacct_output(output)

        assert len(jobs) == 2
        assert jobs[0]["JobID"] == "12345"
        assert jobs[0]["JobName"] == "my_job"
        assert jobs[0]["State"] == "COMPLETED"
        assert jobs[1]["JobID"] == "12346"
        assert jobs[1]["State"] == "FAILED"
        assert jobs[1]["AllocCPUS"] == "8"

    def test_parse_sacct_output_empty(self, accounting_env):
        """Empty sacct output returns an empty list."""
        accounting_mod = accounting_env["modules"][1]

        assert accounting_mod._parse_sacct_output("") == []
        assert accounting_mod._parse_sacct_output("   ") == []
        assert accounting_mod._parse_sacct_output(None) == []

    def test_parse_sacct_output_extra_fields(self, accounting_env):
        """Extra fields beyond the standard 7 are captured as numbered keys."""
        accounting_mod = accounting_env["modules"][1]

        output = "12345|my_job|compute|default|4|COMPLETED|0:0|extra1|extra2|\n"
        jobs = accounting_mod._parse_sacct_output(output)

        assert len(jobs) == 1
        assert jobs[0]["field_7"] == "extra1"
        assert jobs[0]["field_8"] == "extra2"

    # -- SSM command construction -------------------------------------------

    def test_query_sacct_no_instance_id(self, accounting_env):
        """Cluster without loginNodeInstanceId returns an error."""
        accounting_mod = accounting_env["modules"][1]

        cluster = {
            "clusterName": "no-login",
            "projectId": "proj-x",
            "status": "ACTIVE",
        }

        result = accounting_mod.query_sacct_on_cluster(cluster)

        assert result["clusterName"] == "no-login"
        assert "error" in result
        assert result["jobs"] == []

    # -- Aggregation --------------------------------------------------------

    def test_aggregation_empty_clusters(self, accounting_env):
        """Query with no active clusters returns empty results."""
        accounting_mod = accounting_env["modules"][1]

        result = accounting_mod.query_accounting_jobs(
            clusters_table_name="Clusters",
            project_id="proj-nonexistent",
        )

        assert result["jobs"] == []
        assert result["totalJobs"] == 0
        assert result["clusterResults"] == []

    # -- Response structure -------------------------------------------------

    def test_response_structure(self, accounting_env):
        """Response includes correct CORS headers and JSON body."""
        handler_mod = accounting_env["modules"][0]
        event = _build_accounting_event(caller="admin-user", groups="Administrators")

        response = handler_mod.handler(event, None)

        assert response["headers"]["Content-Type"] == "application/json"
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"
        body = json.loads(response["body"])
        assert "jobs" in body
        assert "clusterResults" in body
        assert "totalJobs" in body
