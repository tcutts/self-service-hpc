# Implementation Plan: DESTRUCTION_FAILED UI Support

## Overview

Add frontend handling for the `DESTRUCTION_FAILED` cluster status across the UI: CSS badge, cluster list table (progress and actions columns), toast notifications, cluster detail page, and documentation. All changes follow existing patterns in `frontend/js/app.js` and `frontend/css/styles.css`. The `destroyCluster()` function and polling logic require no modifications — only rendering and transition detection code is extended.

## Tasks

- [x] 1. Add CSS badge class and update cluster list table rendering
  - [x] 1.1 Add `badge-destruction_failed` CSS class to `frontend/css/styles.css`
    - Add `.badge-destruction_failed { background: #f8d7da; color: var(--color-danger); }` after the existing `.badge-failed` rule
    - _Requirements: 1.3_
  - [x] 1.2 Add DESTRUCTION_FAILED case to the progress column render in `loadClusters()`
    - In `clustersTableConfig._progress.render()`, add an `else if (row.status === 'DESTRUCTION_FAILED')` branch after the existing `FAILED` branch
    - Display `progress.stepDescription` (with step X of Y) when present, and `errorMessage` when present
    - Style with `color:var(--color-danger);font-size:0.8rem` matching the existing FAILED display
    - Fall back to `'Destruction failed'` when neither field is present
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [x] 1.3 Add DESTRUCTION_FAILED to the actions column render in `loadClusters()`
    - Change the existing `['ACTIVE', 'FAILED'].includes(row.status)` check to `['ACTIVE', 'FAILED', 'DESTRUCTION_FAILED'].includes(row.status)`
    - When `row.status === 'DESTRUCTION_FAILED'`, set button label to `'Retry Destroy'` instead of `'Destroy'`
    - The button calls the same `destroyCluster()` function with `btn-danger btn-sm` classes
    - _Requirements: 2.1, 2.2, 2.3_
  - [x] 1.4 Add DESTROYING → DESTRUCTION_FAILED toast detection in `loadClusters()`
    - In the status transition detection loop, add an `else if` for `prev === 'DESTROYING' && c.status === 'DESTRUCTION_FAILED'`
    - Call `showToast()` with error type and a message including the cluster name
    - Place after the existing `DESTROYING → DESTROYED` transition check
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 2. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Add DESTRUCTION_FAILED support to the cluster detail page
  - [x] 3.1 Add `destructionFailedAt` timestamp to the detail panel header in `loadClusterDetail()`
    - Add a conditional `detail-row` for `destructionFailedAt` after the existing `destroyedAt` row
    - _Requirements: 5.3_
  - [x] 3.2 Add DESTRUCTION_FAILED error box and info box in `loadClusterDetail()`
    - After the existing `FAILED` error box block, add a `if (cluster.status === 'DESTRUCTION_FAILED')` block
    - Render an `error-box` div with heading "Destruction Failed", step info from `progress` (Step X of Y: description), `errorMessage`, and `destructionFailedAt` timestamp
    - Render an `info-box` div explaining the error and that the user can retry (destruction is idempotent)
    - _Requirements: 5.1, 5.2, 5.6_
  - [x] 3.3 Add "Retry Destroy" button for DESTRUCTION_FAILED on the detail page
    - Change the existing `['ACTIVE', 'FAILED'].includes(cluster.status)` check to `['ACTIVE', 'FAILED', 'DESTRUCTION_FAILED'].includes(cluster.status)`
    - When `cluster.status === 'DESTRUCTION_FAILED'`, set button label to `'Retry Destroy'` instead of `'Destroy Cluster'`
    - Use `btn-danger` class, calling `destroyCluster()` with the correct project ID and cluster name
    - _Requirements: 5.4, 5.5_
  - [x] 3.4 Add DESTROYING → DESTRUCTION_FAILED toast detection in `loadClusterDetail()`
    - In the status transition detection block, add an `else if` for `prev === 'DESTROYING' && cluster.status === 'DESTRUCTION_FAILED'`
    - Call `showToast()` with error type and a message including the cluster name
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 4. Verify polling behaviour (no code changes expected)
  - Confirm that `DESTRUCTION_FAILED` is NOT in the `['CREATING', 'DESTROYING']` transitional list in `loadClusters()` — so list polling stops correctly
  - Confirm that `loadClusterDetail()` only starts polling for `CREATING` and `DESTROYING` — so detail polling is not started for `DESTRUCTION_FAILED`
  - Add a comment in the polling section of `loadClusters()` noting that `DESTRUCTION_FAILED` is intentionally excluded as a terminal status
  - _Requirements: 6.1, 6.2, 6.3_

