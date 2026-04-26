# Feature: project-update, Properties 1–3: Update lifecycle
"""Property-based tests verifying update lifecycle transitions.

Property 1: ACTIVE → UPDATING → ACTIVE round-trip produces status ACTIVE
with an empty errorMessage.
**Validates: Requirements 1.2, 1.3, 3.7**

Property 2: For any project in UPDATING status and any non-empty error
string, the failure handler transitions to ACTIVE with matching errorMessage.
**Validates: Requirements 1.4, 3.8**

Property 3: For any project in UPDATING status, attempting transition to
DESTROYING raises ConflictError and status remains UPDATING.
**Validates: Requirements 1.5**
"""

import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    PROJECTS_TABLE_NAME,
    _PROJECT_MGMT_DIR,
    create_projects_table,
    reload_project_mgmt_modules,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

project_id_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

project_name_strategy = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" "),
).filter(lambda s: s.strip()).map(str.strip)

error_message_strategy = st.text(
    min_size=1,
    max_size=200,
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
).filter(lambda s: s.strip()).map(str.strip)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_active_project(projects_table, project_id, project_name):
    """Insert a project in ACTIVE status into the mocked DynamoDB table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": project_name,
        "costAllocationTag": project_id,
        "status": "ACTIVE",
        "errorMessage": "",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
    })


def _seed_updating_project(projects_table, project_id, project_name):
    """Insert a project in UPDATING status into the mocked DynamoDB table."""
    projects_table.put_item(Item={
        "PK": f"PROJECT#{project_id}",
        "SK": "METADATA",
        "projectId": project_id,
        "projectName": project_name,
        "costAllocationTag": project_id,
        "status": "UPDATING",
        "errorMessage": "",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
        "statusChangedAt": "2024-01-01T00:00:00+00:00",
    })


# ---------------------------------------------------------------------------
# Property 1: Update lifecycle round-trip
# ---------------------------------------------------------------------------

@given(
    project_id=project_id_strategy,
    project_name=project_name_strategy,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_update_lifecycle_roundtrip(project_id, project_name):
    """For any project in ACTIVE status, transitioning to UPDATING and then
    back to ACTIVE should produce a project whose status is ACTIVE and whose
    errorMessage is empty.

    **Validates: Requirements 1.2, 1.3, 3.7**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
    })

    projects_table = create_projects_table()
    _, _, _, errors_mod = reload_project_mgmt_modules()

    # Re-import lifecycle after reload so it uses the moto-bound boto3
    from conftest import _load_module_from, _PROJECT_MGMT_DIR
    lifecycle_mod = _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")

    # Seed an ACTIVE project
    _seed_active_project(projects_table, project_id, project_name)

    # Transition ACTIVE → UPDATING
    lifecycle_mod.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="UPDATING",
    )

    # Verify intermediate state is UPDATING
    item = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )["Item"]
    assert item["status"] == "UPDATING"

    # Transition UPDATING → ACTIVE (success path, no error message)
    lifecycle_mod.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="ACTIVE",
    )

    # Verify final state
    item = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )["Item"]

    assert item["status"] == "ACTIVE", (
        f"Expected ACTIVE but got {item['status']}"
    )
    assert item.get("errorMessage", "") == "", (
        f"Expected empty errorMessage but got '{item.get('errorMessage')}'"
    )


# ---------------------------------------------------------------------------
# Property 2: Update failure preserves ACTIVE status with error message
# ---------------------------------------------------------------------------

@given(
    project_id=project_id_strategy,
    project_name=project_name_strategy,
    error_msg=error_message_strategy,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_update_failure_preserves_active_with_error(project_id, project_name, error_msg):
    """For any project in UPDATING status and any non-empty error string,
    the failure handler transitions to ACTIVE with matching errorMessage.

    **Validates: Requirements 1.4, 3.8**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_TABLE_NAME": PROJECTS_TABLE_NAME,
    })

    projects_table = create_projects_table()
    _, _, _, errors_mod = reload_project_mgmt_modules()

    # Re-import lifecycle after reload so it uses the moto-bound boto3
    from conftest import _load_module_from, _PROJECT_MGMT_DIR
    lifecycle_mod = _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")

    # Seed a project already in UPDATING status
    _seed_updating_project(projects_table, project_id, project_name)

    # Transition UPDATING → ACTIVE with an error message (failure path)
    lifecycle_mod.transition_project(
        table_name=PROJECTS_TABLE_NAME,
        project_id=project_id,
        target_status="ACTIVE",
        error_message=error_msg,
    )

    # Verify final state
    item = projects_table.get_item(
        Key={"PK": f"PROJECT#{project_id}", "SK": "METADATA"},
    )["Item"]

    assert item["status"] == "ACTIVE", (
        f"Expected ACTIVE but got {item['status']}"
    )
    assert item["errorMessage"] == error_msg, (
        f"Expected errorMessage '{error_msg}' but got '{item.get('errorMessage')}'"
    )


# ---------------------------------------------------------------------------
# Property 3: UPDATING blocks DESTROYING transition
# ---------------------------------------------------------------------------

@given(
    project_id=project_id_strategy,
    project_name=project_name_strategy,
)
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_updating_blocks_destroying_transition(project_id, project_name):
    """For any project in UPDATING status, attempting transition to DESTROYING
    raises ConflictError and status remains UPDATING.

    Uses validate_transition (pure logic, no DynamoDB) to verify the state
    machine rejects the transition.

    **Validates: Requirements 1.5**
    """
    from conftest import _load_module_from
    lifecycle_mod = _load_module_from(_PROJECT_MGMT_DIR, "lifecycle")

    # validate_transition should reject UPDATING → DESTROYING
    try:
        lifecycle_mod.validate_transition("UPDATING", "DESTROYING")
        raised = False
    except Exception as exc:
        # Check by class name to avoid module identity issues across reloads
        raised = type(exc).__name__ == "ConflictError"

    assert raised, (
        "Expected ConflictError when transitioning from UPDATING to DESTROYING"
    )

    # Also verify the state machine dict directly: UPDATING targets don't include DESTROYING
    assert "DESTROYING" not in lifecycle_mod.VALID_TRANSITIONS.get("UPDATING", []), (
        "DESTROYING should not be a valid target from UPDATING"
    )
