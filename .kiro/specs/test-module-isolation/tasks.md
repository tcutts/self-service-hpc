# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Module Cross-Contamination Under Multi-File Collection
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate `sys.modules` cross-contamination between test files
  - **Scoped PBT Approach**: Scope the property to concrete failing cases — run pairs of test files that import identically-named modules from different Lambda packages in a single pytest subprocess
  - Create `tests/test_bug_condition_module_isolation.py` with a property-based test using Hypothesis
  - Bug Condition from design: `isBugCondition(input)` where `input.testFiles.count > 1 AND EXISTS file_a, file_b importing same module name from different packages AND file_a collected before file_b`
  - Test strategy: Use `subprocess` to invoke pytest with specific file pairs that are known to conflict (e.g., `test_pcs_sizing.py` + `test_sfn_project_consolidation_properties.py`, `test_connection_info_properties.py` + `test_validate_ami_available.py`)
  - Generate random orderings of conflicting test file pairs using Hypothesis `st.permutations` or `st.sampled_from`
  - Assert: pytest exit code == 0 for each pair (expected behavior: all pass together)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (pytest returns non-zero exit code for conflicting pairs, confirming the bug exists)
  - Document counterexamples found (which file pairs fail and with what error messages)
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Individual Test File Behavior Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Create `tests/test_preservation_module_isolation.py` with property-based tests using Hypothesis
  - Observe: Run each test file in `tests/` individually on UNFIXED code and record pass/fail results
  - Observe: Run `pytest test/lambda/ -v` and confirm all tests pass
  - Observe: Run property-based test files individually and confirm Hypothesis generates examples correctly
  - Write property-based test: for all test files in `tests/` (sampled via `st.sampled_from`), running the file individually via subprocess produces exit code 0
  - Write property-based test: for all test files in `test/lambda/`, running them produces exit code 0 (confirms `test/lambda/conftest.py` is unaffected)
  - Verify tests pass on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (each file passes individually, confirming baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Implement module isolation fix

  - [x] 3.1 Create `tests/conftest.py` with `load_lambda_module()` helper
    - Create `tests/conftest.py` with `load_lambda_module(package_name, module_name)` function
    - Use `importlib.util.spec_from_file_location()` to load modules by absolute file path (matching `test/lambda/conftest.py` pattern)
    - Resolve file path as `lambda/{package_name}/{module_name}.py` relative to project root
    - Register module in `sys.modules` under bare name so transitive `from errors import ...` resolves correctly
    - Support loading from `shared` package (e.g., `load_lambda_module("shared", "validators")`)
    - Support Pattern C: allow loading same module name from different packages with distinct references (e.g., `cluster_errors = load_lambda_module("cluster_operations", "errors")`)
    - Add `_ensure_shared_modules()` helper to pre-load `lambda/shared/` modules (`authorization`, `pcs_versions`, `validators`)
    - Add autouse session-scoped fixture or module-level cleanup to snapshot/restore `sys.modules` between test modules
    - Include Hypothesis settings profile (deadline=None) matching `tests/unit/conftest.py` pattern
    - Do NOT modify `test/lambda/conftest.py` or `tests/unit/conftest.py`
    - _Bug_Condition: isBugCondition(input) where multiple test files import same module name from different packages_
    - _Expected_Behavior: load_lambda_module() returns correct module from specified package directory regardless of prior imports_
    - _Preservation: test/lambda/conftest.py unchanged, tests/unit/conftest.py unchanged, individual test behavior preserved_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.5_

  - [x] 3.2 Update root-level test files (Pattern A — cluster_operations only)
    - Update the following files to replace `sys.path.insert()` + `sys.modules` clearing with `load_lambda_module()` calls:
      - `tests/test_cluster_destruction_properties.py`
      - `tests/test_deletion_progress_properties.py`
      - `tests/test_pcs_sizing.py`
      - `tests/test_pcs_sizing_properties.py`
      - `tests/test_pcs_sizing_integration.py`
      - `tests/test_sfn_consolidation_properties.py`
      - `tests/test_sfn_destruction_consolidation_properties.py`
      - `tests/test_bug_condition_cluster_destruction_hangs.py`
      - `tests/test_preservation_cluster_destruction_hangs.py`
      - `tests/test_scheduler_log_delivery_properties.py`
      - `tests/test_notification_properties.py`
      - `tests/test_login_node_event_properties.py`
      - `tests/test_deregister_cluster_name_properties.py`
      - `tests/test_ami_validation_cluster_creation.py`
    - Remove all `sys.path.insert(0, ...)` blocks
    - Remove all `sys.modules` clearing guards (e.g., `if _cached_errors is not None` blocks)
    - Replace with `load_lambda_module("cluster_operations", "module_name")` calls
    - Ensure `_ensure_shared_modules()` is called where shared modules are needed
    - _Requirements: 2.1, 2.2, 2.3, 2.5_

  - [x] 3.3 Update root-level test files (Pattern B — project_management only)
    - Update `tests/test_sfn_project_consolidation_properties.py`
    - Replace sys.path/sys.modules manipulation with `load_lambda_module("project_management", ...)` calls
    - Load lifecycle, project_deploy, project_update, project_destroy, errors from project_management
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.4 Update root-level test files (Pattern C — both packages)
    - Update `tests/test_sfn_error_propagation_properties.py`
    - Load cluster_operations errors as `cluster_errors = load_lambda_module("cluster_operations", "errors")`
    - Load project_management errors as `project_errors = load_lambda_module("project_management", "errors")`
    - Load cluster_creation, cluster_destruction from cluster_operations
    - Load lifecycle, project_deploy, project_update, project_destroy from project_management
    - Ensure both error modules coexist without collision
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.5 Update root-level test files (Pattern D — template_management)
    - Update the following files:
      - `tests/test_bug_condition_launch_template.py`
      - `tests/test_preservation_launch_template.py`
      - `tests/test_validate_ami_available.py`
    - Replace sys.path/sys.modules manipulation with `load_lambda_module("template_management", ...)` calls
    - Load templates, errors, ami_lookup from template_management
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.6 Update root-level test files (Pattern E — shared only)
    - Update the following files:
      - `tests/test_posix_username_validation.py`
      - `tests/test_posix_username_validation_properties.py`
    - Replace sys.path/sys.modules manipulation with `load_lambda_module("shared", "validators")` calls
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.7 Update root-level test files (Pattern F — handler from cluster_operations)
    - Update `tests/test_connection_info_properties.py`
    - Replace sys.path/sys.modules manipulation with `load_lambda_module("cluster_operations", "handler")` call
    - Ensure handler.py loads with correct transitive imports (errors, clusters, auth from cluster_operations)
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.8 Update root-level test files (Pattern A — bug condition/preservation tests)
    - Update the following files:
      - `tests/test_bug_condition_userdata_crash.py`
      - `tests/test_preservation_userdata_crash.py`
    - Replace sys.path/sys.modules manipulation with `load_lambda_module("cluster_operations", ...)` calls
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.9 Update `tests/unit/` test files
    - Update the following files to replace sys.path/sys.modules manipulation with `load_lambda_module()` calls:
      - `tests/unit/test_cluster_destruction.py`
      - `tests/unit/test_deregister_cluster_name.py`
      - `tests/unit/test_cleanup_scheduler_log_delivery.py`
      - `tests/unit/test_validate_template_version.py`
      - `tests/unit/test_authorization.py`
      - `tests/unit/test_pcs_versions.py`
      - `tests/unit/test_login_node_refresh.py`
      - `tests/unit/test_record_cluster_connection.py`
      - `tests/unit/test_create_pcs_cluster_version.py`
      - And any other files in `tests/unit/` that use `sys.path.insert()` or `sys.modules` manipulation
    - The `tests/conftest.py` helper is available to `tests/unit/` via pytest's conftest hierarchy
    - Do NOT modify `tests/unit/conftest.py` (Hypothesis settings must remain unchanged)
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 3.5_

  - [x] 3.10 Update `tests/integration/test_destruction_workflow.py`
    - Replace sys.path/sys.modules manipulation with `load_lambda_module("cluster_operations", ...)` calls
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.11 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Module Cross-Contamination Under Multi-File Collection
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior (all conflicting file pairs pass together)
    - When this test passes, it confirms the expected behavior is satisfied
    - Run `tests/test_bug_condition_module_isolation.py`
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed — all file pairs now pass together)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 3.12 Verify preservation tests still pass
    - **Property 2: Preservation** - Individual Test File Behavior Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run `tests/test_preservation_module_isolation.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions — each file still passes individually)
    - Confirm all tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full test suite: `pytest tests/ test/lambda/ -v`
  - Verify zero failures caused by module cross-contamination
  - Run with different collection orders to confirm order-independence
  - Run `pytest test/lambda/ -v` separately to confirm no regressions in existing infrastructure
  - Ensure all tests pass, ask the user if questions arise.
