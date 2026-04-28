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

### Cluster Naming Rules

- Must be **non-empty** and contain only alphanumeric characters, hyphens (`-`), and underscores (`_`).
- Must be **globally unique across projects** — a name used by project A cannot be used by project B.
- **Can be reused within the same project** — if you previously had a cluster named `my-cluster` in your project, you can create a new one with the same name.

### What Happens

1. The cluster name is validated and checked against the global name registry.
2. The project's budget status is checked — creation is blocked if the budget is breached.
3. A Step Functions workflow is started to orchestrate the multi-step creation process:
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
| Cluster not in ACTIVE or FAILED status | `CONFLICT` | 409 |
| Cluster does not exist | `NOT_FOUND` | 404 |
| Caller is not a project member | `AUTHORISATION_ERROR` | 403 |

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
