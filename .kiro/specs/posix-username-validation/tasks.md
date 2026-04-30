# Implementation Plan: POSIX Username Validation

## Overview

Add consistent POSIX username validation across the HPC self-service platform. A shared Python validator module enforces the rules in the backend API and provisioning layer, while the frontend mirrors the same regex with inline validation on the user creation form. Documentation is updated to describe the format constraints. Implementation proceeds bottom-up: shared validator → backend integration → provisioning guard → frontend validation → documentation → final wiring and verification.

## Tasks

- [x] 1. Create shared POSIX username validator module
  - [x] 1.1 Create `lambda/shared/validators.py` with `validate_posix_username()` function
    - Define `POSIX_USERNAME_REGEX = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")` and `POSIX_USERNAME_MAX_LENGTH = 32`
    - Implement `validate_posix_username(username: str) -> tuple[bool, str]` that checks rules in order: empty → too long → invalid start character → invalid characters
    - Return `(True, "")` for valid usernames, `(False, "<specific error message>")` for invalid ones
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 1.2 Write property test: Validator matches reference specification (Property 1)
    - **Property 1: Validator matches reference specification (model-based)**
    - Use `hypothesis` with `st.text(min_size=0, max_size=64)` to generate arbitrary strings
    - Assert `validate_posix_username()` returns `(True, "")` if and only if the string matches `^[a-z_][a-z0-9_-]{0,31}$`
    - Minimum 100 iterations
    - **Validates: Requirements 1.3, 1.4, 1.5, 1.7, 2.3, 2.4**

  - [x] 1.3 Write property test: Invalid inputs produce descriptive error messages (Property 2)
    - **Property 2: Invalid inputs produce descriptive error messages**
    - Use `hypothesis` with `st.text(min_size=0, max_size=64).filter(lambda s: not re.match(r'^[a-z_][a-z0-9_-]{0,31}$', s))` to generate invalid strings
    - Assert the returned error message is a non-empty string
    - Minimum 100 iterations
    - **Validates: Requirements 2.2**

  - [x] 1.4 Write unit tests for the shared validator
    - Test valid examples: `"a"`, `"jsmith"`, `"_admin"`, `"dev-user-01"`, `"a" * 32`
    - Test invalid examples: `""`, `"A"`, `"1user"`, `"-user"`, `"user@corp"`, `"user.name"`, `"user name"`, `"a" * 33`
    - Test error message specificity for each rule violation
    - Test edge cases: single-character usernames (`"a"`, `"_"`), all-digit body (`"a123"`), all-hyphen body (`"a---"`)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 2. Integrate validation into backend user creation API
  - [x] 2.1 Add `validate_posix_username()` call in `lambda/user_management/users.py` `create_user()` function
    - Import `validate_posix_username` from `validators`
    - Call validation at the top of `create_user()`, before `_allocate_posix_uid()`
    - Raise `ValidationError(error_msg, {"field": "userId"})` when validation fails
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x] 2.2 Write unit tests for backend validation integration
    - Mock Cognito and DynamoDB to verify they are NOT called when `userId` is invalid
    - Test that `create_user()` raises `ValidationError` for invalid usernames (empty, too long, invalid start, invalid chars)
    - Test that `create_user()` proceeds normally for valid usernames
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

- [x] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Add defensive validation to POSIX provisioning module
  - [x] 4.1 Add `validate_posix_username()` guard in `lambda/cluster_operations/posix_provisioning.py`
    - Import `validate_posix_username` from `validators`
    - Add validation check in `generate_user_creation_commands()` after the existing empty check
    - Return empty list if validation fails
    - Add a `logger.warning()` when rejecting an invalid username
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 4.2 Write property test: Valid usernames produce well-formed shell commands (Property 3)
    - **Property 3: Valid usernames produce well-formed shell commands**
    - Use `hypothesis` with `st.from_regex(r'[a-z_][a-z0-9_-]{0,31}', fullmatch=True)` for usernames and `st.integers(min_value=1000, max_value=65534)` for UID/GID
    - Assert the result is a list of exactly 3 commands containing `groupadd`, `useradd`, and `chown`
    - Assert `user_id` appears only as a command argument or in `/home/{user_id}` path
    - Minimum 100 iterations
    - **Validates: Requirements 5.1, 5.3**

  - [x] 4.3 Write property test: Invalid usernames produce empty command lists (Property 4)
    - **Property 4: Invalid usernames produce empty command lists**
    - Use `hypothesis` with invalid string generator combined with arbitrary UID/GID
    - Assert `generate_user_creation_commands()` returns an empty list for all invalid usernames
    - Minimum 100 iterations
    - **Validates: Requirements 5.2**

  - [x] 4.4 Write unit tests for provisioning defensive check
    - Test that valid usernames produce correct `groupadd`, `useradd`, `chown` commands
    - Test that invalid usernames (`""`, `"user@corp"`, `"A"`, `"-bad"`) return empty list
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 5. Add frontend inline validation on User ID field
  - [x] 5.1 Add `validatePosixUsername()` JavaScript function and inline validation UI in `frontend/js/app.js`
    - Add `POSIX_USERNAME_REGEX` and `validatePosixUsername()` function matching the Python validator logic
    - Add `<div id="user-id-error" class="field-error" role="alert" aria-live="polite">` below the User ID input
    - Add `maxlength="32"` attribute to the `#new-user-id` input
    - Add `input` event listener on `#new-user-id` that calls `validatePosixUsername()`, shows/hides the error div, and enables/disables `#btn-submit-user`
    - Ensure the submit button starts disabled when the form is shown (empty field = invalid)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 5.2 Write unit tests for frontend/backend validation parity
    - Define a shared set of test vectors (valid and invalid usernames)
    - Verify the JavaScript regex and Python validator agree on all test vectors
    - _Requirements: 2.4, 3.1_

- [x] 6. Update admin documentation
  - [x] 6.1 Update `docs/admin/user-management.md` with POSIX username format rules
    - Update the `userId` field description in the "Creating a User" section to include format rules
    - Add allowed characters (lowercase letters, digits, underscores, hyphens), start character constraint, and max length (32)
    - Add valid username examples: `jsmith`, `_admin01`, `dev-user`
    - Add invalid username examples with explanations: `Jane.Smith` (uppercase, dot), `admin@corp` (@ symbol)
    - Document the `VALIDATION_ERROR` response for invalid `userId` in the Error Cases table
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 7. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases
- The Python validator in `lambda/shared/validators.py` is the single source of truth; the JavaScript version in `frontend/js/app.js` mirrors the same rules
- No CDK or Cognito construct changes are needed — valid POSIX usernames are a strict subset of Cognito's accepted username format
