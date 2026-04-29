# Design Document: DESTRUCTION_FAILED UI Support

## Overview

This feature adds frontend handling for the `DESTRUCTION_FAILED` cluster status across the entire UI surface: status badge, cluster list table (progress and actions columns), cluster detail page, toast notifications, and polling behaviour. The backend already supports this status and accepts retry requests — the frontend simply has no awareness of it.

All changes follow existing patterns in `frontend/js/app.js` and `frontend/css/styles.css`. The feature touches:

- **CSS**: One new badge class (`badge-destruction_failed`)
- **`loadClusters()`**: Progress column render, actions column render, polling logic, status transition detection
- **`loadClusterDetail()`**: Status-specific section rendering, polling logic, status transition detection
- **`destroyCluster()`**: No changes needed — already handles the DELETE call and 409 conflict
- **Documentation**: `docs/project-admin/cluster-management.md` updates

## Architecture

No architectural changes. This is a UI-only feature that extends existing rendering and polling patterns to cover a new status value. The data flow is unchanged:

```
API (GET /clusters) → loadClusters() → table render functions → DOM
API (GET /clusters/{name}) → loadClusterDetail() → detail panel HTML → DOM
```

The `DESTRUCTION_FAILED` status is already returned by the API. The frontend currently ignores it, rendering a bare badge with no progress, no actions, and no toast.

## Components and Interfaces

### 1. CSS Badge Class

**File**: `frontend/css/styles.css`

Add `badge-destruction_failed` following the existing `badge-failed` pattern:

```css
.badge-destruction_failed { background: #f8d7da; color: var(--color-danger); }
```

This matches `badge-failed` exactly — red background (`#f8d7da`) and red text (`var(--color-danger)`). The existing badge render pattern `badge-${status.toLowerCase()}` already produces `badge-destruction_failed` from the status string, so no JS changes are needed for badge class application.

### 2. Cluster List Table — Progress Column

**File**: `frontend/js/app.js`, inside `loadClusters()` → `clustersTableConfig._progress.render()`

Add a case for `DESTRUCTION_FAILED` in the progress column render function, after the existing `FAILED` case. Display:

- The step description from `progress.stepDescription` (where destruction failed)
- The error message from `errorMessage` (if present)
- Styled with `color: var(--color-danger)` consistent with the existing `FAILED` display

```javascript
} else if (row.status === 'DESTRUCTION_FAILED') {
  const progress = row.progress || {};
  let info = '';
  if (progress.stepDescription) {
    info += `Step ${progress.currentStep || '?'} of ${progress.totalSteps || '?'}: ${esc(progress.stepDescription)}`;
  }
  if (row.errorMessage) {
    info += (info ? '<br>' : '') + esc(row.errorMessage);
  }
  return `<span style="color:var(--color-danger);font-size:0.8rem">${info || 'Destruction failed'}</span>`;
}
```

### 3. Cluster List Table — Actions Column

**File**: `frontend/js/app.js`, inside `loadClusters()` → `clustersTableConfig._actions.render()`

Add `DESTRUCTION_FAILED` to the statuses that show a destroy button. The button text changes to "Retry Destroy" to communicate that this is a retry, but it calls the same `destroyCluster()` function:

```javascript
if (['ACTIVE', 'FAILED', 'DESTRUCTION_FAILED'].includes(row.status)) {
  const label = row.status === 'DESTRUCTION_FAILED' ? 'Retry Destroy' : 'Destroy';
  return `<button class="btn btn-danger btn-sm" onclick="destroyCluster('${esc(projectId)}','${esc(row.clusterName)}')">${label}</button>`;
}
```

### 4. Status Transition Detection — Toast Notifications

**File**: `frontend/js/app.js`, in both `loadClusters()` and `loadClusterDetail()`

Add a transition case for `DESTROYING → DESTRUCTION_FAILED` alongside the existing transition detections:

