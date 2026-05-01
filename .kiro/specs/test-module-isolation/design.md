# Test Module Isolation Bugfix Design

## Overview

The test suite in `tests/` suffers from cross-contamination between test files when run together in a single pytest process. Multiple Lambda packages share identically-named modules (`errors.py`, `auth.py`, `handler.py`, etc.), and the current approach of manually manipulating `sys.path` and `sys.modules` at the top of each test file is fragile, order-dependent, and incomplete. The fix introduces a shared `tests/conftest.py` with a `load_lambda_module()` helper that uses `importlib.util.spec_from_file_location()` to load modules by absolute file path — the same proven pattern already working in `test/lambda/conftest.py`.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — when two or more test files in `tests/` are collected by pytest in a single process and they import identically-named modules from different Lambda packages, causing `sys.modules` cache collisions
- **Property (P)**: The desired behavior — each test file loads the correct module from the intended Lambda package directory regardless of collection order, using path-based imports
- **Preservation**: Existing test behavior when files are run in isolation, the `test/lambda/conftest.py` infrastructure, and the Makefile `test` target must remain unchanged
- **`_load_module_from()`**: The function in `test/lambda/conftest.py` that uses `importlib.util.spec_from_file_location()` to load a module by absolute file path, avoiding `sys.path` collisions
- **`load_lambda_module()`**: The new fixture/helper to be created in `tests/conftest.py` that provides the same isolation capability to all test files under `tests/`
- **Transitive imports**: When a loaded module (e.g., `cluster_creation.py`) does `from errors import ...`, it must resolve to the correct `errors.py` from the same Lambda package

## Bug Details

### Bug Condition

