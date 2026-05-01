# Implementation Plan: Admin User Provisioning

## Overview

Implement a Lambda-backed CloudFormation custom resource that ensures at least one Administrator user exists after every Foundation stack deployment. The implementation spans a Python Lambda function (`lambda/admin_provisioner/handler.py`), a CDK construct (`lib/constructs/admin-provisioner.ts`), integration into the Foundation stack, and comprehensive tests. Each task builds incrementally — Lambda logic first, then CDK infrastructure, then wiring and documentation.

## Tasks

- [x] 1. Implement the admin provisioner Lambda function
  - [x] 1.1 Create `lambda/admin_provisioner/handler.py` with CloudFormation custom resource handler
    - Implement `handler(event, context)` that routes `Create`, `Update`, and `Delete` request types
    - `Create` and `Update` call the provisioning logic; `Delete` returns SUCCESS as a no-op
    - Implement `_send_response(event, context, status, data, reason)` using `urllib.request` to POST the cfnresponse JSON to the CloudFormation pre-signed URL
    - Implement `_scan_for_admin(table_name)` that scans PlatformUsers for any record with `role=Administrator` AND `SK=PROFILE`, returning `True` if found
    - Implement `_generate_password(length=16)` using Python `secrets` module to produce a password with at least one uppercase, one lowercase, one digit, and one symbol character
    - Implement `_allocate_posix_uid(table_name)` using atomic DynamoDB `UpdateItem` with `ADD` on the `COUNTER`/`POSIX_UID` item (same pattern as `lambda/user_management/users.py`)
    - Implement `_create_admin_user(table_name, user_pool_id, email, password)` that orchestrates: allocate POSIX UID → Cognito `AdminCreateUser` with `TemporaryPassword` → `AdminAddUserToGroup("Administrators")` → DynamoDB `PutItem` with `attribute_not_exists(PK)` condition. On DynamoDB failure, roll back by deleting the Cognito user. On Cognito group failure, roll back by deleting the Cognito user.
    - Set `PhysicalResourceId` to `AdminProvisioner-<timestamp>` on create, `AdminProvisioner-existing` when admin exists, `AdminProvisioner-failed` on failure
    - Return `AdminUserName` and `AdminUserPassword` in response `Data` only when a new user is created
    - Read `TABLE_NAME`, `USER_POOL_ID`, and `ADMIN_EMAIL` from `ResourceProperties`
    - Use module-level boto3 client/resource initialisation for connection reuse
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x] 1.2 Write property test: admin detection scans by role, not userId
    - **Property 1: Admin detection scans by role, not userId**
    - Create `test/lambda/test_admin_provisioner_properties.py`
    - Use Hypothesis to generate random lists of user records with varying `userId`, `role` ∈ {User, Administrator}, and `SK` ∈ {PROFILE, OTHER}
    - Assert `_scan_for_admin` returns `True` iff at least one record has `role=Administrator` AND `SK=PROFILE`
    - Use `@settings(max_examples=10)`
    - Mock DynamoDB scan using moto
    - **Validates: Requirements 1.1, 1.3**

  - [x] 1.3 Write property test: existing admin prevents all write operations
    - **Property 2: Existing admin prevents all write operations**
    - Generate table states containing at least one admin record
    - Invoke the handler with a Create event and verify zero DynamoDB write calls and zero Cognito mutating calls
    - Assert SUCCESS response with empty Data
    - Use `@settings(max_examples=5)`
    - **Validates: Requirements 1.2, 6.1, 8.1**

  - [x] 1.4 Write property test: created DynamoDB record contains all required attributes
    - **Property 3: Created DynamoDB record contains all required attributes with correct values**
    - Generate random valid email strings, POSIX UID counter values, and Cognito sub strings
    - Mock Cognito and DynamoDB, invoke creation logic, capture the PutItem call arguments
    - Assert the record contains all required attributes: PK, SK, userId, displayName, email, role, posixUid, posixGid, status, cognitoSub, createdAt, updatedAt with correct values
    - Use `@settings(max_examples=10)`
    - **Validates: Requirements 2.2, 3.2**

  - [x] 1.5 Write property test: generated password meets Cognito policy
    - **Property 4: Generated password meets Cognito policy**
    - Call `_generate_password()` repeatedly via Hypothesis (use `@given(st.integers(min_value=16, max_value=64))` for length parameter)
    - Assert each password is at least 16 characters and contains at least one uppercase, one lowercase, one digit, and one symbol
    - Use `@settings(max_examples=10)`
    - **Validates: Requirements 4.2**

  - [x] 1.6 Write property test: creation failure leaves no partial state
    - **Property 5: Creation failure leaves no partial state**
    - Generate random error injection points from `["cognito_create", "cognito_group", "dynamodb_put"]`
    - For each injection point, mock the corresponding service call to raise an error
    - Assert: if Cognito creation fails, no DynamoDB PutItem is attempted; if DynamoDB PutItem fails after Cognito creation, the Cognito user is deleted
    - Use `@settings(max_examples=10)`
    - **Validates: Requirements 6.3, 6.4**

  - [x] 1.7 Write property test: service errors propagate to CloudFormation response
    - **Property 6: Service errors propagate to CloudFormation response**
    - Generate random error messages and injection points (scan failure, Cognito error, POSIX UID allocation failure)
    - Assert the cfnresponse contains `Status=FAILED` and the `Reason` string includes the original error message
    - Use `@settings(max_examples=10)`
    - **Validates: Requirements 7.1, 7.2, 7.3**

  - [x] 1.8 Write property test: update events with changed properties cannot bypass admin detection
    - **Property 7: Update events with changed properties cannot bypass admin detection**
    - Generate Update events with varying `AdminEmail` values, with an existing admin in the table
    - Assert zero DynamoDB write calls, zero Cognito mutating calls, and SUCCESS response with empty Data
    - Use `@settings(max_examples=10)`
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**

