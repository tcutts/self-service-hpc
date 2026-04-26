# Template Edit UI Fixes — Bugfix Design

## Overview

The edit template modal (`showEditTemplateDialog` in `frontend/js/app.js`) has two defects: (1) the `.modal-content` container has no overflow handling, so on smaller viewports the Save/Cancel buttons are clipped offscreen, and (2) the AMI ID field lacks the "Detect" button that exists in the create template form, forcing users to manually look up AMI IDs when editing. The fix adds scrollable overflow to the modal and replicates the Detect button + `inferArchitecture()` → `fetchDefaultAmi()` flow from the create form into the edit modal.

## Glossary

- **Bug_Condition (C)**: Either (a) the edit modal is rendered on a viewport too small to display all content, or (b) the user opens the edit modal and needs AMI auto-detection — both conditions where the current code fails to provide the expected UI.
- **Property (P)**: (a) The modal content is scrollable so all controls are reachable, and (b) a Detect button is present and functional in the edit modal.
- **Preservation**: All existing edit-modal behavior (save, cancel, escape, manual AMI entry) and the entire create-template form must remain unchanged.
- **`showEditTemplateDialog(template)`**: The function in `frontend/js/app.js` (line 1019) that builds and displays the edit template modal.
- **`renderTemplatesPage(container)`**: The function in `frontend/js/app.js` (line 840) that renders the templates page including the create template form with its Detect button.
- **`inferArchitecture(instanceTypes)`**: Helper (line 818) that returns `'arm64'` or `'x86_64'` based on instance type prefixes.
- **`fetchDefaultAmi(arch)`**: Async helper (line 832) that calls the API to get the latest PCS sample AMI for a given architecture.

## Bug Details

### Bug Condition

The bug manifests in two independent scenarios within the edit template modal. First, when the modal is displayed on a viewport where the form content exceeds the available height, the user cannot scroll to reach the Save/Cancel buttons. Second, when the user needs to change instance types and wants the matching AMI auto-detected, no Detect button exists in the edit modal (unlike the create form).

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type { action: 'openEditModal', viewportHeight: number, template: Template }
         OR { action: 'detectAmi', context: 'editModal' }
  OUTPUT: boolean

  IF input.action == 'openEditModal' THEN
    modalContentHeight := computeRenderedHeight(showEditTemplateDialog(input.template))
    RETURN modalContentHeight > input.viewportHeight
  END IF

  IF input.action == 'detectAmi' AND input.context == 'editModal' THEN
    detectButton := document.querySelector('#edit-template-modal #btn-edit-detect-ami')
    RETURN detectButton == NULL
  END IF

  RETURN FALSE
