# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Polling Not Started After Workflow Action When API Returns Non-Transitional State
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate polling is never started after a workflow action when the immediate API response lacks transitional states
  - **Scoped PBT Approach**: For each action type (deployProject, destroyProject, updateProject, createCluster, destroyCluster, recreateCluster, bulkDeployProjects, bulkUpdateProjects, bulkDestroyProjects), scope the property to concrete cases where the API response returns non-transitional states
  - **Bug Condition from design**: `isBugCondition(input)` is true when `actionTriggered AND noTransitionalInResponse` — i.e., a workflow action was just triggered and the immediate API response contains no resources in transitional states (DEPLOYING, DESTROYING, UPDATING for projects; CREATING, DESTROYING for clusters)
  - **Test approach**: Create a test file `test/frontend/workflow-progress-refresh.property.test.js` using fast-check
  - **Test setup**: Mock `apiCall` to return non-transitional states after action calls. Extract the polling decision logic from `loadProjects()` and `loadClusters()` into testable pure functions (or test via side-effect observation on `state.pollTimers`)
  - For project actions: call the action handler, mock `loadProjects()` API response with all projects in non-transitional states (CREATED, ACTIVE, ARCHIVED), assert that `state.pollTimers['project-list-poll']` is set (force-polling started)
  - For cluster actions: call the action handler, mock `loadClusters()` API response with all clusters in non-transitional states (ACTIVE, FAILED, DESTROYED), assert that `state.pollTimers['list-{projectId}']` is set (force-polling started)
  - Generate action types from the set of 9 workflow actions using `fc.constantFrom()`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists: after action triggers, no polling timer is set because `loadProjects()`/`loadClusters()` calls `stopXxxPolling()` when no transitional states are found)
  - Document counterexamples found (e.g., "After deployProject('proj-1'), state.pollTimers has no 'project-list-poll' entry")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Normal Page Load Polling Behavior Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Test approach**: Create preservation tests in the same file `test/frontend/workflow-progress-refresh.property.test.js`
  - **Observe on UNFIXED code**:
    - Observe: `loadProjects()` with all projects in non-transitional states (ACTIVE, CREATED, ARCHIVED) → `stopProjectListPolling()` is called, no poll timer set
    - Observe: `loadProjects()` with at least one project in transitional state (DEPLOYING, DESTROYING, UPDATING) → `startProjectListPolling()` is called, poll timer set
    - Observe: `loadClusters(pid)` with all clusters in non-transitional states (ACTIVE, FAILED, DESTROYED) → `stopClusterListPolling()` is called, no poll timer set
    - Observe: `loadClusters(pid)` with at least one cluster in transitional state (CREATING, DESTROYING) → `startClusterListPolling()` is called, poll timer set
    - Observe: `navigate()` clears all `state.pollTimers`
  - **Write property-based tests**:
    - Generate random arrays of project objects with statuses drawn from `fc.constantFrom('CREATED', 'ACTIVE', 'ARCHIVED', 'DEPLOYING', 'DESTROYING', 'UPDATING')` — for cases where NO force-poll is active, assert polling starts iff at least one transitional status is present
    - Generate random arrays of cluster objects with statuses drawn from `fc.constantFrom('ACTIVE', 'FAILED', 'DESTROYED', 'CREATING', 'DESTROYING')` — for cases where NO force-poll is active, assert polling starts iff at least one transitional status is present
    - Test that `navigate()` resets all poll timers (including any force-poll state after fix is applied)
  - Verify tests pass on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.5_