- [x] 2. Write example-based unit tests for the admin provisioner Lambda
  - [x] 2.1 Create `test/lambda/test_admin_provisioner.py` with example-based tests
    - Use moto to mock DynamoDB and Cognito
    - `test_create_event_no_existing_admin` — happy path: scan returns empty, user created in Cognito + DynamoDB, response includes credentials
    - `test_create_event_existing_admin_skips` — scan returns admin record, no writes, empty Data
    - `test_update_event_existing_admin_skips` — Update lifecycle with existing admin, no writes
    - `test_delete_event_noop` — Delete lifecycle returns SUCCESS with no side effects
    - `test_cognito_user_force_change_password` — verify `AdminCreateUser` uses `TemporaryPassword` param
    - `test_condition_expression_on_putitem` — verify `attribute_not_exists(PK)` in PutItem
    - `test_posix_uid_atomic_increment` — verify UpdateItem with ADD on counter
    - `test_dynamodb_put_failure_rolls_back_cognito_user` — verify Cognito user deleted on DynamoDB failure
    - `test_cognito_group_failure_rolls_back_cognito_user` — verify Cognito user deleted on group add failure
    - `test_scan_failure_returns_failed_response` — verify FAILED cfnresponse on scan error
    - `test_update_event_changed_email_does_not_create_second_admin` — verify Update with different AdminEmail, admin exists, no writes, empty Data
    - `test_update_event_no_credential_modification` — verify Update event never modifies existing Cognito user or DynamoDB record
    - _Requirements: 1.1, 1.2, 2.1, 2.3, 2.4, 2.5, 3.1, 3.3, 5.1, 5.2, 5.3, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 8.1, 8.2, 8.3, 8.4, 8.5_

- [x] 3. Checkpoint — Verify Lambda logic and tests
  - Ensure all Python tests pass (`.venv/bin/pytest test/lambda/test_admin_provisioner.py test/lambda/test_admin_provisioner_properties.py -v`), ask the user if questions arise.

- [x] 4. Implement the AdminProvisioner CDK construct
  - [x] 4.1 Create `lib/constructs/admin-provisioner.ts` CDK construct
    - Define `AdminProvisionerProps` interface with `platformUsersTable`, `userPool`, and `adminEmail` properties
    - Validate that `adminEmail` is provided — throw a clear error during synthesis if missing
    - Create a Lambda function using `lambda.Function` with Python 3.13 runtime, code from `lambda/admin_provisioner/`, handler `handler.handler`, 60-second timeout
    - Pass `TABLE_NAME`, `USER_POOL_ID`, and `ADMIN_EMAIL` as Lambda environment variables
    - Grant least-privilege IAM: DynamoDB `Scan`, `PutItem`, `UpdateItem` on the PlatformUsers table; Cognito `AdminCreateUser`, `AdminAddUserToGroup`, `AdminGetUser`, `AdminDeleteUser` on the User Pool
    - Create a `CfnCustomResource` with `ServiceToken` pointing to the Lambda function ARN, passing `TableName`, `UserPoolId`, and `AdminEmail` as resource properties
    - Create `CfnOutput` for `AdminUserName` and `AdminUserPassword` using `Fn.getAtt` on the custom resource response Data
    - _Requirements: 2.2, 4.3, 5.1, 5.2_

  - [x] 4.2 Integrate AdminProvisioner into `lib/foundation-stack.ts`
    - Import `AdminProvisioner` from `./constructs/admin-provisioner`
    - Instantiate after `DatabaseTables` and `CognitoAuth` constructs (between construct #2 and #5 in the existing order)
    - Pass `databaseTables.platformUsersTable`, `cognitoAuth.userPool`, and `this.node.tryGetContext('adminEmail')` as props
    - _Requirements: 2.2, 5.1, 5.2_

- [x] 5. Write CDK construct tests
  - [x] 5.1 Create `test/admin-provisioner.test.ts` with Jest tests
    - `test_lambda_created_with_correct_runtime` — assert Lambda uses Python 3.13 runtime
    - `test_lambda_has_required_env_vars` — assert TABLE_NAME, USER_POOL_ID, ADMIN_EMAIL in environment
    - `test_iam_policy_least_privilege` — assert only required DynamoDB and Cognito actions are granted
    - `test_cfn_outputs_created` — assert AdminUserName and AdminUserPassword outputs exist
    - `test_custom_resource_references_lambda` — assert CfnCustomResource ServiceToken points to Lambda ARN
    - `test_synth_fails_without_admin_email` — assert synthesis throws when adminEmail is not provided
    - _Requirements: 2.2, 4.3, 5.1, 5.2_

- [x] 6. Checkpoint — Verify all tests pass
  - Ensure all tests pass (`make test`), ask the user if questions arise.

- [x] 7. Update documentation
  - [x] 7.1 Update `docs/` with admin provisioning documentation
    - Document the admin provisioning feature: what it does, when it runs, how to configure `adminEmail`
    - Document the deployment command with adminEmail context: `cdk deploy -c adminEmail=ops@company.com`
    - Document the first-login flow: retrieve password from CloudFormation outputs, log in, reset password
    - Document idempotent behaviour: redeployments skip creation when admin exists
    - Document security considerations: the provisioner cannot be used to create unauthorized admins or reset credentials on stack updates
    - _Requirements: 2.2, 4.3, 5.1, 5.2, 6.1, 6.2, 8.1, 8.2, 8.3_

- [x] 8. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass (`make test`), ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases using moto for AWS service mocking
- CDK construct tests use Jest with standard CDK assertions
- Python Lambda uses Python 3.13; CDK construct uses TypeScript — matching existing project conventions
