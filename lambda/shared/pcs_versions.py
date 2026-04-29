"""Single source of truth for supported PCS Slurm versions and their OS mappings."""

# Ordered mapping of supported Slurm version -> AMI OS prefix.
# Update this dict when AWS PCS adds or deprecates Slurm versions.
SUPPORTED_SLURM_VERSIONS: dict[str, str] = {
    "24.11": "amzn2",
    "25.05": "amzn2",
    "25.11": "al2023",
}

DEFAULT_SLURM_VERSION: str = "25.11"
