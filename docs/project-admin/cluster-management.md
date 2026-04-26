# Cluster Management

This guide covers creating, monitoring, and destroying HPC clusters within a project. Cluster operations are available to **Project Users** and **Project Administrators**.

## Overview

Clusters are ephemeral HPC environments provisioned using AWS Parallel Computing Service (PCS). Each cluster includes:

- A **login node** (head node) for SSH/DCV access and job submission
- **Compute nodes** that execute Slurm jobs, with elastic scaling
- An **FSx for Lustre** filesystem linked to the project's S3 storage bucket
- **EFS home directories** mounted for each authorised user
- **Slurm accounting** enabled for job tracking

Clusters are created from predefined **cluster templates** that specify instance types, node counts, and software configuration.

## Listing Available Templates

**Endpoint:** `GET /templates`
**Required role:** Any authenticated user

Before creating a cluster, review the available templates:

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
  "templateId": "cpu-general"
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `clusterName` | string | Yes | Unique name for the cluster (alphanumeric, hyphens, underscores) |
| `templateId` | string | Yes | ID of the cluster template to use |

### Cluster Naming Rules

- Must be **non-empty** and contain only alphanumeric characters, hyphens (`-`), and underscores (`_`).
- Must be **globally unique across projects** — a name used by project A cannot be used by project B.
- **Can be reused within the same project** — if you previously had a cluster named `my-cluster` in your project, you can create a new one with the same name.

### What Happens

1. The cluster name is validated and checked against the global name registry.
2. The project's budget status is checked — creation is blocked if the budget is breached.
3. A Step Functions workflow is started to orchestrate the multi-step creation process:
   - Register the cluster name in the global registry
   - Create an FSx for Lustre filesystem with a data repository association to the project S3 bucket
   - Create the PCS cluster with Slurm accounting enabled
   - Create the login node group (public subnet, static scaling)
   - Create the compute node group (private subnet, elastic scaling)
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

1. An FSx data repository export task syncs data back to the project S3 bucket.
2. PCS compute node groups, queue, and cluster are deleted.
3. The FSx for Lustre filesystem is deleted.
4. The cluster record is updated to `DESTROYED` in DynamoDB.

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
  "templateId": "gpu-basic"
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `templateId` | string | No | ID of the cluster template to use. If omitted or empty, the template from the destroyed cluster record is used. |

### What Happens

1. The caller's authorisation is verified — only project members can recreate clusters.
2. The existing cluster record is retrieved and its status is checked — only `DESTROYED` clusters can be recreated.
3. The template is resolved: the request body `templateId` is used if provided, otherwise the stored `templateId` from the destroyed cluster record is used.
4. The project's budget status is checked — recreation is blocked if the budget is breached.
5. The same Step Functions creation workflow used for new clusters is started, provisioning fresh AWS resources (FSx filesystem, PCS cluster, node groups, queue).
6. The destroyed cluster record is overwritten with a new `CREATING` record, and progress is tracked in DynamoDB.

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

## Best Practices

- **Destroy clusters when not in use** — clusters incur costs while running, even if no jobs are active.
- **Use descriptive cluster names** — names like `genomics-run-42` or `training-gpu-jan` help identify purpose.
- **Monitor creation progress** — check the cluster status if creation takes longer than expected (typically 10–15 minutes).
- **Export data before destruction** — the platform automatically exports FSx data to S3, but verify important results are saved.
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
