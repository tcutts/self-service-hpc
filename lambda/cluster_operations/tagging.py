"""Resource tagging helpers for cluster operations.

Builds standardised tag sets for cluster resources, ensuring every
resource is tagged with the project Cost_Allocation_Tag and the
cluster name.
"""

from typing import Any


def build_resource_tags(project_id: str, cluster_name: str) -> list[dict[str, str]]:
    """Return the tag set for cluster resources.

    Every resource created during cluster provisioning receives:
    - ``Project`` — the project identifier (cost allocation tag)
    - ``ClusterName`` — the human-readable cluster name

    The returned list uses the format expected by the Resource Groups
    Tagging API (``[{"Key": ..., "Value": ...}, ...]``).
    """
    return [
        {"Key": "Project", "Value": project_id},
        {"Key": "ClusterName", "Value": cluster_name},
    ]


def build_boto3_tags(project_id: str, cluster_name: str) -> list[dict[str, str]]:
    """Return tags in the boto3 ``Tags`` format used by most AWS APIs.

    Identical structure to :func:`build_resource_tags` but provided as
    a separate function for clarity when the caller needs the
    ``[{"Key": ..., "Value": ...}]`` shape explicitly.
    """
    return build_resource_tags(project_id, cluster_name)


def tags_as_dict(project_id: str, cluster_name: str) -> dict[str, str]:
    """Return tags as a plain dictionary.

    Useful for APIs that accept ``{"TagKey": "TagValue"}`` maps
    (e.g. FSx ``Tags`` on create).
    """
    return {
        "Project": project_id,
        "ClusterName": cluster_name,
    }
