# Feature: bulk-actions-ui, Property 9: Batch template eligibility
"""Property-based tests verifying batch template eligibility.

Property 9: Batch template eligibility — only existing templates succeed.
For any batch of template identifiers containing a mix of existing and
non-existing template IDs, the batch delete endpoint returns "success" only
for templates that exist in the database. Non-existing templates receive
"error" entries.

**Validates: Requirements 7.4, 7.6**
"""

import json
import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    TEMPLATES_TABLE_NAME,
    _TEMPLATE_MGMT_DIR,
    create_templates_table,
    reload_template_mgmt_modules,
    build_admin_event,
    _load_module_from,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate a template ID: short alphanumeric string
template_id_strategy = st.text(
    min_size=3,
    max_size=12,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

# Generate 1-5 template entries, each with a unique ID and a boolean
# indicating whether the template should exist in DynamoDB
template_batch_strategy = st.lists(
    st.tuples(template_id_strategy, st.booleans()),
    min_size=1,
    max_size=5,
    unique_by=lambda t: t[0],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_template(templates_table, template_id):
    """Insert a template into the mocked DynamoDB table."""
    templates_table.put_item(Item={
        "PK": f"TEMPLATE#{template_id}",
        "SK": "METADATA",
        "templateId": template_id,
        "templateName": f"Template {template_id}",
        "description": "A test template",
        "instanceTypes": ["c7g.medium"],
        "loginInstanceType": "c7g.medium",
        "minNodes": 1,
        "maxNodes": 4,
        "amiId": "ami-placeholder",
        "softwareStack": {"scheduler": "slurm"},
        "createdAt": "2024-01-01T00:00:00+00:00",
    })


def _setup_env():
    """Set environment variables for mocked AWS."""
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "TEMPLATES_TABLE_NAME": TEMPLATES_TABLE_NAME,
        "USER_POOL_ID": "",
    })


def _parse_batch_response(response):
    """Parse the batch response body and return (results, summary)."""
    body = json.loads(response["body"])
    return body["results"], body["summary"]


# ---------------------------------------------------------------------------
# Property 9: Batch delete — only existing templates succeed
# ---------------------------------------------------------------------------

@given(templates=template_batch_strategy)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_batch_delete_only_existing_templates_succeed(templates):
    """For any batch of template IDs where some exist and some don't, batch
    delete returns "success" only for templates that exist in the database.
    Non-existing templates get "error".

    **Validates: Requirements 7.4, 7.6**
    """
    _setup_env()
    templates_table = create_templates_table()
    reload_template_mgmt_modules()

    # Seed only the templates marked as existing
    for tid, exists in templates:
        if exists:
            _seed_template(templates_table, tid)

    # Re-import handler after reload
    handler_mod = _load_module_from(_TEMPLATE_MGMT_DIR, "handler")

    template_ids = [tid for tid, _ in templates]
    event = build_admin_event(
        "POST",
        "/templates/batch/delete",
        body={"templateIds": template_ids},
    )

    response = handler_mod.handler(event, None)
    assert response["statusCode"] == 200

    results, summary = _parse_batch_response(response)

    # Verify each result matches eligibility
    exists_map = {tid: exists for tid, exists in templates}
    for result in results:
        tid = result["id"]
        if exists_map[tid]:
            assert result["status"] == "success", (
                f"Template '{tid}' exists and should succeed for delete, "
                f"got: {result}"
            )
        else:
            assert result["status"] == "error", (
                f"Template '{tid}' does not exist and should fail for delete, "
                f"got: {result}"
            )

    # Verify summary counts
    expected_success = sum(1 for _, exists in templates if exists)
    assert summary["total"] == len(templates)
    assert summary["succeeded"] == expected_success
    assert summary["failed"] == len(templates) - expected_success
