# Requirements Document

## Introduction

When the HPC Foundation stack is deployed, the platform must ensure that at least one Administrator user exists. If no user with the Administrator role is found in the PlatformUsers DynamoDB table, the system provisions a default admin user in both Cognito and DynamoDB. The initial temporary password is securely generated at deploy time, meets Cognito password policy requirements, and is communicated to the deployer via a CloudFormation output. The admin user must reset this password on first login.

## Glossary

- **Admin_Provisioner**: A CDK custom resource that runs during Foundation stack deployment to detect or create the initial Administrator user.
- **PlatformUsers_Table**: The DynamoDB table storing user profiles with partition key `USER#{userId}` and sort key `PROFILE`. Contains attributes including `userId`, `role`, `status`, and `cognitoSub`.
- **Cognito_User_Pool**: The `hpc-platform-users` Cognito User Pool used for platform authentication, with email-based sign-in and a password policy requiring 12+ characters with uppercase, lowercase, digits, and symbols.
- **Administrators_Group**: The Cognito User Pool group named `Administrators` that grants platform administrator access.
- **Administrator_Role**: The `Administrator` value for the `role` attribute on a PlatformUsers_Table record, indicating full management access.
- **POSIX_UID_Counter**: An atomic counter item in the PlatformUsers_Table (PK=`COUNTER`, SK=`POSIX_UID`) used to allocate globally unique POSIX UIDs starting at 10000.
- **Deployer**: The person or CI/CD pipeline executing `make deploy` against the Foundation stack.
- **Temporary_Password**: A securely generated password that meets Cognito_User_Pool password policy and requires a mandatory reset on first login.

## Requirements

### Requirement 1: Detect Existing Administrator

**User Story:** As a Deployer, I want the Foundation stack to detect whether an Administrator user already exists, so that it does not create a duplicate admin on redeployment.

#### Acceptance Criteria

1. WHEN the Foundation stack is deployed, THE Admin_Provisioner SHALL scan the PlatformUsers_Table for any record where the `role` attribute equals `Administrator` and the `SK` attribute equals `PROFILE`.
2. WHEN the Admin_Provisioner finds one or more records with Administrator_Role, THE Admin_Provisioner SHALL skip user creation and complete successfully.
3. THE Admin_Provisioner SHALL treat any `userId` value as a valid Administrator match, not only the default `admin` userId.

### Requirement 2: Create Default Admin User in Cognito

**User Story:** As a Deployer, I want a default admin user created in the Cognito User Pool when no Administrator exists, so that I can log in and manage the platform immediately after deployment.

#### Acceptance Criteria

1. WHEN no Administrator_Role user is found in the PlatformUsers_Table, THE Admin_Provisioner SHALL create a Cognito user with username `admin` in the Cognito_User_Pool.
2. WHEN creating the Cognito user, THE Admin_Provisioner SHALL set the email attribute to a value provided as a mandatory CDK context parameter (`adminEmail`). IF the `adminEmail` context parameter is not provided, THE Admin_Provisioner SHALL cause the CDK synthesis to fail with a clear error message.
3. WHEN creating the Cognito user, THE Admin_Provisioner SHALL mark the `email_verified` attribute as `true`.
4. WHEN creating the Cognito user, THE Admin_Provisioner SHALL set the user status to `FORCE_CHANGE_PASSWORD` so that the Deployer must reset the password on first login.
5. WHEN creating the Cognito user, THE Admin_Provisioner SHALL add the user to the Administrators_Group.

### Requirement 3: Create Default Admin User in DynamoDB

**User Story:** As a Deployer, I want the admin user record stored in the PlatformUsers DynamoDB table, so that the platform recognises the admin through its standard user management logic.

#### Acceptance Criteria

1. WHEN no Administrator_Role user is found in the PlatformUsers_Table, THE Admin_Provisioner SHALL allocate a POSIX UID by atomically incrementing the POSIX_UID_Counter.
2. WHEN creating the DynamoDB record, THE Admin_Provisioner SHALL store the record with partition key `USER#admin`, sort key `PROFILE`, and attributes: `userId` set to `admin`, `displayName` set to `Admin`, `email` matching the Cognito user email, `role` set to `Administrator`, `posixUid` and `posixGid` set to the allocated POSIX UID, `status` set to `ACTIVE`, `cognitoSub` set to the Cognito user's `sub` attribute, and `createdAt` and `updatedAt` set to the current ISO 8601 UTC timestamp.
3. THE Admin_Provisioner SHALL use a DynamoDB condition expression `attribute_not_exists(PK)` to prevent overwriting an existing user record with the same userId.

### Requirement 4: Secure Password Generation

