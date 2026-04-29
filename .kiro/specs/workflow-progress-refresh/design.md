# Workflow Progress Refresh Bugfix Design

## Overview

After a user triggers a workflow action (deploy/destroy/update project, create/destroy/recreate cluster, or bulk actions), the frontend immediately calls `loadProjects()` or `loadClusters()`, but the backend hasn't updated the resource status yet. Since polling only starts when transitional states are detected in the API response, polling never begins and the UI shows no progress indicator until manual refresh.

The fix introduces a "force-polling" mechanism: after any workflow action, the system starts polling for a configurable duration regardless of the initial API response. Force-polling stops only when all resources have settled into non-transitional states **and** the minimum force-poll duration has elapsed. This is a frontend-only change confined to `frontend/js/app.js` and `frontend/js/config.js`.

## Glossary

- **Bug_Condition (C)**: A workflow action has just been triggered (deploy, destroy, update, create, recreate, or bulk variant) and the immediate API response does not yet reflect a transitional state, so polling is never started
- **Property (P)**: After any workflow action, polling SHALL be force-started for a minimum duration so the UI picks up the transitional state as soon as the backend processes it
- **Preservation**: Existing behavior that must remain unchanged — normal page loads without a preceding action trigger must only poll when transitional states are detected in the API response; navigation cleanup, toast notifications, and normal poll-stop logic must continue working
- **`loadProjects()`**: The function in `frontend/js/app.js` that fetches project data from the API and decides whether to start/stop project list polling based on transitional states
- **`loadClusters(projectId)`**: The function in `frontend/js/app.js` that fetches cluster data from the API and decides whether to start/stop cluster list polling based on transitional states
- **`state.pollTimers`**: Object in `frontend/js/app.js` that tracks active `setInterval` IDs keyed by resource identifier
- **Transitional states**: `DEPLOYING`, `DESTROYING`, `UPDATING` for projects; `CREATING`, `DESTROYING` for clusters
- **Force-poll duration**: A configurable time window (e.g., 60 seconds) during which polling continues regardless of API response content

## Bug Details

### Bug Condition

The bug manifests when a user triggers any workflow action and the immediate `loadProjects()` or `loadClusters()` call returns data where the resource has not yet transitioned to a transitional state. The polling decision logic in both functions only starts polling when it finds resources in transitional states. Since the backend hasn't updated yet, no transitional states are found, polling is never started, and the UI remains stale.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type { actionType, immediateApiResponse }
  OUTPUT: boolean

  LET actionTriggered = input.actionType IN [
    'deployProject', 'destroyProject', 'updateProject',
    'createCluster', 'destroyCluster', 'recreateCluster',
    'bulkDeployProjects', 'bulkUpdateProjects', 'bulkDestroyProjects'
  ]

  LET noTransitionalInResponse =
    (input.actionType is project-related
      AND NO resource IN input.immediateApiResponse.projects
          HAS status IN ['DEPLOYING', 'DESTROYING', 'UPDATING'])
    OR
    (input.actionType is cluster-related
      AND NO resource IN input.immediateApiResponse.clusters
          HAS status IN ['CREATING', 'DESTROYING'])

  RETURN actionTriggered AND noTransitionalInResponse
