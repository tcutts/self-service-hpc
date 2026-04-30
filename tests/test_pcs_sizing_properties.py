"""Property-based tests for pcs_sizing.py controller sizing logic.

[PBT: Property 1] For any valid maxNodes in [1, 2047], determine_controller_size
returns the smallest PCS tier whose capacity >= maxNodes + 1.
"""

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

# Add cluster_operations first so its errors.py is found
sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)

# Clear cached modules to ensure correct imports
_cached_errors = sys.modules.get("errors")
if _cached_errors is not None:
    _errors_file = getattr(_cached_errors, "__file__", "") or ""
    if "cluster_operations" not in _errors_file:
        del sys.modules["errors"]

for _mod in ["pcs_sizing"]:
    if _mod in sys.modules:
        del sys.modules[_mod]

from pcs_sizing import PCS_SIZE_TIERS, determine_controller_size  # noqa: E402


# ===================================================================
# [PBT: Property 1] Smallest sufficient tier selection
# ===================================================================

class TestSmallestSufficientTierSelection:
    """[PBT: Property 1] For any integer maxNodes in [1, 2047],
    determine_controller_size returns the smallest PCS tier whose
    capacity is >= maxNodes + 1.

    **Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 4.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(max_nodes=st.integers(min_value=1, max_value=2047))
    def test_returned_tier_is_smallest_sufficient(self, max_nodes):
        """The returned tier's capacity >= maxNodes + 1, and no smaller
        tier has sufficient capacity.

        **Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 4.3**
        """
        result = determine_controller_size(max_nodes)
        total_managed = max_nodes + 1

        # Build a lookup from tier name to capacity
        tier_capacities = {name: cap for name, cap in PCS_SIZE_TIERS}

        # The returned tier must have sufficient capacity
        assert tier_capacities[result] >= total_managed, (
            f"Tier {result} (capacity {tier_capacities[result]}) "
            f"cannot handle {total_managed} managed instances "
            f"(maxNodes={max_nodes})"
        )

        # Find the index of the returned tier
        tier_names = [name for name, _ in PCS_SIZE_TIERS]
        result_idx = tier_names.index(result)

        # No smaller tier should have sufficient capacity
        if result_idx > 0:
            smaller_tier_name, smaller_tier_cap = PCS_SIZE_TIERS[result_idx - 1]
            assert smaller_tier_cap < total_managed, (
                f"Smaller tier {smaller_tier_name} (capacity {smaller_tier_cap}) "
                f"could handle {total_managed} managed instances, but "
                f"{result} was returned instead (maxNodes={max_nodes})"
            )


import pytest

from errors import ValidationError  # noqa: E402


# ===================================================================
# [PBT: Property 2] Over-capacity rejection
# ===================================================================

class TestOverCapacityRejection:
    """[PBT: Property 2] For any integer maxNodes greater than 2,047,
    determine_controller_size SHALL raise a ValidationError.

    **Validates: Requirements 1.4, 3.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(max_nodes=st.integers(min_value=2048, max_value=100_000))
    def test_over_capacity_raises_validation_error(self, max_nodes):
        """Over-capacity maxNodes values must be rejected with
        ValidationError.

        **Validates: Requirements 1.4, 3.3**
        """
        with pytest.raises(ValidationError):
            determine_controller_size(max_nodes)


# ===================================================================
# [PBT: Property 3] Non-positive input rejection
# ===================================================================

class TestNonPositiveInputRejection:
    """[PBT: Property 3] For any integer maxNodes less than 1,
    determine_controller_size SHALL raise a ValidationError.

    **Validates: Requirements 3.1**
    """

    @settings(max_examples=100, deadline=None)
    @given(max_nodes=st.integers(max_value=0))
    def test_non_positive_raises_validation_error(self, max_nodes):
        """Non-positive maxNodes values must be rejected with
        ValidationError.

        **Validates: Requirements 3.1**
        """
        with pytest.raises(ValidationError):
            determine_controller_size(max_nodes)


# ===================================================================
# [PBT: Property 4] Non-integer input rejection
# ===================================================================

class TestNonIntegerInputRejection:
    """[PBT: Property 4] For any value that is not an integer (floats,
    strings, None, booleans), determine_controller_size SHALL raise a
    ValidationError.

    **Validates: Requirements 3.2**
    """

    @settings(max_examples=100, deadline=None)
    @given(value=st.one_of(st.floats(), st.text(), st.none(), st.booleans()))
    def test_non_integer_raises_validation_error(self, value):
        """Non-integer inputs must be rejected with ValidationError.

        **Validates: Requirements 3.2**
        """
        with pytest.raises(ValidationError):
            determine_controller_size(value)
