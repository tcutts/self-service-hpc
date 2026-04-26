# Implementation Plan: Cluster Recreation

## Overview

This plan implements a cluster recreation capability that allows users to re-create previously destroyed clusters using the same name and template configuration. The implementation is incremental: backend handler logic first, then frontend changes, CDK infrastructure, documentation updates, and finally property-based tests. The feature reuses the existing creation Step Functions workflow, so no new state machine is needed.

## Tasks

- [x] 1. Add the recreate cluster handler to the cluster operations Lambda
  - [x] 1.1 Add `_handle_recreate_cluster` function to `lambda/cluster_operations/handler.py`
    - Implement the function accepting `event`, `project_id`, and `cluster_name` parameters
    - Verify caller authorisation using `is_project_user(event, project_id)`
    - Retrieve the existing cluster record using `get_cluster(CLUSTERS_TABLE_NAME, project_id, cluster_name)`
    - Validate that the cluster status is `DESTROYED`; raise `ConflictError` if not, with a message indicating the current status and that only DESTROYED clusters can be recreated
    - Parse the optional request body to extract `templateId`; if not provided or empty, fall back to the `templateId` from the destroyed cluster record
    - Check project budget breach using `check_budget_breach(PROJECTS_TABLE_NAME, project_id)`; raise `BudgetExceededError` if breached
    - Start the creation Step Functions execution with payload `{projectId, clusterName, templateId, createdBy}`
    - Return 202 Accepted with `{message, projectId, clusterName, templateId}`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 4.1, 4.2_

  - [x] 1.2 Add route matching for the recreate endpoint in the `handler()` function
    - Add an `elif` branch matching `resource == "/projects/{projectId}/clusters/{clusterName}/recreate"` and `http_method == "POST"`
    - Extract `projectId` and `clusterName` from `pathParameters`
    - Call `_handle_recreate_cluster(event, project_id, cluster_name)`
    - _Requirements: 1.1_

  - [x] 1.3 Write unit tests for the recreate handler in `test/lambda/test_unit_cluster_operations.py`
    - Add a new `TestClusterRecreation` test class with class-scoped `mock_aws` fixture
    - Test successful recreation: seed a DESTROYED cluster and non-breached project, mock SFN client, verify 202 response with correct projectId, clusterName, and templateId
    - Test recreation with template override: seed a DESTROYED cluster with templateId "tpl-old", send recreate with `{"templateId": "tpl-new"}`, verify response uses "tpl-new"
    - Test recreation with empty body: send recreate with no body, verify stored templateId is used
    - Test recreation with empty templateId in body: send `{"templateId": ""}`, verify stored templateId is used
    - Test non-existent cluster returns 404 NOT_FOUND
    - Test ACTIVE cluster returns 409 CONFLICT with status in message
    - Test CREATING cluster returns 409 CONFLICT
    - Test FAILED cluster returns 409 CONFLICT
    - Test DESTROYING cluster returns 409 CONFLICT
    - Test budget breached returns 403 BUDGET_EXCEEDED
    - Test unauthorised user returns 403 AUTHORISATION_ERROR
    - Test Administrator can recreate (Administrators group)
    - Test Project Administrator can recreate (ProjectAdmin-{projectId} group)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.2, 2.3, 3.2, 4.1, 4.2_

- [x] 2. Checkpoint — Ensure all backend tests pass
  - Run `python -m pytest test/lambda/test_unit_cluster_operations.py -v` and verify all tests pass including the new recreation tests.

- [x] 3. Write property-based tests for cluster recreation
  - [x] 3.1 Create `test/lambda/test_property_cluster_recreation.py` with property tests
    - **Property 1: Template resolution** — Generate random stored templateId and optional override templateId using Hypothesis strategies. Seed a DESTROYED cluster and non-breached project. Call the recreate handler. Verify: if override is non-empty, response templateId equals override; otherwise response templateId equals stored value. Min 50 examples. Tag: `Feature: cluster-recreation, Property 1: Template resolution uses override when provided, stored value otherwise`
    - **Property 2: Non-DESTROYED status rejection** — Generate random non-DESTROYED statuses (from `st.sampled_from(["CREATING", "ACTIVE", "FAILED", "DESTROYING"])`). Seed a cluster with that status. Call recreate. Verify 409 CONFLICT response. Min 50 examples. Tag: `Feature: cluster-recreation, Property 2: Non-DESTROYED cluster status rejects recreation`
    - **Property 3: Budget breach blocks recreation** — Generate random project IDs and cluster names. Seed a DESTROYED cluster with a breached project. Call recreate. Verify 403 BUDGET_EXCEEDED response. Min 50 examples. Tag: `Feature: cluster-recreation, Property 3: Budget breach blocks cluster recreation`
    - **Property 4: Unauthorised caller rejection** — Generate random project IDs and caller usernames. Build events where the caller's groups do not include the target project. Seed a DESTROYED cluster. Call recreate. Verify 403 AUTHORISATION_ERROR response. Min 50 examples. Tag: `Feature: cluster-recreation, Property 4: Unauthorised caller cannot recreate clusters`
    - Follow the existing pattern from `test_property_cluster_names.py` and `test_property_budget_breach_blocks_creation.py`: use `@mock_aws` decorator per test, `reload_cluster_ops_handler_modules()`, and `os.environ.update()` for table names
    - _Requirements: 1.2, 1.3, 2.3, 3.2, 4.1, 4.2_

