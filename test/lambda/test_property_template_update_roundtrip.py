# Feature: template-editing, Property 1: Update storage round-trip
# Feature: template-editing, Property 2: Timestamp invariants on update
# Feature: template-editing, Property 3: Invalid fields are rejected
"""Property-based tests verifying template update round-trip behaviour.

Property 1: updating a cluster template and then retrieving it returns a
template with all editable fields equal to the update payload.
**Validates: Requirements 1.1, 8.1**

Property 2: updating a cluster template preserves the original createdAt
value and sets a valid updatedAt ISO 8601 timestamp >= createdAt.
**Validates: Requirements 1.2, 1.3, 8.2**

Property 3: for any template update request containing at least one invalid
editable field, update_template SHALL raise a ValidationError and leave the
stored template record unchanged.
**Validates: Requirements 3.1, 3.2**
"""

import os
from datetime import datetime

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws
import pytest

from conftest import (
    AWS_REGION,
    TEMPLATES_TABLE_NAME,
    create_templates_table,
    reload_template_mgmt_modules,
)

# ---------------------------------------------------------------------------
# Reuse strategies from test_property_template_roundtrip.py
# ---------------------------------------------------------------------------

VALID_INSTANCE_TYPES = [
    "c7g.medium", "c7g.large", "c7g.xlarge",
    "c6i.large", "c6i.xlarge",
    "m6i.large", "m6i.xlarge",
    "g4dn.xlarge", "g4dn.2xlarge",
    "g5.xlarge",
    "hpc7a.12xlarge",
    "t3.medium", "t3.large",
]

