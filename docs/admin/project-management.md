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

### Progress Tracking

While the project is in `DESTROYING` status, the `GET /projects/{projectId}` endpoint includes a `progress` object, identical in format to the deploy progress. The UI displays a progress bar and polls every 5 seconds.

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
