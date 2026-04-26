# Implementation Plan: Project Update

## Overview

Add an update capability to the project lifecycle so Administrators can update a project's infrastructure stack via `cdk deploy` without destroying and recreating it. Implementation follows the existing deploy/destroy patterns: lifecycle state extension, workflow step handlers, API handler, CDK infrastructure, frontend UI, documentation, and tests.

## Tasks

- [x] 1. Extend lifecycle state machine for UPDATING
  - [x] 1.1 Add UPDATING to VALID_TRANSITIONS in `lambda/project_management/lifecycle.py`
    - Add `"UPDATING"` to the `ACTIVE` target list: `"ACTIVE": ["DESTROYING", "UPDATING"]`
    - Add new entry: `"UPDATING": ["ACTIVE"]`
    - No changes needed to `validate_transition()` or `transition_project()` — they already work generically from the dict
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.7_

  - [x] 1.2 Write property test for update lifecycle round-trip
    - **Property 1: Update lifecycle round-trip**
    - **Validates: Requirements 1.2, 1.3, 3.7**
    - Create `test/lambda/test_property_update_lifecycle.py`
    - For any project in ACTIVE status, transitioning to UPDATING and back to ACTIVE should produce status ACTIVE with empty errorMessage
    - Use `@mock_aws`, low example count

  - [x] 1.3 Write property test for update failure preserving ACTIVE status
    - **Property 2: Update failure preserves ACTIVE status with error message**
    - **Validates: Requirements 1.4, 3.8**
    - In `test/lambda/test_property_update_lifecycle.py`
    - For any project in UPDATING status and any non-empty error string, failure handler transitions to ACTIVE with matching errorMessage

  - [x] 1.4 Write property test for UPDATING blocking DESTROYING
    - **Property 3: UPDATING blocks DESTROYING transition**
    - **Validates: Requirements 1.5**
    - In `test/lambda/test_property_update_lifecycle.py`
    - For any project in UPDATING status, attempting transition to DESTROYING raises ConflictError and status remains UPDATING

- [x] 2. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implement update workflow step handlers
  - [x] 3.1 Create `lambda/project_management/project_update.py` with all step functions
    - Follow the same pattern as `project_deploy.py` and `project_destroy.py`
    - Implement `validate_update_state`: verify project exists and status is UPDATING, snapshot current infrastructure outputs into `previousOutputs` in the event payload
    - Implement `start_cdk_update`: start CodeBuild with `npx cdk deploy HpcProject-{projectId} --exclusively --require-approval never`
    - Implement `check_update_status`: poll CodeBuild, return `updateComplete: True/False`
    - Implement `extract_stack_outputs`: describe CloudFormation stack, extract infrastructure outputs (reuse same logic as deploy)
    - Implement `record_updated_infrastructure`: compare old vs new outputs and log warnings for changed critical IDs, write updated infrastructure IDs to DynamoDB, transition to ACTIVE
    - Implement `handle_update_failure`: transition back to ACTIVE (not CREATED), store error message
    - Implement `step_handler` entry point with `STEP_DISPATCH` dict
    - Use 5 steps with progress tracking matching `STEP_LABELS`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.3, 4.4_

  - [x] 3.2 Write unit tests for update step handlers
    - Create `test/lambda/test_unit_project_update.py` following the pattern in `test_unit_project_deploy.py`
    - Test `validate_update_state`: succeeds for UPDATING, fails for other statuses, snapshots previous outputs
    - Test `start_cdk_update`: passes correct CDK command to CodeBuild
    - Test `check_update_status`: returns correct completion flag for each build status
    - Test `record_updated_infrastructure`: writes correct fields, detects changed IDs, transitions to ACTIVE
    - Test `handle_update_failure`: transitions to ACTIVE, stores error message
    - Test progress tracking: each step updates DynamoDB with correct step number and description
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

  - [x] 3.3 Write property test for CDK command format
    - **Property 7: CDK command format is correct for any project ID**
    - **Validates: Requirements 3.2, 3.3**
    - Create `test/lambda/test_property_update_workflow.py`
    - For any valid project ID string, `start_cdk_update` passes CDK_COMMAND equal to `npx cdk deploy HpcProject-{projectId} --exclusively --require-approval never`

  - [x] 3.4 Write property test for infrastructure outputs round-trip
    - **Property 8: Infrastructure outputs round-trip through DynamoDB**
    - **Validates: Requirements 3.5, 3.6, 4.3**
    - In `test/lambda/test_property_update_workflow.py`
    - For any set of valid infrastructure output values, after `record_updated_infrastructure` writes them, reading the project record returns the same values

  - [x] 3.5 Write property test for changed infrastructure ID warnings
    - **Property 9: Changed infrastructure IDs trigger warnings**
    - **Validates: Requirements 4.4**
    - In `test/lambda/test_property_update_workflow.py`
    - For any pair of old and new infrastructure output maps where at least one critical field differs, `record_updated_infrastructure` emits a WARNING-level log entry identifying each changed resource

