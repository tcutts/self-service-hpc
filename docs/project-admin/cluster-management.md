# Cluster Management

This guide covers creating, monitoring, and destroying HPC clusters within a project. Cluster operations are available to **Project Users** and **Project Administrators**.

## Overview

Clusters are ephemeral HPC environments provisioned using AWS Parallel Computing Service (PCS). Each cluster includes:

- A **login node** (head node) for SSH/DCV access and job submission
- **Compute nodes** that execute Slurm jobs, with elastic scaling
- **Project data access** at `/data` — either via **FSx for Lustre** (high-performance cache linked to the project S3 bucket) or **Mountpoint for Amazon S3** (direct S3 mount, lower cost, faster provisioning)
- **EFS home directories** mounted for each authorised user
- **Slurm accounting** enabled for job tracking
- **Dedicated IAM roles and instance profiles** for login and compute nodes (`AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`)

Clusters are created from predefined **cluster templates** that specify instance types, node counts, and software configuration. When creating a cluster, you can choose the storage mode and optionally override the template's default compute node scaling limits.

## Listing Available Templates

**Endpoint:** `GET /templates`
**Required role:** Any authenticated user

Before creating a cluster, review the available templates:

> **Tip:** The Project ID field on the Clusters page provides autocomplete suggestions from your existing projects. Start typing to see matching project IDs.

### Response (200 OK)

```json
{
  "templates": [
    {
      "templateId": "cpu-general",
      "templateName": "General CPU Workloads",
      "description": "Cost-effective CPU cluster using Graviton-based c7g.medium instances.",
      "instanceTypes": ["c7g.medium"],
      "loginInstanceType": "c7g.medium",
      "minNodes": 1,
      "maxNodes": 10
    },
    {
      "templateId": "gpu-basic",
      "templateName": "Basic GPU Workloads",
      "description": "Basic GPU cluster using NVIDIA T4-based g4dn.xlarge instances.",
      "instanceTypes": ["g4dn.xlarge"],
      "loginInstanceType": "g4dn.xlarge",
      "minNodes": 1,
      "maxNodes": 4
    }
  ]
}
```

## Creating a Cluster

**Endpoint:** `POST /projects/{projectId}/clusters`
**Required role:** Project User or Project Administrator

### Request

