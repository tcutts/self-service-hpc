# Implementation Plan: Project Lifecycle Management

## Overview

This plan implements a full project lifecycle state machine, Step Functions-based infrastructure orchestration, budget type selection, immediate budget enforcement, persistent project context, and context-sensitive UI actions. The implementation builds incrementally: backend state machine and validation first, then deploy/destroy orchestration, budget enhancements, CDK infrastructure, frontend changes, and finally documentation updates.

## Tasks

- [x] 1. Create the lifecycle state transition module and update project creation
  - [x] 1.1 Create `lambda/project_management/lifecycle.py` with state machine logic
    - Define `VALID_TRANSITIONS` dict mapping each `Project_Status` to its allowed target statuses
    - Implement `validate_transition(current_status, target_status)` that raises `ConflictError` with a descriptive message listing valid transitions when an invalid transition is attempted
    - Implement `transition_project(table_name, project_id, target_status, error_message)` that atomically updates the project status using a DynamoDB `ConditionExpression` on the current status, sets `statusChangedAt` and `updatedAt` timestamps, and handles `ConditionalCheckFailedException` as a `ConflictError`
    - _Requirements: 1.2, 1.4, 1.5, 1.6_

  - [x] 1.2 Modify `lambda/project_management/projects.py` to set initial status to CREATED
    - Change `create_project()` to set `status: "CREATED"` instead of `"ACTIVE"`
    - Add new fields to the initial project record: `budgetType: "MONTHLY"`, `currentStep: 0`, `totalSteps: 0`, `stepDescription: ""`, `errorMessage: ""`, `statusChangedAt: now`, `trustedCidrRanges: []`
    - Change default `budgetLimit` to `50`
    - _Requirements: 1.1_

  - [x] 1.3 Write unit tests for lifecycle module
    - Test all valid transitions succeed (CREATED→DEPLOYING, DEPLOYING→ACTIVE, DEPLOYING→CREATED, ACTIVE→DESTROYING, DESTROYING→ARCHIVED, DESTROYING→ACTIVE)
    - Test all invalid transitions raise `ConflictError` with descriptive messages
    - Test atomic transition with `ConditionExpression` failure
    - Test that `statusChangedAt` and `updatedAt` are set on transition
    - Add tests to `test/lambda/test_unit_project_management.py` or a new test file
    - _Requirements: 1.4, 1.5, 1.6_

