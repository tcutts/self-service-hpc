# Requirements Document

## Introduction

The backend cluster destruction workflow already supports a `DESTRUCTION_FAILED` status — when destruction encounters sub-resource deletion failures, polling timeouts, or unexpected API errors, the cluster transitions from DESTROYING to DESTRUCTION_FAILED. The destroy API endpoint accepts clusters in DESTRUCTION_FAILED status for retry. However, the frontend UI has no handling for this status: no status badge, no action buttons, no progress/error display, no toast notifications, and no detail page support. Users whose cluster destruction fails see no visual feedback and have no way to retry from the UI.

This feature adds full frontend support for the DESTRUCTION_FAILED cluster status, following the existing patterns used for FAILED (creation failure) and the "Mark as Failed" stale-cluster recovery flow.

## Glossary

- **Frontend**: The single-page vanilla JavaScript application (`frontend/js/app.js`) that renders cluster lists, detail pages, progress bars, and action buttons.
- **Cluster_List_Table**: The table rendered by `loadClusters()` showing all clusters for a project, with columns for name, template, status, progress, and actions.
- **Cluster_Detail_Page**: The page rendered by `loadClusterDetail()` showing full details for a single cluster, including status, progress, connection info, and action buttons.
- **Status_Badge**: The inline `<span class="badge badge-{status}">` element that displays a cluster's current status with colour-coded styling.
- **Progress_Column**: The "Progress" column in the Cluster_List_Table that renders progress bars for transitional states and error messages for failed states.
- **Actions_Column**: The "Actions" column in the Cluster_List_Table that renders context-sensitive buttons (Destroy, Recreate, Mark as Failed) based on cluster status.
- **Toast_Notification**: The transient in-app notification displayed via `showToast()` to inform the user of status transitions or action results.
- **Polling**: The periodic GET requests the Frontend makes to refresh cluster data during transitional states (CREATING, DESTROYING).
- **Destroy_API**: The existing `DELETE /projects/{projectId}/clusters/{clusterName}` endpoint that initiates cluster destruction and accepts clusters in ACTIVE, FAILED, or DESTRUCTION_FAILED status.
- **destroyCluster_Function**: The existing `destroyCluster()` JavaScript function that calls the Destroy_API and handles the response.

## Requirements

### Requirement 1: DESTRUCTION_FAILED Status Badge

**User Story:** As a platform user, I want to see a clearly styled danger badge when a cluster is in DESTRUCTION_FAILED status, so that I can immediately recognise that destruction has failed.

#### Acceptance Criteria

1. WHEN a cluster has status DESTRUCTION_FAILED, THE Frontend SHALL render a Status_Badge with the text "DESTRUCTION_FAILED".
2. THE Frontend SHALL apply a CSS class `badge-destruction_failed` to the Status_Badge for clusters in DESTRUCTION_FAILED status.
3. THE `badge-destruction_failed` CSS class SHALL use danger/error styling (red background and red text) consistent with the existing `badge-failed` class.

### Requirement 2: Retry Destroy Button in Cluster List Actions Column

**User Story:** As a platform user, I want a "Retry Destroy" button in the cluster list table when a cluster is in DESTRUCTION_FAILED status, so that I can retry the destruction without leaving the list view.

#### Acceptance Criteria

1. WHEN a cluster has status DESTRUCTION_FAILED, THE Actions_Column SHALL render a "Retry Destroy" button.
2. WHEN the user clicks the "Retry Destroy" button, THE Frontend SHALL call the destroyCluster_Function with the cluster's project ID and cluster name.
3. THE "Retry Destroy" button SHALL use the `btn-danger` CSS class, consistent with the existing "Destroy" button for ACTIVE and FAILED clusters.

### Requirement 3: Failure Information in Cluster List Progress Column

**User Story:** As a platform user, I want to see failure details in the progress column when a cluster is in DESTRUCTION_FAILED status, so that I can understand what went wrong without navigating to the detail page.

#### Acceptance Criteria

1. WHEN a cluster has status DESTRUCTION_FAILED, THE Progress_Column SHALL display the destruction failure information.
2. WHEN the cluster record includes a `progress` object with `stepDescription`, THE Progress_Column SHALL display the step description indicating where destruction failed.
3. WHEN the cluster record includes an `errorMessage` field, THE Progress_Column SHALL display the error message text.
4. THE failure information SHALL be styled with danger colouring (using `var(--color-danger)`) consistent with the existing FAILED status error display.

### Requirement 4: Toast Notification for DESTROYING to DESTRUCTION_FAILED Transition

**User Story:** As a platform user, I want to be notified when a cluster transitions from DESTROYING to DESTRUCTION_FAILED, so that I know destruction has failed and I need to take action.

#### Acceptance Criteria

1. WHEN the Frontend detects a cluster status transition from DESTROYING to DESTRUCTION_FAILED via Polling, THE Frontend SHALL display a Toast_Notification with an error message indicating that cluster destruction has failed.
2. THE Toast_Notification SHALL include the cluster name in the message.
3. THE Toast_Notification SHALL use the error style (red styling) to indicate a failure condition.
4. THE status transition detection SHALL work in both the Cluster_List_Table polling and the Cluster_Detail_Page polling.

### Requirement 5: DESTRUCTION_FAILED Support on Cluster Detail Page

**User Story:** As a platform user, I want the cluster detail page to show failure details and a retry option when a cluster is in DESTRUCTION_FAILED status, so that I can investigate the failure and retry destruction.

#### Acceptance Criteria

1. WHEN the Cluster_Detail_Page loads a cluster with status DESTRUCTION_FAILED, THE Cluster_Detail_Page SHALL render the Status_Badge with `badge-destruction_failed` styling.
2. WHEN the cluster record includes a `progress` object, THE Cluster_Detail_Page SHALL display an error box showing the step where destruction failed (e.g. "Step X of Y: description").
3. WHEN the cluster record includes a `destructionFailedAt` timestamp, THE Cluster_Detail_Page SHALL display the failure timestamp.
4. THE Cluster_Detail_Page SHALL render a "Retry Destroy" button that calls the destroyCluster_Function.
5. THE "Retry Destroy" button SHALL use the `btn-danger` CSS class.
6. THE Cluster_Detail_Page SHALL display an informational message explaining that the cluster destruction encountered an error and that the user can retry.

### Requirement 6: Polling Behaviour for DESTRUCTION_FAILED Status

**User Story:** As a platform user, I want the UI to stop polling when a cluster reaches DESTRUCTION_FAILED status, so that the browser does not make unnecessary API requests for a non-transitional state.

#### Acceptance Criteria

1. THE Frontend SHALL treat DESTRUCTION_FAILED as a non-transitional (terminal) status for polling purposes.
2. WHEN all clusters in the Cluster_List_Table are in non-transitional statuses (ACTIVE, FAILED, DESTROYED, DESTRUCTION_FAILED), THE Frontend SHALL stop Polling for the cluster list (unless a force-poll window is active).
3. WHEN the Cluster_Detail_Page displays a cluster in DESTRUCTION_FAILED status, THE Frontend SHALL NOT start Polling for the cluster detail.

### Requirement 7: Documentation Updates

**User Story:** As a platform user, I want the documentation to describe the DESTRUCTION_FAILED status in the UI, so that I understand what the status means and how to recover.

#### Acceptance Criteria

1. THE documentation SHALL describe the DESTRUCTION_FAILED status badge and its meaning in the cluster management guide.
2. THE documentation SHALL describe the "Retry Destroy" button and the retry workflow for clusters in DESTRUCTION_FAILED status.
3. THE documentation SHALL be updated in the `docs/` directory alongside the code changes.
