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
