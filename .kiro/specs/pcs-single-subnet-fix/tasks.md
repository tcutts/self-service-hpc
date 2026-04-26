# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** — Multi-Subnet CreateCluster Call
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate `create_pcs_cluster` passes all subnets instead of one
  - **Scoped PBT Approach**: Generate subnet ID lists of length 2–4 using `hypothesis.strategies`; for each, mock `pcs_client.create_cluster` and call `create_pcs_cluster` with the generated list; assert the mock received exactly one subnet (`private_subnet_ids[:1]`)
  - Create test file `test/lambda/test_property_pcs_single_subnet_bug.py`
  - Use `unittest.mock.patch` to mock `pcs_client.create_cluster` (return a fake `{"cluster": {"id": "pcs-123", "arn": "arn:..."}}` response)
  - Also mock `_update_step_progress` to avoid DynamoDB calls
  - Load `cluster_creation` module using `conftest._load_module_from` pattern
  - Use `@settings(max_examples=10)` to keep test fast per steering rules
  - Bug condition from design: `isBugCondition(event) = LENGTH(event["privateSubnetIds"]) > 1`
  - Expected behavior from design: `networking.subnetIds` passed to `create_cluster` has exactly 1 element, equal to `private_subnet_ids[0]`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct — it proves the bug exists: all subnets are passed instead of just the first)
  - Document counterexamples found (e.g., `privateSubnetIds=["subnet-a", "subnet-b"]` → mock receives `["subnet-a", "subnet-b"]` instead of `["subnet-a"]`)
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 2.1, 2.2_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** — Non-Subnet Parameters and Other Functions Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Create test file `test/lambda/test_property_pcs_single_subnet_preservation.py`
  - Use `unittest.mock.patch` to mock `pcs_client.create_cluster`, `pcs_client.create_compute_node_group`, `fsx_client.create_file_system`, and `_update_step_progress`
  - Load `cluster_creation` module using `conftest._load_module_from` pattern
  - Use `@settings(max_examples=10)` to keep tests fast per steering rules
  - **Observation 1**: Call `create_pcs_cluster` with a single-subnet event on unfixed code; observe that `networking.subnetIds` receives `["subnet-a"]` — this is the non-bug-condition case (`LENGTH(privateSubnetIds) == 1`)
  - **Observation 2**: Call `create_pcs_cluster` on unfixed code; observe that `clusterName`, `scheduler`, `size`, `securityGroupIds`, `slurmConfiguration`, and `tags` are passed unchanged
  - **Observation 3**: Call `create_compute_node_group` on unfixed code; observe that `subnetIds` receives the full `private_subnet_ids` list
  - **Observation 4**: Call `create_fsx_filesystem` on unfixed code; observe that `SubnetIds` receives `[private_subnet_ids[0]]`
  - Write property-based tests:
    - **Test A**: For single-subnet events (non-bug-condition), `create_pcs_cluster` passes the single subnet correctly
    - **Test B**: For all events, non-subnet parameters (`clusterName`, `scheduler`, `size`, `securityGroupIds`, `slurmConfiguration`, `tags`) are passed identically to `create_cluster`
    - **Test C**: For all events, `create_compute_node_group` passes the full `private_subnet_ids` list to `subnetIds`
    - **Test D**: For all events, `create_fsx_filesystem` passes `[private_subnet_ids[0]]` to `SubnetIds`
  - Verify all tests pass on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Fix for PCS single-subnet CreateCluster bug

  - [x] 3.1 Implement the fix
    - In `lambda/cluster_operations/cluster_creation.py`, function `create_pcs_cluster` (line ~504)
    - Change `"subnetIds": private_subnet_ids,` to `"subnetIds": private_subnet_ids[:1],`
    - This passes only the first subnet as a single-element list to the PCS `CreateCluster` API
    - No other files or functions require changes
    - _Bug_Condition: isBugCondition(event) where LENGTH(event["privateSubnetIds"]) > 1_
    - _Expected_Behavior: networking.subnetIds passed to CreateCluster has exactly 1 element equal to private_subnet_ids[0]_
    - _Preservation: create_compute_node_group still passes full private_subnet_ids; create_login_node_group still passes full public_subnet_ids; create_fsx_filesystem still uses private_subnet_ids[0]; retry logic unchanged; all non-subnet CreateCluster parameters unchanged_
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** — Multi-Subnet CreateCluster Call
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior (exactly one subnet passed)
    - When this test passes, it confirms the expected behavior is satisfied
    - Run `pytest test/lambda/test_property_pcs_single_subnet_bug.py -v`
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** — Non-Subnet Parameters and Other Functions Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run `pytest test/lambda/test_property_pcs_single_subnet_preservation.py -v`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all preservation tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 4. Checkpoint — Ensure all tests pass
  - Run full test suite: `pytest test/lambda/test_property_pcs_single_subnet_bug.py test/lambda/test_property_pcs_single_subnet_preservation.py -v`
  - Ensure all property-based and preservation tests pass
  - Ensure no regressions in existing cluster operations tests: `pytest test/lambda/test_unit_cluster_operations.py -v`
  - Ask the user if questions arise
