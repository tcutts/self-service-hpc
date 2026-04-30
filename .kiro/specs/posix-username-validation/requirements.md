# Requirements Document

## Introduction

The HPC self-service platform creates POSIX user accounts on Linux cluster nodes using the `userId` field directly in bash commands (`groupadd`, `useradd`, `chown`). Currently, no validation enforces that `userId` conforms to POSIX/Linux username rules. If an administrator enters an email address, special characters, or other invalid values as the `userId`, the POSIX account creation silently fails (errors are masked by `2>/dev/null || true`), leaving the user unable to access cluster resources. This also presents a command injection risk since the `userId` is interpolated directly into shell commands.

This feature adds consistent POSIX username validation across the backend API, the frontend form, and the admin documentation to ensure every `userId` is a valid Linux username before it reaches Cognito or the shell.

## Glossary

- **User_Management_API**: The Lambda-backed REST API (`POST /users`) that creates platform users, allocates POSIX UIDs, creates Cognito accounts, and stores records in DynamoDB.
- **User_Creation_Form**: The "Add New User" form in the frontend web portal (`frontend/js/app.js`) that collects `userId`, display name, email, and role.
- **POSIX_Username**: A string that is valid as a Linux username: starts with a lowercase letter or underscore, followed by up to 31 lowercase letters, digits, underscores, or hyphens. Maximum 32 characters total. Must not contain `@`, `.`, spaces, or uppercase letters.
- **Username_Validator**: A reusable validation function (or regex) that determines whether a given string is a valid POSIX_Username.
- **POSIX_Provisioning_Module**: The module (`lambda/cluster_operations/posix_provisioning.py`) that generates bash commands using the `userId` for `groupadd`, `useradd`, and `chown` on cluster nodes.
- **Admin_Documentation**: The user management guide (`docs/admin/user-management.md`) that describes the `POST /users` API and field requirements.
- **Cognito_User_Pool**: The Amazon Cognito User Pool where platform users are created with `userId` as the Cognito username.

## Requirements

### Requirement 1: Backend POSIX Username Validation

**User Story:** As a platform administrator, I want the backend API to reject invalid POSIX usernames at creation time, so that only valid Linux usernames are stored and used for POSIX account provisioning.

#### Acceptance Criteria

1. WHEN a `POST /users` request is received, THE User_Management_API SHALL validate the `userId` field against POSIX_Username rules before creating the Cognito user or DynamoDB record.
2. WHEN the `userId` is empty or missing, THE User_Management_API SHALL return HTTP 400 with error code `VALIDATION_ERROR` and a message indicating that `userId` is required.
3. WHEN the `userId` contains characters outside the set of lowercase letters (`a-z`), digits (`0-9`), underscores (`_`), and hyphens (`-`), THE User_Management_API SHALL return HTTP 400 with error code `VALIDATION_ERROR` and a message describing the allowed character set.
4. WHEN the `userId` starts with a hyphen or a digit, THE User_Management_API SHALL return HTTP 400 with error code `VALIDATION_ERROR` and a message indicating that the username must start with a lowercase letter or underscore.
5. WHEN the `userId` exceeds 32 characters in length, THE User_Management_API SHALL return HTTP 400 with error code `VALIDATION_ERROR` and a message indicating the maximum length of 32 characters.
6. WHEN the `userId` contains an `@` symbol, a dot (`.`), or a space, THE User_Management_API SHALL return HTTP 400 with error code `VALIDATION_ERROR` and a message indicating that these characters are not permitted in a POSIX username.
7. WHEN the `userId` passes all POSIX_Username validation rules, THE User_Management_API SHALL proceed with Cognito user creation and DynamoDB record storage.

### Requirement 2: Shared Username Validation Logic

**User Story:** As a developer, I want a single reusable validation function for POSIX usernames, so that the backend and frontend enforce identical rules without divergence.

#### Acceptance Criteria

