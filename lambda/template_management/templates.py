"""Core cluster template management business logic.

Handles template CRUD operations and DynamoDB persistence, plus
seeding of default templates for initial deployment.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from errors import DuplicateError, InternalError, NotFoundError, ValidationError
from pcs_versions import DEFAULT_SLURM_VERSION, SUPPORTED_SLURM_VERSIONS

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")

VALID_INSTANCE_TYPE_PREFIXES = (
    "c5", "c5n", "c6g", "c6i", "c6gn", "c7g", "c7gn", "c7i",
    "m5", "m6g", "m6i", "m7g", "m7i",
    "r5", "r6g", "r6i", "r7g", "r7i",
    "g4dn", "g5", "g6", "p3", "p4d", "p5",
    "hpc6a", "hpc6id", "hpc7a", "hpc7g",
    "t3", "t3a", "t4g",
    "x2idn", "x2iedn",
    "trn1", "inf1", "inf2",
    "dl1",
)


def create_template(
    table_name: str,
    template_id: str,
    template_name: str,
    description: str,
    instance_types: list[str],
    login_instance_type: str,
    min_nodes: int,
    max_nodes: int,
    ami_id: str,
    software_stack: dict[str, Any],
    login_ami_id: str = "",
) -> dict[str, Any]:
    """Create a new cluster template in DynamoDB.

    Validates all fields and stores the template with a conditional put
    to prevent duplicates.
    """
    _validate_template_fields(
        template_id=template_id,
        template_name=template_name,
        instance_types=instance_types,
        login_instance_type=login_instance_type,
        min_nodes=min_nodes,
        max_nodes=max_nodes,
        ami_id=ami_id,
        software_stack=software_stack,
    )

    now = datetime.now(timezone.utc).isoformat()
    template_record = {
        "PK": f"TEMPLATE#{template_id}",
        "SK": "METADATA",
        "templateId": template_id,
        "templateName": template_name,
        "description": description,
        "instanceTypes": instance_types,
        "loginInstanceType": login_instance_type,
        "minNodes": min_nodes,
        "maxNodes": max_nodes,
        "amiId": ami_id,
        "loginAmiId": login_ami_id,
        "softwareStack": software_stack,
        "createdAt": now,
    }

    table = dynamodb.Table(table_name)
    try:
        table.put_item(
            Item=template_record,
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise DuplicateError(
                f"Template '{template_id}' already exists.",
                {"templateId": template_id},
            )
        raise InternalError(f"Failed to store template record: {exc}")

    return _sanitise_record(template_record)


def update_template(
    table_name: str,
    template_id: str,
    template_name: str,
    description: str,
    instance_types: list[str],
    login_instance_type: str,
    min_nodes: int,
    max_nodes: int,
    ami_id: str,
    software_stack: dict[str, Any],
    login_ami_id: str = "",
) -> dict[str, Any]:
    """Update an existing cluster template in DynamoDB.

    Validates all editable fields, then atomically updates the record
    using a condition expression to ensure the template exists.

    Raises NotFoundError if the template does not exist.
    """
    _validate_template_fields(
        template_id=template_id,
        template_name=template_name,
        instance_types=instance_types,
        login_instance_type=login_instance_type,
        min_nodes=min_nodes,
        max_nodes=max_nodes,
        ami_id=ami_id,
        software_stack=software_stack,
    )

    now = datetime.now(timezone.utc).isoformat()
    table = dynamodb.Table(table_name)

    try:
        response = table.update_item(
            Key={"PK": f"TEMPLATE#{template_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET templateName = :tn, description = :desc, "
                "instanceTypes = :it, loginInstanceType = :lit, "
                "minNodes = :minN, maxNodes = :maxN, "
                "amiId = :ami, loginAmiId = :loginAmi, "
                "softwareStack = :ss, updatedAt = :ua"
            ),
            ExpressionAttributeValues={
                ":tn": template_name,
                ":desc": description,
                ":it": instance_types,
                ":lit": login_instance_type,
                ":minN": min_nodes,
                ":maxN": max_nodes,
                ":ami": ami_id,
                ":loginAmi": login_ami_id,
                ":ss": software_stack,
                ":ua": now,
            },
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise NotFoundError(
                f"Template '{template_id}' not found.",
                {"templateId": template_id},
            )
        raise InternalError(f"Failed to update template record: {exc}")

    return _sanitise_record(response["Attributes"])


def get_template(table_name: str, template_id: str) -> dict[str, Any]:
    """Retrieve a single cluster template by templateId."""
    table = dynamodb.Table(table_name)
    response = table.get_item(
        Key={"PK": f"TEMPLATE#{template_id}", "SK": "METADATA"},
    )
    if "Item" not in response:
        raise NotFoundError(
            f"Template '{template_id}' not found.",
            {"templateId": template_id},
        )
    return _sanitise_record(response["Item"])


def list_templates(table_name: str) -> list[dict[str, Any]]:
    """List all cluster templates."""
    table = dynamodb.Table(table_name)
    response = table.scan(
        FilterExpression=(
            boto3.dynamodb.conditions.Attr("SK").eq("METADATA")
            & boto3.dynamodb.conditions.Attr("PK").begins_with("TEMPLATE#")
        ),
    )
    return [_sanitise_record(item) for item in response.get("Items", [])]


def delete_template(table_name: str, template_id: str) -> None:
    """Delete a cluster template from DynamoDB.

    Raises NotFoundError if the template does not exist.
    """
    # Verify template exists before deleting
    get_template(table_name, template_id)

    table = dynamodb.Table(table_name)
    table.delete_item(
        Key={"PK": f"TEMPLATE#{template_id}", "SK": "METADATA"},
    )


def seed_default_templates(table_name: str) -> list[dict[str, Any]]:
    """Seed the two default cluster templates if they do not already exist.

    This function is idempotent — it skips templates that already exist.
    Intended to be called by a CDK custom resource on first deployment.

    Returns a list of seeded template records (empty list if both exist).
    """
    defaults = _get_default_template_definitions()
    seeded = []

    for defn in defaults:
        try:
            result = create_template(table_name=table_name, **defn)
            seeded.append(result)
            logger.info("Seeded default template: %s", defn["template_id"])
        except DuplicateError:
            logger.info(
                "Default template '%s' already exists, skipping.",
                defn["template_id"],
            )

    return seeded


def _get_default_template_definitions() -> list[dict[str, Any]]:
    """Return the definitions for the two default cluster templates."""
    return [
        {
            "template_id": "cpu-general",
            "template_name": "General CPU",
            "description": (
                "Cost-effective CPU cluster template suitable for general "
                "HPC workloads. Uses Graviton-based c7g.medium instances."
            ),
            "instance_types": ["c7g.medium"],
            "login_instance_type": "c7g.medium",
            "min_nodes": 1,
            "max_nodes": 10,
            "ami_id": "ami-placeholder-cpu",
            "software_stack": {
                "scheduler": "slurm",
                "schedulerVersion": DEFAULT_SLURM_VERSION,
            },
        },
        {
            "template_id": "gpu-basic",
            "template_name": "Basic GPU",
            "description": (
                "Basic GPU cluster template suitable for introductory GPU "
                "workloads. Uses NVIDIA T4-based g4dn.xlarge instances."
            ),
            "instance_types": ["g4dn.xlarge"],
            "login_instance_type": "g4dn.xlarge",
            "min_nodes": 1,
            "max_nodes": 4,
            "ami_id": "ami-placeholder-gpu",
            "software_stack": {
                "scheduler": "slurm",
                "schedulerVersion": DEFAULT_SLURM_VERSION,
                "cudaVersion": "12.4",
            },
        },
    ]


def _validate_template_fields(
    template_id: str,
    template_name: str,
    instance_types: list[str],
    login_instance_type: str,
    min_nodes: int,
    max_nodes: int,
    ami_id: str,
    software_stack: dict[str, Any] | None = None,
) -> None:
    """Validate template fields and raise ValidationError on failure."""
    if not template_id or not template_id.strip():
        raise ValidationError("templateId is required.", {"field": "templateId"})

    if not template_name or not template_name.strip():
        raise ValidationError("templateName is required.", {"field": "templateName"})

    if not instance_types or not isinstance(instance_types, list):
        raise ValidationError(
            "instanceTypes must be a non-empty list.",
            {"field": "instanceTypes"},
        )

    for it in instance_types:
        if not isinstance(it, str) or not it.strip():
            raise ValidationError(
                "Each instance type must be a non-empty string.",
                {"field": "instanceTypes"},
            )

    if not login_instance_type or not isinstance(login_instance_type, str):
        raise ValidationError(
            "loginInstanceType is required and must be a string.",
            {"field": "loginInstanceType"},
        )

    if not isinstance(min_nodes, int) or min_nodes < 0:
        raise ValidationError(
            "minNodes must be a non-negative integer.",
            {"field": "minNodes"},
        )

    if not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValidationError(
            "maxNodes must be a positive integer.",
            {"field": "maxNodes"},
        )

    if min_nodes > max_nodes:
        raise ValidationError(
            "minNodes cannot exceed maxNodes.",
            {"fields": ["minNodes", "maxNodes"]},
        )

    if not ami_id or not isinstance(ami_id, str) or not ami_id.strip():
        raise ValidationError(
            "amiId is required and must be a non-empty string.",
            {"field": "amiId"},
        )

    if software_stack is not None:
        scheduler_version = software_stack.get("schedulerVersion")
        if scheduler_version is not None and scheduler_version not in SUPPORTED_SLURM_VERSIONS:
            supported = ", ".join(sorted(SUPPORTED_SLURM_VERSIONS))
            raise ValidationError(
                f"schedulerVersion '{scheduler_version}' is not supported. "
                f"Supported versions: {supported}.",
                {"field": "schedulerVersion"},
            )


def _sanitise_record(item: dict[str, Any]) -> dict[str, Any]:
    """Remove DynamoDB key attributes from a template record for API response."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}