- [x] 2. Add deploy, destroy, and edit API routes to the project management handler
  - [x] 2.1 Add `POST /projects/{projectId}/deploy` route to `lambda/project_management/handler.py`
    - Implement `_handle_deploy_project(event, project_id)` that verifies Administrator role, verifies project status is CREATED, transitions to DEPLOYING via `lifecycle.transition_project()`, sets `currentStep=0` and `totalSteps=5`, starts the project deploy Step Functions execution, and returns 202 Accepted
    - Add the `PROJECT_DEPLOY_STATE_MACHINE_ARN` environment variable read
    - _Requirements: 2.1, 2.4_

  - [x] 2.2 Add `POST /projects/{projectId}/destroy` route to `lambda/project_management/handler.py`
    - Implement `_handle_destroy_project_infra(event, project_id)` that verifies Administrator role, verifies project status is ACTIVE, checks for active/creating clusters (reuse `_get_active_clusters` from `projects.py`), transitions to DESTROYING via `lifecycle.transition_project()`, sets `currentStep=0` and `totalSteps=5`, starts the project destroy Step Functions execution, and returns 202 Accepted
    - Add the `PROJECT_DESTROY_STATE_MACHINE_ARN` environment variable read
    - _Requirements: 3.1, 3.2, 3.3, 3.6_

  - [x] 2.3 Add `PUT /projects/{projectId}` route to `lambda/project_management/handler.py`
    - Implement `_handle_edit_project(event, project_id)` that verifies Project Admin or Administrator role, verifies project status is ACTIVE, validates `budgetLimit > 0` and `budgetType` in `("MONTHLY", "TOTAL")`, calls the updated `set_budget()` with the new `budget_type` parameter, and returns 200 with the updated project
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 2.4 Enhance `GET /projects/{projectId}` to include progress fields
    - Modify `_handle_get_project` to include a `progress` object (`currentStep`, `totalSteps`, `stepDescription`) when the project status is DEPLOYING or DESTROYING
    - _Requirements: 2.5, 2.6, 3.7, 3.8_

  - [x] 2.5 Write unit tests for new handler routes
    - Test deploy route: success (CREATED project), rejection for non-CREATED status, rejection for non-admin
    - Test destroy route: success (ACTIVE project, no clusters), rejection with active clusters, rejection for non-ACTIVE status, rejection for non-admin
    - Test edit route: success (ACTIVE project, valid budget), rejection for non-ACTIVE status, rejection for non-project-admin, validation errors for invalid budgetLimit and budgetType
    - Test GET project includes progress fields for DEPLOYING/DESTROYING statuses
    - _Requirements: 2.1, 2.4, 3.1, 3.2, 3.6, 6.4, 6.5, 6.6_

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Update budget module with budget type and immediate breach clearing
  - [x] 4.1 Modify `lambda/project_management/budget.py` to support budget type
    - Add `budget_type: str = "MONTHLY"` parameter to `set_budget()`
    - When `budget_type` is `"MONTHLY"`, keep existing `TimeUnit: "MONTHLY"` behaviour
    - When `budget_type` is `"TOTAL"`, set `TimeUnit: "ANNUALLY"` with `TimePeriod` from project creation date to `2099-12-31`
    - Store `budgetType` in the DynamoDB project record alongside `budgetLimit`
    - Validate that `budget_type` is one of `"MONTHLY"` or `"TOTAL"`; reject with `ValidationError` otherwise
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x] 4.2 Implement immediate budget breach clearing in `set_budget()`
    - After updating the AWS Budget, call `ce_client.get_cost_and_usage()` to get current spend
    - If `new_limit > current_spend`, set `budgetBreached = False` in the same DynamoDB update
    - If `new_limit <= current_spend`, retain `budgetBreached` flag and log that the budget remains exceeded
    - Log the breach clearing event with project ID, previous limit, new limit, and caller identity
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 4.3 Write unit tests for budget type and breach clearing
    - Test `set_budget()` with `budget_type="MONTHLY"` creates budget with `TimeUnit: "MONTHLY"`
    - Test `set_budget()` with `budget_type="TOTAL"` creates budget with `TimeUnit: "ANNUALLY"` and correct `TimePeriod`
    - Test budget breach clearing when new limit exceeds current spend
    - Test budget breach retained when new limit is below current spend
    - Test default budget type is MONTHLY when not specified
    - Test rejection of invalid budget type values
    - Test rejection of zero or negative budget limit
    - _Requirements: 7.1, 7.3, 7.4, 8.1, 8.2, 8.3, 8.6, 8.7_

- [x] 5. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Create project deploy Step Function handlers
  - [x] 6.1 Create `lambda/project_management/project_deploy.py` with step handlers
    - Implement `validate_project_state(event)` — verify project exists and status is DEPLOYING, update progress to step 1
    - Implement `start_cdk_deploy(event)` — start a CodeBuild project that runs `npx cdk deploy HpcProject-${PROJECT_ID} --require-approval never`, pass project parameters as environment variables, update progress to step 2
    - Implement `check_deploy_status(event)` — poll CodeBuild build status, return `deployComplete: True/False`, update progress to step 3
    - Implement `extract_stack_outputs(event)` — describe the CloudFormation stack to extract VpcId, EfsFileSystemId, S3BucketName, security group IDs, update progress to step 4
    - Implement `record_infrastructure(event)` — write infrastructure IDs to the project DynamoDB record, transition status to ACTIVE via `lifecycle.transition_project()`, update progress to step 5
    - Implement `handle_deploy_failure(event)` — transition project back to CREATED, store error message
    - Mirror the progress tracking pattern from `cluster_creation.py` (`_update_step_progress`)
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_

  - [x] 6.2 Write unit tests for project deploy step handlers
    - Test `validate_project_state` succeeds for DEPLOYING project, fails for other statuses
    - Test `record_infrastructure` writes correct fields and transitions to ACTIVE
    - Test `handle_deploy_failure` transitions back to CREATED and stores error message
    - Test progress tracking updates DynamoDB with correct step numbers and descriptions
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_