template_id_strategy = st.text(
    min_size=1, max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

template_name_strategy = st.text(
    min_size=1, max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
).filter(lambda s: s.strip())

description_strategy = st.text(
    min_size=0, max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P")),
)

instance_types_strategy = st.lists(
    st.sampled_from(VALID_INSTANCE_TYPES),
    min_size=1, max_size=5,
)

login_instance_type_strategy = st.sampled_from(VALID_INSTANCE_TYPES)

ami_id_strategy = st.text(
    min_size=1, max_size=12,
    alphabet=st.characters(whitelist_categories=("L", "N")),
).map(lambda s: f"ami-{s}")

software_stack_strategy = st.just({"scheduler": "slurm"})


@st.composite
def valid_template_definition(draw):
    """Generate a valid cluster template definition with min_nodes <= max_nodes."""
    min_nodes = draw(st.integers(min_value=0, max_value=5))
    max_nodes = draw(st.integers(min_value=max(1, min_nodes), max_value=10))
    return {
        "template_id": draw(template_id_strategy),
        "template_name": draw(template_name_strategy),
        "description": draw(description_strategy),
        "instance_types": draw(instance_types_strategy),
        "login_instance_type": draw(login_instance_type_strategy),
        "min_nodes": min_nodes,
        "max_nodes": max_nodes,
        "ami_id": draw(ami_id_strategy),
        "software_stack": draw(software_stack_strategy),
    }


@st.composite
def valid_update_payload(draw):
    """Generate a valid set of editable fields for a template update."""
    min_nodes = draw(st.integers(min_value=0, max_value=5))
    max_nodes = draw(st.integers(min_value=max(1, min_nodes), max_value=10))
    return {
        "template_name": draw(template_name_strategy),
        "description": draw(description_strategy),
        "instance_types": draw(instance_types_strategy),
        "login_instance_type": draw(login_instance_type_strategy),
        "min_nodes": min_nodes,
        "max_nodes": max_nodes,
        "ami_id": draw(ami_id_strategy),
        "software_stack": draw(software_stack_strategy),
    }


# ---------------------------------------------------------------------------
# Property 1: Update storage round-trip
# ---------------------------------------------------------------------------

@given(
    template_def=valid_template_definition(),
    update_payload=valid_update_payload(),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_update_storage_roundtrip(template_def, update_payload):
    """For any valid template and any valid update payload, updating the
    template and then retrieving it SHALL return a record where every
    editable field equals the corresponding value from the update payload.

    **Validates: Requirements 1.1, 8.1**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    create_templates_table()
    _, templates_mod, _ = reload_template_mgmt_modules()

    # Step 1: Create the template
    templates_mod.create_template(
        table_name=TEMPLATES_TABLE_NAME,
        **template_def,
    )

    # Step 2: Update the template with the new payload
    templates_mod.update_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
        **update_payload,
    )

    # Step 3: Retrieve the template
    retrieved = templates_mod.get_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
    )

    # Assert all editable fields match the update payload
    assert retrieved["templateName"] == update_payload["template_name"]
    assert retrieved["description"] == update_payload["description"]
    assert retrieved["instanceTypes"] == update_payload["instance_types"]
    assert retrieved["loginInstanceType"] == update_payload["login_instance_type"]
    assert retrieved["minNodes"] == update_payload["min_nodes"]
    assert retrieved["maxNodes"] == update_payload["max_nodes"]
    assert retrieved["amiId"] == update_payload["ami_id"]
    assert retrieved["softwareStack"] == update_payload["software_stack"]

    # Immutable fields should be preserved
    assert retrieved["templateId"] == template_def["template_id"]


# ---------------------------------------------------------------------------
# Property 2: Timestamp invariants on update
# ---------------------------------------------------------------------------

@given(
    template_def=valid_template_definition(),
    update_payload=valid_update_payload(),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_timestamp_invariants_on_update(template_def, update_payload):
    """For any valid template and any valid update payload, updating the
    template SHALL preserve the original createdAt value unchanged AND set
    an updatedAt field containing a valid ISO 8601 UTC timestamp that is
    greater than or equal to the createdAt value.

    **Validates: Requirements 1.2, 1.3, 8.2**
    """
    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    create_templates_table()
    _, templates_mod, _ = reload_template_mgmt_modules()

    # Step 1: Create the template and capture original createdAt
    created = templates_mod.create_template(
        table_name=TEMPLATES_TABLE_NAME,
        **template_def,
    )
    original_created_at = created["createdAt"]

    # Step 2: Update the template
    templates_mod.update_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
        **update_payload,
    )

    # Step 3: Retrieve the template
    retrieved = templates_mod.get_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
    )

    # Assert createdAt is preserved unchanged
    assert retrieved["createdAt"] == original_created_at

    # Assert updatedAt is present and is a valid ISO 8601 string
    assert "updatedAt" in retrieved
    updated_at_str = retrieved["updatedAt"]
    updated_at = datetime.fromisoformat(updated_at_str)

    # Assert updatedAt >= createdAt
    created_at = datetime.fromisoformat(original_created_at)
    assert updated_at >= created_at


# ---------------------------------------------------------------------------
# Strategy: generate an invalid update payload (one invalid field at a time)
# ---------------------------------------------------------------------------

@st.composite
def invalid_update_payload(draw):
    """Generate an update payload where exactly ONE field is invalid.

    Starts from a valid base payload, then replaces one field with an
    invalid value.  Returns (payload_dict, description_of_invalid_field).
    """
    # Build a valid base first
    min_nodes = draw(st.integers(min_value=0, max_value=5))
    max_nodes = draw(st.integers(min_value=max(1, min_nodes), max_value=10))
    base = {
        "template_name": draw(template_name_strategy),
        "description": draw(description_strategy),
        "instance_types": draw(instance_types_strategy),
        "login_instance_type": draw(login_instance_type_strategy),
        "min_nodes": min_nodes,
        "max_nodes": max_nodes,
        "ami_id": draw(ami_id_strategy),
        "software_stack": draw(software_stack_strategy),
    }

    # Pick which field to invalidate
    invalid_choice = draw(st.sampled_from([
        "empty_template_name",
        "empty_instance_types",
        "min_exceeds_max",
        "non_positive_max_nodes",
        "empty_ami_id",
        "non_string_login_instance_type",
    ]))

    if invalid_choice == "empty_template_name":
        # Empty or whitespace-only templateName
        base["template_name"] = draw(st.sampled_from(["", "   ", "\t"]))
    elif invalid_choice == "empty_instance_types":
        base["instance_types"] = []
    elif invalid_choice == "min_exceeds_max":
        # Ensure minNodes > maxNodes
        base["max_nodes"] = draw(st.integers(min_value=1, max_value=5))
        base["min_nodes"] = base["max_nodes"] + draw(st.integers(min_value=1, max_value=5))
    elif invalid_choice == "non_positive_max_nodes":
        base["max_nodes"] = draw(st.integers(min_value=-10, max_value=0))
    elif invalid_choice == "empty_ami_id":
        base["ami_id"] = draw(st.sampled_from(["", "   ", "\t"]))
    elif invalid_choice == "non_string_login_instance_type":
        base["login_instance_type"] = draw(st.sampled_from([123, None, [], {}]))

    return base, invalid_choice


# ---------------------------------------------------------------------------
# Property 3: Invalid fields are rejected
# ---------------------------------------------------------------------------

@given(
    template_def=valid_template_definition(),
    invalid_payload_and_reason=invalid_update_payload(),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@mock_aws
def test_invalid_fields_are_rejected(template_def, invalid_payload_and_reason):
    """For any template update request containing at least one invalid
    editable field, update_template SHALL raise a ValidationError and
    leave the stored template record unchanged.

    **Validates: Requirements 3.1, 3.2**
    """
    invalid_payload, _reason = invalid_payload_and_reason

    os.environ.update({
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    })

    create_templates_table()
    _, templates_mod, errors_mod = reload_template_mgmt_modules()

    # Step 1: Create the template with valid data
    templates_mod.create_template(
        table_name=TEMPLATES_TABLE_NAME,
        **template_def,
    )

    # Step 2: Retrieve the original record for later comparison
    original = templates_mod.get_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
    )

    # Step 3: Attempt the invalid update — must raise ValidationError
    with pytest.raises(errors_mod.ValidationError):
        templates_mod.update_template(
            table_name=TEMPLATES_TABLE_NAME,
            template_id=template_def["template_id"],
            **invalid_payload,
        )

    # Step 4: Verify the stored record is unchanged
    after_failed_update = templates_mod.get_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
    )
    assert after_failed_update == original