END FUNCTION
```

### Examples

- **Scroll bug**: User opens edit modal on a 768×600 viewport. The modal renders ~650px of content. The Save button is at the bottom and is clipped — the user cannot scroll to it. Expected: modal content scrolls so Save is reachable.
- **Scroll bug (edge)**: User opens edit modal on a 1920×1080 viewport. All content fits. Expected: no scrollbar appears, modal displays normally.
- **Detect button missing**: User opens edit modal for a template with `instanceTypes: ['c7g.medium']`. There is no Detect button next to the AMI ID field. Expected: a Detect button is present, and clicking it populates the AMI field with the latest arm64 PCS sample AMI.
- **Detect button flow**: User changes instance types from `c5.xlarge` to `c7g.xlarge` in the edit modal and clicks Detect. Expected: `inferArchitecture(['c7g.xlarge'])` returns `'arm64'`, `fetchDefaultAmi('arm64')` is called, and the AMI field is populated with the result.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Mouse clicks on Save with valid data must continue to submit the PUT request and update the template
- Cancel button and Escape key must continue to close the modal without saving
- Manually entered AMI IDs must continue to be accepted and saved
- The create template form must continue to function identically, including its existing Detect button and auto-detect-on-blur behavior
- All existing form fields, validation rules, and error messages in the edit modal must remain unchanged

**Scope:**
All inputs that do NOT involve (a) viewport overflow of the edit modal or (b) AMI auto-detection in the edit modal should be completely unaffected by this fix. This includes:
- The create template form and its Detect button
- All other modal dialogs in the application
- All non-modal pages and navigation
- The edit modal's save, cancel, and validation logic

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Missing `overflow-y` and `max-height` on `.modal-content`**: The CSS class `.modal-content` (in `frontend/css/styles.css`) has no `max-height` or `overflow-y` property. When the form content exceeds the viewport, the modal simply extends beyond the screen with no scroll mechanism. The `.modal-overlay` uses `align-items: center` which vertically centers the modal, but does nothing to constrain its height.

2. **Missing Detect button markup in `showEditTemplateDialog`**: The edit modal's AMI ID field (line ~1068 in `app.js`) is a plain `<input>` without the flex wrapper and Detect button that the create form uses (line ~905). The create form wraps the AMI input in `<div style="display:flex;gap:0.5rem">` with a `<button id="btn-detect-ami">Detect</button>` — this markup is absent from the edit modal.

3. **Missing event listener for Detect in edit modal**: Even if the button were added, there is no `detectAndPopulateAmi` equivalent wired up for the edit modal's instance types and AMI fields.

4. **Missing hint element in edit modal**: The create form has an `<small id="ami-hint">` element that shows detection status messages. The edit modal has no equivalent, so detection feedback would have nowhere to display.

## Correctness Properties

Property 1: Bug Condition — Modal Scrollability on Small Viewports

_For any_ viewport height where the edit modal content exceeds the available space, the `.modal-content` element SHALL have constrained max-height and overflow-y scrolling so that all form fields and the Save/Cancel buttons are reachable by scrolling.

**Validates: Requirements 2.1**

Property 2: Bug Condition — Detect Button Presence and Functionality

_For any_ template object passed to `showEditTemplateDialog`, the resulting modal SHALL contain a Detect button adjacent to the AMI ID input, and clicking that button SHALL call `inferArchitecture()` with the current instance types and `fetchDefaultAmi()` with the inferred architecture, populating the AMI ID field with the result.

**Validates: Requirements 2.2, 2.3**

Property 3: Preservation — Existing Edit Modal Behavior

_For any_ interaction with the edit modal that does NOT involve viewport overflow or AMI detection (save, cancel, escape, manual AMI entry, validation), the fixed code SHALL produce exactly the same behavior as the original code, preserving all existing functionality.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Property 4: Preservation — Create Template Form Unchanged

_For any_ interaction with the create template form, the fixed code SHALL produce exactly the same behavior as the original code, preserving the existing Detect button, auto-detect-on-blur, and all form functionality.

**Validates: Requirements 3.5**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `frontend/css/styles.css`

**Class**: `.modal-content`

**Specific Changes**:
1. **Add max-height constraint**: Add `max-height: calc(100vh - 2rem)` (or similar) to `.modal-content` so the modal never exceeds the viewport height minus some padding.
2. **Add overflow-y**: Add `overflow-y: auto` to `.modal-content` so content scrolls when it exceeds the max-height.
3. **Add display flex column** (optional): Consider `display: flex; flex-direction: column` if the button bar needs to be pinned, but simple `overflow-y: auto` on the whole content block is sufficient for this case.

**File**: `frontend/js/app.js`

**Function**: `showEditTemplateDialog(template)`

**Specific Changes**:
4. **Add Detect button markup**: Replace the plain AMI ID `<input>` with the same flex-wrapper layout used in the create form:
   ```html
   <div style="display:flex;gap:0.5rem">
     <input type="text" id="edit-tpl-ami" value="..." style="flex:1" />
     <button class="btn" type="button" id="btn-edit-detect-ami">Detect</button>
   </div>
   <small class="form-hint" id="edit-ami-hint">Click Detect to find the latest PCS sample AMI for the current instance types.</small>
   ```

5. **Wire up Detect button event listener**: After appending the modal to the DOM, add an event listener for `#btn-edit-detect-ami` that:
   - Reads instance types from `#edit-tpl-instance`
   - Calls `inferArchitecture(types)`
   - Calls `fetchDefaultAmi(arch)`
   - Populates `#edit-tpl-ami` with the result
   - Updates `#edit-ami-hint` with status/result text

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bugs on unfixed code, then verify the fixes work correctly and preserve existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that inspect the CSS properties of `.modal-content` and the DOM structure of the edit modal. Run these tests on the UNFIXED code to observe failures and understand the root cause.

