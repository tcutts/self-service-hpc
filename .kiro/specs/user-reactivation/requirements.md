# Requirements Document

## Introduction

This document defines the requirements for adding user reactivation support to the Self-Service HPC Platform. Currently, when an Administrator deactivates a user (via `DELETE /users/{userId}`), the user is marked as INACTIVE in DynamoDB, their Cognito account is disabled, and all active sessions are revoked. However, there is no mechanism to restore access for a deactivated user — the only workaround is to create a new user with a different userId, which loses the original POSIX UID/GID and audit trail continuity.

This feature adds a reactivation flow that allows Administrators to re-enable a previously deactivated user, preserving their POSIX identity, project memberships, and audit records. The reactivation is exposed as a new API endpoint, implemented in the existing user management Lambda, and surfaced in the Web Portal UI.

## Glossary

- **Web_Portal**: The web-based administration and access interface for the HPC platform, implemented as a serverless application (API Gateway + Lambda + DynamoDB + CloudFront).
- **Administrator**: A platform-level user who manages users, projects, and cluster templates. Identified by membership in the Cognito `Administrators` group.
- **User_Management_Lambda**: The Python Lambda function that handles user CRUD operations, including POSIX UID/GID assignment, Cognito user lifecycle, and DynamoDB persistence. Located at `lambda/user_management/`.
- **PlatformUsers_Table**: The DynamoDB table storing user records with `PK=USER#{userId}`, `SK=PROFILE`, including status, POSIX identity, and Cognito subject ID.
- **Cognito_User_Pool**: The Amazon Cognito User Pool used for authentication and identity management on the platform.
- **Reactivation**: The process of restoring a previously deactivated (INACTIVE) user to ACTIVE status, re-enabling their Cognito account, and making them eligible for platform access again.
- **POSIX_Identity**: The globally unique POSIX UID and GID assigned to a user at creation time, used for file ownership consistency across all clusters.

## Requirements

### Requirement 1: Reactivate a Deactivated User

**User Story:** As an Administrator, I want to reactivate a previously deactivated user, so that the user can regain access to the platform without losing their POSIX identity, project memberships, or audit history.

#### Acceptance Criteria

1. WHEN an Administrator submits a reactivation request for a user with INACTIVE status, THE User_Management_Lambda SHALL set the user status to ACTIVE in the PlatformUsers_Table and re-enable the user account in the Cognito_User_Pool.
2. WHEN a user is reactivated, THE User_Management_Lambda SHALL preserve the user's existing POSIX UID, POSIX GID, project memberships, and all historical audit records without modification.
3. WHEN a user is reactivated, THE User_Management_Lambda SHALL return a confirmation response containing the reactivated user's profile including userId, displayName, email, posixUid, posixGid, and the updated ACTIVE status.
4. IF an Administrator submits a reactivation request for a user who is already ACTIVE, THEN THE User_Management_Lambda SHALL reject the request with a validation error indicating the user is already active.
5. IF an Administrator submits a reactivation request for a userId that does not exist, THEN THE User_Management_Lambda SHALL reject the request with a not-found error.

### Requirement 2: Reactivation Authorisation

**User Story:** As a platform security officer, I want only Administrators to be able to reactivate users, so that user lifecycle management remains controlled and auditable.

#### Acceptance Criteria

1. IF a non-Administrator user submits a reactivation request, THEN THE User_Management_Lambda SHALL reject the request with an authorisation error.
2. WHEN an Administrator successfully reactivates a user, THE Web_Portal SHALL log the reactivation action to Amazon CloudWatch Logs including the Administrator's user identifier, the reactivated userId, the action type, and a timestamp.

### Requirement 3: Reactivation API Endpoint

**User Story:** As a platform engineer, I want a dedicated API endpoint for user reactivation, so that the operation is clearly distinguished from user creation and follows RESTful conventions.

#### Acceptance Criteria

1. THE Web_Portal SHALL expose a `POST /users/{userId}/reactivate` endpoint that accepts a reactivation request for the specified userId.
2. WHEN the reactivation endpoint receives a valid request from an authorised Administrator, THE User_Management_Lambda SHALL process the reactivation and return an HTTP 200 response with the updated user profile.
3. THE Web_Portal SHALL configure the `POST /users/{userId}/reactivate` endpoint with the same Cognito authoriser used by all other user management endpoints.

### Requirement 4: Web Portal Reactivation UI

**User Story:** As an Administrator, I want to see a reactivation button for deactivated users in the Web Portal, so that I can restore user access through the same interface I use for other user management tasks.

#### Acceptance Criteria

1. WHEN the Web_Portal displays the user list, THE Web_Portal SHALL show a "Reactivate" action button for each user with INACTIVE status.
2. WHEN an Administrator clicks the "Reactivate" button for an INACTIVE user, THE Web_Portal SHALL send a reactivation request to the API and display a confirmation message upon success.
3. WHEN a reactivation request fails, THE Web_Portal SHALL display the error message returned by the API to the Administrator.
4. WHEN a user is successfully reactivated, THE Web_Portal SHALL refresh the user list to reflect the updated ACTIVE status.

### Requirement 5: User List Includes Deactivated Users

**User Story:** As an Administrator, I want to see both active and inactive users in the user list, so that I can identify deactivated users who may need to be reactivated.

#### Acceptance Criteria

1. WHEN an Administrator requests the user list, THE User_Management_Lambda SHALL return both ACTIVE and INACTIVE users.
2. THE Web_Portal SHALL visually distinguish ACTIVE users from INACTIVE users in the user list using status badges.

### Requirement 6: Documentation Update

**User Story:** As an Administrator, I want the user management documentation to describe the reactivation process, so that I can reference it when managing user lifecycle.

#### Acceptance Criteria

1. THE Platform SHALL update the user management documentation to describe the reactivation endpoint, including the HTTP method, path, required authorisation role, request format, and response format.
2. THE Platform SHALL update the user management documentation to describe the updated user lifecycle as: `Created (ACTIVE) → Deactivated (INACTIVE) → Reactivated (ACTIVE)`.
3. THE Platform SHALL update the API reference documentation to include the `POST /users/{userId}/reactivate` endpoint with request and response examples and error cases.