- [x] 3. Implement force-polling mechanism

  - [x] 3.1 Add `forcePollDurationMs` config to `frontend/js/config.js`
    - Add `forcePollDurationMs: 60000` (60 seconds) to the CONFIG object
    - This controls how long force-polling continues after a workflow action
    - _Bug_Condition: isBugCondition(input) where actionTriggered AND noTransitionalInResponse_
    - _Expected_Behavior: Force-polling duration is configurable_
    - _Preservation: No existing config values are changed_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 3.2 Add force-poll state fields to `frontend/js/app.js`
    - Add `forceProjectPollUntil: null` to the `state` object
    - Add `forceClusterPollUntil: null` to the `state` object
    - These timestamps indicate when force-polling should expire
    - When set to a future timestamp (`Date.now() + CONFIG.forcePollDurationMs`), polling continues regardless of API response
    - _Bug_Condition: No mechanism exists to force polling after action triggers_
    - _Expected_Behavior: State fields track force-poll expiry per resource type_
    - _Preservation: Existing state fields unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 3.3 Modify project action handlers to set force-poll timestamp and start polling
    - In `deployProject()`: after API call succeeds, set `state.forceProjectPollUntil = Date.now() + CONFIG.forcePollDurationMs` and call `startProjectListPolling()` before `loadProjects()`
    - In `updateProject()`: same pattern — set force-poll timestamp and start polling
    - In `showDestroyConfirmation()` confirm handler: same pattern — set force-poll timestamp and start polling
    - In `bulkDeployProjects()`: same pattern — set force-poll timestamp and start polling
    - In `bulkUpdateProjects()`: same pattern — set force-poll timestamp and start polling
    - In `bulkDestroyProjects()` confirm handler: same pattern — set force-poll timestamp and start polling
    - _Bug_Condition: Action handlers call loadProjects() without signaling that polling should be forced_
    - _Expected_Behavior: After any project action, forceProjectPollUntil is set to future timestamp and polling starts immediately_
    - _Preservation: Action handlers still call loadProjects() and show toasts as before_
    - _Requirements: 2.1, 2.2, 2.6, 2.7_

  - [x] 3.4 Modify cluster action handlers to set force-poll timestamp and start polling
    - In `btn-submit-cluster` click handler: after API call succeeds, set `state.forceClusterPollUntil = Date.now() + CONFIG.forcePollDurationMs` and call `startClusterListPolling(pid)` before `loadClusters(pid)`
    - In `destroyCluster()`: same pattern — set force-poll timestamp and start polling
    - In `recreateCluster()`: same pattern — set force-poll timestamp and start polling
    - _Bug_Condition: Action handlers call loadClusters() without signaling that polling should be forced_
    - _Expected_Behavior: After any cluster action, forceClusterPollUntil is set to future timestamp and polling starts immediately_
    - _Preservation: Action handlers still call loadClusters() and show toasts as before_
    - _Requirements: 2.3, 2.4, 2.5_

  - [x] 3.5 Modify `loadProjects()` polling decision to respect force-poll timestamp
    - Change the polling stop condition: only call `stopProjectListPolling()` when there are no transitional states **AND** `Date.now() >= (state.forceProjectPollUntil || 0)`
    - If force-poll window is still active (`Date.now() < state.forceProjectPollUntil`), continue polling even if no transitional states are found — call `startProjectListPolling()` instead of `stopProjectListPolling()`
    - When force-poll expires and no transitional states remain, reset `state.forceProjectPollUntil = null`
    - _Bug_Condition: loadProjects() stops polling when no transitional states found, even right after action trigger_
    - _Expected_Behavior: Polling continues during force-poll window regardless of API response content_
    - _Preservation: When no force-poll is active (forceProjectPollUntil is null or in the past), behavior is identical to original_
    - _Requirements: 2.1, 2.2, 2.6, 2.7, 2.8, 3.1_

  - [x] 3.6 Modify `loadClusters()` polling decision to respect force-poll timestamp
    - Same change as 3.5 but for clusters: only call `stopClusterListPolling()` when there are no transitional states **AND** `Date.now() >= (state.forceClusterPollUntil || 0)`
    - If force-poll window is still active, continue polling even if no transitional states are found
    - When force-poll expires and no transitional states remain, reset `state.forceClusterPollUntil = null`
    - _Bug_Condition: loadClusters() stops polling when no transitional states found, even right after action trigger_
    - _Expected_Behavior: Polling continues during force-poll window regardless of API response content_
    - _Preservation: When no force-poll is active, behavior is identical to original_
    - _Requirements: 2.3, 2.4, 2.5, 2.8, 3.2_

  - [x] 3.7 Modify `navigate()` to reset force-poll state on page transitions
    - In the `navigate()` function, after clearing `state.pollTimers`, also reset `state.forceProjectPollUntil = null` and `state.forceClusterPollUntil = null`
    - This ensures force-polling doesn't persist across page transitions
    - _Bug_Condition: N/A (cleanup concern)_
    - _Expected_Behavior: Force-poll state is cleaned up on navigation_
    - _Preservation: Existing navigate() cleanup behavior (clearing pollTimers, clearing TableModule state) is unchanged_
    - _Requirements: 3.3_

  - [x] 3.8 Reset force-poll state in `clearSession()`
    - In `clearSession()`, also reset `state.forceProjectPollUntil = null` and `state.forceClusterPollUntil = null`
    - Ensures force-poll state is cleaned up on logout
    - _Preservation: Existing clearSession() cleanup behavior unchanged_
    - _Requirements: 3.3_

  - [x] 3.9 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Force-Polling Starts After Workflow Action
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior: after any workflow action, polling should be active regardless of API response content
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed — force-polling is now started after every workflow action)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 3.10 Verify preservation tests still pass
    - **Property 2: Preservation** - Normal Page Load Polling Behavior Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions — normal page loads without preceding actions still use the original polling logic)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full frontend test suite: `npx jest --selectProjects frontend`
  - Ensure all existing tests (staleness, bulk-toolbar, table-module-selection, autocomplete, cluster-storage-form, members-tab-visibility) continue to pass
  - Ensure the new property tests (bug condition + preservation) both pass
  - Ask the user if questions arise
