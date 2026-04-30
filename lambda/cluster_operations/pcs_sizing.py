"""PCS controller sizing logic.

Maps the requested maxNodes count to the appropriate AWS PCS controller
size tier. This module contains no AWS API calls and no external state.
"""

from errors import ValidationError

# AWS PCS controller size tiers: (tier_name, max_managed_instances)
# Ordered from smallest to largest.
PCS_SIZE_TIERS: list[tuple[str, int]] = [
    ("SMALL", 32),
    ("MEDIUM", 512),
    ("LARGE", 2048),
]

# Maximum supported maxNodes value (LARGE tier capacity minus 1 login node)
MAX_SUPPORTED_MAX_NODES: int = PCS_SIZE_TIERS[-1][1] - 1  # 2047


def determine_controller_size(max_nodes: int) -> str:
    """Return the smallest PCS controller size that can manage the workload.

    Args:
        max_nodes: Maximum number of compute node instances. Must be a
            positive integer no greater than 2,047.

    Returns:
        One of ``"SMALL"``, ``"MEDIUM"``, or ``"LARGE"``.

    Raises:
        ValidationError: If *max_nodes* is not a positive integer or
            exceeds the PCS maximum capacity.
    """
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool):
        raise ValidationError(
            "maxNodes must be an integer.",
            {"field": "maxNodes", "value": str(max_nodes)},
        )

    if max_nodes < 1:
        raise ValidationError(
            "maxNodes must be at least 1.",
            {"field": "maxNodes", "value": max_nodes},
        )

    total_managed = max_nodes + 1  # compute nodes + 1 login node

    if total_managed > PCS_SIZE_TIERS[-1][1]:
        raise ValidationError(
            f"Total managed instance count ({total_managed}) exceeds "
            f"the maximum PCS cluster capacity of "
            f"{PCS_SIZE_TIERS[-1][1]} managed instances. "
            f"maxNodes must be at most {MAX_SUPPORTED_MAX_NODES}.",
            {
                "field": "maxNodes",
                "value": max_nodes,
                "totalManaged": total_managed,
                "maxCapacity": PCS_SIZE_TIERS[-1][1],
            },
        )

    for tier_name, tier_capacity in PCS_SIZE_TIERS:
        if total_managed <= tier_capacity:
            return tier_name

    # Unreachable — the capacity check above guarantees a match.
    raise ValidationError(
        f"No PCS tier found for {total_managed} managed instances.",
        {"field": "maxNodes", "value": max_nodes},
    )