- [x] 4. Implement update API handler
  - [x] 4.1 Add `_handle_update_project` to `lambda/project_management/handler.py`
    - Add route matching for `POST /projects/{projectId}/update`
    - Verify caller is Administrator
    - Verify project exists and status is ACTIVE (return 409 if not)
    - Call `lifecycle.transition_project()` to move to UPDATING
    - Set initial progress: `currentStep=0, totalSteps=5`
    - Start Step Functions execution with `{"projectId": project_id}`
    - Return 202 Accepted with message, projectId, and status
    - Add `PROJECT_UPDATE_STATE_MACHINE_ARN` environment variable reading
    - Update `_handle_get_project` to include `UPDATING` in the list of transitional statuses that return the `progress` object
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 5.1_

  - [x] 4.2 Write unit tests for update handler route
    - Add tests to `test/lambda/test_unit_project_management.py`
    - Test update returns 202 for valid ACTIVE project with admin caller
    - Test update returns 403 for non-admin caller
    - Test update returns 409 for non-ACTIVE project
    - Test update returns 404 for nonexistent project
    - Test GET project includes progress object for UPDATING status
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 5.1_

  - [x] 4.3 Write property test for non-admin rejection
    - **Property 4: Non-admin callers are rejected from update**
    - **Validates: Requirements 2.2**
    - Create `test/lambda/test_property_update_api.py`
    - For any non-Administrator caller identity, calling the update endpoint returns 403 and project status remains unchanged

  - [x] 4.4 Write property test for only ACTIVE projects can be updated
    - **Property 5: Only ACTIVE projects can be updated**
    - **Validates: Requirements 2.3, 3.1**
    - In `test/lambda/test_property_update_api.py`
    - For any project status that is not ACTIVE, calling the update endpoint returns 409 and project status remains unchanged

  - [x] 4.5 Write property test for valid update trigger
    - **Property 6: Valid update triggers transition and returns 202**
    - **Validates: Requirements 2.5, 2.6**
    - In `test/lambda/test_property_update_api.py`
    - For any ACTIVE project and any Administrator caller, calling update transitions to UPDATING, sets currentStep=0, totalSteps=5, and returns 202

- [x] 5. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Add CDK infrastructure for update
  - [x] 6.1 Add update resources to `lib/foundation-stack.ts`
    - Add a new Lambda function `hpc-project-update-steps` (Python 3.13, handler `project_update.step_handler`, code from `lambda/project_management`, 300s timeout)
    - Grant the update Lambda: read/write on Projects table, CodeBuild `StartBuild`/`BatchGetBuilds`, CloudFormation `DescribeStacks`
    - Add a new Step Functions state machine `hpc-project-update` with 5-step workflow, 30-second wait loop for CodeBuild polling, catch-all failure handler, 2-hour timeout
    - Reuse the existing `cdkDeployProject` (CodeBuild) — no new CodeBuild project
    - Add `POST /projects/{projectId}/update` API Gateway route with Cognito authorisation, integrated with the Project Management Lambda
    - Add `PROJECT_UPDATE_STATE_MACHINE_ARN` environment variable to the Project Management Lambda
    - Grant the Project Management Lambda `states:StartExecution` on the update state machine
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 6.2 Write CDK snapshot tests for update resources
    - Add tests to `test/foundation-stack.test.ts`
    - Verify the update state machine (`hpc-project-update`) is synthesised
    - Verify the update step Lambda (`hpc-project-update-steps`) is created with correct runtime, handler, and timeout
    - Verify the API Gateway includes the `/update` route with Cognito auth
    - Verify IAM permissions for the update Lambda (DynamoDB, CodeBuild, CloudFormation)
    - Verify the state machine count increases to 5
    - Verify `PROJECT_UPDATE_STATE_MACHINE_ARN` is in the Project Management Lambda environment
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement frontend changes
  - [x] 8.1 Update `frontend/js/app.js` for update UI
    - Add `UPDATING` to the transitional states in `loadProjects()` that display a progress bar (alongside DEPLOYING and DESTROYING)
    - Add an "Update" button in the actions column for ACTIVE projects, next to "Edit" and "Destroy"
    - Add `UPDATING` to the list of transitional statuses that trigger 5-second polling
    - Detect `UPDATING → ACTIVE` transitions in the status cache and show success/failure toasts (check for errorMessage to distinguish)
    - Add `updateProject(projectId)` async function that calls `POST /projects/{projectId}/update` and refreshes the project list
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [x] 9. Update documentation
  - [x] 9.1 Update `docs/admin/project-management.md` with update documentation
    - Add UPDATING to the lifecycle states table
    - Update the state transitions diagram to include ACTIVE ↔ UPDATING
    - Add a new "Updating a Project" section covering: endpoint, prerequisites (ACTIVE status), what happens during update, progress tracking, cluster safety, failure handling, and troubleshooting
    - Document that updates do not disrupt running clusters because CloudFormation updates preserve resources with stable logical IDs
    - _Requirements: 7.1, 7.2, 7.4_

  - [x] 9.2 Update `docs/api/reference.md` with update endpoint
    - Add `POST /projects/{projectId}/update` endpoint documentation
    - Include: required role (Administrator), path parameters, request body (none), response (202 Accepted), error codes (403, 404, 409)
    - Update the GET /projects/{projectId} response documentation to include UPDATING as a transitional status with progress object
    - _Requirements: 7.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (Properties 1–9)
- Unit tests validate specific examples and edge cases
- The update workflow closely mirrors the existing deploy workflow for consistency
- Python is used for Lambda functions; TypeScript for CDK infrastructure — matching existing project conventions
