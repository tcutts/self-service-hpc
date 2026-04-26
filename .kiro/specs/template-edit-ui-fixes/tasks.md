# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Modal Scroll and Detect Button Missing
  - **IMPORTANT**: Write this property-based test BEFORE implementing the fix
  - **GOAL**: Surface counterexamples that demonstrate both bugs exist in the unfixed code
  - **Scoped PBT Approach**: Since these are deterministic structural bugs, scope the property to concrete checks against the source artifacts
  - **Test file**: `test/lambda/test_property_template_edit_ui_bugs.py`
  - **Test 1 — Modal overflow**: Parse `frontend/css/styles.css`, find the `.modal-content` rule block, and assert it contains `max-height` and `overflow-y: auto`. This will FAIL on unfixed code because neither property exists.
  - **Test 2 — Detect button markup**: Read `frontend/js/app.js`, extract the `showEditTemplateDialog` function's template literal, and assert it contains an element with id `btn-edit-detect-ami`. This will FAIL on unfixed code because the button does not exist.
  - **Test 3 — Detect event listener**: Assert the `showEditTemplateDialog` function body contains an `addEventListener` call wired to `btn-edit-detect-ami`. This will FAIL on unfixed code.
  - **Test 4 — AMI hint element**: Assert the edit modal template contains an element with id `edit-ami-hint`. This will FAIL on unfixed code.
  - Run tests on UNFIXED code — expect FAILURE (this confirms the bugs exist)
  - Document counterexamples found (e.g., "`.modal-content` has no `max-height` or `overflow-y`", "no element `btn-edit-detect-ami` in edit modal")
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Existing Edit Modal and Create Form Behavior
  - **IMPORTANT**: Follow observation-first methodology
  - **Test file**: `test/lambda/test_property_template_edit_ui_preservation.py`
  - Observe on UNFIXED code: the `.modal-content` CSS rule has `background`, `border-radius`, `padding`, `width`, `max-width`, `box-shadow` properties
  - Observe on UNFIXED code: the `showEditTemplateDialog` function contains all existing form field ids (`edit-tpl-id`, `edit-tpl-name`, `edit-tpl-desc`, `edit-tpl-instance`, `edit-tpl-login-instance`, `edit-tpl-min`, `edit-tpl-max`, `edit-tpl-ami`, `edit-tpl-scheduler`, `edit-tpl-scheduler-ver`, `edit-tpl-cuda`)
  - Observe on UNFIXED code: the `showEditTemplateDialog` function contains Save (`edit-tpl-save-btn`) and Cancel (`edit-tpl-cancel-btn`) button handlers
  - Observe on UNFIXED code: the `showEditTemplateDialog` function contains Escape key handler
  - Observe on UNFIXED code: the create form in `renderTemplatesPage` contains `btn-detect-ami` and `ami-hint` elements
  - Observe on UNFIXED code: the create form wires up `blur` event on `new-tpl-instance` for auto-detect
  - **Preservation property tests**:
  - Test 1 — All existing `.modal-content` CSS properties are preserved (generated from observed set)
  - Test 2 — All existing edit modal form field ids are present (property: for all field ids in the observed set, the id appears in the template)
  - Test 3 — Save and Cancel button handlers are present in edit modal
  - Test 4 — Escape key handler is present in edit modal
  - Test 5 — Create form Detect button (`btn-detect-ami`) and hint (`ami-hint`) are unchanged
  - Test 6 — Create form auto-detect-on-blur wiring is unchanged
  - Test 7 — Edit modal validation rules are preserved (templateName, instanceTypes, loginInstanceType, amiId, minNodes/maxNodes checks)
  - Verify all tests PASS on UNFIXED code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Fix for edit template modal scroll and missing Detect button

  - [x] 3.1 Add max-height and overflow-y to `.modal-content` CSS
    - In `frontend/css/styles.css`, add `max-height: calc(100vh - 2rem)` and `overflow-y: auto` to the `.modal-content` rule
    - This constrains the modal height to the viewport minus padding and enables scrolling when content overflows
    - _Bug_Condition: isBugCondition(input) where input.action == 'openEditModal' and modalContentHeight > viewportHeight_
    - _Expected_Behavior: modal content is scrollable so all controls are reachable_
    - _Preservation: existing `.modal-content` properties (background, border-radius, padding, width, max-width, box-shadow) must remain unchanged_
    - _Requirements: 2.1, 3.1_

  - [x] 3.2 Add Detect button markup to edit template modal
    - In `frontend/js/app.js` `showEditTemplateDialog`, replace the plain AMI ID input with a flex-wrapper layout matching the create form:
      ```html
      <div style="display:flex;gap:0.5rem">
        <input type="text" id="edit-tpl-ami" value="..." style="flex:1" />
        <button class="btn" type="button" id="btn-edit-detect-ami">Detect</button>
      </div>
      <small class="form-hint" id="edit-ami-hint">Click Detect to find the latest PCS sample AMI for the current instance types.</small>
      ```
    - _Bug_Condition: isBugCondition(input) where input.action == 'detectAmi' and input.context == 'editModal'_
    - _Expected_Behavior: Detect button with id `btn-edit-detect-ami` is present in the edit modal DOM_
    - _Preservation: all existing form fields and their ids must remain unchanged_
    - _Requirements: 2.2, 3.2, 3.3_

  - [x] 3.3 Wire up Detect button event listener in edit modal
    - After appending the modal to the DOM, add an event listener for `#btn-edit-detect-ami` that:
      - Reads instance types from `#edit-tpl-instance` and splits by comma
      - Calls `inferArchitecture(types)` to determine architecture
      - Updates `#edit-ami-hint` with "Looking up latest PCS sample AMI for {arch}…"
      - Calls `await fetchDefaultAmi(arch)` to get the AMI
      - On success: populates `#edit-tpl-ami` with `result.amiId` and updates hint with result name/arch
      - On failure: updates hint with "Could not find a PCS sample AMI for {arch}. Enter an AMI ID manually."
    - Follow the same pattern as `detectAndPopulateAmi` in the create form
    - _Bug_Condition: isBugCondition(input) where input.action == 'detectAmi' and input.context == 'editModal'_
    - _Expected_Behavior: clicking Detect calls inferArchitecture → fetchDefaultAmi and populates AMI field_
    - _Preservation: create form's detectAndPopulateAmi and blur handler must remain unchanged_
    - _Requirements: 2.3, 3.5_

  - [x] 3.4 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Modal Scroll and Detect Button Present
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior (max-height set, overflow-y auto, Detect button present, event listener wired)
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bugs are fixed)
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Existing Edit Modal and Create Form Behavior
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all preservation tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full test suite: `pytest test/lambda/test_property_template_edit_ui_bugs.py test/lambda/test_property_template_edit_ui_preservation.py -v`
  - Ensure all exploration tests (Property 1) now PASS
  - Ensure all preservation tests (Property 2) still PASS
  - Ensure no other existing tests are broken by running `pytest test/lambda/ -v --timeout=60`
  - Ask the user if questions arise
