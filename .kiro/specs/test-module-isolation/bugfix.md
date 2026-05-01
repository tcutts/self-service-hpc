# Bugfix Requirements Document

## Introduction

When running the full Python test suite (`pytest tests/ test/lambda/ -v`), 26 tests fail due to cross-contamination between test files via `sys.modules`. The project has 5+ Lambda packages that each contain identically-named modules (`errors.py`, `auth.py`, `handler.py`, etc.). Each test file in the `tests/` directory passes in isolation, but when run together in the same pytest process, one test file's imports poison `sys.modules` for subsequent test files.

The `test/lambda/` directory already has a robust solution via `conftest.py` with `_load_module_from()` using `importlib.util.spec_from_file_location()`. However, the `tests/` directory has no shared `conftest.py` and instead relies on fragile, inconsistent manual `sys.modules` clearing at the top of each file. This manual approach is order-dependent, incomplete, and breaks when pytest collects files in a different order than expected.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN multiple test files in `tests/` are collected by pytest in a single process AND those files import identically-named modules from different Lambda packages (e.g., `errors.py` from `cluster_operations` vs `errors.py` from `project_management`) THEN the system uses stale cached modules from `sys.modules` that were loaded by a previously-collected test file, causing import resolution to return the wrong module

1.2 WHEN a test file in `tests/` uses manual `sys.modules` clearing guards (e.g., checking `if "cluster_operations" not in _errors_file`) THEN the system fails to clear modules when pytest collection order differs from the assumed order, because the guard condition checks for a specific prior import that may not have occurred yet

1.3 WHEN a test file in `tests/` adds Lambda directories to `sys.path` with `sys.path.insert(0, ...)` AND another test file has already added a different Lambda directory for the same module name THEN the system resolves imports from the wrong Lambda directory because stale `sys.path` entries from prior test files take precedence

1.4 WHEN test files in `tests/` clear only a subset of cached modules (e.g., clearing `errors` but not `handler`, `auth`, or `cluster_creation`) THEN the system retains stale references in the uncleaned modules, causing cross-contamination through transitive imports

1.5 WHEN the full test suite is run (`pytest tests/ test/lambda/ -v`) THEN 26 tests fail with import errors, wrong module references, or incorrect behavior due to modules loaded from the wrong Lambda package

### Expected Behavior (Correct)

2.1 WHEN multiple test files in `tests/` are collected by pytest in a single process AND those files import identically-named modules from different Lambda packages THEN the system SHALL load the correct module from the intended Lambda package directory for each test file, regardless of collection order

2.2 WHEN any test file in `tests/` needs to import a module that shares a name with modules in other Lambda packages THEN the system SHALL use `importlib.util.spec_from_file_location()` to load modules by absolute file path, avoiding reliance on `sys.modules` cache or `sys.path` ordering

2.3 WHEN test files in `tests/` are run in any order within a single pytest process THEN the system SHALL produce the same test results as when each test file is run in isolation

2.4 WHEN the full test suite is run (`pytest tests/ test/lambda/ -v`) THEN the system SHALL pass all tests that pass individually, with zero failures caused by module cross-contamination

2.5 WHEN a test file in `tests/` imports modules from a specific Lambda package THEN the system SHALL ensure all transitive imports within that module also resolve to the correct Lambda package, not to identically-named modules from other packages

### Unchanged Behavior (Regression Prevention)

3.1 WHEN test files in `test/lambda/` are run using the existing `conftest.py` fixtures (`_load_module_from()`, `reload_*_modules()`) THEN the system SHALL CONTINUE TO load modules correctly via `importlib.util.spec_from_file_location()` and all existing tests SHALL CONTINUE TO pass

3.2 WHEN individual test files in `tests/` are run in isolation (`pytest tests/test_foo.py -v`) THEN the system SHALL CONTINUE TO pass each test file individually

3.3 WHEN the Makefile `test` target is run (which only runs `test/lambda/` tests) THEN the system SHALL CONTINUE TO execute successfully with no changes to behavior

3.4 WHEN property-based tests using Hypothesis are run THEN the system SHALL CONTINUE TO generate and test examples correctly with no interference from the module isolation mechanism

3.5 WHEN Lambda source modules in `lambda/*/` are imported by the test infrastructure THEN the system SHALL CONTINUE TO load them with their original module-level initialization (e.g., boto3 client creation, environment variable reads) intact