- [x] 5. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Update documentation
  - [x] 6.1 Update `docs/project-admin/cluster-management.md` Destruction Failure Recovery section
    - Add a subsection describing the UI behaviour for DESTRUCTION_FAILED: the danger badge in the cluster list and detail page, the "Retry Destroy" button in both views, the toast notification on transition, and the progress column showing where destruction failed
    - _Requirements: 7.1, 7.2, 7.3_

- [-] 7. Write tests
  - [x] 7.1 Write property test: DESTRUCTION_FAILED badge rendering (Property 1)
    - Create `test/frontend/destruction-failed.property.test.js`
    - Generate random cluster objects with status `DESTRUCTION_FAILED` and varying `clusterName` values
    - Extract the badge render logic and verify the output contains the text `DESTRUCTION_FAILED` and the CSS class `badge-destruction_failed`
    - Use fast-check with at least 100 iterations
    - **Property 1: DESTRUCTION_FAILED badge rendering**
    - **Validates: Requirements 1.1, 1.2**
  - [x] 7.2 Write property test: Retry Destroy button in actions column (Property 2)
    - In the same test file, generate random cluster objects with status `DESTRUCTION_FAILED` and varying project IDs and cluster names
    - Extract the actions column render logic and verify the output contains a "Retry Destroy" button with `btn-danger` class that calls `destroyCluster` with the correct arguments
    - **Property 2: Retry Destroy button in actions column**
    - **Validates: Requirements 2.1, 2.2, 2.3**
  - [x] 7.3 Write property test: Progress column failure display (Property 3)
    - Generate random cluster objects with status `DESTRUCTION_FAILED`, optional `progress.stepDescription`, optional `errorMessage`
    - Extract the progress column render logic and verify it displays available failure info with danger styling
    - **Property 3: Progress column failure display**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
  - [x] 7.4 Write property test: Toast notification on DESTROYING → DESTRUCTION_FAILED transition (Property 4)
    - Generate random cluster names and simulate the status cache transition from DESTROYING to DESTRUCTION_FAILED
    - Verify that `showToast` is called with an error-type message containing the cluster name
    - **Property 4: Toast notification on DESTROYING → DESTRUCTION_FAILED transition**
    - **Validates: Requirements 4.1, 4.2, 4.3**
  - [x] 7.5 Write property test: Detail page failure display (Property 5)
    - Generate random cluster objects with status `DESTRUCTION_FAILED`, varying `progress` objects and `destructionFailedAt` timestamps
    - Extract the detail page render logic and verify the error box contains step info and the failure timestamp
    - **Property 5: Detail page failure display**
    - **Validates: Requirements 5.2, 5.3**
  - [x] 7.6 Write property test: Polling treats DESTRUCTION_FAILED as terminal (Property 6)
    - Generate random sets of clusters where all statuses are in {ACTIVE, FAILED, DESTROYED, DESTRUCTION_FAILED}
    - Verify that the transitional filter produces an empty array (no polling triggered)
    - **Property 6: Polling treats DESTRUCTION_FAILED as terminal**
    - **Validates: Requirements 6.1, 6.2**
  - [-] 7.7 Write unit tests for DESTRUCTION_FAILED UI behaviour
    - Create `test/frontend/destruction-failed.test.js`
    - Test CSS class `badge-destruction_failed` exists with correct colour values (Req 1.3)
    - Test detail page renders informational retry message (Req 5.6)
    - Test detail page renders "Retry Destroy" button with `btn-danger` (Req 5.4, 5.5)
    - Test toast transition detection works in both `loadClusters` and `loadClusterDetail` code paths (Req 4.4)
    - Test detail page does not start polling for DESTRUCTION_FAILED (Req 6.3)
    - _Requirements: 1.3, 4.4, 5.4, 5.5, 5.6, 6.3_

- [ ] 8. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The `destroyCluster()` function requires no changes — it already handles the DELETE call, confirmation dialog, and 409 conflict
- Polling logic requires no code changes — `DESTRUCTION_FAILED` is already excluded from the transitional statuses list. Task 4 verifies this and adds a clarifying comment.
- Property tests use fast-check (already used in the project) and follow the pattern in existing `*.property.test.js` files
- Each task references specific requirements for traceability