1. THE Username_Validator SHALL accept a string and return a validation result indicating whether the string is a valid POSIX_Username.
2. WHEN the string is invalid, THE Username_Validator SHALL return a human-readable error message describing the first rule that was violated.
3. THE Username_Validator SHALL enforce all of the following rules: the string contains only lowercase letters, digits, underscores, and hyphens; the string starts with a lowercase letter or underscore; the string is between 1 and 32 characters in length.
4. FOR ALL valid POSIX_Username strings, THE Username_Validator SHALL accept the string. FOR ALL strings that violate any rule, THE Username_Validator SHALL reject the string. (Correctness property: the validator is consistent with the POSIX_Username definition.)

### Requirement 3: Frontend POSIX Username Validation

**User Story:** As a platform administrator, I want the user creation form to validate the User ID field before submission, so that I receive immediate feedback on invalid usernames without waiting for a server round-trip.

#### Acceptance Criteria

1. WHEN the administrator types a value into the User ID field of the User_Creation_Form, THE User_Creation_Form SHALL validate the value against POSIX_Username rules on each input change.
2. WHEN the User ID field contains an invalid POSIX_Username, THE User_Creation_Form SHALL display an inline error message below the field describing the validation failure.
3. WHILE the User ID field contains an invalid POSIX_Username, THE User_Creation_Form SHALL disable the "Create User" submit button.
4. WHEN the User ID field contains a valid POSIX_Username, THE User_Creation_Form SHALL hide any inline error message and enable the "Create User" submit button.
5. WHEN the User ID field is empty, THE User_Creation_Form SHALL display a message indicating that User ID is required and disable the "Create User" submit button.

### Requirement 4: Cognito Username Compatibility

**User Story:** As a platform operator, I want validated POSIX usernames to work correctly as Cognito usernames, so that authentication functions reliably with short alphanumeric identifiers.

#### Acceptance Criteria

1. WHEN a valid POSIX_Username is used as the Cognito username in `AdminCreateUser`, THE Cognito_User_Pool SHALL accept the username without error.
2. THE Cognito_User_Pool SHALL retain `signInAliases: { email: true }` so that users sign in with their email address, while the Cognito username remains the POSIX_Username for internal identity mapping.
3. WHEN a POSIX_Username is 1 character long, THE User_Management_API SHALL accept the username, provided the Cognito_User_Pool minimum username length constraint is satisfied.

### Requirement 5: POSIX Provisioning Safety

**User Story:** As a security engineer, I want the POSIX provisioning module to only receive validated usernames, so that shell command injection via crafted `userId` values is prevented.

#### Acceptance Criteria

1. WHEN `generate_user_creation_commands` is called with a `user_id` that passes POSIX_Username validation, THE POSIX_Provisioning_Module SHALL produce syntactically correct bash commands for `groupadd`, `useradd`, and `chown`.
2. IF `generate_user_creation_commands` is called with a `user_id` that does not pass POSIX_Username validation, THEN THE POSIX_Provisioning_Module SHALL return an empty command list.
3. FOR ALL valid POSIX_Username strings, the generated bash commands SHALL contain the `user_id` only in positions where a Linux username is expected (as arguments to `groupadd`, `useradd`, `chown`, and in the `/home/{user_id}` path). (Correctness property: no shell metacharacter injection is possible with validated usernames.)

### Requirement 6: Admin Documentation Update

**User Story:** As a platform administrator, I want the user management documentation to clearly describe the `userId` format requirements, so that I understand the constraints before creating users.

#### Acceptance Criteria

1. THE Admin_Documentation SHALL include a description of the POSIX_Username format rules in the `userId` field documentation for the `POST /users` endpoint.
2. THE Admin_Documentation SHALL list the allowed characters (lowercase letters, digits, underscores, hyphens), the starting character constraint (lowercase letter or underscore), and the maximum length (32 characters).
3. THE Admin_Documentation SHALL include at least two examples of valid usernames and at least two examples of invalid usernames with explanations of why each is invalid.
4. THE Admin_Documentation SHALL document the `VALIDATION_ERROR` response returned when an invalid `userId` is submitted.
