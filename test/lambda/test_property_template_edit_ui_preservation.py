# Feature: template-edit-ui-fixes, Property 2: Preservation — Existing Edit Modal and Create Form Behavior
"""Preservation property tests that verify all existing behavior in the edit
modal and create template form is unchanged.

These tests observe the UNFIXED code and assert that specific structural
elements are present.  They MUST PASS on the unfixed code (confirming the
baseline) and MUST CONTINUE TO PASS after the fix is applied (confirming
no regressions).

The tests parse CSS and JavaScript source files directly — no browser or
DOM engine is required.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""

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
# Helpers (same pattern as test_property_template_edit_ui_bugs.py)
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
# Observed sets (captured from UNFIXED code)
# ---------------------------------------------------------------------------

# CSS properties observed in the `.modal-content` rule on unfixed code
MODAL_CONTENT_CSS_PROPERTIES = [
    "background",
    "border-radius",
    "padding",
    "width",
    "max-width",
    "box-shadow",
]

# Form field ids observed in showEditTemplateDialog on unfixed code
EDIT_MODAL_FIELD_IDS = [
    "edit-tpl-id",
    "edit-tpl-name",
    "edit-tpl-desc",
    "edit-tpl-instance",
    "edit-tpl-login-instance",
    "edit-tpl-min",
    "edit-tpl-max",
    "edit-tpl-ami",
    "edit-tpl-scheduler",
    "edit-tpl-scheduler-ver",
    "edit-tpl-cuda",
]

# Validation checks observed in the save handler on unfixed code.
# Each tuple is (field_description, regex_pattern_in_save_handler).
EDIT_MODAL_VALIDATION_PATTERNS = [
    ("templateName required", r"if\s*\(\s*!templateName\s*\)"),
    ("instanceTypes required", r"if\s*\(\s*!instanceTypes\.length\s*\)"),
    ("loginInstanceType required", r"if\s*\(\s*!loginInstanceType\s*\)"),
    ("amiId required", r"if\s*\(\s*!amiId\s*\)"),
    ("minNodes <= maxNodes", r"if\s*\(\s*minNodes\s*>\s*maxNodes\s*\)"),
    ("validateInstanceType", r"validateInstanceType\s*\("),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPreservationEditModal:
    """Verify existing edit modal and create form behavior is preserved."""

    # -- Test 1: .modal-content CSS properties preserved --------------------

    @pytest.mark.parametrize("css_prop", MODAL_CONTENT_CSS_PROPERTIES)
    def test_modal_content_css_property_preserved(self, css_prop: str):
        """The `.modal-content` CSS rule SHALL continue to contain the
        property ``{css_prop}``.

        **Validates: Requirements 3.1**
        """
        css = _read_text(_CSS_PATH)
        block = _extract_css_rule(css, ".modal-content")
        assert block is not None, ".modal-content rule not found in styles.css"
        # Match the property name at the start of a declaration
        pattern = re.compile(r"(?:^|;|\n)\s*" + re.escape(css_prop) + r"\s*:")
        assert pattern.search(block), (
            f"`.modal-content` is missing the `{css_prop}` CSS property"
        )

    # -- Test 2: All edit modal form field ids present ----------------------

    @pytest.mark.parametrize("field_id", EDIT_MODAL_FIELD_IDS)
    def test_edit_modal_field_id_present(self, field_id: str):
        """The `showEditTemplateDialog` function SHALL contain an element
        with id ``{field_id}``.

        **Validates: Requirements 3.2, 3.3**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found"
        assert field_id in body, (
            f"Edit modal is missing form field with id `{field_id}`"
        )

    # -- Test 3: Save and Cancel button handlers present --------------------

    def test_edit_modal_save_button_handler(self):
        """The `showEditTemplateDialog` function SHALL wire an event
        listener to the Save button (`edit-tpl-save-btn`).

        **Validates: Requirements 3.3**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found"
        assert "edit-tpl-save-btn" in body, (
            "Save button id `edit-tpl-save-btn` not found in edit modal"
        )
        # Verify addEventListener is wired to the save button
        assert re.search(
            r"edit-tpl-save-btn.*addEventListener.*click",
            body,
            re.DOTALL,
        ), "Save button `addEventListener('click', …)` not found"

    def test_edit_modal_cancel_button_handler(self):
        """The `showEditTemplateDialog` function SHALL wire an event
        listener to the Cancel button (`edit-tpl-cancel-btn`).

        **Validates: Requirements 3.4**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found"
        assert "edit-tpl-cancel-btn" in body, (
            "Cancel button id `edit-tpl-cancel-btn` not found in edit modal"
        )
        assert re.search(
            r"edit-tpl-cancel-btn.*addEventListener.*click",
            body,
            re.DOTALL,
        ), "Cancel button `addEventListener('click', …)` not found"

    # -- Test 4: Escape key handler present ---------------------------------

    def test_edit_modal_escape_key_handler(self):
        """The `showEditTemplateDialog` function SHALL register a keydown
        listener that closes the modal when Escape is pressed.

        **Validates: Requirements 3.4**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found"
        # Check for the Escape key handler pattern
        assert re.search(r"addEventListener\s*\(\s*['\"]keydown['\"]", body), (
            "No `keydown` event listener found in edit modal"
        )
        assert re.search(r"""e\.key\s*===?\s*['"]Escape['"]""", body), (
            "No Escape key check found in edit modal keydown handler"
        )


