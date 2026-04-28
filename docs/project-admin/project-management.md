# Project Membership and Budgets

This guide covers managing project membership, budget limits, and project editing. These operations require the **Project Administrator** role for the target project. Platform Administrators also have access.

## Overview

As a Project Administrator, you control:

- **Who can access your project** — adding and removing project members
- **What roles members have** — Project Administrator or Project User
- **How much the project can spend** — setting budget limits with automatic alerts
- **Budget type** — choosing between monthly or total project lifetime budgets

## Managing Members

Membership operations require the **Project Administrator** role for the target project. Platform Administrators can also manage members on any project. End Users cannot add, remove, or change member roles.

### Listing Members

**Endpoint:** `GET /projects/{projectId}/members`
**Required role:** Project Administrator (for this project) or Platform Administrator

Returns all current members of the project, sorted by the date they were added.

#### Response (200 OK)

```json
[
  {
    "userId": "jsmith",
    "displayName": "Jane Smith",
    "role": "PROJECT_ADMIN",
    "addedAt": "2025-01-10T09:00:00Z"
  },
  {
    "userId": "bwilson",
    "displayName": "Bob Wilson",
    "role": "PROJECT_USER",
    "addedAt": "2025-01-15T12:00:00Z"
  }
]
```

Each member entry includes:

| Field | Description |
|-------|-------------|
| `userId` | The platform user ID |
| `displayName` | The user's display name (falls back to userId if unavailable) |
| `role` | `PROJECT_ADMIN` or `PROJECT_USER` |
| `addedAt` | ISO 8601 timestamp of when the user was added to the project |

### Adding a Member

**Endpoint:** `POST /projects/{projectId}/members`
**Required role:** Project Administrator (for this project)

#### Request

```json
{
  "userId": "jsmith",
  "role": "PROJECT_USER"
}
```

#### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `userId` | string | Yes | The platform user ID to add |
| `role` | string | No | `PROJECT_ADMIN` or `PROJECT_USER` (defaults to `PROJECT_USER`) |

#### What Happens

1. The platform verifies the user exists and is active on the platform.
2. The user is added to the appropriate Cognito group (`ProjectAdmin-{projectId}` or `ProjectUser-{projectId}`).
3. A membership record is created in the Projects DynamoDB table.
4. If the project has active clusters, the user's POSIX account is propagated to all cluster nodes via SSM Run Command.

#### Response (201 Created)

```json
{
  "projectId": "genomics-team",
  "userId": "jsmith",
  "role": "PROJECT_USER",
  "addedAt": "2025-01-15T12:00:00Z"
}
```

#### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| User does not exist on the platform | `NOT_FOUND` | 404 |
| User is already a member | `DUPLICATE_ERROR` | 409 |
| Caller is not a Project Administrator | `AUTHORISATION_ERROR` | 403 |

### Removing a Member

**Endpoint:** `DELETE /projects/{projectId}/members/{userId}`
**Required role:** Project Administrator (for this project)

#### What Happens

1. The user is removed from the Cognito project group.
2. The membership record is deleted from DynamoDB.

Note: The user's files on EFS home directories are **not deleted** when they are removed from a project. An administrator can manage these files manually if needed.

#### Response (200 OK)

```json
{
  "message": "User 'jsmith' removed from project 'genomics-team'."
}
```

### Changing a Member's Role

**Endpoint:** `PUT /projects/{projectId}/members/{userId}`
**Required role:** Project Administrator (for this project) or Platform Administrator

Changes a member's role between `PROJECT_ADMIN` and `PROJECT_USER`.

#### Request

```json
{
  "role": "PROJECT_ADMIN"
}
```

#### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | Yes | `PROJECT_ADMIN` or `PROJECT_USER` |

#### What Happens

1. The platform verifies the user is an existing member of the project.
2. The user is removed from their current Cognito group (`ProjectAdmin-{projectId}` or `ProjectUser-{projectId}`).
3. The user is added to the new Cognito group matching the requested role.
4. The membership record in DynamoDB is updated with the new role.

If the user already has the requested role, the request is a no-op and returns the current record unchanged.