```json
{
  "clusterName": "genomics-run-42",
  "templateId": "cpu-general",
  "storageMode": "mountpoint",
  "lustreCapacityGiB": 2400,
  "minNodes": 2,
  "maxNodes": 20
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `clusterName` | string | Yes | Unique name for the cluster (alphanumeric, hyphens, underscores) |
| `templateId` | string | Yes | ID of the cluster template to use |
| `storageMode` | string | No | Storage mode for project data access. Valid values: `lustre` (FSx for Lustre) or `mountpoint` (Mountpoint for Amazon S3). Defaults to `mountpoint` when omitted. |
| `lustreCapacityGiB` | integer | No | FSx for Lustre storage capacity in GiB. Must be at least 1200 and a multiple of 1200 (i.e. 1200, 2400, 3600, …). Defaults to 1200. Only used when `storageMode` is `lustre`; ignored otherwise. |
| `minNodes` | integer | No | Minimum number of compute nodes. Must be >= 0 and <= `maxNodes`. When omitted, the template's default value is used. |
| `maxNodes` | integer | No | Maximum number of compute nodes. Must be >= 1 and >= `minNodes`. When omitted, the template's default value is used. |

> **Note:** AMI IDs are validated against EC2 at both template creation/update time and at cluster creation time. If an AMI does not exist in the current region or is not in the `available` state, the request is rejected with a clear validation error. This prevents late failures during compute node group creation. Auto-detected AMIs (via the "Auto-detect AMI" button) are already verified as available and do not require additional validation.

### Cluster Naming Rules

- Must be **non-empty** and contain only alphanumeric characters, hyphens (`-`), and underscores (`_`).
- Must be **globally unique across projects** — a name used by project A cannot be used by project B.
- **Can be reused within the same project** — if you previously had a cluster named `my-cluster` in your project, you can create a new one with the same name.

### What Happens

1. The cluster name is validated and checked against the global name registry.
2. The project's deployment status is checked — creation is blocked if the project has not been deployed yet (status is not `ACTIVE`). The **Create Cluster** button is greyed out with a tooltip explaining that the project must be deployed first.
3. The project's budget status is checked — creation is blocked if the budget is breached.
4. A Step Functions workflow is started to orchestrate the multi-step creation process:
   - Register the cluster name in the global registry
   - Create dedicated IAM roles and instance profiles for the cluster (`AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`)
   - Wait for instance profiles to propagate in IAM
   - **If `storageMode` is `lustre`:** Create an FSx for Lustre filesystem (with the specified `lustreCapacityGiB` capacity) and a data repository association to the project S3 bucket. The FSx filesystem is mounted at `/data` on login and compute nodes.
   - **If `storageMode` is `mountpoint`:** Skip FSx provisioning entirely. Instead, attach an inline IAM policy granting S3 access to the login and compute roles, and mount the project S3 bucket directly at `/data` on login and compute nodes using Mountpoint for Amazon S3. This is faster and lower cost than FSx for Lustre.
   - Create the PCS cluster with Slurm accounting enabled
   - Create the login node group (public subnet, static scaling) using the cluster-specific login instance profile
   - Create the compute node group (private subnet, elastic scaling) using the cluster-specific compute instance profile, with the effective `minNodes` and `maxNodes` values (from overrides or template defaults)
   - Create the PCS queue
   - Provision POSIX user accounts on all nodes
   - Tag all resources with `Project` and `ClusterName` tags
4. The cluster status is set to `CREATING` and progress is tracked in DynamoDB.

### Response (202 Accepted)

```json
{
  "message": "Cluster 'genomics-run-42' creation started.",
  "projectId": "genomics-team",
  "clusterName": "genomics-run-42",
  "templateId": "cpu-general"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Invalid cluster name format | `VALIDATION_ERROR` | 400 |
| Name used by another project | `VALIDATION_ERROR` | 400 |
| Invalid `storageMode` value | `VALIDATION_ERROR` | 400 |
| `lustreCapacityGiB` less than 1200 | `VALIDATION_ERROR` | 400 |
| `lustreCapacityGiB` not a multiple of 1200 | `VALIDATION_ERROR` | 400 |
| `minNodes` less than 0 | `VALIDATION_ERROR` | 400 |
| `maxNodes` less than 1 | `VALIDATION_ERROR` | 400 |
| `minNodes` exceeds `maxNodes` | `VALIDATION_ERROR` | 400 |
| Budget breached | `BUDGET_EXCEEDED` | 403 |
| Caller is not a project member | `AUTHORISATION_ERROR` | 403 |

## Monitoring Cluster Creation

**Endpoint:** `GET /projects/{projectId}/clusters/{clusterName}`
**Required role:** Project User or Project Administrator

While a cluster is being created, the response includes progress information:

### Response (200 OK) — During Creation

```json
{
  "clusterName": "genomics-run-42",
  "projectId": "genomics-team",
  "templateId": "cpu-general",
  "status": "CREATING",
  "createdBy": "jsmith",
  "createdAt": "2025-01-15T14:00:00Z",
  "progress": {
    "currentStep": 4,
    "totalSteps": 10,
    "stepDescription": "Creating PCS cluster"
  }
}
```

You can navigate away from the web portal and return later — the status is always available via the API.

### Cluster Status Lifecycle

```
CREATING → ACTIVE    (success — cluster ready for use)
CREATING → FAILED    (failure — resources automatically rolled back)
ACTIVE   → DESTROYING → DESTROYED
ACTIVE   → DESTROYING → DESTRUCTION_FAILED
DESTROYED → CREATING  (via POST /clusters/{clusterName}/recreate)
```

### Notifications

- **On success:** The creating user receives an email notification with the cluster name and connection details.
- **On failure:** The creating user receives an email notification with the error description. All partially created resources are automatically cleaned up.

## Listing Clusters

**Endpoint:** `GET /projects/{projectId}/clusters`
**Required role:** Project User or Project Administrator

### Response (200 OK)

```json
{
  "clusters": [
    {
      "clusterName": "genomics-run-42",
      "projectId": "genomics-team",
      "templateId": "cpu-general",
      "status": "ACTIVE",
      "createdBy": "jsmith",
      "createdAt": "2025-01-15T14:00:00Z"
    }
  ]
}
```

## Viewing Cluster Details

**Endpoint:** `GET /projects/{projectId}/clusters/{clusterName}`
**Required role:** Project User or Project Administrator

For **ACTIVE** clusters, the response includes SSH and DCV connection details:

### Response (200 OK) — Active Cluster

```json
{
  "clusterName": "genomics-run-42",
  "projectId": "genomics-team",
  "templateId": "cpu-general",
  "storageMode": "mountpoint",
  "status": "ACTIVE",
  "createdBy": "jsmith",
  "createdAt": "2025-01-15T14:00:00Z",
  "loginNodeIp": "54.123.45.67",
  "connectionInfo": {
    "ssh": "ssh -p 22 <username>@54.123.45.67",
    "dcv": "https://54.123.45.67:8443"
  }
}
```

> **Note:** Connection details are only provided for clusters in `ACTIVE` status. Clusters in `CREATING`, `FAILED`, or `DESTROYING` status do not expose connection information.

> **Note:** If the project budget has been breached, cluster details are withheld and a `BUDGET_EXCEEDED` error is returned.

## Destroying a Cluster

**Endpoint:** `DELETE /projects/{projectId}/clusters/{clusterName}`
**Required role:** Project User or Project Administrator

### What Happens

1. **If `storageMode` is `lustre`:** An FSx data repository export task syncs data back to the project S3 bucket, then the FSx for Lustre filesystem is deleted.
2. **If `storageMode` is `mountpoint`:** The FSx export and deletion steps are skipped. The Mountpoint S3 inline IAM policy is removed from the login and compute roles.
3. PCS compute node groups, queue, and cluster are deleted.
4. The cluster-specific IAM resources are cleaned up — roles are removed from instance profiles, instance profiles are deleted, managed policies are detached, and IAM roles are deleted for both the login and compute profiles.
5. The cluster record is updated to `DESTROYED` in DynamoDB.

Home directories (EFS) and project storage (S3) are **preserved** after cluster destruction.

### Response (202 Accepted)

```json
{
  "message": "Cluster 'genomics-run-42' destruction started.",
  "projectId": "genomics-team",
  "clusterName": "genomics-run-42"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Cluster not in ACTIVE, FAILED, or DESTRUCTION_FAILED status | `CONFLICT` | 409 |
| Cluster does not exist | `NOT_FOUND` | 404 |
| Caller is not a project member | `AUTHORISATION_ERROR` | 403 |

### Concurrent Deletion Prevention

Only one destruction workflow can run for a given cluster at a time. When you initiate cluster destruction, the system uses an atomic status transition to ensure that exactly one request succeeds if multiple users click "Destroy" simultaneously.

If another user has already started destroying the cluster, you will receive a **409 Conflict** error and the web portal will display a toast notification: **"This resource is already being destroyed"**. No duplicate workflow is started — the original destruction continues normally.

### Monitoring Destruction Progress

While a cluster is being destroyed, the web portal displays a progress bar in both the **cluster list table** and the **cluster detail page**. The progress bar shows the current step, total steps, a description of the operation in progress, and the percentage complete.

The destruction workflow consists of **8 steps**:

| Step | Description | What Happens |
|------|-------------|--------------|
| 1 | Exporting data to S3 | Starts an FSx data repository export task to sync filesystem data back to the project S3 bucket. Skipped for `mountpoint` clusters. |
| 2 | Checking export status | Polls the FSx export task until it completes. Skipped for `mountpoint` clusters. |
| 3 | Deleting compute resources | Initiates deletion of PCS compute node groups and the queue. |
| 4 | Waiting for resource cleanup | Polls PCS until the compute node groups and queue are fully removed. |
| 5 | Deleting cluster | Deletes the PCS cluster resource. |
| 6 | Deleting filesystem | Deletes the FSx for Lustre filesystem. Skipped for `mountpoint` clusters. |
| 7 | Cleaning up IAM and templates | Removes IAM roles, instance profiles, launch templates, Mountpoint S3 policies, and deregisters the cluster name. |
| 8 | Finalising destruction | Sets the cluster status to DESTROYED and clears progress fields. |

The page refreshes automatically during destruction — you can navigate away and return later to check progress. When destruction completes, a toast notification confirms the cluster has been destroyed.

#### API Response During Destruction

```json
{
  "clusterName": "genomics-run-42",
  "projectId": "genomics-team",
  "status": "DESTROYING",
  "progress": {
    "currentStep": 3,
    "totalSteps": 8,
    "stepDescription": "Deleting compute resources"
  }
}
```

If destruction fails at any step, the progress bar remains at the last successfully started step so you can see where the failure occurred.

## Destruction Failure Recovery

In rare cases, the destruction workflow can encounter an error that prevents it from completing. When this happens, the cluster transitions to `DESTRUCTION_FAILED` status instead of remaining stuck in `DESTROYING`.

### What `DESTRUCTION_FAILED` Means

The `DESTRUCTION_FAILED` status indicates that the cluster destruction workflow encountered an error and could not finish cleaning up all resources. The cluster is no longer being actively destroyed, but some resources may still exist in a partially deleted state.

### When It Occurs

A cluster transitions to `DESTRUCTION_FAILED` when any of the following happens during the destruction workflow:

- **Sub-resource deletion failure** — a PCS compute node group or queue deletion fails (e.g. due to a service error or conflict), and the failure is detected before entering the polling loop.
- **Polling timeout** — the PCS sub-resource deletion polling loop or the FSx data repository export polling loop exceeds its maximum retry count (~60 minutes each) without completing.
- **Unexpected API error** — a PCS or FSx API call returns an unexpected error (e.g. throttling, access denied, or an internal service error) during the polling phase.
- **Workflow timeout** — the overall Step Functions state machine exceeds its 2-hour execution timeout.

### Recovery Options

When a cluster is in `DESTRUCTION_FAILED` status, you can take the following actions:

1. **Retry destruction** — send another `DELETE /projects/{projectId}/clusters/{clusterName}` request. The destruction workflow is idempotent — it treats already-deleted resources as successful no-ops and only attempts to delete resources that still exist. This allows a retry to pick up where the previous attempt left off.

2. **Investigate the failure** — check the destruction progress to see which step failed. The progress bar shows the last step that was attempted, which can help identify the root cause (e.g. a specific PCS resource that could not be deleted).

3. **Contact an administrator** — if repeated retries fail, contact a platform administrator who can investigate the underlying AWS resources and resolve any blocking issues (e.g. resource dependencies, permission problems, or service outages).

### UI Behaviour

When a cluster transitions to `DESTRUCTION_FAILED`, the web portal provides visual feedback and recovery controls across several surfaces.

**Status Badge**

A danger-styled badge displaying `DESTRUCTION_FAILED` appears in both the cluster list table and the cluster detail page. The badge uses red background and red text styling, consistent with the existing `FAILED` badge, so the failure state is immediately visible.

**Retry Destroy Button**

A "Retry Destroy" button is available in two places:

- **Cluster list table** — in the Actions column, alongside where the "Destroy" button normally appears for active clusters.
- **Cluster detail page** — below the failure details section.

Both buttons use danger (red) styling and call the same destruction endpoint (`DELETE /projects/{projectId}/clusters/{clusterName}`). Clicking the button shows a confirmation dialog before sending the request. The destruction workflow is idempotent, so retrying picks up where the previous attempt left off.

**Toast Notification**

When the UI detects that a cluster has transitioned from `DESTROYING` to `DESTRUCTION_FAILED` during automatic polling, a toast notification appears with an error message including the cluster name. This notification uses red error styling to indicate a failure condition. The transition is detected in both the cluster list view and the cluster detail view.

**Progress Column**

In the cluster list table, the Progress column displays failure details for `DESTRUCTION_FAILED` clusters:

- The step where destruction failed (e.g. "Step 4 of 8: Waiting for resource cleanup"), drawn from the `progress` object in the API response.
- The error message text, if the cluster record includes an `errorMessage` field.

This information is styled with danger colouring, consistent with the existing display for `FAILED` clusters. On the cluster detail page, the same step and error information is shown in a dedicated error box, along with the `destructionFailedAt` timestamp and an informational message explaining that the user can retry.

### API Response — Destruction Failed

```json
{
  "clusterName": "genomics-run-42",
  "projectId": "genomics-team",
  "status": "DESTRUCTION_FAILED",
  "destructionFailedAt": "2025-01-15T16:30:00Z",
  "progress": {
    "currentStep": 4,
    "totalSteps": 8,
    "stepDescription": "Waiting for resource cleanup"
  }
}
```

## Recreating a Cluster

**Endpoint:** `POST /projects/{projectId}/clusters/{clusterName}/recreate`
**Required role:** Project User or Project Administrator

If a cluster has been destroyed, you can recreate it using the same cluster name and template configuration. Recreation starts the same creation workflow as a new cluster, provisioning fresh AWS resources while reusing the original cluster name.

### Request

The request body is optional. If you want to use the same template as the original cluster, you can send an empty body or omit the body entirely.

To override the template, provide a `templateId`:

```json
{
  "templateId": "gpu-basic",
  "storageMode": "lustre",
  "lustreCapacityGiB": 2400,
  "minNodes": 1,
  "maxNodes": 8
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `templateId` | string | No | ID of the cluster template to use. If omitted or empty, the template from the destroyed cluster record is used. |
| `storageMode` | string | No | Storage mode for project data access (`lustre` or `mountpoint`). If omitted, the `storageMode` from the destroyed cluster record is used. |
| `lustreCapacityGiB` | integer | No | FSx for Lustre storage capacity in GiB. Same validation rules as cluster creation. Only used when `storageMode` is `lustre`. |
| `minNodes` | integer | No | Minimum number of compute nodes. If omitted, the resolved template's default value is used. |
| `maxNodes` | integer | No | Maximum number of compute nodes. If omitted, the resolved template's default value is used. |

### What Happens

1. The caller's authorisation is verified — only project members can recreate clusters.
2. The existing cluster record is retrieved and its status is checked — only `DESTROYED` clusters can be recreated.
3. The template is resolved: the request body `templateId` is used if provided, otherwise the stored `templateId` from the destroyed cluster record is used.
4. The storage mode is resolved: the request body `storageMode` is used if provided, otherwise the `storageMode` from the destroyed cluster record is used. This allows you to change the storage mode on recreation.
5. The project's budget status is checked — recreation is blocked if the budget is breached.
6. The same Step Functions creation workflow used for new clusters is started, provisioning fresh AWS resources based on the resolved storage mode and scaling configuration.
7. The destroyed cluster record is overwritten with a new `CREATING` record, and progress is tracked in DynamoDB.

Home directories (EFS) and project storage (S3) are **preserved** across destruction and recreation — they do not need to be re-created.

### Response (202 Accepted)

```json
{
  "message": "Cluster 'genomics-run-42' recreation started.",
  "projectId": "genomics-team",
  "clusterName": "genomics-run-42",
  "templateId": "cpu-general"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Cluster does not exist | `NOT_FOUND` | 404 |
| Cluster not in DESTROYED status | `CONFLICT` | 409 |
| Project budget breached | `BUDGET_EXCEEDED` | 403 |
| Caller is not a project member | `AUTHORISATION_ERROR` | 403 |

## Stuck Cluster Recovery

In rare cases, a cluster can become stuck in `CREATING` status if the backend creation workflow terminates unexpectedly — for example, if the rollback handler encounters an error or the workflow exceeds its 2-hour timeout. The platform includes multiple layers of automatic and manual recovery to handle these situations.

### Automatic Detection

The system uses two mechanisms to automatically detect and recover stuck clusters:

1. **Last-resort DynamoDB update**: If the creation workflow fails and the rollback handler itself encounters an error, a direct DynamoDB SDK call (not a Lambda function) marks the cluster record as `FAILED` with an error message before the workflow terminates. This ensures the cluster record is updated even when the rollback code path fails.

2. **EventBridge timeout detection**: An EventBridge rule monitors the cluster creation Step Functions state machine for terminal execution states (timed out, failed, or aborted). When detected, a Lambda handler reads the execution input to identify the cluster, checks whether the record is still in `CREATING` status, and transitions it to `FAILED`. This covers the case where the workflow times out after 2 hours without any handler running.

In most cases, one of these mechanisms will automatically transition a stuck cluster to `FAILED` status without any user intervention.

### Staleness Warning in the UI

If a cluster has been in `CREATING` status for more than 2.5 hours (slightly longer than the 2-hour workflow timeout), the web portal displays a warning badge indicating that creation may have failed. This serves as a visual indicator that the backend workflow has likely stopped, even if the automatic recovery mechanisms have not yet updated the record.

The warning does not stop the normal status polling — if the cluster transitions to `ACTIVE` or `FAILED` in the background, the UI will update accordingly.

### Manually Marking a Cluster as Failed

When the staleness warning appears, a **Mark as Failed** button is shown next to the cluster. Clicking this button sends a request to the force-fail API endpoint:

**Endpoint:** `POST /projects/{projectId}/clusters/{clusterName}/fail`
**Required role:** Project User or Project Administrator

This endpoint transitions the cluster from `CREATING` to `FAILED` status. It only works on clusters currently in `CREATING` status — clusters in any other status are rejected.

### After Marking as Failed

Once a cluster is in `FAILED` status (whether via automatic detection or manual action), you can take the same corrective actions as any other failed cluster:

- **Destroy** the cluster to clean up any partially created resources
- **Recreate** the cluster to start a fresh creation workflow

See [Destroying a Cluster](#destroying-a-cluster) and [Recreating a Cluster](#recreating-a-cluster) for details.

## Per-Cluster IAM Roles and Instance Profiles

Each cluster is provisioned with its own dedicated IAM roles and instance profiles, created automatically during cluster creation and cleaned up during cluster destruction:

- **Login instance profile**: `AWSPCS-{projectId}-{clusterName}-login`
- **Compute instance profile**: `AWSPCS-{projectId}-{clusterName}-compute`

Both roles are granted baseline permissions required by PCS (`pcs:RegisterComputeNodeGroupInstance`), SSM (`AmazonSSMManagedInstanceCore`), and CloudWatch (`CloudWatchAgentServerPolicy`).

Because each cluster has its own isolated IAM roles, this enables future per-cluster permission customisation — for example, granting a specific cluster's compute nodes access to additional S3 paths or AWS services without affecting other clusters in the same project.

If cluster creation fails, any partially created IAM resources are automatically rolled back as part of the cleanup process.

## Security Groups

Each project has two security groups that control traffic between the PCS cluster controller (slurmctld) and the EC2 node instances (slurmd). These are created as part of the project infrastructure and shared by all clusters in the project.

**Head Node SG** (attached to login node instances):

| Port | Source | Purpose |
|------|--------|---------|
| 22 (TCP) | Trusted CIDR ranges | SSH access |
| 8443 (TCP) | Trusted CIDR ranges | DCV remote desktop |
| 6818 (TCP) | Compute Node SG | slurmctld → slurmd communication |
| 60001–63000 (TCP) | Compute Node SG | srun relay traffic from compute nodes |
| All outbound | 0.0.0.0/0 | Outbound traffic (package installs, AWS APIs) |

**Compute Node SG** (attached to the PCS cluster ENI and compute node instances):

| Port | Source | Purpose |
|------|--------|---------|
| All traffic | Head Node SG | Login node → compute node communication |
| All traffic | Self | Inter-compute-node traffic (Slurm, MPI, srun) |
| All outbound | 0.0.0.0/0 | Outbound traffic |

The Slurm port rules (6818 and 60001–63000) on the Head Node SG are required by PCS so the cluster controller can reach slurmd on login nodes and srun traffic can flow between compute and login nodes. Without these rules, PCS may repeatedly terminate and replace login node instances because the controller cannot verify node health.

## Best Practices

- **Destroy clusters when not in use** — clusters incur costs while running, even if no jobs are active.
- **Use descriptive cluster names** — names like `genomics-run-42` or `training-gpu-jan` help identify purpose.
- **Choose the right storage mode** — use `mountpoint` (the default) for general workloads that need S3 access with fast provisioning and lower cost. Use `lustre` when your workload requires high-throughput, low-latency filesystem access (e.g. large-scale parallel I/O).
- **Size Lustre capacity appropriately** — Lustre capacity is provisioned in 1200 GiB (1.2 TiB) increments. Start with the minimum (1200 GiB) and increase only if your working dataset requires it.
- **Monitor creation progress** — check the cluster status if creation takes longer than expected (typically 5–10 minutes for `mountpoint`, 10–15 minutes for `lustre`).
- **Export data before destruction** — for `lustre` clusters, the platform automatically exports FSx data to S3, but verify important results are saved. For `mountpoint` clusters, data is already in S3.
- **Recreate instead of creating new** — if you need the same cluster environment again, use the recreate action to reuse the cluster name and template configuration.

## Table Sorting and Filtering

The Clusters table in the web portal supports interactive sorting and filtering to help you locate clusters quickly.

### Sorting

Click any column header to sort the table by that column. The following columns are sortable:

- **Cluster Name** — alphabetical sort
- **Template** — alphabetical sort
- **Status** — alphabetical sort

Click a column header once to sort in ascending order. Click the same header again to sort in descending order. A sort indicator (▲ for ascending, ▼ for descending) appears next to the active column header. Clicking a different column header switches the sort to that column in ascending order.

The Progress and Actions columns are not sortable.

### Filtering

A search input is displayed above the Clusters table. Type any text to filter the table rows — only rows where at least one column value contains the search term are shown. Filtering is case-insensitive and matches partial text. For example, typing "active" will show only clusters with an ACTIVE status, and typing "gpu" will match clusters whose name or template contains "gpu".

Clear the search input to show all rows again. If no rows match the filter, a message is displayed indicating no matching results were found.

### State Preservation

Sort and filter settings are preserved during automatic data refreshes, so the table does not reset while you are monitoring cluster creation or destruction progress. Navigating to a different page resets the sort and filter to their defaults.

## Table Features

The Clusters table includes several features to improve usability when working with large numbers of rows.

### Viewport-Constrained Scrolling

The table is displayed within a scroll container that fits within the visible browser window. If the table has more rows than can fit on screen, a vertical scrollbar appears. The page header and navigation remain visible at all times — you do not need to scroll the entire page to reach the bottom of the table.

### Sticky Headers

Column headers remain fixed at the top of the table while you scroll through rows, so you can always see which column is which.

### Sorting

Click any sortable column header to sort the table. Click the same header again to reverse the sort direction. A sort indicator (▲/▼) shows the current direction. See [Table Sorting and Filtering](#table-sorting-and-filtering) above for the full list of sortable columns.

### Filtering

Type in the search input above the table to filter rows by any visible column value. Filtering is case-insensitive and matches partial text. The Actions and Progress columns are not included in filter matching.

### State Preservation

Sort and filter settings are maintained during automatic data refreshes but reset when navigating to a different page. State is held in memory only and is not persisted across browser sessions.