**User Story:** As a Deployer, I want the initial admin password generated securely at deploy time, so that no hardcoded password exists in the source code or CloudFormation template.

#### Acceptance Criteria

1. THE Admin_Provisioner SHALL generate the Temporary_Password at runtime using a cryptographically secure random generator.
2. THE Admin_Provisioner SHALL generate a Temporary_Password that is at least 16 characters long and contains at least one uppercase letter, one lowercase letter, one digit, and one symbol.
3. THE Admin_Provisioner SHALL NOT store the Temporary_Password in the source code, CDK construct code, or synthesised CloudFormation template parameters.

### Requirement 5: Communicate Initial Password to Deployer

**User Story:** As a Deployer, I want the initial admin password displayed after deployment, so that I can log in to the platform for the first time.

#### Acceptance Criteria

1. WHEN the Admin_Provisioner creates a new admin user, THE Admin_Provisioner SHALL output the Temporary_Password as a CloudFormation stack output named `AdminUserPassword`.
2. WHEN the Admin_Provisioner creates a new admin user, THE Admin_Provisioner SHALL output the admin username as a CloudFormation stack output named `AdminUserName`.
3. WHEN the Admin_Provisioner detects an existing Administrator, THE Admin_Provisioner SHALL NOT emit the `AdminUserPassword` or `AdminUserName` stack outputs.

### Requirement 6: Idempotent Deployment Behaviour

**User Story:** As a Deployer, I want repeated deployments to be safe and predictable, so that running `make deploy` multiple times does not corrupt the admin user state.

#### Acceptance Criteria

1. WHEN the Foundation stack is deployed and an Administrator_Role user already exists, THE Admin_Provisioner SHALL complete without modifying any existing user records in the PlatformUsers_Table or Cognito_User_Pool.
2. WHEN the Foundation stack is updated (not a fresh deploy) and the admin user was previously created, THE Admin_Provisioner SHALL NOT regenerate or overwrite the Temporary_Password.
3. IF the Admin_Provisioner fails to create the Cognito user, THEN THE Admin_Provisioner SHALL NOT create a partial record in the PlatformUsers_Table.
4. IF the Admin_Provisioner fails to create the DynamoDB record after creating the Cognito user, THEN THE Admin_Provisioner SHALL delete the Cognito user to avoid orphaned accounts.

### Requirement 7: Error Handling

**User Story:** As a Deployer, I want clear error reporting when admin provisioning fails, so that I can diagnose and resolve issues without inspecting CloudWatch logs.

#### Acceptance Criteria

1. IF the Admin_Provisioner fails to scan the PlatformUsers_Table, THEN THE Admin_Provisioner SHALL report the failure reason in the CloudFormation deployment event and cause the stack deployment to fail.
2. IF the Admin_Provisioner fails to create the Cognito user due to a service error, THEN THE Admin_Provisioner SHALL report the Cognito error message in the CloudFormation deployment event.
3. IF the Admin_Provisioner fails to allocate a POSIX UID, THEN THE Admin_Provisioner SHALL report the DynamoDB error and cause the stack deployment to fail.

### Requirement 8: Stack Update Abuse Prevention

**User Story:** As a Deployer, I want the admin provisioner to be resistant to stack update manipulation, so that a person with CloudFormation access cannot use stack updates to create unauthorized admin accounts, reset existing admin credentials, or bypass the existing admin detection.

#### Acceptance Criteria

1. WHEN the Admin_Provisioner receives an Update event, THE Admin_Provisioner SHALL execute the same admin detection logic as a Create event, scanning the PlatformUsers_Table for any existing Administrator_Role user before taking any action.
2. WHEN the Admin_Provisioner receives an Update event and an Administrator_Role user already exists, THE Admin_Provisioner SHALL skip user creation regardless of whether the `AdminEmail` resource property has changed from the original deployment.
3. THE Admin_Provisioner SHALL NOT modify, overwrite, or reset the credentials of any existing user in the Cognito_User_Pool or the PlatformUsers_Table during an Update event.
4. WHEN the Admin_Provisioner skips user creation due to an existing Administrator, THE Admin_Provisioner SHALL return empty Data in the CloudFormation response, ensuring the `AdminUserName` and `AdminUserPassword` stack outputs do not expose new credential values.
5. THE Admin_Provisioner SHALL NOT use the `AdminEmail` resource property to determine whether a new admin user should be created; the sole criterion for creation SHALL be the absence of any Administrator_Role user in the PlatformUsers_Table.
6. IF the PlatformUsers_Table contains an existing record with partition key `USER#admin`, THEN THE Admin_Provisioner SHALL NOT overwrite that record, enforced by the `attribute_not_exists(PK)` condition expression on all DynamoDB PutItem calls.
