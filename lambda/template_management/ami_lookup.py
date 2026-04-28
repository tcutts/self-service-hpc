"""PCS sample AMI lookup.

Discovers the latest AWS PCS sample AMI for a given CPU architecture
by querying EC2 DescribeImages with the PCS naming convention filter.

PCS sample AMI naming convention:
    aws-pcs-sample_ami-{os}-{arch}-slurm-{version}-{timestamp}

The latest Slurm version (25.11) uses Amazon Linux 2023 (al2023).
"""

import logging
from typing import Any

import boto3

from errors import InternalError, ValidationError
from pcs_versions import DEFAULT_SLURM_VERSION, SUPPORTED_SLURM_VERSIONS

logger = logging.getLogger(__name__)

ec2_client = boto3.client("ec2")


def get_latest_pcs_ami(
    arch: str = "x86_64",
    slurm_version: str = DEFAULT_SLURM_VERSION,
) -> dict[str, Any]:
    """Return the latest PCS sample AMI for the given architecture.

    Args:
        arch: CPU architecture — "x86_64" or "arm64".
        slurm_version: PCS Slurm version to look up (e.g. "24.11", "25.11").

    Returns:
        Dict with amiId, name, architecture, and creationDate.

    Raises:
        ValidationError: If *slurm_version* is not in SUPPORTED_SLURM_VERSIONS.
        InternalError: If no matching AMI is found.
    """
    if slurm_version not in SUPPORTED_SLURM_VERSIONS:
        raise ValidationError(
            f"Unsupported Slurm version '{slurm_version}'. "
            f"Supported versions: {', '.join(SUPPORTED_SLURM_VERSIONS)}"
        )

    os_prefix = SUPPORTED_SLURM_VERSIONS[slurm_version]
    name_pattern = (
        f"aws-pcs-sample_ami-{os_prefix}-{arch}-slurm-{slurm_version}*"
    )

    try:
        response = ec2_client.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": [name_pattern]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
    except Exception as exc:
        logger.error("Failed to query EC2 AMIs: %s", exc)
        raise InternalError(f"Failed to look up PCS sample AMI: {exc}")

    images = response.get("Images", [])
    if not images:
        raise InternalError(
            f"No PCS sample AMI found for architecture '{arch}' "
            f"(pattern: {name_pattern})."
        )

    # Sort by CreationDate descending and pick the latest
    images.sort(key=lambda img: img.get("CreationDate", ""), reverse=True)
    latest = images[0]

    return {
        "amiId": latest["ImageId"],
        "name": latest.get("Name", ""),
        "architecture": latest.get("Architecture", arch),
        "creationDate": latest.get("CreationDate", ""),
    }
