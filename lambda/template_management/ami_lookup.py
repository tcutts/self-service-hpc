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

from errors import InternalError

logger = logging.getLogger(__name__)

ec2_client = boto3.client("ec2")

# Latest PCS sample AMI parameters
_PCS_AMI_OS = "al2023"
_PCS_SLURM_VERSION = "25.11"


def get_latest_pcs_ami(arch: str = "x86_64") -> dict[str, Any]:
    """Return the latest PCS sample AMI for the given architecture.

    Args:
        arch: CPU architecture — "x86_64" or "arm64".

    Returns:
        Dict with amiId, name, architecture, and creationDate.

    Raises:
        InternalError: If no matching AMI is found.
    """
    name_pattern = (
        f"aws-pcs-sample_ami-{_PCS_AMI_OS}-{arch}-slurm-{_PCS_SLURM_VERSION}*"
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
