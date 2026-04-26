"""Unit tests for the Cluster Template Management Lambda.

Covers:
- Template creation happy path (201), duplicate rejection (409), missing fields (400)
- Template retrieval: get single template, get nonexistent (404), list templates
- Template deletion: delete template, delete nonexistent (404)
- Default template seeding: cpu-general and gpu-basic, idempotent on re-seed
- Authorisation: admin can create/delete, non-admin cannot create/delete,
  any authenticated user can read

Requirements: 3.1, 3.2, 3.3, 3.4

Infrastructure is set up once per test class via the ``template_mgmt_env``
fixture from conftest.py, avoiding repeated DynamoDB table creation.
"""

import json

import pytest

from conftest import (
    TEMPLATES_TABLE_NAME,
    build_admin_event,
    build_non_admin_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_template_body(template_id="tpl-test", template_name="Test Template"):
    """Return a minimal valid template creation body."""
    return {
        "templateId": template_id,
        "templateName": template_name,
        "description": "A test template",
        "instanceTypes": ["c7g.medium"],
        "loginInstanceType": "c7g.medium",
        "minNodes": 1,
        "maxNodes": 10,
        "amiId": "ami-test123",
        "softwareStack": {"scheduler": "slurm"},
    }


# ---------------------------------------------------------------------------
# Template creation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("template_mgmt_env")
class TestTemplateCreation:
    """Validates: Requirement 3.1"""

    def test_create_template_returns_201(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "POST", "/templates",
            body=_valid_template_body("tpl-create-ok"),
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-create-ok"
        assert body["templateName"] == "Test Template"
        assert body["instanceTypes"] == ["c7g.medium"]
        assert "createdAt" in body

    def test_create_template_stores_record_in_dynamodb(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        table = template_mgmt_env["table"]

        event = build_admin_event(
            "POST", "/templates",
            body=_valid_template_body("tpl-stored"),
        )
        handler_mod.handler(event, None)

        item = table.get_item(
            Key={"PK": "TEMPLATE#tpl-stored", "SK": "METADATA"}
        )
        assert "Item" in item
        assert item["Item"]["templateId"] == "tpl-stored"

    def test_duplicate_template_returns_409(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "POST", "/templates",
            body=_valid_template_body("tpl-dup"),
        )
        handler_mod.handler(event, None)

        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"]["code"] == "DUPLICATE_ERROR"
        assert "tpl-dup" in body["error"]["message"]

    def test_missing_template_id_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        payload = _valid_template_body()
        del payload["templateId"]
        event = build_admin_event("POST", "/templates", body=payload)
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_template_name_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        payload = _valid_template_body("tpl-noname")
        del payload["templateName"]
        event = build_admin_event("POST", "/templates", body=payload)
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_instance_types_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        payload = _valid_template_body("tpl-noinst")
        del payload["instanceTypes"]
        event = build_admin_event("POST", "/templates", body=payload)
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_body_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event("POST", "/templates")
        event["body"] = None
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Template retrieval
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("template_mgmt_env")
class TestTemplateRetrieval:
    """Validates: Requirement 3.1"""

    def test_get_template_returns_details(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        # Create a template first
        handler_mod.handler(
            build_admin_event("POST", "/templates", body=_valid_template_body("tpl-get")),
            None,
        )

        event = build_admin_event(
            "GET", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-get"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-get"
        assert body["templateName"] == "Test Template"

    def test_get_nonexistent_template_returns_404(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "GET", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-ghost"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    def test_list_templates_returns_created_templates(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        # Create two templates
        for tid in ["tpl-list-a", "tpl-list-b"]:
            handler_mod.handler(
                build_admin_event("POST", "/templates", body=_valid_template_body(tid, f"Template {tid}")),
                None,
            )

        event = build_admin_event("GET", "/templates")
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        template_ids = [t["templateId"] for t in body["templates"]]
        assert "tpl-list-a" in template_ids
        assert "tpl-list-b" in template_ids


# ---------------------------------------------------------------------------
# Template deletion
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("template_mgmt_env")
class TestTemplateDeletion:
    """Validates: Requirement 3.2"""

    def test_delete_template_succeeds(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        table = template_mgmt_env["table"]

        handler_mod.handler(
            build_admin_event("POST", "/templates", body=_valid_template_body("tpl-del")),
            None,
        )

        event = build_admin_event(
            "DELETE", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-del"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200

        # Verify record is gone
        item = table.get_item(
            Key={"PK": "TEMPLATE#tpl-del", "SK": "METADATA"}
        )
        assert "Item" not in item

    def test_delete_nonexistent_template_returns_404(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "DELETE", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-no-exist"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Default template seeding
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("template_mgmt_env")
class TestDefaultTemplateSeeding:
    """Validates: Requirement 3.3"""

    def test_seed_creates_cpu_general_and_gpu_basic(self, template_mgmt_env):
        _, templates_mod, _ = template_mgmt_env["modules"]

        seeded = templates_mod.seed_default_templates(table_name=TEMPLATES_TABLE_NAME)

        template_ids = [t["templateId"] for t in seeded]
        assert "cpu-general" in template_ids
        assert "gpu-basic" in template_ids
        assert len(seeded) == 2

    def test_seed_is_idempotent(self, template_mgmt_env):
        _, templates_mod, _ = template_mgmt_env["modules"]

        # First seed already happened in the test above (class-scoped env).
        # Seed again — should return empty list (both already exist).
        seeded = templates_mod.seed_default_templates(table_name=TEMPLATES_TABLE_NAME)

        assert seeded == []

    def test_seeded_cpu_general_has_correct_fields(self, template_mgmt_env):
        _, templates_mod, _ = template_mgmt_env["modules"]

        tpl = templates_mod.get_template(
            table_name=TEMPLATES_TABLE_NAME, template_id="cpu-general",
        )

        assert tpl["templateName"] == "General CPU"
        assert "c7g.medium" in tpl["instanceTypes"]
        assert tpl["minNodes"] == 1
        assert tpl["maxNodes"] == 10

    def test_seeded_gpu_basic_has_correct_fields(self, template_mgmt_env):
        _, templates_mod, _ = template_mgmt_env["modules"]

        tpl = templates_mod.get_template(
            table_name=TEMPLATES_TABLE_NAME, template_id="gpu-basic",
        )

        assert tpl["templateName"] == "Basic GPU"
        assert "g4dn.xlarge" in tpl["instanceTypes"]
        assert tpl["minNodes"] == 1
        assert tpl["maxNodes"] == 4


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("template_mgmt_env")
class TestTemplateAuthorisation:
    """Validates: Requirement 3.4"""

    def test_admin_can_create_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "POST", "/templates",
            body=_valid_template_body("tpl-auth-create"),
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 201

    def test_admin_can_delete_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        handler_mod.handler(
            build_admin_event("POST", "/templates", body=_valid_template_body("tpl-auth-del")),
            None,
        )

        event = build_admin_event(
            "DELETE", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-auth-del"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_non_admin_cannot_create_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_non_admin_event(
            "POST", "/templates",
            body=_valid_template_body("tpl-sneaky"),
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_non_admin_cannot_delete_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_non_admin_event(
            "DELETE", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-auth-create"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_authenticated_user_can_list_templates(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_non_admin_event("GET", "/templates")
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "templates" in body

    def test_authenticated_user_can_get_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        # Ensure a template exists (may already from prior tests)
        handler_mod.handler(
            build_admin_event("POST", "/templates", body=_valid_template_body("tpl-auth-read")),
            None,
        )

        event = build_non_admin_event(
            "GET", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-auth-read"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-auth-read"

    def test_unauthenticated_user_cannot_list_templates(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = {
            "httpMethod": "GET",
            "resource": "/templates",
            "pathParameters": None,
            "requestContext": {"authorizer": {"claims": {}}},
            "body": None,
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    def test_unauthenticated_user_cannot_get_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = {
            "httpMethod": "GET",
            "resource": "/templates/{templateId}",
            "pathParameters": {"templateId": "tpl-auth-read"},
            "requestContext": {"authorizer": {"claims": {}}},
            "body": None,
        }
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"


# ---------------------------------------------------------------------------
# Template update
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("template_mgmt_env")
class TestTemplateUpdate:
    """Validates: Requirements 1.1, 1.4, 1.5, 2.1, 2.2, 3.1, 3.2, 3.3"""

    def _create_template(self, handler_mod, template_id="tpl-upd-base"):
        """Create a template to be used as the update target."""
        handler_mod.handler(
            build_admin_event(
                "POST", "/templates",
                body=_valid_template_body(template_id, f"Original {template_id}"),
            ),
            None,
        )

    def _update_body(self, **overrides):
        """Return a valid update payload with optional field overrides."""
        body = {
            "templateName": "Updated Template",
            "description": "Updated description",
            "instanceTypes": ["c7g.large"],
            "loginInstanceType": "c7g.large",
            "minNodes": 2,
            "maxNodes": 20,
            "amiId": "ami-updated123",
            "softwareStack": {"scheduler": "slurm", "schedulerVersion": "25.01"},
        }
        body.update(overrides)
        return body

    # --- Happy path (Req 1.1, 2.2) ---

    def test_update_template_returns_200_with_updated_fields(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        self._create_template(handler_mod, "tpl-upd-200")

        update_payload = self._update_body()
        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            body=update_payload,
            path_parameters={"templateId": "tpl-upd-200"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["templateId"] == "tpl-upd-200"
        assert body["templateName"] == "Updated Template"
        assert body["description"] == "Updated description"
        assert body["instanceTypes"] == ["c7g.large"]
        assert body["loginInstanceType"] == "c7g.large"
        assert int(body["minNodes"]) == 2
        assert int(body["maxNodes"]) == 20
        assert body["amiId"] == "ami-updated123"
        assert body["softwareStack"] == {"scheduler": "slurm", "schedulerVersion": "25.01"}
        assert "updatedAt" in body

    def test_admin_can_update_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        self._create_template(handler_mod, "tpl-upd-admin")

        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            body=self._update_body(),
            path_parameters={"templateId": "tpl-upd-admin"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 200

    # --- Not found (Req 1.4) ---

    def test_update_nonexistent_template_returns_404(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            body=self._update_body(),
            path_parameters={"templateId": "tpl-does-not-exist"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    # --- Mismatched templateId (Req 1.5) ---

    def test_mismatched_body_template_id_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        self._create_template(handler_mod, "tpl-upd-mismatch")

        payload = self._update_body(templateId="tpl-different-id")
        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            body=payload,
            path_parameters={"templateId": "tpl-upd-mismatch"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"]["field"] == "templateId"

    # --- Authorisation (Req 2.1) ---

    def test_non_admin_cannot_update_template(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        self._create_template(handler_mod, "tpl-upd-noauth")

        event = build_non_admin_event(
            "PUT", "/templates/{templateId}",
            body=self._update_body(),
            path_parameters={"templateId": "tpl-upd-noauth"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "AUTHORISATION_ERROR"

    # --- Empty body (Req 3.3) ---

    def test_update_with_empty_body_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]

        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            path_parameters={"templateId": "tpl-upd-empty"},
        )
        event["body"] = None
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"

    # --- Invalid fields (Req 3.1, 3.2) ---

    def test_update_with_empty_template_name_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        self._create_template(handler_mod, "tpl-upd-badname")

        payload = self._update_body(templateName="")
        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            body=payload,
            path_parameters={"templateId": "tpl-upd-badname"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"]["field"] == "templateName"

    def test_update_with_min_nodes_exceeding_max_returns_400(self, template_mgmt_env):
        handler_mod, _, _ = template_mgmt_env["modules"]
        self._create_template(handler_mod, "tpl-upd-badnodes")

        payload = self._update_body(minNodes=50, maxNodes=5)
        event = build_admin_event(
            "PUT", "/templates/{templateId}",
            body=payload,
            path_parameters={"templateId": "tpl-upd-badnodes"},
        )
        response = handler_mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"]["code"] == "VALIDATION_ERROR"