class TestPreservationCreateForm:
    """Verify the create template form is unchanged."""

    # -- Test 5: Create form Detect button and hint present -----------------

    def test_create_form_detect_button_present(self):
        """The `renderTemplatesPage` function SHALL contain an element
        with id `btn-detect-ami` (the create form's Detect button).

        **Validates: Requirements 3.5**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "renderTemplatesPage")
        assert body is not None, "renderTemplatesPage function not found"
        assert "btn-detect-ami" in body, (
            "Create form Detect button (`btn-detect-ami`) not found"
        )

    def test_create_form_ami_hint_present(self):
        """The `renderTemplatesPage` function SHALL contain an element
        with id `ami-hint` (the create form's AMI hint).

        **Validates: Requirements 3.5**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "renderTemplatesPage")
        assert body is not None, "renderTemplatesPage function not found"
        assert "ami-hint" in body, (
            "Create form AMI hint element (`ami-hint`) not found"
        )

    # -- Test 6: Create form auto-detect-on-blur wiring --------------------

    def test_create_form_blur_event_on_instance_types(self):
        """The `renderTemplatesPage` function SHALL wire a `blur` event
        listener on the `new-tpl-instance` input for AMI auto-detection.

        **Validates: Requirements 3.5**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "renderTemplatesPage")
        assert body is not None, "renderTemplatesPage function not found"
        assert "new-tpl-instance" in body, (
            "Create form instance types input (`new-tpl-instance`) not found"
        )
        assert re.search(
            r"new-tpl-instance.*addEventListener.*blur",
            body,
            re.DOTALL,
        ), "Blur event listener on `new-tpl-instance` not found"


class TestPreservationEditModalValidation:
    """Verify edit modal validation rules are preserved."""

    # -- Test 7: Validation rules preserved ---------------------------------

    @pytest.mark.parametrize(
        "description,pattern",
        EDIT_MODAL_VALIDATION_PATTERNS,
        ids=[v[0] for v in EDIT_MODAL_VALIDATION_PATTERNS],
    )
    def test_edit_modal_validation_rule_preserved(
        self, description: str, pattern: str
    ):
        """The `showEditTemplateDialog` save handler SHALL contain the
        validation check for: {description}.

        **Validates: Requirements 3.2, 3.3**
        """
        js = _read_text(_JS_PATH)
        body = _extract_function_body(js, "showEditTemplateDialog")
        assert body is not None, "showEditTemplateDialog function not found"
        assert re.search(pattern, body), (
            f"Validation rule `{description}` not found in edit modal save handler"
        )
