# Implementation Plan: User Reactivation

## Overview

Add user reactivation support to the Self-Service HPC Platform. This involves adding a `reactivate_user()` business logic function, a new handler route for `POST /users/{userId}/reactivate`, updating `list_users()` to return all users regardless of status, adding the API Gateway resource in CDK, updating the frontend with a Reactivate button, writing property and unit tests, and updating documentation.

## Tasks

- [x] 1. Implement reactivation business logic and handler route
  - [x] 1.1 Add `reactivate_user()` function to `lambda/user_management/users.py`
    - Fetch user record from DynamoDB by `PK=USER#{userId}, SK=PROFILE`
    - Validate user exists (raise `NotFoundError` if not)
    - Validate user status is INACTIVE (raise `ValidationError` if already ACTIVE)
    - Update DynamoDB status to ACTIVE and set `updatedAt` timestamp
    - Call `cognito.admin_enable_user()` to re-enable the Cognito account
    - Log Cognito failures as warnings without rolling back (matches existing `deactivate_user()` pattern)
    - Return the sanitised user profile with updated status
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 1.2 Add handler route for `POST /users/{userId}/reactivate` in `lambda/user_management/handler.py`
    - Add route match for `resource == "/users/{userId}/reactivate"` and `http_method == "POST"`
    - Implement `_handle_reactivate_user(event, user_id)` function
    - Check `is_administrator(event)` — raise `AuthorisationError` for non-admins
    - Call `reactivate_user()` and return HTTP 200 with the updated user profile
    - Import `reactivate_user` from `users` module
    - _Requirements: 1.1, 2.1, 2.2, 3.1, 3.2_

  - [x] 1.3 Update `list_users()` in `lambda/user_management/users.py` to return both ACTIVE and INACTIVE users
    - Replace the GSI query (`StatusIndex` with `status=ACTIVE`) with a table Scan
    - Filter on `SK = PROFILE` to exclude the COUNTER row
    - Handle DynamoDB Scan pagination for completeness
    - Return sanitised records for all users regardless of status
    - _Requirements: 5.1_

  - [x] 1.4 Write unit tests for reactivation in `test/lambda/test_unit_user_management.py`
    - Add `TestUserReactivationHappyPath` class: reactivate returns 200, status ACTIVE in DynamoDB, Cognito re-enabled
    - Add `TestUserReactivationPosixPreservation` class: POSIX UID/GID unchanged after deactivate → reactivate
    - Add `TestUserReactivationValidation` class: already-active user returns 400, nonexistent user returns 404
    - Add `TestUserReactivationAuthorisation` class: non-admin rejected with 403
    - Add `TestListUsersIncludesInactive` class: list returns both ACTIVE and INACTIVE users
    - Use existing `user_mgmt_env` class-scoped fixture and `build_admin_event`/`build_non_admin_event` helpers
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 5.1_

- [x] 2. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Add property-based tests for reactivation
  - [x] 3.1 Write property test for reactivation round-trip and POSIX preservation in `test/lambda/test_property_reactivation_roundtrip.py`
    - **Property 1: Reactivation round-trip restores user to ACTIVE with correct profile**
    - **Property 2: Reactivation preserves POSIX identity**
    - Use Hypothesis strategies for randomised userId, displayName, and email
    - Create → deactivate → reactivate round-trip, assert status is ACTIVE and profile fields are correct
    - Assert posixUid and posixGid match the values from creation
    - Use `@mock_aws` per-example, `@settings(max_examples=100, deadline=None)`
    - Use shared `conftest.py` helpers (`create_users_table`, `create_cognito_pool`, `reload_user_mgmt_modules`)
    - **Validates: Requirements 1.1, 1.2, 1.3, 3.2**

  - [x] 3.2 Write property test for active-user rejection in `test/lambda/test_property_reactivation_roundtrip.py`
    - **Property 3: Reactivating an already-active user is rejected**
    - For any created user still in ACTIVE status, reactivation returns HTTP 400 with `VALIDATION_ERROR`
    - Use `@settings(max_examples=100, deadline=None)`
    - **Validates: Requirements 1.4**

  - [x] 3.3 Extend existing admin-only property test in `test/lambda/test_property_admin_only.py`
    - **Property 4: Non-administrator reactivation is rejected**
    - Add `("POST", "/users/{userId}/reactivate", False, True)` to `ADMIN_ONLY_OPERATIONS` list
    - Verify non-admin callers receive HTTP 403 with `AUTHORISATION_ERROR` for the reactivation endpoint
    - **Validates: Requirements 2.1**

  - [x] 3.4 Write property test for list completeness in `test/lambda/test_property_reactivation_roundtrip.py`
    - **Property 5: User list returns both ACTIVE and INACTIVE users**
    - Create a random set of users, deactivate a random subset, assert list_users returns all users
    - Use `@settings(max_examples=100, deadline=None)`
    - **Validates: Requirements 5.1**

- [x] 4. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Add CDK infrastructure for the reactivate endpoint
  - [x] 5.1 Add API Gateway resource in `lib/foundation-stack.ts`
    - Add `reactivate` sub-resource under the existing `userIdResource` (`{userId}`)
    - Add `POST` method with `userManagementIntegration` and `cognitoMethodOptions`
    - No new Lambda permissions needed — `AdminEnableUser` is already in the Cognito policy
    - _Requirements: 3.1, 3.3_

  - [x] 5.2 Add CDK synthesis test for the reactivate endpoint in `test/foundation-stack.test.ts`
    - Add test asserting the `reactivate` resource exists under `{userId}`
    - Verify the method count increases to account for the new POST method
    - _Requirements: 3.1, 3.3_

- [x] 6. Update frontend with reactivation UI
  - [x] 6.1 Update `loadUsers()` in `frontend/js/app.js` to show Reactivate button for INACTIVE users
    - In the Actions column, show "Reactivate" button for INACTIVE users and "Deactivate" button for ACTIVE users
    - Use `btn-primary` class for the Reactivate button to visually distinguish from the Deactivate button
    - _Requirements: 4.1, 5.2_

  - [x] 6.2 Add `reactivateUser()` function to `frontend/js/app.js`
    - Prompt for confirmation before reactivating
    - Call `apiCall('POST', '/users/${encodeURIComponent(userId)}/reactivate')`
    - Show success toast and refresh user list on success
    - Show error toast on failure
    - _Requirements: 4.2, 4.3, 4.4_

- [x] 7. Update documentation
  - [x] 7.1 Update `docs/admin/user-management.md`
    - Add "Reactivating a User" section with endpoint, request format, response format, and error cases
    - Update the User Lifecycle section to show `Created (ACTIVE) → Deactivated (INACTIVE) → Reactivated (ACTIVE)`
    - Update the Listing Users section to note it returns both ACTIVE and INACTIVE users
    - _Requirements: 6.1, 6.2_

  - [x] 7.2 Update `docs/api/reference.md`
    - Add `POST /users/{userId}/reactivate` endpoint documentation with request/response examples and error table
    - Update `GET /users` documentation to note it returns both ACTIVE and INACTIVE users
    - _Requirements: 6.3_

- [-] 8. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The design uses Python for Lambda code and TypeScript for CDK — no language selection needed
- No new IAM permissions are required; the existing Cognito policy already includes `AdminEnableUser`