#### Response (200 OK)

```json
{
  "userId": "jsmith",
  "projectId": "genomics-team",
  "role": "PROJECT_ADMIN",
  "addedAt": "2025-01-15T12:00:00Z"
}
```

#### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| User is not a member of the project | `NOT_FOUND` | 404 |
| Invalid role value | `VALIDATION_ERROR` | 400 |
| Caller is not a Project Administrator | `AUTHORISATION_ERROR` | 403 |

### Roles

| Role | Capabilities |
|------|-------------|
| `PROJECT_ADMIN` | Manage members, set budgets, edit project, create/destroy clusters, access clusters, manage data |
| `PROJECT_USER` | Create/destroy clusters, access clusters, manage data |

A Project Administrator has all the capabilities of a Project User, plus the ability to manage membership, budgets, and project settings.

## Editing a Project

**Endpoint:** `PUT /projects/{projectId}`
**Required role:** Project Administrator (for this project) or Administrator

The project edit view allows you to update budget settings while keeping project identity fields read-only. The project must be in `ACTIVE` status.

### Read-Only Fields

The following fields are displayed but cannot be changed:

| Field | Description |
|-------|-------------|
| `projectId` | Unique project identifier |
| `projectName` | Human-readable project name |
| `costAllocationTag` | AWS cost allocation tag value |

These fields are shown as disabled (greyed out) inputs in the UI to indicate they are not editable.

### Editable Fields

