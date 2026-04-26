# Feature: self-service-hpc, Property 7: Cluster template storage round-trip
"""Property-based test verifying that storing a cluster template and then
retrieving it returns a template with all fields equal to the original
definition.

**Validates: Requirements 3.1**
"""

import os

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st
from moto import mock_aws

from conftest import (
    AWS_REGION,
    TEMPLATES_TABLE_NAME,
    create_templates_table,
    reload_template_mgmt_modules,
)

# ---------------------------------------------------------------------------
# Strategies
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


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------

@given(template_def=valid_template_definition())
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@mock_aws
def test_template_storage_roundtrip(template_def):
    """For any valid cluster template definition, storing the template and
    then retrieving it SHALL return a template with all fields equal to the
    original definition.

    **Validates: Requirements 3.1**
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

    # Store the template
    created = templates_mod.create_template(
        table_name=TEMPLATES_TABLE_NAME,
        **template_def,
    )

    # Retrieve the template
    retrieved = templates_mod.get_template(
        table_name=TEMPLATES_TABLE_NAME,
        template_id=template_def["template_id"],
    )

    # Verify all user-supplied fields match
    assert retrieved["templateId"] == template_def["template_id"]
    assert retrieved["templateName"] == template_def["template_name"]
    assert retrieved["description"] == template_def["description"]
    assert retrieved["instanceTypes"] == template_def["instance_types"]
    assert retrieved["loginInstanceType"] == template_def["login_instance_type"]
    assert retrieved["minNodes"] == template_def["min_nodes"]
    assert retrieved["maxNodes"] == template_def["max_nodes"]
    assert retrieved["amiId"] == template_def["ami_id"]
    assert retrieved["softwareStack"] == template_def["software_stack"]

    # createdAt is generated server-side — just verify it exists
    assert "createdAt" in retrieved

    # created and retrieved should be identical
    assert created == retrieved