**Test Cases**:
1. **Modal overflow test**: Render the edit modal and inspect `.modal-content` computed styles — assert `max-height` is set and `overflow-y` is `auto` (will fail on unfixed code because neither property is set)
2. **Detect button existence test**: Call `showEditTemplateDialog(template)` and query for `#btn-edit-detect-ami` (will fail on unfixed code because the button does not exist)
3. **Detect button click test**: Click the Detect button in the edit modal and assert `fetchDefaultAmi` is called with the correct architecture (will fail on unfixed code — no button to click)
4. **AMI population test**: After Detect completes, assert the AMI input field is populated (will fail on unfixed code)

**Expected Counterexamples**:
- `.modal-content` has no `max-height` or `overflow-y` properties
- No element with id `btn-edit-detect-ami` exists in the edit modal DOM
- Possible causes: missing CSS rules, missing HTML markup, missing event listeners

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  IF input.action == 'openEditModal' THEN
    modal := showEditTemplateDialog_fixed(input.template)
    modalContent := modal.querySelector('.modal-content')
    ASSERT modalContent.style.maxHeight IS SET
    ASSERT modalContent.style.overflowY == 'auto'
    ASSERT allFormFieldsReachable(modalContent)
  END IF

  IF input.action == 'detectAmi' THEN
    modal := showEditTemplateDialog_fixed(input.template)
    detectBtn := modal.querySelector('#btn-edit-detect-ami')
    ASSERT detectBtn != NULL
    detectBtn.click()
    ASSERT fetchDefaultAmi WAS CALLED WITH inferArchitecture(input.template.instanceTypes)
    ASSERT modal.querySelector('#edit-tpl-ami').value == fetchDefaultAmi.result.amiId
  END IF
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT showEditTemplateDialog_original(input) = showEditTemplateDialog_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for save, cancel, escape, and manual AMI entry, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Save preservation**: Verify clicking Save with valid data still submits the correct PUT request payload — test with various template configurations on unfixed code, then verify identical behavior after fix
2. **Cancel/Escape preservation**: Verify Cancel button and Escape key still close the modal without side effects
3. **Manual AMI preservation**: Verify manually typed AMI IDs are still used in the save payload
4. **Create form preservation**: Verify the create template form's Detect button and auto-detect-on-blur still function identically

### Unit Tests

- Test that `.modal-content` CSS includes `max-height` and `overflow-y: auto`
- Test that `showEditTemplateDialog` renders a Detect button with id `btn-edit-detect-ami`
- Test that clicking Detect calls `inferArchitecture` with parsed instance types
- Test that clicking Detect calls `fetchDefaultAmi` with the inferred architecture
- Test that the AMI input is populated after successful detection
- Test that the hint text updates during and after detection
- Test that Save, Cancel, and Escape still work as before

### Property-Based Tests

- Generate random template objects (varying instance types, AMI IDs, node counts) and verify the edit modal always contains a Detect button
- Generate random instance type lists and verify Detect always calls `inferArchitecture` → `fetchDefaultAmi` with correct arguments
- Generate random template objects and verify Save payload matches expected structure (preservation)

### Integration Tests

- Open edit modal on a small viewport, scroll to Save, click Save — verify template is updated
- Open edit modal, change instance types, click Detect, verify AMI field is populated, then Save
- Open edit modal, manually enter AMI, Save — verify manual AMI is used
- Open create form, verify Detect button and auto-detect-on-blur still work after the fix
