# Bugfix Requirements Document

## Introduction

The edit template modal dialog (`showEditTemplateDialog`) in the HPC Self-Service Portal has two UI bugs. First, the modal content can exceed the viewport height on smaller screens because the `.modal-content` class has no overflow handling, making the Save/Cancel buttons unreachable. Second, the edit modal is missing the "Detect" button for AMI auto-detection that exists in the create template form, forcing users to manually enter AMI IDs when editing templates.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the edit template modal is opened on a screen where the modal content exceeds the viewport height THEN the system renders the Save and Cancel buttons offscreen with no way to scroll to them, making it impossible to save changes or dismiss the modal via buttons.

1.2 WHEN a user opens the edit template modal to change instance types THEN the system displays only a plain text input for AMI ID with no "Detect" button, requiring the user to manually look up and enter the correct PCS sample AMI ID.

1.3 WHEN a user changes instance types in the edit template modal (e.g. switching from x86_64 to arm64 instances) THEN the system provides no mechanism to auto-detect the matching AMI, risking architecture mismatches between instance types and AMI.

### Expected Behavior (Correct)

2.1 WHEN the edit template modal is opened on a screen where the modal content exceeds the viewport height THEN the system SHALL make the modal content scrollable so that all form fields and the Save/Cancel buttons are accessible.

2.2 WHEN a user opens the edit template modal THEN the system SHALL display a "Detect" button next to the AMI ID input field, matching the layout of the create template form.

2.3 WHEN a user clicks the "Detect" button in the edit template modal THEN the system SHALL infer the architecture from the current instance types, call `fetchDefaultAmi()` with that architecture, and populate the AMI ID field with the result — using the same `inferArchitecture()` → `fetchDefaultAmi()` flow as the create template form.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the edit template modal is opened on a screen large enough to display all content THEN the system SHALL CONTINUE TO display the modal centered on screen without unnecessary scrollbars.

3.2 WHEN a user manually enters an AMI ID in the edit template modal THEN the system SHALL CONTINUE TO accept and save the manually entered value.

3.3 WHEN a user clicks Save in the edit template modal with valid data THEN the system SHALL CONTINUE TO submit the PUT request and update the template successfully.

3.4 WHEN a user clicks Cancel or presses Escape in the edit template modal THEN the system SHALL CONTINUE TO close the modal without saving changes.

3.5 WHEN the create template form is used THEN the system SHALL CONTINUE TO function identically, including its existing Detect button and AMI auto-detection on blur.
