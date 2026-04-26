# API Reference

Complete reference for the Self-Service HPC Platform REST API. All endpoints are served through Amazon API Gateway and require authentication via Amazon Cognito JWT tokens.

## Authentication

All API requests must include a valid Cognito ID token in the `Authorization` header:

```
Authorization: Bearer <cognito-id-token>
```

Tokens are obtained by authenticating against the Cognito User Pool using the User Pool Client ID. Tokens expire after 1 hour; use the refresh token (valid for 30 days) to obtain new access tokens.

## Base URL

```
https://<api-gateway-id>.execute-api.<region>.amazonaws.com/prod
```

## Roles

| Role | Description |
|------|-------------|
| `Administrator` | Platform-level admin. Can manage users, projects, and templates. |
| `Project_Administrator` | Project-level admin. Can manage membership, budgets, and clusters for their project. Also has all Project_User rights. |
| `Project_User` | Project member. Can create/destroy clusters, access clusters, and manage data within their project. |

Role membership is managed via Cognito groups: `Administrators`, `ProjectAdmin-{projectId}`, `ProjectUser-{projectId}`.

---

## User Management

### POST /users

Create a new platform user.

**Required role:** Administrator

**Request body:**

```json
{
  "userId": "string (required)",
  "displayName": "string (required)",
  "email": "string (required)"
}
```

**Response (201 Created):**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | Missing or empty required field |
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `DUPLICATE_ERROR` | 409 | userId already exists |

---

### GET /users

List all platform users. Returns both ACTIVE and INACTIVE users.

**Required role:** Administrator

**Response (200 OK):**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |

---

### GET /users/{userId}

Get a single user's details.

**Required role:** Administrator, or the user themselves

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `userId` | string | The user identifier |

**Response (200 OK):**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator and is not the requested user |
| `NOT_FOUND` | 404 | User does not exist |

---

### DELETE /users/{userId}

Deactivate a platform user. Disables the Cognito account and revokes all sessions.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `userId` | string | The user identifier |

**Response (200 OK):**