The bug manifests when multiple test files in `tests/` are collected by pytest in a single process and those files import identically-named modules from different Lambda packages. The manual `sys.path.insert()` + `sys.modules` clearing approach is order-dependent, incomplete (doesn't clear all transitive modules), and uses guard conditions that assume a specific prior import state.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type TestSession (pytest collection of multiple test files)
  OUTPUT: boolean
  
  RETURN input.testFiles.count > 1
         AND EXISTS file_a, file_b IN input.testFiles WHERE
             file_a.importsModule(name) FROM package_x
             AND file_b.importsModule(name) FROM package_y
             AND package_x != package_y
             AND name IN ['errors', 'handler', 'auth', 'cluster_names',
                          'cluster_creation', 'cluster_destruction', 'lifecycle',
                          'templates', 'pcs_sizing', 'pcs_versions']
         AND file_a.collectedBefore(file_b)
         AND sys.modules[name].__file__ RESOLVES TO package_x (not package_y)
END FUNCTION
```

### Examples

- `test_pcs_sizing.py` loads `errors` from `cluster_operations/`, then `test_sfn_project_consolidation_properties.py` tries to import `errors` from `project_management/` but gets the cached `cluster_operations/errors.py` — **ImportError or wrong exception class**
- `test_connection_info_properties.py` loads `handler` from `cluster_operations/`, then `test_validate_ami_available.py` imports `templates` which does `from errors import ...` and gets the wrong `errors` module — **class identity mismatch on ValidationError**
- `test_bug_condition_launch_template.py` adds `_TEMPLATE_MGMT_DIR` and `_CLUSTER_OPS_DIR` to `sys.path`, then `test_sfn_error_propagation_properties.py` clears some modules but not all transitive dependencies — **stale references in already-loaded modules**
- Running `pytest tests/ -v` produces 26 failures; running each file individually with `pytest tests/test_foo.py -v` produces 0 failures — **order-dependent behavior**

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The `test/lambda/conftest.py` file and all tests under `test/lambda/` must continue to work exactly as before with no modifications
- Individual test files in `tests/` must continue to pass when run in isolation
- The Makefile `test` target (which only runs `test/lambda/` tests) must continue to execute successfully
- Hypothesis property-based tests must continue to generate and test examples correctly
- Lambda source modules must continue to load with their original module-level initialization (boto3 clients, env var reads) intact
- The existing `tests/unit/conftest.py` (Hypothesis settings) must remain unchanged

**Scope:**
All inputs that do NOT involve running multiple test files together in a single pytest process should be completely unaffected by this fix. This includes:
- Running a single test file in isolation
- Running tests under `test/lambda/` (which already has proper isolation)
- Running the Makefile `test` target
- Any non-Python test execution (jest, CDK tests)

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **`sys.modules` Cache Pollution**: When pytest collects multiple test files in one process, module-level import code runs at collection time. The first file to import `errors` (or `handler`, `auth`, etc.) registers it in `sys.modules`. Subsequent files that need a *different* `errors` module get the cached version unless they explicitly clear it.

2. **Incomplete `sys.modules` Clearing**: The manual guards (e.g., `if "cluster_operations" not in _errors_file: del sys.modules["errors"]`) only clear the top-level module. They don't clear transitive dependencies — if `cluster_creation` was already loaded with a reference to the wrong `errors`, clearing `errors` alone doesn't fix the stale reference inside `cluster_creation`.

3. **Order-Dependent Guard Conditions**: Guards like `if _cached_errors is not None` assume a specific prior state. If pytest collects files in a different order (alphabetical, random with `pytest-randomly`), the guard may not trigger when needed or may trigger unnecessarily.

4. **`sys.path` Accumulation**: Each test file inserts directories at position 0 of `sys.path`. Over a session, `sys.path` accumulates entries from all test files, making import resolution unpredictable for any module not explicitly cleared from `sys.modules`.

5. **No Isolation Between Test Files**: Unlike `test/lambda/conftest.py` which uses `importlib.util.spec_from_file_location()` to load modules by absolute path (bypassing `sys.path` and `sys.modules` entirely), the `tests/` directory has no shared infrastructure for isolated loading.

## Correctness Properties

Property 1: Bug Condition - Module Isolation Under Concurrent Collection

_For any_ pytest session collecting two or more test files from `tests/` that import identically-named modules from different Lambda packages, the `load_lambda_module()` helper SHALL return the correct module loaded from the specified Lambda package directory, with all transitive imports resolving within that same package.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Individual Test File Behavior

_For any_ test file in `tests/` that previously passed when run in isolation, the refactored test file using `load_lambda_module()` SHALL produce the same test results (same assertions pass, same mocks work, same imports available), preserving all existing test behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `tests/conftest.py` (NEW)

**Function**: `load_lambda_module(package_name, module_name)`

**Specific Changes**:

1. **Create `tests/conftest.py`** with a `load_lambda_module()` helper function:
   - Uses `importlib.util.spec_from_file_location()` to load modules by absolute file path
   - Accepts a `package_name` (e.g., `"cluster_operations"`) and `module_name` (e.g., `"errors"`)
   - Resolves the file path as `lambda/{package_name}/{module_name}.py`
   - Registers the module in `sys.modules` under a namespaced key (e.g., `cluster_operations.errors`) to avoid collisions
   - Also registers under the bare name temporarily during loading so transitive `from errors import ...` statements resolve correctly within the same package
   - Returns the loaded module object

2. **Pre-load shared modules**: The conftest should provide a mechanism to load `lambda/shared/` modules (e.g., `validators`, `authorization`, `pcs_versions`) that are used as a common layer across packages.

3. **Autouse fixture for `sys.modules` cleanup**: An autouse session-scoped or module-scoped fixture that snapshots `sys.modules` before each test module and restores it after, preventing cross-contamination between test files.

4. **Update all 24 test files in `tests/`** to replace manual `sys.path`/`sys.modules` manipulation with calls to `load_lambda_module()`:
   - Remove `sys.path.insert(0, ...)` blocks
   - Remove `sys.modules` clearing guards
   - Replace with `load_lambda_module("cluster_operations", "cluster_destruction")` etc.

5. **Update all 22 test files in `tests/unit/`** with the same pattern (using relative paths from `tests/unit/` to `lambda/`).

6. **Handle Pattern C** (`test_sfn_error_propagation_properties.py`): This file needs modules from both `cluster_operations` and `project_management` simultaneously with aliased error modules. The helper must support loading the same module name from different packages with distinct references (e.g., `cluster_errors = load_lambda_module("cluster_operations", "errors")`).

7. **Handle Pattern F** (`test_connection_info_properties.py`): This file needs `handler.py` specifically from `cluster_operations`, not `template_management`. The path-based loading inherently solves this.

8. **Preserve Hypothesis settings**: Keep the existing `tests/unit/conftest.py` with its Hypothesis profile configuration unchanged.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, demonstrate the bug exists on unfixed code by running the full test suite, then verify the fix eliminates all 26 failures while preserving individual test behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Run the full test suite (`pytest tests/ test/lambda/ -v`) on the UNFIXED code and capture the 26 failures. Analyze the failure messages to confirm they are caused by `sys.modules` cross-contamination (wrong module loaded, class identity mismatches, ImportError for attributes that exist in the correct module but not the cached one).

**Test Cases**:
1. **Cross-Package Error Module Test**: Run `test_pcs_sizing.py` followed by `test_sfn_project_consolidation_properties.py` in one session (will fail on unfixed code)
2. **Handler Module Collision Test**: Run `test_connection_info_properties.py` followed by any template_management test in one session (will fail on unfixed code)
3. **Transitive Import Test**: Run `test_sfn_consolidation_properties.py` followed by `test_sfn_error_propagation_properties.py` in one session (will fail on unfixed code)
4. **Full Suite Test**: Run `pytest tests/ -v` and observe 26 failures (will fail on unfixed code)

**Expected Counterexamples**:
- `ImportError: cannot import name 'ValidationError' from 'errors'` (wrong errors module cached)
- `AssertionError: isinstance check fails` (class loaded from wrong package)
- `AttributeError: module 'handler' has no attribute 'build_connection_info'` (wrong handler cached)
- Possible causes: `sys.modules` caching, `sys.path` ordering, incomplete module clearing

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed test infrastructure produces correct module loading.

**Pseudocode:**
```
FOR ALL test_session WHERE isBugCondition(test_session) DO
  result := run_pytest(test_session, with_new_conftest=True)
  ASSERT result.failures == 0
  ASSERT result.all_modules_loaded_from_correct_package()
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed test files produce the same results as the original test files.

**Pseudocode:**
```
FOR ALL test_file IN tests/ WHERE NOT isBugCondition({test_file}) DO
  ASSERT run_pytest_single(test_file, original) == run_pytest_single(test_file, fixed)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It can generate many different test file orderings to verify isolation holds
- It catches edge cases in module loading that manual tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Run each test file individually on BOTH unfixed and fixed code, comparing results. Then run the full suite on fixed code and verify zero failures.

**Test Cases**:
1. **Individual File Preservation**: Run each of the 47 test files individually and verify same pass/fail result before and after the fix
2. **Hypothesis Test Preservation**: Run property-based tests individually and verify they still generate examples and find the same results
3. **test/lambda/ Preservation**: Run `pytest test/lambda/ -v` and verify zero regressions
4. **Makefile Target Preservation**: Run `make test` and verify it still passes

### Unit Tests

- Test that `load_lambda_module("cluster_operations", "errors")` returns a module whose `__file__` points to `lambda/cluster_operations/errors.py`
- Test that `load_lambda_module("project_management", "errors")` returns a different module than `load_lambda_module("cluster_operations", "errors")`
- Test that transitive imports resolve correctly (loading `cluster_creation` gets `cluster_operations/errors`, not `project_management/errors`)
- Test that loading the same module twice returns a consistent reference
- Test that `load_lambda_module("shared", "validators")` works for shared-only imports

### Property-Based Tests

- Generate random orderings of test file pairs that import conflicting modules and verify isolation holds for all orderings
- Generate random sequences of `load_lambda_module()` calls with different package/module combinations and verify each returns the correct module
- Test that for any sequence of module loads, `module.__file__` always points to the expected package directory

### Integration Tests

- Run the full test suite (`pytest tests/ test/lambda/ -v`) and verify zero failures
- Run with `pytest-randomly` to verify order-independence
- Run `tests/` and `tests/unit/` and `tests/integration/` together in one session
