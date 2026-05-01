"""Shared pytest configuration and module-loading helpers for tests/.

Provides ``load_lambda_module()`` — a path-based module loader that avoids
``sys.modules`` cross-contamination when multiple test files import
identically-named modules from different Lambda packages.

This mirrors the proven ``_load_module_from()`` pattern in
``test/lambda/conftest.py`` and makes it available to every test file
under ``tests/``.

Also configures Hypothesis with ``deadline=None`` to prevent flaky
DeadlineExceeded errors from cold module imports.
"""

import importlib
import importlib.util
import os
import sys

from hypothesis import settings

# ---------------------------------------------------------------------------
# Hypothesis settings — match tests/unit/conftest.py pattern
# ---------------------------------------------------------------------------
settings.register_profile("ci", deadline=None)
settings.load_profile("ci")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LAMBDA_ROOT = os.path.join(_PROJECT_ROOT, "lambda")
_SHARED_DIR = os.path.join(_LAMBDA_ROOT, "shared")

_SHARED_MODULE_NAMES = ("authorization", "pcs_versions", "validators")


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

# Cache: (package_name, module_name) → loaded module object.
# Ensures that multiple test files loading the same module get the same
# object, so ``patch("module_name.attr", ...)`` targets the correct instance.
# Stored as a module attribute on a sentinel module in sys.modules so the
# cache is shared across all conftest instances (pytest loads tests/conftest.py
# separately from unit test files that import it via importlib).
_CACHE_KEY = "_load_lambda_module_cache_holder"
if _CACHE_KEY not in sys.modules:
    import types
    _holder = types.ModuleType(_CACHE_KEY)
    _holder.cache = {}  # type: ignore[attr-defined]
    sys.modules[_CACHE_KEY] = _holder
_module_cache: dict = sys.modules[_CACHE_KEY].cache  # type: ignore[union-attr]


def load_lambda_module(package_name: str, module_name: str, *, force_reload: bool = False):
    """Load a Lambda module by absolute file path, avoiding sys.path collisions.

    Multiple Lambda packages share identically-named files (``errors.py``,
    ``handler.py``, ``auth.py``, etc.).  Using
    ``importlib.util.spec_from_file_location`` lets us load the correct one
    without relying on ``sys.path`` ordering or ``sys.modules`` cache state.

    Results are cached by ``(package_name, module_name)`` so that repeated
    calls return the same module object.  This is critical when test files
    use string-based ``patch("module_name.attr", ...)`` — the patch must
    target the same object that the function under test references.

    Parameters
    ----------
    package_name:
        The Lambda package directory name (e.g. ``"cluster_operations"``).
    module_name:
        The module file name without ``.py`` (e.g. ``"errors"``).
    force_reload:
        If True, bypass the cache and re-execute the module.  Use this
        when the module must be loaded inside a ``mock_aws()`` context
        so that module-level boto3 clients point to moto.

    Returns
    -------
    module
        The loaded module object.  Also registered in ``sys.modules``
        under the bare *module_name* so that transitive
        ``from errors import ...`` statements inside the loaded module
        resolve correctly.

    Examples
    --------
    >>> errors = load_lambda_module("cluster_operations", "errors")
    >>> errors.__file__  # doctest: +SKIP
    '.../lambda/cluster_operations/errors.py'

    Pattern C — same module name from different packages::

        cluster_errors = load_lambda_module("cluster_operations", "errors")
        project_errors = load_lambda_module("project_management", "errors")
        # cluster_errors and project_errors are distinct module objects
    """
    key = (package_name, module_name)
    if not force_reload:
        cached = _module_cache.get(key)
        if cached is not None:
            # Re-register under bare name so transitive imports resolve
            sys.modules[module_name] = cached
            return cached

    filepath = os.path.join(_LAMBDA_ROOT, package_name, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    mod = importlib.util.module_from_spec(spec)
    # Register under bare name so intra-package imports resolve correctly
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    if not force_reload:
        _module_cache[key] = mod
    return mod


def _ensure_shared_modules():
    """Pre-load shared Lambda Layer modules into ``sys.modules``.

    In production the Lambda Layer puts ``lambda/shared/`` on the Python
    path.  In tests we replicate that by loading the shared modules
    explicitly so that ``from authorization import *`` (used inside
    per-package ``auth.py`` files) resolves correctly.

    Uses the same cache as ``load_lambda_module()`` to avoid re-loading.
    """
    for name in _SHARED_MODULE_NAMES:
        load_lambda_module("shared", name)


# ---------------------------------------------------------------------------
# sys.modules isolation
# ---------------------------------------------------------------------------
# Each test file calls load_lambda_module() at module level, which
# overwrites sys.modules[bare_name] with the correct module for that
# file's package.  This is sufficient to prevent cross-contamination
# because:
#   1. load_lambda_module() always loads from the absolute file path
#   2. It always overwrites sys.modules[module_name] before exec_module
#   3. Transitive imports resolve against the freshly-registered module
#
# No fixture-based cleanup is needed — the explicit overwrites in each
# test file's module-level code handle isolation.
