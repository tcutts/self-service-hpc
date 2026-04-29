# Project Management (Administrator)

This guide covers creating, deploying, updating, editing, and destroying projects at the platform level. Project creation and lifecycle operations require the **Administrator** role. Budget editing is also available to **Project Administrators**.

For managing project membership and budgets, see the [Project Administrator guide](../project-admin/project-management.md).

## Overview

A project is the primary organisational unit in the platform. Each project provides:

- A **dedicated VPC** for network isolation between projects
- An **EFS filesystem** for persistent per-user home directories
- An **S3 bucket** for bulk project data storage
- **Cost allocation tags** for tracking spending
- **Membership controls** — users must be explicitly added to a project

## Project Lifecycle

Every project follows a defined lifecycle. Infrastructure is not provisioned at creation time — an Administrator must explicitly deploy it after reviewing the project configuration.

### Lifecycle States

| Status | Description |
|--------|-------------|
| `CREATED` | Project record exists but no infrastructure has been provisioned. |
| `DEPLOYING` | Infrastructure provisioning is in progress (VPC, EFS, S3, security groups). |
| `ACTIVE` | Infrastructure is fully provisioned. Clusters can be created and users can work. |
| `UPDATING` | Infrastructure update is in progress. Running clusters are not affected. |
| `DESTROYING` | Infrastructure teardown is in progress. |
| `ARCHIVED` | Infrastructure has been removed. The project record is retained for audit purposes. |

### State Transitions

```
CREATED ──► DEPLOYING ──► ACTIVE ──► DESTROYING ──► ARCHIVED
                │            │  ▲        │
                └──► CREATED │  │        └──► ACTIVE
              (on failure)   ▼  │      (on failure)
                          UPDATING
                        (on success
                         or failure)
```

| From | To | Trigger |
|------|----|---------|
| CREATED | DEPLOYING | Administrator triggers deploy |
| DEPLOYING | ACTIVE | Deployment completes successfully |
| DEPLOYING | CREATED | Deployment fails (rollback) |
| ACTIVE | UPDATING | Administrator triggers update |
| UPDATING | ACTIVE | Update completes (success or failure) |
| ACTIVE | DESTROYING | Administrator triggers destroy |
| DESTROYING | ARCHIVED | Destruction completes successfully |
| DESTROYING | ACTIVE | Destruction fails (rollback) |

Any transition not listed above is rejected by the API with a descriptive error message listing the valid transitions from the current state.

## Creating a Project

**Endpoint:** `POST /projects`
**Required role:** Administrator

### Request

