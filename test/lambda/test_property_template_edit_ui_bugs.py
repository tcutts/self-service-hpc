# Feature: template-edit-ui-fixes, Property 1: Bug Condition — Modal Scroll and Detect Button Missing
"""Bug condition exploration tests that inspect the source artifacts to confirm
the two UI bugs exist in the unfixed code.

These tests parse the CSS and JavaScript source files directly.  They are
deterministic (no randomised inputs needed) because the bugs are structural:
specific CSS properties and DOM elements are simply absent.

**Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3**
"""

import os
import re
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Paths to source artifacts (relative to repo root)
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CSS_PATH = _REPO_ROOT / "frontend" / "css" / "styles.css"
_JS_PATH = _REPO_ROOT / "frontend" / "js" / "app.js"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_text(path: pathlib.Path) -> str:
    """Read a source file and return its text content."""
    return path.read_text(encoding="utf-8")


def _extract_css_rule(css_text: str, selector: str) -> str | None:
    """Extract the declaration block for *selector* from raw CSS text.

    Returns the text between the opening ``{`` and closing ``}`` for the
    first top-level rule whose selector matches exactly, or ``None`` if
    the selector is not found.
    """
    # Match the selector followed by its block.  We use a simple brace-
    # depth counter rather than a full CSS parser — sufficient for the
    # single-level rules we care about.
    pattern = re.compile(
        r"(?:^|\n)\s*" + re.escape(selector) + r"\s*\{",
        re.MULTILINE,
    )
    m = pattern.search(css_text)
    if m is None:
        return None
    start = m.end()  # just past the opening brace
    depth = 1
    pos = start
    while pos < len(css_text) and depth > 0:
        if css_text[pos] == "{":
            depth += 1
        elif css_text[pos] == "}":
            depth -= 1
        pos += 1
    return css_text[start : pos - 1]


def _extract_function_body(js_text: str, func_name: str) -> str | None:
    """Extract the body of a named JS function from raw source text.

    Handles ``function funcName(…) { … }`` declarations.  Returns the
    text between the opening and closing braces, or ``None`` if the
    function is not found.
    """
    pattern = re.compile(
        r"function\s+" + re.escape(func_name) + r"\s*\([^)]*\)\s*\{",
    )
    m = pattern.search(js_text)
    if m is None:
        return None
    start = m.end()
    depth = 1
    pos = start
    while pos < len(js_text) and depth > 0:
        if js_text[pos] == "{":
            depth += 1
        elif js_text[pos] == "}":
            depth -= 1
        pos += 1
    return js_text[start : pos - 1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBugConditionExploration:
    """Surface counterexamples proving the two UI bugs exist."""

    # -- Test 1: Modal overflow ------------------------------------------------

    def test_modal_content_has_max_height(self):
        """The `.modal-content` CSS rule SHALL contain a `max-height` property
        so the modal never exceeds the viewport.

        **Validates: Requirements 2.1**

        Counterexample on unfixed code: `.modal-content` has no `max-height`.
        """
        css = _read_text(_CSS_PATH)
        block = _extract_css_rule(css, ".modal-content")
        assert block is not None, ".modal-content rule not found in styles.css"
        assert "max-height" in block, (
            "COUNTEREXAMPLE: `.modal-content` has no `max-height` property — "
            "modal can overflow the viewport"
        )

    def test_modal_content_has_overflow_y_auto(self):
        """The `.modal-content` CSS rule SHALL contain `overflow-y: auto` so
        content scrolls when it exceeds the max-height.

        **Validates: Requirements 2.1**

        Counterexample on unfixed code: `.modal-content` has no `overflow-y`.
        """
        css = _read_text(_CSS_PATH)
        block = _extract_css_rule(css, ".modal-content")
        assert block is not None, ".modal-content rule not found in styles.css"
        assert re.search(r"overflow-y\s*:\s*auto", block), (
            "COUNTEREXAMPLE: `.modal-content` has no `overflow-y: auto` — "
            "content cannot scroll when it overflows"
        )

    # -- Test 2: Detect button markup -----------------------------------------

    def test_edit_modal_has_detect_button(self):
        """The `showEditTemplateDialog` template literal SHALL contain an
        element with id `btn-edit-detect-ami`.

        **Validates: Requirements 1.2, 2.2**

        Counterexample on unfixed code: no Detect button in edit modal.
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found in app.js"
        assert "btn-edit-detect-ami" in body, (
            "COUNTEREXAMPLE: no element with id `btn-edit-detect-ami` in "
            "the edit modal — Detect button is missing"
        )

    # -- Test 3: Detect event listener ----------------------------------------

    def test_edit_modal_has_detect_event_listener(self):
        """The `showEditTemplateDialog` function body SHALL contain an
        `addEventListener` call wired to `btn-edit-detect-ami`.

        **Validates: Requirements 1.3, 2.3**

        Counterexample on unfixed code: no event listener for Detect button.
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found in app.js"
        # Look for getElementById or querySelector targeting the detect button
        # followed by addEventListener, or a combined pattern.
        has_listener = (
            "btn-edit-detect-ami" in body
            and "addEventListener" in body
        )
        assert has_listener, (
            "COUNTEREXAMPLE: no `addEventListener` wired to "
            "`btn-edit-detect-ami` in the edit modal — Detect click handler "
            "is missing"
        )

    # -- Test 4: AMI hint element ---------------------------------------------

    def test_edit_modal_has_ami_hint(self):
        """The `showEditTemplateDialog` template literal SHALL contain an
        element with id `edit-ami-hint`.

        **Validates: Requirements 1.2, 2.2**

        Counterexample on unfixed code: no AMI hint element in edit modal.
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found in app.js"
        assert "edit-ami-hint" in body, (
            "COUNTEREXAMPLE: no element with id `edit-ami-hint` in the edit "
            "modal — AMI detection hint/status element is missing"
        )