END FUNCTION
```

### Examples

- User clicks "Deploy" on a CREATED project → `deployProject()` calls API, then `loadProjects()` → API returns project still in CREATED status → no transitional state detected → polling not started → UI shows no progress bar until manual refresh
- User clicks "Destroy" on an ACTIVE cluster → `destroyCluster()` calls API, then `loadClusters()` → API returns cluster still in ACTIVE status → no transitional state detected → polling not started → UI shows no "Destroying…" indicator
- User clicks "Create Cluster" → `btn-submit-cluster` handler calls API, then `loadClusters()` → API response doesn't include the new cluster yet → no transitional state detected → polling not started
- User performs "Deploy All" bulk action → `bulkDeployProjects()` calls API, then `loadProjects()` → API returns all projects still in CREATED status → no transitional states → polling not started for any of them

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Normal page navigation to projects/clusters (without a preceding action trigger) must only start polling when transitional states are detected in the API response
- Navigating away from a page must continue to stop all active polling timers (including any force-poll timers)
- Status transition toast notifications (e.g., DEPLOYING → ACTIVE, CREATING → FAILED) must continue to fire
- Normal polling stop behavior (all resources settled into non-transitional states, no force-poll active) must continue to work
- Page refresh during an active workflow must continue to detect transitional states from the API response and start polling as before
- Mouse clicks, table rendering, sort/filter, and all other UI interactions must be unaffected

**Scope:**
All code paths that do NOT involve a workflow action trigger should be completely unaffected by this fix. This includes:
- Direct page loads / navigation to projects or clusters pages
- Manual browser refresh
- Table interactions (sort, filter, select)
- User management, template management, and accounting pages

## Hypothesized Root Cause

Based on the bug description and code analysis, the root cause is:

1. **Polling decision is purely reactive to API response**: Both `loadProjects()` (line ~808) and `loadClusters()` (line ~1920) check the current API response for transitional states and only call `startProjectListPolling()` / `startClusterListPolling()` when they find them. There is no mechanism to force polling based on a recently-triggered action.

2. **Backend processing delay**: The backend API (Lambda + Step Functions / DynamoDB) takes a non-trivial amount of time to transition a resource from its current state to a transitional state after the action API call returns. The immediate `loadProjects()` / `loadClusters()` call races against this backend processing.

3. **No action-awareness in polling logic**: The action handlers (`deployProject`, `destroyProject`, `updateProject`, `destroyCluster`, `recreateCluster`, cluster creation submit, and all bulk handlers) call `loadProjects()` / `loadClusters()` immediately after the API action succeeds, but they don't communicate to the polling logic that an action was just triggered and polling should be forced.

4. **Single-shot load after action**: Each action handler calls `loadProjects()` or `loadClusters()` exactly once after the action API call. If that single call doesn't find transitional states, there's no retry or follow-up mechanism.

## Correctness Properties

Property 1: Bug Condition - Force-Polling Starts After Workflow Action

_For any_ workflow action trigger (deploy, destroy, update project; create, destroy, recreate cluster; bulk deploy, bulk update, bulk destroy), the system SHALL force-start polling immediately after the action API call succeeds, regardless of whether the subsequent `loadProjects()` or `loadClusters()` response contains transitional states. The force-poll timer SHALL be active and the corresponding poll interval SHALL be running.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**

Property 2: Bug Condition - Force-Polling Termination

_For any_ force-polling session, the system SHALL stop force-polling only when BOTH conditions are met: (a) all resources in the API response are in non-transitional states, AND (b) the force-poll minimum duration has elapsed. If either condition is not met, polling SHALL continue.

**Validates: Requirements 2.8**

Property 3: Preservation - Normal Page Load Polling Behavior

_For any_ page load or navigation to the projects or clusters page where NO workflow action was triggered immediately before, the system SHALL use the existing polling logic (start polling only when transitional states are detected in the API response) and SHALL NOT force-start polling.

**Validates: Requirements 3.1, 3.2**

Property 4: Preservation - Navigation Cleanup

_For any_ navigation event away from the current page, the system SHALL stop all active polling timers including any force-poll timers, preserving the existing cleanup behavior.

**Validates: Requirements 3.3**

Property 5: Preservation - Toast Notifications on Status Transitions

_For any_ status transition detected during polling (e.g., DEPLOYING → ACTIVE, CREATING → FAILED), the system SHALL continue to display the appropriate toast notification, preserving the existing notification behavior.

**Validates: Requirements 3.4**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `frontend/js/config.js`

**Specific Changes**:
1. **Add force-poll duration config**: Add a `forcePollDurationMs` configuration value (e.g., 60000ms = 60 seconds) that controls how long force-polling continues after a workflow action

**File**: `frontend/js/app.js`

**Function**: Multiple action handlers + `loadProjects()` + `loadClusters()`

**Specific Changes**:
1. **Add force-poll state tracking**: Add `forceProjectPollUntil` and `forceClusterPollUntil` timestamp fields to the `state` object. When set to a future timestamp, these indicate that polling should continue regardless of API response content

2. **Modify action handlers to set force-poll flag**: In each action handler (`deployProject`, `updateProject`, `showDestroyConfirmation` confirm handler, `bulkDeployProjects`, `bulkUpdateProjects`, `bulkDestroyProjects` confirm handler, cluster creation submit handler, `destroyCluster`, `recreateCluster`), set the appropriate force-poll timestamp to `Date.now() + CONFIG.forcePollDurationMs` and call the corresponding `startXxxPolling()` function before calling `loadProjects()` / `loadClusters()`

3. **Modify `loadProjects()` polling decision**: Change the polling stop condition so that `stopProjectListPolling()` is only called when there are no transitional states **and** `Date.now() >= state.forceProjectPollUntil`. If the force-poll window is still active, continue polling even if no transitional states are found

4. **Modify `loadClusters()` polling decision**: Same change as above but for clusters — only call `stopClusterListPolling()` when there are no transitional states **and** `Date.now() >= state.forceClusterPollUntil`

5. **Modify `navigate()` cleanup**: Ensure the force-poll timestamps are reset to `null` (or `0`) when navigating away, so force-polling doesn't persist across page transitions

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that simulate workflow action triggers followed by `loadProjects()` / `loadClusters()` calls where the API returns non-transitional states. Verify that polling is NOT started on the unfixed code (demonstrating the bug).

**Test Cases**:
1. **Deploy Project Test**: Call `deployProject()`, mock API to return project in CREATED status → verify `startProjectListPolling()` is not called (will fail on unfixed code — confirms bug)
2. **Destroy Cluster Test**: Call `destroyCluster()`, mock API to return cluster in ACTIVE status → verify `startClusterListPolling()` is not called (will fail on unfixed code — confirms bug)
3. **Create Cluster Test**: Trigger cluster creation, mock API to return empty clusters list → verify `startClusterListPolling()` is not called (will fail on unfixed code — confirms bug)
4. **Bulk Deploy Test**: Call `bulkDeployProjects()`, mock API to return all projects in CREATED status → verify `startProjectListPolling()` is not called (will fail on unfixed code — confirms bug)

**Expected Counterexamples**:
- After action trigger, `state.pollTimers` has no entry for the relevant polling key
- `loadProjects()` / `loadClusters()` finds no transitional states and calls `stopXxxPolling()` instead of `startXxxPolling()`

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := triggerAction_fixed(input)
  ASSERT pollingIsActive(result)
  ASSERT forcePollingTimestampIsSet(result)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT loadProjects_original(input) = loadProjects_fixed(input)
  ASSERT loadClusters_original(input) = loadClusters_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain (various combinations of resource states)
- It catches edge cases that manual unit tests might miss (e.g., mixed transitional and non-transitional states)
- It provides strong guarantees that behavior is unchanged for all non-action-triggered loads

**Test Plan**: Observe behavior on UNFIXED code first for normal page loads with various resource state combinations, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Normal Load Preservation**: Verify that `loadProjects()` without a preceding action trigger starts/stops polling based solely on transitional states in the API response — same as before the fix
2. **Navigation Cleanup Preservation**: Verify that `navigate()` clears all poll timers including any force-poll state
3. **Toast Notification Preservation**: Verify that status transition toasts continue to fire during polling
4. **Normal Poll Stop Preservation**: Verify that when no force-poll is active and all resources are non-transitional, polling stops normally

### Unit Tests

- Test that each action handler sets the force-poll timestamp and starts polling
- Test that `loadProjects()` continues polling when force-poll is active even with no transitional states
- Test that `loadClusters()` continues polling when force-poll is active even with no transitional states
- Test that force-polling stops when both conditions are met (no transitional states + duration elapsed)
- Test that `navigate()` resets force-poll state
- Test edge cases: multiple rapid action triggers, action trigger while already force-polling

### Property-Based Tests

- Generate random project state arrays (various status combinations) and verify that without force-poll active, polling decisions match the original logic
- Generate random cluster state arrays and verify the same preservation for cluster polling
- Generate random action types and verify force-polling is always activated after any workflow action
- Generate random timestamps relative to force-poll deadline and verify correct termination behavior

### Integration Tests

- Test full flow: trigger deploy → verify force-polling starts → mock backend transition to DEPLOYING → verify progress bar appears → mock transition to ACTIVE → verify polling stops and toast fires
- Test full flow: trigger cluster creation → verify force-polling starts → mock cluster appears in CREATING → verify progress bar → mock transition to ACTIVE → verify polling stops
- Test navigation during force-polling: trigger action → navigate away → verify all timers cleared → navigate back → verify no stale force-polling