- [x] 7. Create project destroy Step Function handlers
  - [x] 7.1 Create `lambda/project_management/project_destroy.py` with step handlers
    - Implement `validate_and_check_clusters(event)` — verify project status is DESTROYING, check no active clusters remain, update progress to step 1
    - Implement `start_cdk_destroy(event)` — start a CodeBuild project that runs `npx cdk destroy HpcProject-${PROJECT_ID} --force`, update progress to step 2
    - Implement `check_destroy_status(event)` — poll CodeBuild build status, return `destroyComplete: True/False`, update progress to step 3
    - Implement `clear_infrastructure(event)` — clear infrastructure IDs (vpcId, efsFileSystemId, s3BucketName, cdkStackName) from the project record, update progress to step 4
    - Implement `archive_project(event)` — transition status to ARCHIVED via `lifecycle.transition_project()`, update progress to step 5
    - Implement `handle_destroy_failure(event)` — transition project back to ACTIVE, store error message
    - _Requirements: 3.1, 3.3, 3.4, 3.5, 3.7, 3.8_

  - [x] 7.2 Write unit tests for project destroy step handlers
    - Test `validate_and_check_clusters` succeeds with no active clusters, fails with active clusters
    - Test `clear_infrastructure` clears all infrastructure fields
    - Test `archive_project` transitions to ARCHIVED
    - Test `handle_destroy_failure` transitions back to ACTIVE and stores error message
    - _Requirements: 3.1, 3.4, 3.5_

- [x] 8. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Add CDK infrastructure for project lifecycle orchestration
  - [x] 9.1 Add CodeBuild project to `lib/foundation-stack.ts` for CDK deploy/destroy
    - Create a CodeBuild project with a Node.js environment that has CDK CLI available
    - Grant the CodeBuild project permissions to deploy/destroy CloudFormation stacks and manage project infrastructure resources (VPC, EFS, S3, security groups, CloudWatch)
    - Configure the CodeBuild project to receive project parameters as environment variables
    - _Requirements: 2.1, 3.3_

  - [x] 9.2 Add project deploy Step Functions state machine to `lib/foundation-stack.ts`
    - Create a new Lambda function for project deploy step handlers pointing to `lambda/project_management/project_deploy.py`
    - Define the state machine with steps: Validate → Start CDK Deploy → Check Status (with wait loop polling every 30s) → Extract Outputs → Record Infrastructure, with a catch block that invokes the failure handler
    - Grant the Lambda function DynamoDB read/write on Projects table, CodeBuild start/describe permissions, CloudFormation describe permissions
    - Mirror the existing cluster creation state machine pattern
    - _Requirements: 2.1, 2.5, 2.6_

  - [x] 9.3 Add project destroy Step Functions state machine to `lib/foundation-stack.ts`
    - Create a new Lambda function for project destroy step handlers pointing to `lambda/project_management/project_destroy.py`
    - Define the state machine with steps: Validate & Check Clusters → Start CDK Destroy → Check Status (with wait loop polling every 30s) → Clear Infrastructure → Archive Project, with a catch block that invokes the failure handler
    - Grant the Lambda function DynamoDB read/write on Projects and Clusters tables, CodeBuild start/describe permissions
    - _Requirements: 3.3, 3.7, 3.8_

  - [x] 9.4 Add new API Gateway routes to `lib/foundation-stack.ts`
    - Add `POST /projects/{projectId}/deploy` route with Cognito authoriser, integrated with the project management Lambda
    - Add `POST /projects/{projectId}/destroy` route with Cognito authoriser, integrated with the project management Lambda
    - Add `PUT /projects/{projectId}` route with Cognito authoriser, integrated with the project management Lambda
    - Pass the deploy and destroy state machine ARNs as environment variables to the project management Lambda
    - Grant the project management Lambda `states:StartExecution` permission on both state machines
    - Grant the project management Lambda `ce:GetCostAndUsage` permission for budget breach clearing
    - _Requirements: 2.1, 3.3, 6.4_

  - [x] 9.5 Write CDK snapshot/assertion tests for new infrastructure
    - Add tests to `test/foundation-stack.test.ts` verifying the CodeBuild project, both Step Functions state machines, new Lambda functions, and new API Gateway routes are synthesised correctly
    - Verify IAM permissions are correctly scoped
    - _Requirements: 2.1, 3.3_