```json
{
  "message": "User jsmith has been deactivated."
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | User does not exist |

---

### POST /users/{userId}/reactivate

Reactivate a previously deactivated user. Re-enables the Cognito account and restores the user to ACTIVE status. The user's POSIX identity, project memberships, and audit history are preserved.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `userId` | string | The user identifier |

**Request body:** None required.

**Response (200 OK):**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | User is already ACTIVE |
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | User does not exist |

---

## Project Management

### POST /projects

Create a new project. The project is created in `CREATED` status with a default budget of $50 MONTHLY. No infrastructure is provisioned until the project is deployed.

**Required role:** Administrator

**Request body:**

```json
{
  "projectId": "string (required)",
  "projectName": "string (required)",
  "costAllocationTag": "string (optional, defaults to projectId)"
}
```

**Response (201 Created):**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | Missing or empty required field |
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `DUPLICATE_ERROR` | 409 | projectId already exists |

---

### GET /projects

List all projects.

**Required role:** Administrator

**Response (200 OK):**

```json
{
  "projects": [
    {
      "projectId": "genomics-team",
      "projectName": "Genomics Research Team",
      "status": "ACTIVE",
      "budgetLimit": 5000.0,
      "budgetBreached": false,
      "createdAt": "2025-01-15T11:00:00Z"
    }
  ]
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |

---

### GET /projects/{projectId}

Get a single project's details. When the project is in `DEPLOYING` or `DESTROYING` status, the response includes a `progress` object with the current step information.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Response (200 OK) — ACTIVE project:**

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

**Response (200 OK) — DEPLOYING or DESTROYING project:**

```json
{
  "projectId": "genomics-team",
  "projectName": "Genomics Research Team",
  "costAllocationTag": "genomics-team",
  "status": "DEPLOYING",
  "budgetLimit": 50,
  "budgetType": "MONTHLY",
  "budgetBreached": false,
  "progress": {
    "currentStep": 2,
    "totalSteps": 5,
    "stepDescription": "Starting CDK deploy"
  },
  "createdAt": "2025-01-15T11:00:00Z"
}
```

**Progress fields:**

| Field | Type | Description |
|-------|------|-------------|
| `progress.currentStep` | number | The current step number (1-based) |
| `progress.totalSteps` | number | Total number of steps in the operation |
| `progress.stepDescription` | string | Human-readable description of the current step |

The `progress` object is only included when the project status is `DEPLOYING` or `DESTROYING`.

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | Project does not exist |

---

### POST /projects/{projectId}/deploy

Initiate infrastructure deployment for a project. The project must be in `CREATED` status. Deployment runs asynchronously via a Step Functions state machine.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Request body:** None required.

**Response (202 Accepted):**

```json
{
  "message": "Project 'genomics-team' deployment started.",
  "projectId": "genomics-team",
  "status": "DEPLOYING"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | Project does not exist |
| `CONFLICT` | 409 | Project is not in CREATED status |

---

### POST /projects/{projectId}/destroy

Initiate infrastructure destruction for a project. The project must be in `ACTIVE` status and must have no active or creating clusters. Destruction runs asynchronously via a Step Functions state machine.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Request body:** None required.

**Response (202 Accepted):**

```json
{
  "message": "Project 'genomics-team' destruction started.",
  "projectId": "genomics-team",
  "status": "DESTROYING"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | Project does not exist |
| `CONFLICT` | 409 | Project is not in ACTIVE status, or project has active clusters |

---

### PUT /projects/{projectId}

Update editable project fields. Only `budgetLimit` and `budgetType` can be changed. The project must be in `ACTIVE` status.

If the new budget limit exceeds the current spend, the `budgetBreached` flag is cleared immediately in the same request.

**Required role:** Project Administrator (for this project) or Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Request body:**

```json
{
  "budgetLimit": 5000,
  "budgetType": "MONTHLY"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `budgetLimit` | number | Yes | Budget amount in USD. Must be greater than zero. |
| `budgetType` | string | Yes | `"MONTHLY"` (resets each calendar month) or `"TOTAL"` (covers entire project lifetime). |

**Response (200 OK):**

Returns the updated project record:

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | budgetLimit is zero, negative, or not a number |
| `VALIDATION_ERROR` | 400 | budgetType is not MONTHLY or TOTAL |
| `AUTHORISATION_ERROR` | 403 | Caller is not a Project Administrator for this project or an Administrator |
| `NOT_FOUND` | 404 | Project does not exist |
| `CONFLICT` | 409 | Project is not in ACTIVE status |

---

### DELETE /projects/{projectId}

Delete a project. All clusters must be destroyed first.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Response (200 OK):**

```json
{
  "message": "Project 'genomics-team' has been deleted."
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | Project does not exist |
| `CONFLICT` | 409 | Project has active clusters |

---

## Project Membership

### POST /projects/{projectId}/members

Add a user to a project.

**Required role:** Project Administrator (for this project) or Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Request body:**

```json
{
  "userId": "string (required)",
  "role": "string (optional, default: PROJECT_USER)"
}
```

Valid roles: `PROJECT_ADMIN`, `PROJECT_USER`

**Response (201 Created):**

```json
{
  "projectId": "genomics-team",
  "userId": "jsmith",
  "role": "PROJECT_USER",
  "addedAt": "2025-01-15T12:00:00Z"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | Missing userId or invalid role |
| `AUTHORISATION_ERROR` | 403 | Caller is not a Project Administrator for this project |
| `NOT_FOUND` | 404 | User does not exist on the platform |
| `DUPLICATE_ERROR` | 409 | User is already a member of the project |

---

### DELETE /projects/{projectId}/members/{userId}

Remove a user from a project.

**Required role:** Project Administrator (for this project) or Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |
| `userId` | string | The user identifier to remove |

**Response (200 OK):**

```json
{
  "message": "User 'jsmith' removed from project 'genomics-team'."
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not a Project Administrator for this project |
| `NOT_FOUND` | 404 | User is not a member of the project |

---

## Project Budget

### PUT /projects/{projectId}/budget

Set or update the project budget limit.

**Required role:** Project Administrator (for this project) or Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Request body:**

```json
{
  "budgetLimit": 5000.00
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `budgetLimit` | number | Yes | Monthly budget limit in USD |

**Response (200 OK):**

```json
{
  "projectId": "genomics-team",
  "budgetLimit": 5000.0,
  "message": "Budget limit set to $5000.00 for project 'genomics-team'."
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | budgetLimit missing or not a number |
| `AUTHORISATION_ERROR` | 403 | Caller is not a Project Administrator for this project |

---

## Cluster Templates

### POST /templates

Create a new cluster template.

**Required role:** Administrator

**Request body:**

```json
{
  "templateId": "string (required)",
  "templateName": "string (required)",
  "description": "string (optional)",
  "instanceTypes": ["string"] ,
  "loginInstanceType": "string (required)",
  "minNodes": 0,
  "maxNodes": 0,
  "amiId": "string (required)",
  "softwareStack": {}
}
```

**Response (201 Created):**

```json
{
  "templateId": "cpu-general",
  "templateName": "General CPU Workloads",
  "description": "Cost-effective CPU cluster template.",
  "instanceTypes": ["c7g.medium"],
  "loginInstanceType": "c7g.medium",
  "minNodes": 1,
  "maxNodes": 10,
  "amiId": "ami-0abc123",
  "softwareStack": {"scheduler": "slurm", "schedulerVersion": "24.11"},
  "createdAt": "2025-01-15T10:00:00Z"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | Missing required fields |
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `DUPLICATE_ERROR` | 409 | templateId already exists |

---

### GET /templates

List all cluster templates.

**Required role:** Any authenticated user

**Response (200 OK):**

```json
{
  "templates": [
    {
      "templateId": "cpu-general",
      "templateName": "General CPU Workloads",
      "description": "Cost-effective CPU cluster template.",
      "instanceTypes": ["c7g.medium"],
      "loginInstanceType": "c7g.medium",
      "minNodes": 1,
      "maxNodes": 10,
      "createdAt": "2025-01-15T10:00:00Z"
    }
  ]
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not authenticated |

---

### GET /templates/{templateId}

Get a single template's details.

**Required role:** Any authenticated user

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `templateId` | string | The template identifier |

**Response (200 OK):**

```json
{
  "templateId": "cpu-general",
  "templateName": "General CPU Workloads",
  "description": "Cost-effective CPU cluster template.",
  "instanceTypes": ["c7g.medium"],
  "loginInstanceType": "c7g.medium",
  "minNodes": 1,
  "maxNodes": 10,
  "amiId": "ami-0abc123",
  "softwareStack": {"scheduler": "slurm", "schedulerVersion": "24.11"},
  "createdAt": "2025-01-15T10:00:00Z"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not authenticated |
| `NOT_FOUND` | 404 | Template does not exist |

---

### DELETE /templates/{templateId}

Delete a cluster template.

**Required role:** Administrator

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `templateId` | string | The template identifier |

**Response (200 OK):**

```json
{
  "message": "Template 'cpu-general' has been deleted."
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not an Administrator |
| `NOT_FOUND` | 404 | Template does not exist |

---

## Cluster Operations

### POST /projects/{projectId}/clusters

Create a new cluster within a project. Returns immediately; creation is asynchronous.

**Required role:** Project User or Project Administrator (for this project)

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Request body:**

```json
{
  "clusterName": "string (required)",
  "templateId": "string (required)"
}
```

Cluster name rules:
- Non-empty
- Alphanumeric characters, hyphens (`-`), and underscores (`_`) only
- Globally unique across projects (can be reused within the same project)

**Response (202 Accepted):**

```json
{
  "message": "Cluster 'genomics-run-42' creation started.",
  "projectId": "genomics-team",
  "clusterName": "genomics-run-42",
  "templateId": "cpu-general"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `VALIDATION_ERROR` | 400 | Invalid cluster name or missing fields |
| `AUTHORISATION_ERROR` | 403 | Caller is not a project member |
| `BUDGET_EXCEEDED` | 403 | Project budget has been breached |

---

### GET /projects/{projectId}/clusters

List all clusters in a project.

**Required role:** Project User or Project Administrator (for this project)

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |

**Response (200 OK):**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not a project member |

---

### GET /projects/{projectId}/clusters/{clusterName}

Get cluster details. Includes connection info for ACTIVE clusters and progress info for CREATING clusters.

**Required role:** Project User or Project Administrator (for this project)

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |
| `clusterName` | string | The cluster name |

**Response (200 OK) — ACTIVE cluster:**

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

**Response (200 OK) — CREATING cluster:**

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

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not a project member |
| `BUDGET_EXCEEDED` | 403 | Project budget has been breached |
| `NOT_FOUND` | 404 | Cluster does not exist |

---

### DELETE /projects/{projectId}/clusters/{clusterName}

Destroy a cluster. Returns immediately; destruction is asynchronous.

**Required role:** Project User or Project Administrator (for this project)

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |
| `clusterName` | string | The cluster name |

**Response (202 Accepted):**

```json
{
  "message": "Cluster 'genomics-run-42' destruction started.",
  "projectId": "genomics-team",
  "clusterName": "genomics-run-42"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not a project member |
| `NOT_FOUND` | 404 | Cluster does not exist |
| `CONFLICT` | 409 | Cluster is not in ACTIVE or FAILED status |

---

### POST /projects/{projectId}/clusters/{clusterName}/recreate

Recreate a previously destroyed cluster. Reuses the original cluster name and template configuration, with an optional template override. Returns immediately; creation is asynchronous via the same workflow as new cluster creation.

**Required role:** Project User or Project Administrator (for this project)

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `projectId` | string | The project identifier |
| `clusterName` | string | The cluster name of the destroyed cluster to recreate |

**Request body (optional):**

```json
{
  "templateId": "string (optional)"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `templateId` | string | No | Template to use for the recreated cluster. If omitted or empty, the templateId from the destroyed cluster record is used. |

**Response (202 Accepted):**

```json
{
  "message": "Cluster 'genomics-run-42' recreation started.",
  "projectId": "genomics-team",
  "clusterName": "genomics-run-42",
  "templateId": "cpu-general"
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller is not a project member |
| `BUDGET_EXCEEDED` | 403 | Project budget has been breached |
| `NOT_FOUND` | 404 | Cluster does not exist |
| `CONFLICT` | 409 | Cluster is not in DESTROYED status |

---

## Accounting

### GET /accounting/jobs

Query Slurm job accounting records across clusters.

**Required role:** Administrator (all clusters) or Project Administrator (project-scoped)

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `projectId` | string | No | Filter results to a single project |

Without `projectId`: returns jobs across all clusters (Administrator only).
With `projectId`: returns jobs for that project (Administrator or Project Administrator).

**Response (200 OK):**

```json
{
  "jobs": [
    {
      "jobId": "12345",
      "jobName": "my-simulation",
      "user": "jsmith",
      "partition": "compute",
      "state": "COMPLETED",
      "exitCode": "0:0",
      "elapsed": "01:23:45",
      "cluster": "genomics-run-42",
      "projectId": "genomics-team"
    }
  ]
}
```

**Errors:**

| Code | Status | Condition |
|------|--------|-----------|
| `AUTHORISATION_ERROR` | 403 | Caller lacks required role for the query scope |

---

## Error Response Format

All error responses follow a consistent structure:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error description.",
    "details": {}
  }
}
```

### Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `AUTHORISATION_ERROR` | 403 | Caller lacks the required role or permission for the operation |
| `VALIDATION_ERROR` | 400 | Request input is invalid (missing fields, bad format, invalid values) |
| `DUPLICATE_ERROR` | 409 | Resource already exists (duplicate userId, projectId, templateId, or membership) |
| `NOT_FOUND` | 404 | Requested resource does not exist |
| `CONFLICT` | 409 | Operation conflicts with current resource state (e.g., active clusters block project deletion) |
| `BUDGET_EXCEEDED` | 403 | Project budget has been breached; cluster creation and access are blocked |
| `INTERNAL_ERROR` | 500 | Unexpected server error; check CloudWatch logs for details |

### Error Details

The `details` object provides additional context depending on the error type:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "clusterName is required.",
    "details": {
      "field": "clusterName"
    }
  }
}
```

```json
{
  "error": {
    "code": "BUDGET_EXCEEDED",
    "message": "Project 'genomics-team' budget has been exceeded.",
    "details": {
      "projectId": "genomics-team"
    }
  }
}
```
