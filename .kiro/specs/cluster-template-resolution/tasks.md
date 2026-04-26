# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Template Fields Missing From PCS Output and Dispatch Table
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to concrete failing cases — call `create_pcs_cluster` with an event containing `templateId` and verify the output lacks template-driven fields; also verify `_STEP_DISPATCH` has no `resolve_template` entry
  - Test file: `test/lambda/test_property_template_resolution_bug.py`
  - Load `cluster_creation` module using `_load_module_from` pattern from conftest (same as `test_property_pcs_single_subnet_bug.py`)
  - Property test with hypothesis: for any event with a valid `templateId`, `create_pcs_cluster` returns an event that does NOT contain `loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, or `purchaseOption` (from Bug Condition `isBugCondition` in design)
  - Assert that `_STEP_DISPATCH` does not contain a `"resolve_template"` key
  - Mock `pcs_client.create_cluster` to return a fake response with `pcsClusterId` and `pcsClusterArn`
  - Patch `_update_step_progress` to avoid DynamoDB calls
  - Use `@settings(max_examples=10, deadline=None)` to keep tests fast
  - Run test on UNFIXED code — expect FAILURE (this confirms the bug exists)
  - Document counterexamples found (e.g., "`create_pcs_cluster` returns event with only `pcsClusterId` and `pcsClusterArn` — no template fields")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Existing Step Functions Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Test file: `test/lambda/test_property_template_resolution_preservation.py`
  - Load `cluster_creation` module using `_load_module_from` pattern from conftest
  - Observe on UNFIXED code: `create_pcs_cluster` adds only `pcsClusterId` and `pcsClusterArn` to the event, preserving all other keys
  - Observe on UNFIXED code: `create_login_node_group` uses `event.get("loginInstanceType", "c7g.medium")` default when field is absent
  - Observe on UNFIXED code: `create_compute_node_group` uses `event.get("instanceTypes", ["c7g.medium"])`, `event.get("maxNodes", 10)`, `event.get("minNodes", 0)`, `event.get("purchaseOption", "ONDEMAND")` defaults when fields are absent
  - Observe on UNFIXED code: `_STEP_DISPATCH` contains all existing step names (`validate_and_register_name`, `check_budget_breach`, `create_fsx_filesystem`, `check_fsx_status`, `create_fsx_dra`, `create_pcs_cluster`, `create_login_node_group`, `create_compute_node_group`, `create_pcs_queue`, `tag_resources`, `record_cluster`, `handle_creation_failure`)
  - Write property-based test: for any valid event, `create_pcs_cluster` preserves all original event keys and only adds `pcsClusterId` and `pcsClusterArn`
  - Write property-based test: for any event without template fields, `create_compute_node_group` uses the expected defaults for `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption`
  - Write assertion: `_STEP_DISPATCH` contains all 12 existing step names
  - Mock AWS clients (`pcs_client`, `_update_step_progress`) to avoid real API calls
  - Use `@settings(max_examples=10, deadline=None)` to keep tests fast
  - Run tests on UNFIXED code — expect all tests to PASS (confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Fix for missing template resolution step in cluster creation workflow

  - [x] 3.1 Add `resolve_template` function in `cluster_creation.py`
    - Read `TEMPLATES_TABLE_NAME` from environment: `os.environ.get("TEMPLATES_TABLE_NAME", "ClusterTemplates")`
    - Implement `resolve_template(event)` function that:
      - Extracts `templateId` from the event
      - If `templateId` is non-empty, reads the template from ClusterTemplates table using `PK=TEMPLATE#{templateId}`, `SK=METADATA`
      - Raises `ValidationError` if the template is not found (fail fast with clear error)
      - Adds `loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption` from the template to the event
      - If `templateId` is empty/missing, adds sensible defaults: `loginInstanceType="c7g.medium"`, `instanceTypes=["c7g.medium"]`, `maxNodes=10`, `minNodes=0`, `purchaseOption="ONDEMAND"`
      - Returns the augmented event
    - _Bug_Condition: isBugCondition(event) where event has templateId but no loginInstanceType, instanceTypes, maxNodes, minNodes, purchaseOption_
    - _Expected_Behavior: resolve_template reads ClusterTemplates table and adds template fields to event_
    - _Preservation: Existing step functions remain unchanged — resolve_template is a new addition_
    - _Requirements: 2.1, 2.3, 2.4_

  - [x] 3.2 Register `resolve_template` in `_STEP_DISPATCH`
    - Add `"resolve_template": resolve_template` to the `_STEP_DISPATCH.update(...)` call
    - _Requirements: 2.1_

  - [x] 3.3 Add `TEMPLATES_TABLE_NAME` env var to `clusterCreationStepLambda` in `foundation-stack.ts`
    - Add `TEMPLATES_TABLE_NAME: this.clusterTemplatesTable.tableName` to the `clusterCreationStepLambda` environment variables
    - _Requirements: 2.1_

  - [x] 3.4 Grant `clusterTemplatesTable.grantReadData(clusterCreationStepLambda)` in `foundation-stack.ts`
    - Add `this.clusterTemplatesTable.grantReadData(clusterCreationStepLambda)` after the existing DynamoDB grants
    - _Requirements: 2.1_

  - [x] 3.5 Add `ResolveTemplate` LambdaInvoke step to the state machine in `foundation-stack.ts`
    - Create a new `tasks.LambdaInvoke` step named `ResolveTemplate` that invokes `clusterCreationStepLambda` with `step: 'resolve_template'`
    - Insert it into the chain between `checkBudgetBreach` and `parallelFsxAndPcs`: `validateAndRegisterName.next(checkBudgetBreach).next(resolveTemplate).next(parallelFsxAndPcs)...`
    - _Requirements: 2.1, 2.2_

  - [x] 3.6 Add catch handler for the `ResolveTemplate` step
    - Add `resolveTemplate.addCatch(failureChain, catchConfig)` alongside the existing catch handlers
    - _Requirements: 3.5_

  - [x] 3.7 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Template Fields Resolved
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior for `_STEP_DISPATCH` containing `resolve_template`
    - Note: The `create_pcs_cluster` output assertion will still show the bug condition (PCS output lacks template fields) — this is expected because `resolve_template` runs as a separate step BEFORE `create_pcs_cluster`
    - The key validation is that `_STEP_DISPATCH` now contains `"resolve_template"`
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed — `resolve_template` is now registered)
    - _Requirements: 2.1, 2.3, 2.4_

  - [x] 3.8 Verify preservation tests still pass
    - **Property 2: Preservation** - Existing Step Functions Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions — all existing steps still work identically)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full test suite: `python -m pytest test/lambda/test_property_template_resolution_bug.py test/lambda/test_property_template_resolution_preservation.py -v`
  - Ensure all property-based tests pass
  - Run the CDK test suite: `npx jest test/foundation-stack.test.ts` to verify the state machine changes compile correctly
  - Ensure all tests pass, ask the user if questions arise
