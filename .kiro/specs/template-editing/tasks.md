# Implementation Plan: Template Editing

## Overview

Add the ability for administrators to edit cluster templates after creation via a new PUT `/templates/{templateId}` endpoint. Implementation spans backend business logic, Lambda handler routing, frontend edit modal, CDK API Gateway route, API documentation, and tests. Each task builds incrementally — backend first, then frontend, then infrastructure and docs, with tests woven in alongside the code they validate.

## Tasks

- [x] 1. Implement `update_template` in backend business logic
  - [x] 1.1 Add `update_template` function to `lambda/template_management/templates.py`
    - Accept `table_name`, `template_id`, and all editable fields (`templateName`, `description`, `instanceTypes`, `loginInstanceType`, `minNodes`, `maxNodes`, `amiId`, `softwareStack`)
    - Validate all editable fields using the existing `_validate_template_fields` function (pass `template_id` for the validation call)
    - Use `DynamoDB.Table.update_item` with `ConditionExpression="attribute_exists(PK)"` to atomically update all editable fields plus an `updatedAt` ISO 8601 UTC timestamp
    - Catch `ConditionalCheckFailedException` and raise `NotFoundError`
    - Catch other `ClientError` and raise `InternalError`
    - Return the sanitised updated record via `_sanitise_record`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 3.1_

  - [x] 1.2 Write property test: update storage round-trip (Property 1)
    - Create `test/lambda/test_property_template_update_roundtrip.py`
    - Reuse strategies from `test_property_template_roundtrip.py` (`template_id_strategy`, `template_name_strategy`, `instance_types_strategy`, etc.)
    - Add a `valid_update_payload` composite strategy generating a second set of valid editable fields
    - For each generated pair: create a template, update it with the new payload, retrieve it, assert all editable fields match the update payload
    - **Property 1: Update storage round-trip**
    - **Validates: Requirements 1.1, 8.1**

  - [x] 1.3 Write property test: timestamp invariants on update (Property 2)
    - Add to `test/lambda/test_property_template_update_roundtrip.py`
    - After update+retrieve: assert `createdAt` equals the original `createdAt`, assert `updatedAt` is a valid ISO 8601 string, assert `updatedAt >= createdAt`
    - **Property 2: Timestamp invariants on update**
    - **Validates: Requirements 1.2, 1.3, 8.2**

  - [x] 1.4 Write property test: invalid fields are rejected (Property 3)
    - Add to `test/lambda/test_property_template_update_roundtrip.py`
    - Generate invalid update payloads (empty `templateName`, empty `instanceTypes`, `minNodes > maxNodes`, non-positive `maxNodes`, empty `amiId`, non-string `loginInstanceType`)
    - Assert `update_template` raises `ValidationError` and the stored record is unchanged
    - **Property 3: Invalid fields are rejected**
    - **Validates: Requirements 3.1, 3.2**

- [x] 2. Add PUT route to Lambda handler
  - [x] 2.1 Add `_handle_update_template` and PUT route to `lambda/template_management/handler.py`
    - Add import of `update_template` from `templates`
    - Add `_handle_update_template(event, template_id)` function: check `is_administrator`, parse body, reject body `templateId` that differs from path parameter (raise `ValidationError`), extract all editable fields, call `update_template`, return 200 with updated record
    - Add route in the dispatcher: `elif resource == "/templates/{templateId}" and http_method == "PUT":`
    - _Requirements: 1.1, 1.5, 2.1, 2.2_

  - [x] 2.2 Write unit tests for template update in `test/lambda/test_unit_template_management.py`
    - Add a `TestTemplateUpdate` class using the `template_mgmt_env` fixture
    - Test cases: update returns 200 with updated fields, update nonexistent template returns 404, mismatched body templateId returns 400, non-admin cannot update (403), admin can update (200), empty body returns 400, invalid fields return 400 with details
    - _Requirements: 1.1, 1.4, 1.5, 2.1, 2.2, 3.1, 3.2, 3.3_

- [x] 3. Checkpoint — Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Add template edit UI to frontend
  - [x] 4.1 Add Edit button and `showEditTemplateDialog` modal to `frontend/js/app.js`
    - Add an Edit button to each template row in `loadTemplates` (alongside the existing Delete button)
    - Add `editTemplate(templateId)` function: fetch template via GET, then call `showEditTemplateDialog`
    - Implement `showEditTemplateDialog(template)` following the `showEditProjectDialog` pattern: modal overlay, pre-populated form fields for all editable fields, `templateId` displayed as read-only (disabled input), Save and Cancel buttons
    - On Save: collect all field values, validate client-side (non-empty required fields, minNodes <= maxNodes, PCS instance type validation), send PUT to `/templates/{templateId}`, show success toast, close modal, call `loadTemplates()`
    - On error: show error toast
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [x] 5. Add CDK API Gateway route for PUT method
  - [x] 5.1 Add PUT method on `/templates/{templateId}` resource in `lib/self-service-hpc-stack.ts`
    - Document the required CDK configuration as a commented-out block or placeholder (since the stack is currently minimal)
    - The PUT method should integrate with the Template Management Lambda and be protected by the Cognito authoriser, following the same pattern as other PUT routes
    - _Requirements: 6.1_

- [x] 6. Update API documentation
  - [x] 6.1 Add `PUT /templates/{templateId}` section to `docs/api/reference.md`
    - Insert in the Cluster Templates section between the GET and DELETE entries
    - Document: required role (Administrator), request body schema (all editable fields), response format (full template record with `updatedAt`), error codes (VALIDATION_ERROR 400, AUTHORISATION_ERROR 403, NOT_FOUND 404)
    - Document the `updatedAt` field in the response
    - _Requirements: 7.1, 7.2_


## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis with moto-mocked DynamoDB
- Unit tests validate specific examples and edge cases using the shared `template_mgmt_env` fixture
- The CDK stack is currently a minimal placeholder — task 5.1 documents the required configuration for when the stack is built out
- Cluster isolation (Requirement 4) is guaranteed by the existing architecture (values copied at creation time) and requires no code changes