```javascript
} else if (prev === 'DESTROYING' && c.status === 'DESTRUCTION_FAILED') {
  showToast(`Cluster '${c.clusterName}' destruction FAILED`, 'error');
}
```

This follows the exact pattern of the existing `CREATING → FAILED` transition toast.

### 5. Polling Behaviour

**File**: `frontend/js/app.js`, in `loadClusters()`

The polling logic currently treats `CREATING` and `DESTROYING` as transitional. `DESTRUCTION_FAILED` is terminal — no polling needed. The existing filter already excludes it:

```javascript
const transitionalClusters = clusters.filter(c => ['CREATING', 'DESTROYING'].includes(c.status));
```

Since `DESTRUCTION_FAILED` is not in this list, it's already treated as non-transitional. **No change needed for list polling.**

For the detail page, `loadClusterDetail()` only starts polling for `CREATING` and `DESTROYING` statuses. Since `DESTRUCTION_FAILED` doesn't match either condition, **no change needed for detail polling either.**

### 6. Cluster Detail Page

**File**: `frontend/js/app.js`, in `loadClusterDetail()`

Add a `DESTRUCTION_FAILED` section after the existing `FAILED` section. This includes:

1. **Error box** showing where destruction failed (step X of Y: description)
2. **Failure timestamp** from `destructionFailedAt`
3. **Informational message** explaining the error and retry option
4. **"Retry Destroy" button** using `btn-danger` class, calling `destroyCluster()`

```javascript
// DESTRUCTION_FAILED: show failure details and retry
if (cluster.status === 'DESTRUCTION_FAILED') {
  const progress = cluster.progress || {};
  let stepInfo = '';
  if (progress.currentStep && progress.totalSteps) {
    stepInfo = `Step ${progress.currentStep} of ${progress.totalSteps}: ${esc(progress.stepDescription || 'Unknown step')}`;
  }
  html += `<div class="error-box">
    <h4>Destruction Failed</h4>
    ${stepInfo ? `<p>${stepInfo}</p>` : ''}
    ${cluster.errorMessage ? `<p>${esc(cluster.errorMessage)}</p>` : ''}
    ${cluster.destructionFailedAt ? `<p style="font-size:0.8rem;color:var(--color-text-muted)">Failed at: ${esc(cluster.destructionFailedAt)}</p>` : ''}
  </div>`;
  html += `<div class="info-box">
    <p>The cluster destruction workflow encountered an error and could not finish cleaning up all resources. You can retry the destruction — the workflow is idempotent and will pick up where it left off.</p>
  </div>`;
}
```

Also add `DESTRUCTION_FAILED` to the statuses that show a destroy/retry button:

```javascript
if (['ACTIVE', 'FAILED', 'DESTRUCTION_FAILED'].includes(cluster.status)) {
  const label = cluster.status === 'DESTRUCTION_FAILED' ? 'Retry Destroy' : 'Destroy Cluster';
  html += `<div style="margin-top:1rem">
    <button class="btn btn-danger" onclick="destroyCluster('${esc(projectId)}','${esc(clusterName)}')">${label}</button>
  </div>`;
}
```

And add the `destructionFailedAt` timestamp to the detail panel header when present:

```javascript
${cluster.destructionFailedAt ? `<div class="detail-row"><span class="label">Destruction Failed At</span><span>${esc(cluster.destructionFailedAt)}</span></div>` : ''}
```

### 7. Documentation Updates

**File**: `docs/project-admin/cluster-management.md`

Update the "Destruction Failure Recovery" section to describe the UI behaviour:
- The `DESTRUCTION_FAILED` badge appears in the cluster list and detail page
- A "Retry Destroy" button is available in both views
- A toast notification alerts the user when destruction fails
- The progress column shows where destruction failed

## Data Models

No new data models. The feature consumes existing API response fields:

| Field | Source | Usage |
|-------|--------|-------|
| `status` | Cluster object | Badge rendering, conditional logic |
| `progress.currentStep` | Cluster object | Progress column, detail error box |
| `progress.totalSteps` | Cluster object | Progress column, detail error box |
| `progress.stepDescription` | Cluster object | Progress column, detail error box |
| `errorMessage` | Cluster object | Progress column, detail error box |
| `destructionFailedAt` | Cluster object | Detail page timestamp display |

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: DESTRUCTION_FAILED badge rendering

*For any* cluster object with status `DESTRUCTION_FAILED`, the status badge render function SHALL produce HTML containing the text "DESTRUCTION_FAILED" and the CSS class `badge-destruction_failed`.

**Validates: Requirements 1.1, 1.2**

### Property 2: Retry Destroy button in actions column

*For any* cluster object with status `DESTRUCTION_FAILED`, the actions column render function SHALL produce HTML containing a "Retry Destroy" button with the `btn-danger` CSS class that invokes `destroyCluster` with the correct project ID and cluster name.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 3: Progress column failure display

*For any* cluster object with status `DESTRUCTION_FAILED`, the progress column render function SHALL display available failure information — including the step description from `progress.stepDescription` when present and the error message from `errorMessage` when present — styled with danger colouring.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

### Property 4: Toast notification on DESTROYING → DESTRUCTION_FAILED transition

*For any* cluster whose cached status is `DESTROYING` and whose polled status is `DESTRUCTION_FAILED`, the frontend SHALL display a toast notification with error styling that includes the cluster name.

**Validates: Requirements 4.1, 4.2, 4.3**

### Property 5: Detail page failure display

*For any* cluster with status `DESTRUCTION_FAILED` that includes a `progress` object and a `destructionFailedAt` timestamp, the cluster detail page SHALL render an error box showing the failed step information and the failure timestamp.

**Validates: Requirements 5.2, 5.3**

### Property 6: Polling treats DESTRUCTION_FAILED as terminal

*For any* set of clusters where all statuses are in the terminal set {ACTIVE, FAILED, DESTROYED, DESTRUCTION_FAILED}, the frontend SHALL not maintain active polling timers for the cluster list.

**Validates: Requirements 6.1, 6.2**

## Error Handling

No new error paths. The existing `destroyCluster()` function already handles:

- **409 Conflict**: Shows "This resource is already being destroyed" toast
- **Network/API errors**: Shows the error message in a toast
- **Confirmation dialog**: Prompts before sending the DELETE request

The retry flow reuses this function entirely — clicking "Retry Destroy" calls `destroyCluster()` which shows the confirmation dialog, sends the DELETE, and handles all error cases identically to a first-time destroy.

## Testing Strategy

**Property-based testing library**: [fast-check](https://github.com/dubzzz/fast-check) (JavaScript)

### Property-Based Tests

Each correctness property above maps to a property-based test. Tests generate random cluster objects with varying names, progress data, error messages, and timestamps to verify the render functions produce correct output for all valid inputs.

- Minimum **100 iterations** per property test
- Each test tagged with: **Feature: destruction-failed-ui, Property {N}: {description}**
- Tests exercise the render functions extracted from the table config, not the full DOM

### Unit Tests (Example-Based)

- CSS class `badge-destruction_failed` exists with correct colour values (Req 1.3)
- Detail page renders informational retry message (Req 5.6)
- Detail page renders "Retry Destroy" button with `btn-danger` (Req 5.4, 5.5)
- Toast transition detection works in both `loadClusters` and `loadClusterDetail` code paths (Req 4.4)
- Detail page does not start polling for DESTRUCTION_FAILED (Req 6.3)
- Documentation contains DESTRUCTION_FAILED description (Req 7.1, 7.2, 7.3)

### Integration Tests

- End-to-end flow: cluster in DESTRUCTION_FAILED → click Retry Destroy → confirm → API call → polling starts → cluster transitions to DESTROYING
