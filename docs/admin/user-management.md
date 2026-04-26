# User Management

This guide covers creating, viewing, and removing platform users. All user management operations require the **Administrator** role.

## Overview

Platform users are managed through the REST API. Each user receives:

- A **Cognito identity** for authentication (email-based sign-in)
- A **globally unique POSIX UID and GID** for consistent file ownership across all clusters
- A **DynamoDB profile record** tracking their status and metadata

Users are not deleted — they are **deactivated**. Deactivation revokes all active sessions and prevents further login, but preserves the user record and POSIX identity for audit purposes.

## Creating a User

**Endpoint:** `POST /users`
**Required role:** Administrator

### Request

```json
{
  "userId": "jsmith",
  "displayName": "Jane Smith",
  "email": "jane.smith@example.com",
  "role": "User"
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `userId` | string | Yes | Unique identifier for the user (alphanumeric, used as the POSIX username) |
| `displayName` | string | Yes | Human-readable display name |
| `email` | string | Yes | Email address for Cognito account and notifications |
| `role` | string | No | Platform role: `User` (default) or `Administrator`. Administrators have full management access. |

### What Happens

1. The platform validates that the `userId` is not already in use.
2. A globally unique POSIX UID and GID are assigned via an atomic DynamoDB counter (starting at 10000).
3. A Cognito user account is created with a temporary password sent to the user's email.
4. If the role is `Administrator`, the user is added to the Cognito `Administrators` group.
5. The user record is stored in the PlatformUsers DynamoDB table.

### Response (201 Created)

```json
{
  "userId": "jsmith",
  "displayName": "Jane Smith",
  "email": "jane.smith@example.com",
  "role": "User",
  "posixUid": 10001,
  "posixGid": 10001,
  "status": "ACTIVE",
  "createdAt": "2025-01-15T10:30:00Z"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| Duplicate userId | `DUPLICATE_ERROR` | 409 |
| Missing required field | `VALIDATION_ERROR` | 400 |
| Invalid role value | `VALIDATION_ERROR` | 400 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

## Listing Users

**Endpoint:** `GET /users`
**Required role:** Administrator

Returns all platform users, including both ACTIVE and INACTIVE users. This allows Administrators to identify deactivated users who may need to be reactivated.

### Response (200 OK)

```json
{
  "users": [
    {
      "userId": "jsmith",
      "displayName": "Jane Smith",
      "email": "jane.smith@example.com",
      "posixUid": 10001,
      "posixGid": 10001,
      "status": "ACTIVE",
      "createdAt": "2025-01-15T10:30:00Z"
    }
  ]
}
```

## Viewing a User

**Endpoint:** `GET /users/{userId}`
**Required role:** Administrator, or the user themselves

Administrators can view any user's profile. Non-administrators can only view their own profile.

### Response (200 OK)

```json
{
  "userId": "jsmith",
  "displayName": "Jane Smith",
  "email": "jane.smith@example.com",
  "posixUid": 10001,
  "posixGid": 10001,
  "status": "ACTIVE",
  "createdAt": "2025-01-15T10:30:00Z"
}
```

## Deactivating a User

**Endpoint:** `DELETE /users/{userId}`
**Required role:** Administrator

Deactivation does not delete the user record. It:

1. Marks the user as `INACTIVE` in DynamoDB.
2. Disables the Cognito user account.
3. Revokes all active Cognito sessions (global sign-out).

The user's POSIX UID/GID and project memberships are preserved for audit and file ownership consistency.

### Response (200 OK)

```json
{
  "message": "User jsmith has been deactivated."
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| User does not exist | `NOT_FOUND` | 404 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

## Reactivating a User

**Endpoint:** `POST /users/{userId}/reactivate`
**Required role:** Administrator

Reactivation restores a previously deactivated user to active status. It:

1. Validates the user exists and is currently `INACTIVE`.
2. Updates the user status to `ACTIVE` in DynamoDB.
3. Re-enables the Cognito user account.

The user's POSIX UID/GID, project memberships, and all historical audit records are preserved without modification.

### Request

No request body is required. The `userId` is provided as a path parameter.

### Response (200 OK)

```json
{
  "userId": "jsmith",
  "displayName": "Jane Smith",
  "email": "jane.smith@example.com",
  "posixUid": 10001,
  "posixGid": 10001,
  "status": "ACTIVE",
  "createdAt": "2025-01-15T10:30:00Z",
  "updatedAt": "2025-06-20T14:00:00Z"
}
```

### Error Cases

| Scenario | Error Code | HTTP Status |
|----------|-----------|-------------|
| User is already ACTIVE | `VALIDATION_ERROR` | 400 |
| User does not exist | `NOT_FOUND` | 404 |
| Caller is not an Administrator | `AUTHORISATION_ERROR` | 403 |

## POSIX Identity

Every user is assigned a globally unique POSIX UID and GID at creation time. These identifiers are:

- **Consistent** across all projects and clusters on the platform
- **Permanent** — they do not change even if the user is deactivated
- **Sequential** — allocated from an atomic counter starting at 10000

When a cluster is created, POSIX user accounts are provisioned on all nodes with the correct UID/GID, ensuring file ownership is consistent across shared filesystems (EFS home directories and FSx for Lustre mounts).

## User Lifecycle

```
Created (ACTIVE) → Deactivated (INACTIVE) → Reactivated (ACTIVE)
```

Administrators can reactivate a previously deactivated user via `POST /users/{userId}/reactivate`. Reactivation preserves the user's POSIX identity, project memberships, and audit history.

## Table Sorting and Filtering

The Users table in the web portal supports interactive sorting and filtering to help Administrators locate users quickly.

### Sorting

Click any column header to sort the table by that column. The following columns are sortable:

- **User ID** — alphabetical sort
- **Display Name** — alphabetical sort
- **Role** — alphabetical sort
- **POSIX UID** — numeric sort
- **Status** — alphabetical sort

Click a column header once to sort in ascending order. Click the same header again to sort in descending order. A sort indicator (▲ for ascending, ▼ for descending) appears next to the active column header. Clicking a different column header switches the sort to that column in ascending order.

### Filtering

A search input is displayed above the Users table. Type any text to filter the table rows — only rows where at least one column value contains the search term are shown. Filtering is case-insensitive and matches partial text. For example, typing "admin" will match users with the Administrator role as well as any user whose name or ID contains "admin".

Clear the search input to show all rows again. If no rows match the filter, a message is displayed indicating no matching results were found.

### State Preservation

Sort and filter settings are preserved during automatic data refreshes, so the table does not reset while you are working. Navigating to a different page resets the sort and filter to their defaults.

## Table Features

The Users table includes several features to improve usability when working with large numbers of rows.

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