| Field | Type | Description |
|-------|------|-------------|
| `budgetLimit` | number | Budget amount in USD. Must be greater than zero. |
| `budgetType` | string | `"MONTHLY"` or `"TOTAL"` (see [Budget Types](#budget-types) below). |

### Request

```json
{
  "budgetLimit": 5000,
  "budgetType": "MONTHLY"
}
```

### Response (200 OK)

Returns the updated project record including all fields.

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in ACTIVE status | `CONFLICT` | 409 |
| budgetLimit is zero or negative | `VALIDATION_ERROR` | 400 |
| budgetType is not MONTHLY or TOTAL | `VALIDATION_ERROR` | 400 |
| Caller is not a Project Administrator or Administrator | `AUTHORISATION_ERROR` | 403 |

## Managing Budgets

### Setting a Budget Limit

**Endpoint:** `PUT /projects/{projectId}/budget`
**Required role:** Project Administrator (for this project)

#### Request

```json
{
  "budgetLimit": 5000.00
}
```

#### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `budgetLimit` | number | Yes | Budget limit in USD. Must be greater than zero. |

#### What Happens

1. An AWS Budget is created (or updated) filtered by the project's cost allocation tag (`Project={projectId}`).
2. Notification thresholds are configured:
   - **80% threshold** — email notification sent to the Project Administrator
   - **100% threshold** — email notification sent to the Project Administrator and all platform Administrators
3. The budget limit is stored in the project's DynamoDB record.

#### Response (200 OK)

```json
{
  "projectId": "genomics-team",
  "budgetLimit": 5000.0,
  "message": "Budget limit set to $5000.00 for project 'genomics-team'."
}
```

### Budget Types

Projects support two budget types that control how spending is tracked:

| Budget Type | Behaviour |
|-------------|-----------|
| `MONTHLY` | Budget resets at the start of each calendar month. This is the default. |
| `TOTAL` | Budget covers the entire project lifetime without resetting. Useful for fixed-funding projects. |

#### MONTHLY Budget

When `budgetType` is `MONTHLY`, the AWS Budget is configured with `TimeUnit: MONTHLY`. Spending is tracked per calendar month and the budget resets automatically on the first of each month.

This is the default budget type. New projects are created with a $50 MONTHLY budget.

#### TOTAL Budget

When `budgetType` is `TOTAL`, the AWS Budget is configured with `TimeUnit: ANNUALLY` and a time period spanning from the project creation date to a far-future date. This effectively tracks cumulative spending across the entire project lifetime without resetting.

Use this for projects with a fixed total funding allocation (e.g., a grant-funded research project with a $100,000 total budget).

#### Changing Budget Type

You can change the budget type at any time via the project edit view (`PUT /projects/{projectId}`). When the budget type changes, the AWS Budget is recreated with the new time configuration.

### Immediate Budget Breach Clearing

When you increase the budget limit above the current spend, the platform clears the `budgetBreached` flag immediately in the same API request. This means:

- **Cluster creation is restored** — users can create new clusters right away.
- **Cluster access is restored** — SSH/DCV connection details are available again.
- **No waiting required** — you do not need to wait for the AWS Budgets asynchronous evaluation cycle.

The platform compares the new budget limit against the current actual spending reported by AWS Cost Explorer. If the new limit exceeds current spend, the breach is cleared. If the new limit is still at or below current spend, the breach flag is retained and you are informed that the budget remains exceeded.

The breach clearing event is logged with the project ID, previous limit, new limit, and the identity of the user who made the change.

### Budget Breach Consequences

When project spending reaches 100% of the budget limit:

- **Cluster creation is blocked** — new cluster requests are rejected with a `BUDGET_EXCEEDED` error.
- **Cluster access is denied** — SSH/DCV connection details are withheld for existing clusters.
- **Notifications are sent** — the Project Administrator and all platform Administrators receive email alerts.

The `budgetBreached` flag is updated asynchronously via SNS notifications from AWS Budgets. Budget checks use DynamoDB consistent reads to minimise race conditions.

### Resolving a Budget Breach

To restore access after a budget breach:

1. Increase the budget limit using the project edit view (`PUT /projects/{projectId}`) or the budget endpoint (`PUT /projects/{projectId}/budget`) with a value above current spend.
2. Access is restored immediately — the `budgetBreached` flag is cleared in the same request if the new limit exceeds current spend.

Alternatively, destroy unused clusters to reduce ongoing costs, then wait for the budget evaluation cycle.

## Project Deactivation and Reactivation

Projects can be temporarily deactivated to revoke all member access while preserving the team composition. When the project is needed again, reactivation restores all memberships automatically. Both operations require the **Platform Administrator** role.

### Deactivating a Project

**Endpoint:** `POST /projects/{projectId}/deactivate`
**Required role:** Platform Administrator

#### Prerequisites

All clusters in the project must be destroyed before deactivation. If any non-destroyed clusters remain, the request is rejected.

#### What Happens

1. The platform verifies the project is in `ACTIVE` status.
2. The platform checks that no active clusters exist in the project.
3. The `ProjectAdmin-{projectId}` and `ProjectUser-{projectId}` Cognito groups are deleted, immediately revoking all members' role-based access.
4. The project status transitions from `ACTIVE` to `ARCHIVED`.
5. All membership records are preserved in DynamoDB for future reactivation.

If a Cognito group deletion fails, the failure is logged and deactivation continues with the remaining steps.

#### Response (200 OK)

Returns the updated project record with `"status": "ARCHIVED"`.

#### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in ACTIVE status | `CONFLICT` | 409 |
| Active clusters still exist | `CONFLICT` | 409 |
| Caller is not a Platform Administrator | `AUTHORISATION_ERROR` | 403 |

### Reactivating a Project

**Endpoint:** `POST /projects/{projectId}/reactivate`
**Required role:** Platform Administrator

#### What Happens

1. The platform verifies the project is in `ARCHIVED` status.
2. The `ProjectAdmin-{projectId}` and `ProjectUser-{projectId}` Cognito groups are recreated.
3. All preserved membership records are read from DynamoDB.
4. Each member is added back to the appropriate Cognito group based on their stored role (`PROJECT_ADMIN` or `PROJECT_USER`).
5. The project status transitions from `ARCHIVED` to `ACTIVE`.

If restoring a member to a Cognito group fails, the membership record is marked with `PENDING_RESTORATION` status and the failure is logged. The daily POSIX reconciliation process will retry these restorations.

#### Response (200 OK)

Returns the updated project record with `"status": "ACTIVE"`.

#### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Project is not in ARCHIVED status | `CONFLICT` | 409 |
| Caller is not a Platform Administrator | `AUTHORISATION_ERROR` | 403 |