- [x] 10. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement frontend project context and context-sensitive actions
  - [x] 11.1 Add persistent project context to `frontend/js/app.js`
    - Add `state.projectContext` field initialised from `localStorage.getItem('hpc_project_context')`
    - Add a project context indicator in the header area (after auth) showing `Project: {name}` or `None selected`
    - When a user clicks a project in the project list or sets a project on the clusters page, update `state.projectContext` and persist to `localStorage`
    - Pre-populate the cluster operations page project ID field from `state.projectContext`
    - Update `clearSession()` to also clear the project context from localStorage
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 11.2 Replace project list actions with context-sensitive buttons
    - Modify `loadProjects()` to render state-dependent action buttons per project row:
      - CREATED: "Deploy" button calling `POST /projects/{id}/deploy`
      - DEPLOYING: disabled actions with progress bar showing step N of M and description
      - ACTIVE: "Edit" and "Destroy" buttons
      - DESTROYING: disabled actions with progress bar showing step N of M and description
      - ARCHIVED: no action buttons, read-only row
    - Display `Project_Status` as a status badge with appropriate styling for each state
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 11.3 Implement the destroy confirmation dialog
    - Create a modal requiring the user to type the project ID to confirm destruction
    - Keep the "Destroy" confirmation button disabled until the typed value matches the project ID exactly
    - On confirmation, call `POST /projects/{id}/destroy`
    - _Requirements: 4.6, 4.7_

  - [x] 11.4 Implement the project edit dialog
    - Create a modal/panel with disabled (greyed out) inputs for projectId, projectName, costAllocationTag
    - Add editable inputs for budgetLimit (number) and budgetType (select: MONTHLY / TOTAL)
    - On save, call `PUT /projects/{id}` with the updated budgetLimit and budgetType
    - Show success/error toast on completion
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 11.5 Implement progress polling for DEPLOYING/DESTROYING projects
    - Add `startProjectListPolling()`  using `setInterval` (every 5 seconds) mirroring the existing `startClusterListPolling()` pattern
    - Poll `GET /projects` while any project is in DEPLOYING or DESTROYING status
    - Stop polling when navigating away or when no projects are in transitional states
    - Show toast notifications on status transitions (DEPLOYING→ACTIVE, DEPLOYING→CREATED on failure, DESTROYING→ARCHIVED)
    - _Requirements: 2.5, 2.7, 3.7, 4.4_

- [x] 12. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Update documentation
  - [x] 13.1 Update `docs/admin/project-management.md` with lifecycle management
    - Document the new project lifecycle states (CREATED, DEPLOYING, ACTIVE, DESTROYING, ARCHIVED) and their transitions
    - Document the deploy and destroy workflows including progress tracking
    - Document the project edit functionality (budget-only editing)
    - Document the destroy confirmation requirement
    - _Requirements: 1.2, 1.6, 2.1, 3.3, 4.6, 6.1_

  - [x] 13.2 Update `docs/project-admin/project-management.md` with budget type and editing
    - Document budget type selection (MONTHLY vs TOTAL) and its behaviour
    - Document immediate budget breach clearing when budget is increased above current spend
    - Document the project edit view with read-only and editable fields
    - _Requirements: 6.1, 7.1, 8.1, 8.2, 8.3_

  - [x] 13.3 Update `docs/api/reference.md` with new API endpoints
    - Document `POST /projects/{projectId}/deploy` — request, response, error codes
    - Document `POST /projects/{projectId}/destroy` — request, response, error codes
    - Document `PUT /projects/{projectId}` — request body (budgetLimit, budgetType), response, error codes
    - Document enhanced `GET /projects/{projectId}` response with progress fields
    - _Requirements: 2.1, 3.3, 6.4_

- [x] 14. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- The implementation uses Python for Lambda functions and TypeScript for CDK infrastructure, matching the existing codebase conventions
- The project deploy/destroy Step Functions mirror the existing cluster creation/destruction pattern for consistency
- CodeBuild is used for CDK deploy/destroy because CDK synthesis + CloudFormation deployment can exceed Lambda's 15-minute timeout