```json
{
  "projectId": "genomics-team",
  "projectName": "Genomics Research Team",
  "costAllocationTag": "genomics-team"
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `projectId` | string | Yes | Unique project identifier (used in resource naming) |
| `projectName` | string | Yes | Human-readable project name |
| `costAllocationTag` | string | No | Custom cost allocation tag value (defaults to `projectId`) |

### What Happens

1. The project record is created in the Projects DynamoDB table with status `CREATED`.
2. Default budget is set to $50 with budget type `MONTHLY`.
3. Infrastructure fields (vpcId, efsFileSystemId, s3BucketName) are left empty until deployment.

No cloud infrastructure is provisioned at this stage. The Administrator must explicitly deploy the project.

### Response (201 Created)

```json
{
  "projectId": "genomics-team",
  "projectName": "Genomics Research Team",
  "costAllocationTag": "genomics-team",
  "status": "CREATED",
  "budgetLimit": 50,
  "budgetType": "MONTHLY",
  "createdAt": "2025-01-15T11:00:00Z"
}
```

## Deploying a Project

**Endpoint:** `POST /projects/{projectId}/deploy`
**Required role:** Administrator

Initiates infrastructure provisioning for a project in `CREATED` status. The deployment runs asynchronously via a Step Functions state machine.

### Prerequisites

- The project must be in `CREATED` status.

### What Happens

1. The project status transitions to `DEPLOYING`.
2. A Step Functions execution starts, which:
   - Validates the project state
   - Starts a CDK deploy via CodeBuild (`HpcProject-{projectId}` stack)
   - Polls for completion
   - Extracts stack outputs (VPC ID, EFS ID, S3 bucket name)
   - Records infrastructure IDs in the project record
3. On success, the project transitions to `ACTIVE`.
4. On failure, the project transitions back to `CREATED` with an error message stored in the record.

### Progress Tracking

While the project is in `DEPLOYING` status, the `GET /projects/{projectId}` endpoint includes a `progress` object:

```json
{
  "progress": {
    "currentStep": 2,
    "totalSteps": 5,
    "stepDescription": "Starting CDK deploy"
  }
}
```

The UI polls this endpoint and displays a progress bar. You can navigate away and return later — progress is persisted in DynamoDB.

### Response (202 Accepted)

```json
{
  "message": "Project 'genomics-team' deployment started.",
  "projectId": "genomics-team",
  "status": "DEPLOYING"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in CREATED status | `CONFLICT` | 409 |
| Project does not exist | `NOT_FOUND` | 404 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

### Troubleshooting Deployment Failures

If a deployment fails, the project returns to `CREATED` status and the error message is stored in the project record. Common causes:

| Symptom | Cause | Resolution |
|---------|-------|------------|
| CodeBuild INSTALL phase fails with `npm ci` error | Source code not available in the build environment | Redeploy the foundation stack to update the CodeBuild source asset |
| CDK deploy fails with "stack not found" | The `PROJECT_ID` environment variable was not passed to CodeBuild | Check the Step Functions execution history for the `start_cdk_deploy` step |
| CloudFormation stack creation fails | IAM permissions, resource limits, or naming conflicts | Check the CloudFormation events in the AWS Console for the `HpcProject-{projectId}` stack |
| "Failed to Fetch" errors in the web portal during deployment | Browser session token expired while polling for progress | Sign out and sign back in; the portal now refreshes tokens automatically |

## Updating a Project

**Endpoint:** `POST /projects/{projectId}/update`
**Required role:** Administrator

Initiates an infrastructure update for a project in `ACTIVE` status. The update runs `cdk deploy` against the existing `HpcProject-{projectId}` CloudFormation stack, applying only the delta between the current and desired infrastructure state. This is useful when the underlying CDK code changes — for example, new security group rules, updated VPC configuration, or new resource additions.

### Prerequisites

- The project must be in `ACTIVE` status.

### What Happens

1. The project status transitions to `UPDATING`.
2. A Step Functions execution starts, which:
   - Validates the project state and snapshots the current infrastructure outputs
   - Starts a CDK deploy via CodeBuild (`HpcProject-{projectId}` stack with `--exclusively --require-approval never`)
   - Polls for completion
   - Extracts the updated CloudFormation stack outputs
   - Compares old and new outputs, logging warnings if critical resource IDs changed
   - Records the updated infrastructure IDs in the project record
3. On success, the project transitions to `ACTIVE`.
4. On failure, the project transitions back to `ACTIVE` with an error message stored in the record. CloudFormation automatically rolls back the stack to its previous known-good state.

### Cluster Safety

Updates do not disrupt running clusters. CloudFormation updates preserve resources that have stable logical IDs — this includes the VPC, EFS filesystem, S3 bucket, and security groups. Because these resources are not replaced, clusters that reference them continue to operate normally throughout the update.

While a project is in `UPDATING` status, cluster listing, cluster detail retrieval, cluster creation, and cluster destruction all remain available.

If an update changes a critical resource ID (VPC, EFS, security group, or subnet), the workflow logs a warning identifying the changed resource. Existing clusters reference the previous IDs, so this situation requires attention.

### Progress Tracking

While the project is in `UPDATING` status, the `GET /projects/{projectId}` endpoint includes a `progress` object:

```json
{
  "progress": {
    "currentStep": 2,
    "totalSteps": 5,
    "stepDescription": "Starting CDK deploy"
  }
}
```

The UI polls this endpoint and displays a progress bar. You can navigate away and return later — progress is persisted in DynamoDB.

### Response (202 Accepted)

```json
{
  "message": "Project 'genomics-team' update started.",
  "projectId": "genomics-team",
  "status": "UPDATING"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in ACTIVE status | `CONFLICT` | 409 |
| Project does not exist | `NOT_FOUND` | 404 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

### Troubleshooting Update Failures

If an update fails, the project returns to `ACTIVE` status and the error message is stored in the project record. The existing infrastructure remains intact because CloudFormation rolls back failed updates automatically. Common causes:

| Symptom | Cause | Resolution |
|---------|-------|------------|
| CodeBuild INSTALL phase fails with `npm ci` error | Source code not available in the build environment | Redeploy the foundation stack to update the CodeBuild source asset |
| CDK deploy fails with resource conflict | A resource change requires replacement but is in use | Review the CloudFormation events for the `HpcProject-{projectId}` stack and adjust the CDK code to avoid replacing in-use resources |
| CloudFormation stack update rolls back | IAM permissions, resource limits, or invalid configuration | Check the CloudFormation events in the AWS Console for the `HpcProject-{projectId}` stack |
| Update succeeds but a warning about changed resource IDs appears | The CDK code changed a construct ID, causing CloudFormation to replace a resource | Verify that existing clusters still function correctly; clusters created before the update reference the previous resource IDs |
| "Failed to Fetch" errors in the web portal during update | Browser session token expired while polling for progress | Sign out and sign back in; the portal refreshes tokens automatically |

## Editing a Project

**Endpoint:** `PUT /projects/{projectId}`
**Required role:** Project Administrator or Administrator

Only the budget fields can be edited. Project identity fields (projectId, projectName, costAllocationTag) are read-only.

### Prerequisites

- The project must be in `ACTIVE` status.

### Editable Fields

| Field | Type | Description |
|-------|------|-------------|
| `budgetLimit` | number | Budget amount in USD. Must be greater than zero. |
| `budgetType` | string | `"MONTHLY"` or `"TOTAL"`. |

### What Happens

1. The budget is updated in both AWS Budgets and the project DynamoDB record.
2. If the new budget limit exceeds the current spend, the `budgetBreached` flag is cleared immediately — users regain access without waiting for the AWS Budgets evaluation cycle.
3. If the new budget limit is still at or below current spend, the breach flag is retained.

See the [Project Administrator guide](../project-admin/project-management.md) for details on budget types and breach clearing.

### Response (200 OK)

Returns the updated project record.

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in ACTIVE status | `CONFLICT` | 409 |
| Invalid budgetLimit (zero or negative) | `VALIDATION_ERROR` | 400 |
| Invalid budgetType | `VALIDATION_ERROR` | 400 |
| Caller is not a Project Administrator or Administrator | `AUTHORISATION_ERROR` | 403 |

## Destroying a Project

**Endpoint:** `POST /projects/{projectId}/destroy`
**Required role:** Administrator

Initiates infrastructure teardown for a project in `ACTIVE` status. The destruction runs asynchronously via a Step Functions state machine.

### Prerequisites

- The project must be in `ACTIVE` status.
- All clusters in the project must be destroyed first. If active or creating clusters exist, the request is rejected.

### Destroy Confirmation

In the web UI, clicking the "Destroy" button opens a confirmation dialog. The Administrator must type the project ID exactly to enable the confirmation button. This prevents accidental destruction of project infrastructure.

### What Happens

1. The platform verifies no clusters are in `ACTIVE` or `CREATING` status.
2. The project status transitions to `DESTROYING`.
3. A Step Functions execution starts, which:
   - Validates the project state and re-checks for active clusters
   - Starts a CDK destroy via CodeBuild (`HpcProject-{projectId}` stack)
   - Polls for completion
   - Clears infrastructure IDs from the project record
   - Archives the project
4. On success, the project transitions to `ARCHIVED`.
5. On failure, the project transitions back to `ACTIVE` with an error message stored in the record.

### Concurrent Deletion Prevention

Only one destruction workflow can run for a given project at a time. When you initiate project destruction, the system uses an atomic status transition to ensure that exactly one request succeeds if multiple users click "Destroy" simultaneously.

If another user has already started destroying the project, you will receive a **409 Conflict** error and the web portal will display a toast notification: **"This resource is already being destroyed"**. No duplicate workflow is started — the original destruction continues normally.

### Progress Tracking

While the project is in `DESTROYING` status, the `GET /projects/{projectId}` endpoint includes a `progress` object, identical in format to the deploy progress. The web portal displays a progress bar in the project list table showing the current step, total steps, a description of the operation in progress, and the percentage complete.

The destruction workflow consists of **5 steps**:

| Step | Description | What Happens |
|------|-------------|--------------|
| 1 | Validating project state | Re-checks the project status and verifies no active clusters remain. |
| 2 | Starting CDK destruction | Starts a CDK destroy via CodeBuild to tear down the `HpcProject-{projectId}` CloudFormation stack. |
| 3 | Destroying infrastructure | Polls the CDK destroy operation until the CloudFormation stack deletion completes. |
| 4 | Clearing infrastructure records | Removes infrastructure IDs (VPC, EFS, S3 bucket) from the project record in DynamoDB. |
| 5 | Archiving project | Sets the project status to ARCHIVED and clears progress fields. |

The page refreshes automatically during destruction — you can navigate away and return later to check progress. When destruction completes, a toast notification confirms the project has been archived.

#### API Response During Destruction

```json
{
  "projectId": "genomics-team",
  "projectName": "Genomics Research Team",
  "status": "DESTROYING",
  "progress": {
    "currentStep": 2,
    "totalSteps": 5,
    "stepDescription": "Starting CDK destruction"
  }
}
```

If destruction fails at any step, the progress bar remains at the last successfully started step so you can see where the failure occurred.

### Response (202 Accepted)

```json
{
  "message": "Project 'genomics-team' destruction started.",
  "projectId": "genomics-team",
  "status": "DESTROYING"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in ACTIVE status | `CONFLICT` | 409 |
| Project has active clusters | `CONFLICT` | 409 |
| Project does not exist | `NOT_FOUND` | 404 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

## Listing Projects

**Endpoint:** `GET /projects`
**Required role:** Administrator

Returns all projects on the platform. Each project includes its current lifecycle status.

### Response (200 OK)

```json
{
  "projects": [
    {
      "projectId": "genomics-team",
      "projectName": "Genomics Research Team",
      "costAllocationTag": "genomics-team",
      "status": "ACTIVE",
      "budgetLimit": 5000.0,
      "budgetType": "MONTHLY",
      "budgetBreached": false,
      "createdAt": "2025-01-15T11:00:00Z"
    }
  ]
}
```

## Viewing a Project

**Endpoint:** `GET /projects/{projectId}`
**Required role:** Administrator

### Response (200 OK) — ACTIVE project

```json
{
  "projectId": "genomics-team",
  "projectName": "Genomics Research Team",
  "costAllocationTag": "genomics-team",
  "vpcId": "vpc-0abc123def456",
  "efsFileSystemId": "fs-0abc123def456",
  "s3BucketName": "hpc-project-genomics-team-data",
  "budgetLimit": 5000.0,
  "budgetType": "MONTHLY",
  "budgetBreached": false,
  "status": "ACTIVE",
  "createdAt": "2025-01-15T11:00:00Z"
}
```

### Response (200 OK) — DEPLOYING, UPDATING, or DESTROYING project

When a project is in a transitional state, the response includes a `progress` object:

```json
{
  "projectId": "genomics-team",
  "projectName": "Genomics Research Team",
  "status": "UPDATING",
  "progress": {
    "currentStep": 3,
    "totalSteps": 5,
    "stepDescription": "Polling CDK deploy status"
  },
  "createdAt": "2025-01-15T11:00:00Z"
}
```

## Deleting a Project

**Endpoint:** `DELETE /projects/{projectId}`
**Required role:** Administrator

Deletes the project record entirely. This is separate from destroying infrastructure — use `POST /projects/{projectId}/destroy` to tear down infrastructure first.

### Prerequisites

All clusters in the project must be destroyed before the project can be deleted. If active clusters exist, the request is rejected.

### What Happens

1. The platform verifies no clusters are in `ACTIVE` or `CREATING` status.
2. The project CDK stack (`HpcProject-{projectId}`) is destroyed, removing the VPC, EFS, and associated resources.
3. All membership records are removed from DynamoDB.
4. The project record is deleted from DynamoDB.
5. Cognito groups (`ProjectAdmin-{projectId}`, `ProjectUser-{projectId}`) are deleted.

### Response (200 OK)

```json
{
  "message": "Project 'genomics-team' has been deleted."
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project has active clusters | `CONFLICT` | 409 |
| Project does not exist | `NOT_FOUND` | 404 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

## Project Isolation

Each project is deployed into its own VPC, providing network-level isolation:

- Clusters in one project **cannot communicate** with clusters in another project.
- EFS home directories are accessible **only from the owning project's VPC**.
- S3 bucket policies **deny access** from principals outside the project boundary.
- FSx for Lustre filesystems are accessible **only from clusters within the owning project**.

## Cost Allocation

All project resources are tagged with `Project={costAllocationTag}`. This tag is:

- Applied automatically to all resources deployed by the project CDK stack
- Applied to all clusters and their resources created within the project
- Enabled in AWS Cost Explorer for cost reporting and attribution

## Bulk Project Actions

Administrators can select multiple projects and perform operations on all of them at once, rather than acting on each project individually.

### Selecting Projects

The Projects table includes a checkbox column. Use the checkboxes to select individual projects, or click the "Select all" checkbox in the column header to select all visible projects (respecting any active filter). Selections are preserved when you change the filter text — projects that become hidden remain selected.

When one or more projects are selected, a bulk action toolbar appears above the table showing the number of selected items and the available actions.

### Available Bulk Actions

| Button | Action | Eligible Projects |
|--------|--------|-------------------|
| Deploy All | Starts infrastructure deployment for all selected projects | Projects in `CREATED` status |
| Update All | Starts infrastructure update for all selected projects | Stale projects in `ACTIVE` status (see [Staleness Detection](#staleness-detection) below) |
| Destroy All | Starts infrastructure teardown for all selected projects | Projects in `ACTIVE` status with no active or creating clusters |
| Clear Selection | Deselects all projects and hides the toolbar | — |

Each bulk action sends a single batch request to the API. The backend processes each project sequentially and returns per-item results. Projects that are not in the required status or otherwise ineligible receive an error entry in the result — they do not block the remaining projects from being processed.

### Confirmation Dialogs

Destroy All requires you to type **CONFIRM** in a confirmation dialog before proceeding. This prevents accidental destruction of project infrastructure.

### Result Summary

After a bulk action completes, a toast notification displays a summary: "X of Y succeeded, Z failed". If any items failed, the toast uses an error style. On network errors, the selection is preserved so you can retry.

### Progress Tracking

After a bulk operation, each project that transitioned to a new status (DEPLOYING, UPDATING, or DESTROYING) displays its own independent progress bar. The portal polls for updates automatically, and each project's progress advances at its own pace.

### Batch Size Limit

Each bulk action can process up to 25 projects at a time. If you need to act on more than 25 projects, perform the operation in multiple batches.

## Staleness Detection

The platform tracks when the foundation stack was last deployed. When listing projects, the portal compares each ACTIVE project's last update timestamp (`statusChangedAt`) against the foundation stack deployment timestamp.

- If a project was last updated **at or after** the foundation stack deployment, it is considered **up to date**. The Update button is greyed out with a tooltip "Project is up to date".
- If a project was last updated **before** the foundation stack deployment, it is considered **stale**. The Update button is enabled.

This prevents unnecessary updates for projects that are already running the latest infrastructure configuration.

The "Update All" bulk action automatically filters the selection to include only stale projects. If all selected ACTIVE projects are already up to date, the "Update All" button is disabled and a toast message informs you: "All selected projects are already up to date."

## Table Sorting and Filtering

The Projects table in the web portal supports interactive sorting and filtering to help Administrators locate projects quickly.

### Sorting

Click any column header to sort the table by that column. The following columns are sortable:

- **Project ID** — alphabetical sort
- **Name** — alphabetical sort
- **Budget** — numeric sort
- **Status** — alphabetical sort

Click a column header once to sort in ascending order. Click the same header again to sort in descending order. A sort indicator (▲ for ascending, ▼ for descending) appears next to the active column header. Clicking a different column header switches the sort to that column in ascending order.

### Filtering

A search input is displayed above the Projects table. Type any text to filter the table rows — only rows where at least one column value contains the search term are shown. Filtering is case-insensitive and matches partial text. For example, typing "active" will show only projects with an ACTIVE status, and typing "genomics" will match any project whose ID or name contains "genomics".

Clear the search input to show all rows again. If no rows match the filter, a message is displayed indicating no matching results were found.

### State Preservation

Sort and filter settings are preserved during automatic data refreshes, so the table does not reset while you are monitoring project deployments or updates. Navigating to a different page resets the sort and filter to their defaults.

## Table Features

The Projects table includes several features to improve usability when working with large numbers of rows.

### Viewport-Constrained Scrolling

The table is displayed within a scroll container that fits within the visible browser window. If the table has more rows than can fit on screen, a vertical scrollbar appears. The page header and navigation remain visible at all times — you do not need to scroll the entire page to reach the bottom of the table.

### Sticky Headers

Column headers remain fixed at the top of the table while you scroll through rows, so you can always see which column is which.

### Sorting

Click any sortable column header to sort the table. Click the same header again to reverse the sort direction. A sort indicator (▲/▼) shows the current direction. See [Table Sorting and Filtering](#table-sorting-and-filtering) above for the full list of sortable columns.

### Filtering

Type in the search input above the table to filter rows by any visible column value. Filtering is case-insensitive and matches partial text. The Actions column is not included in filter matching.

### State Preservation

Sort and filter settings are maintained during automatic data refreshes but reset when navigating to a different page. State is held in memory only and is not persisted across browser sessions.
