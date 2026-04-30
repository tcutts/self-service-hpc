# Implementation Plan: PCS Controller Sizing

## Overview

Replace the hardcoded `size="SMALL"` in `create_pcs_cluster` with a pure sizing function that dynamically selects the correct AWS PCS controller tier based on `maxNodes + 1` (login node). The implementation adds a new module `pcs_sizing.py`, updates the existing handler call site, and documents the sizing behavior.

## Tasks

- [x] 1. Create the `pcs_sizing` module with `determine_controller_size`
  - [x] 1.1 Create `lambda/cluster_operations/pcs_sizing.py` with the `PCS_SIZE_TIERS` constant, `MAX_SUPPORTED_MAX_NODES` constant, and the `determine_controller_size(max_nodes: int) -> str` function
    - Import `ValidationError` from `errors`
    - Define `PCS_SIZE_TIERS` as a list of `(tier_name, max_managed_instances)` tuples: `[("SMALL", 32), ("MEDIUM", 512), ("LARGE", 2048)]`
    - Define `MAX_SUPPORTED_MAX_NODES = PCS_SIZE_TIERS[-1][1] - 1` (2047)
    - Validate that `max_nodes` is an integer (not bool) — raise `ValidationError` if not
    - Validate that `max_nodes >= 1` — raise `ValidationError` if not
    - Compute `total_managed = max_nodes + 1` and validate it does not exceed `PCS_SIZE_TIERS[-1][1]` — raise `ValidationError` if it does
    - Iterate `PCS_SIZE_TIERS` and return the first tier where `total_managed <= tier_capacity`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 3.1, 3.2, 3.3, 4.1, 4.2_

  - [x] 1.2 Write property test: smallest sufficient tier selection (Property 1)
    - **Property 1: Smallest sufficient tier selection**
    - Create `tests/test_pcs_sizing_properties.py` with path setup matching existing test conventions (see `tests/test_cluster_destruction_properties.py`)
    - Use `@given(st.integers(min_value=1, max_value=2047))` to generate valid `maxNodes` values
    - Assert the returned tier's capacity >= `max_nodes + 1`
    - Assert no smaller tier has sufficient capacity (next-smaller tier capacity < `max_nodes + 1`, or no smaller tier exists)
    - Use `@settings(max_examples=100, deadline=None)`
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 4.3**

  - [x] 1.3 Write property test: over-capacity rejection (Property 2)
    - **Property 2: Over-capacity rejection**
    - In `tests/test_pcs_sizing_properties.py`, add a test using `@given(st.integers(min_value=2048, max_value=100_000))`
    - Assert `ValidationError` is raised for all over-capacity values
    - Use `@settings(max_examples=100, deadline=None)`
    - **Validates: Requirements 1.4, 3.3**

  - [x] 1.4 Write property test: non-positive input rejection (Property 3)
    - **Property 3: Non-positive input rejection**
    - In `tests/test_pcs_sizing_properties.py`, add a test using `@given(st.integers(max_value=0))`
    - Assert `ValidationError` is raised for all non-positive values
    - Use `@settings(max_examples=100, deadline=None)`
    - **Validates: Requirements 3.1**

  - [x] 1.5 Write property test: non-integer input rejection (Property 4)
    - **Property 4: Non-integer input rejection**
    - In `tests/test_pcs_sizing_properties.py`, add a test using `@given(st.one_of(st.floats(), st.text(), st.none(), st.booleans()))`
    - Assert `ValidationError` is raised for all non-integer inputs
    - Use `@settings(max_examples=100, deadline=None)`
    - **Validates: Requirements 3.2**

  - [x] 1.6 Write boundary value unit tests for `determine_controller_size`
    - Create `tests/test_pcs_sizing.py` with path setup matching existing test conventions
    - Test SMALL upper boundary: `maxNodes=31` → `"SMALL"`
    - Test MEDIUM lower boundary: `maxNodes=32` → `"MEDIUM"`
    - Test MEDIUM upper boundary: `maxNodes=511` → `"MEDIUM"`
    - Test LARGE lower boundary: `maxNodes=512` → `"LARGE"`
    - Test LARGE upper boundary: `maxNodes=2047` → `"LARGE"`
    - Test default-equivalent: `maxNodes=10` → `"SMALL"`
    - Test minimum valid: `maxNodes=1` → `"SMALL"`
    - Test over-capacity error: `maxNodes=2048` → `ValidationError`
    - Test non-positive error: `maxNodes=0` → `ValidationError`
    - Test non-integer error: `maxNodes="10"` → `ValidationError`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 3.1, 3.2, 3.3, 4.4_

- [x] 2. Checkpoint - Verify sizing module
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Integrate `determine_controller_size` into `create_pcs_cluster`
  - [x] 3.1 Update `create_pcs_cluster` in `lambda/cluster_operations/cluster_creation.py` to use dynamic sizing
    - Add `from pcs_sizing import determine_controller_size` to the imports at the top of the file
    - Inside `create_pcs_cluster`, before the retry loop, read `max_nodes = event.get("maxNodes", 10)` and call `controller_size = determine_controller_size(max_nodes)`
    - Replace the hardcoded `size="SMALL"` in the `pcs_client.create_cluster()` call with `size=controller_size`
    - The `ValidationError` from `determine_controller_size` propagates naturally — no additional error handling needed
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.2 Write integration tests for the handler-sizing wiring
    - Create `tests/test_pcs_sizing_integration.py` with path setup matching existing test conventions
    - Mock `pcs_client` and `_update_step_progress` in `cluster_creation`
    - Test that `maxNodes=100` in event causes `create_cluster` to be called with `size="MEDIUM"`
    - Test that missing `maxNodes` in event defaults to 10 and calls `create_cluster` with `size="SMALL"`
    - Test that `maxNodes=5000` in event raises `ValidationError` and `create_cluster` is never called
    - Test that the `size` parameter passed to `create_cluster` always matches `determine_controller_size` output for any valid event
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 4. Checkpoint - Verify integration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Update admin documentation
  - [x] 5.1 Create `docs/admin/pcs-controller-sizing.md` documenting the controller sizing behavior
    - Describe the three PCS controller size tiers (SMALL, MEDIUM, LARGE) with their instance limits (32, 512, 2048) and job limits (256, 8192, 16384)
    - Explain that the controller size is automatically selected based on `maxNodes + 1` (the login node is counted)
    - Include the tier selection table showing `maxNodes` ranges: 1–31 → SMALL, 32–511 → MEDIUM, 512–2047 → LARGE
    - State that the controller size cannot be changed after cluster creation
    - Document the maximum supported `maxNodes` value (2,047) and the `ValidationError` returned when exceeded
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 6. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis, consistent with existing `tests/test_cluster_destruction_properties.py`
- Test files follow the project's path setup convention for importing lambda modules
- Run tests with `.venv/bin/pytest tests/test_pcs_sizing.py -v` (keep commands under ~100 chars)
- Checkpoints ensure incremental validation after the sizing module and after integration