- [x] 4. Checkpoint — Ensure all tests pass including property tests
  - Run `python -m pytest test/lambda/test_property_cluster_recreation.py -v` and verify all property tests pass.

- [x] 5. Add the recreate button to the web portal
  - [x] 5.1 Add `recreateCluster()` function to `frontend/js/app.js`
    - Implement `async function recreateCluster(projectId, clusterName)` that shows a confirmation dialog, calls `POST /projects/{projectId}/clusters/{clusterName}/recreate`, shows a success/error toast, and reloads the cluster list
    - _Requirements: 7.2, 7.3, 7.4_

  - [x] 5.2 Add "Recreate" button to the cluster list for DESTROYED clusters in `loadClusters()`
    - In the cluster row rendering, add a "Recreate" button for clusters with `status === 'DESTROYED'`
    - The button should call `recreateCluster(projectId, clusterName)` on click
    - Do not show the button if the project budget is breached (check via a project status fetch or pass budget breach status through)
    - _Requirements: 7.1, 7.5_

  - [x] 5.3 Add "Recreate" button to the cluster detail page for DESTROYED clusters in `loadClusterDetail()`
    - In the DESTROYED cluster detail view, add a "Recreate Cluster" button alongside the existing info message
    - The button should call `recreateCluster(projectId, clusterName)` on click
    - _Requirements: 7.1_

- [x] 6. Add the API Gateway route in CDK infrastructure
  - [x] 6.1 Add `POST /projects/{projectId}/clusters/{clusterName}/recreate` route to `lib/foundation-stack.ts`
    - Under the existing `{clusterName}` resource, add a `recreate` child resource
    - Add a POST method with the Cognito authoriser and the existing cluster operations Lambda integration
    - No new Lambda functions or IAM permissions are needed — the existing cluster operations Lambda already has DynamoDB and Step Functions permissions
    - _Requirements: 1.1_

  - [x] 6.2 Add CDK test assertions for the new route in `test/self-service-hpc.test.ts`
    - Verify the `recreate` API Gateway resource is synthesised under the cluster name resource
    - Verify the POST method uses the Cognito authoriser
    - _Requirements: 1.1_

- [x] 7. Checkpoint — Ensure all tests pass including CDK tests
  - Run `npx jest` and `python -m pytest test/lambda/ -v` to verify all tests pass.

- [x] 8. Update documentation
  - [x] 8.1 Add "Recreating a Cluster" section to `docs/project-admin/cluster-management.md`
    - Add a new section after "Destroying a Cluster" documenting the recreate endpoint
    - Include the endpoint URL, required role, request format (optional templateId), response format (202 Accepted), and error cases table
    - Update the "Cluster Status Lifecycle" diagram to show the DESTROYED → CREATING transition via recreation
    - _Requirements: 8.1, 8.3_

  - [x] 8.2 Add the recreate endpoint to `docs/api/reference.md`
    - Add `POST /projects/{projectId}/clusters/{clusterName}/recreate` under the Cluster Operations section
    - Document path parameters, optional request body (templateId), response schema (202 Accepted), and error codes (NOT_FOUND, CONFLICT, BUDGET_EXCEEDED, AUTHORISATION_ERROR)
    - _Requirements: 8.2_

- [x] 9. Final checkpoint — Ensure all tests pass
  - Run the full test suite: `npx jest` and `python -m pytest test/lambda/ -v` to verify everything passes.

## Notes

- The feature reuses the existing cluster creation Step Functions workflow — no new state machine is needed
- The ClusterNameRegistry's conditional put already supports same-project reuse, so recreation works without deregistering the name
- The creation workflow's `record_cluster` step uses `put_item`, which naturally overwrites the DESTROYED record
- Property-based tests follow the existing Hypothesis patterns in the test suite
- CDK infrastructure changes are minimal — just one new API Gateway route
- All documentation updates follow the existing format in `docs/`
