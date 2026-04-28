# Requirements Document

## Introduction

This feature overhauls user management and role-based access control for the HPC Self-Service Platform. The current system stores user roles ("User" or "Administrator") in DynamoDB and only recognises two tiers: platform administrators and end users. This feature introduces a three-tier role model — Platform Administrator, Project Administrator, and End User — with Cognito groups as the authoritative source of role assignments. It adds a project membership management UI, enforces scoped authorisation across all API operations, ensures POSIX user provisioning on clusters is limited to project members, and introduces a daily reconciliation schedule to audit cluster access.

## Glossary

- **Platform**: The HPC Self-Service Platform, comprising the API Gateway, Lambda functions, DynamoDB tables, Cognito User Pool, and frontend web portal.
- **Cognito_User_Pool**: The Amazon Cognito User Pool that authenticates platform users and issues JWT tokens containing group membership claims.
- **Cognito_Group**: A named group within the Cognito_User_Pool used to represent role assignments (e.g. "Administrators", "ProjectAdmin-{projectId}", "ProjectUser-{projectId}").
- **Platform_Administrator**: A user who belongs to the "Administrators" Cognito_Group. Has full access to all platform operations across all projects.
- **Project_Administrator**: A user who belongs to a "ProjectAdmin-{projectId}" Cognito_Group for a specific project. Can manage membership and settings for that project only.
- **End_User**: A user who belongs to a "ProjectUser-{projectId}" Cognito_Group for a specific project. Can create, update, destroy, and use clusters within that project.
- **Project_Member**: Any user (End_User or Project_Administrator) who has a membership record associating them with a specific project.
- **Membership_Record**: A DynamoDB item in the Projects table (PK=PROJECT#{projectId}, SK=MEMBER#{userId}) that records a user's association with a project and their project-level role.
- **POSIX_Provisioning**: The process of creating Linux user accounts (with UID/GID) on cluster EC2 instances so that Project_Members can log in.
- **POSIX_Reconciliation**: A scheduled process that audits and corrects POSIX user accounts on active cluster nodes to match current project membership.
- **Active_Cluster**: A cluster with status "ACTIVE" in the Clusters DynamoDB table.
- **Membership_Management_UI**: A frontend interface that allows Project_Administrators to add and remove users from their project.
- **Authorization_Module**: The auth.py module in each Lambda function package that extracts Cognito claims and evaluates role-based access.
- **Project_Deactivation**: The process of transitioning a project from ACTIVE to ARCHIVED status. Requires all clusters to be destroyed first. Removes the project's Cognito_Groups (and therefore all members' access) while preserving Membership_Records in DynamoDB for future reactivation.
- **Project_Reactivation**: The process of transitioning a project from ARCHIVED back to ACTIVE status, recreating its Cognito_Groups and restoring all preserved Membership_Records to the appropriate Cognito_Groups.

## Requirements

### Requirement 1: Cognito as Authoritative Role Source

**User Story:** As a platform operator, I want user roles to be defined exclusively in Cognito groups rather than DynamoDB fields, so that role management can be delegated to an external identity provider (e.g. Active Directory) in the future.

#### Acceptance Criteria

1. WHEN a new user is created with the "Administrator" role, THE Platform SHALL add the user to the "Administrators" Cognito_Group and SHALL NOT rely on the DynamoDB "role" field for authorisation decisions.
2. WHEN a user is added to a project, THE Platform SHALL add the user to the appropriate Cognito_Group ("ProjectAdmin-{projectId}" or "ProjectUser-{projectId}") as the authoritative role assignment.
3. THE Authorization_Module SHALL determine a caller's roles by inspecting the "cognito:groups" claim in the JWT token for every API request.
4. THE Authorization_Module SHALL NOT read the "role" field from the PlatformUsers DynamoDB table when making authorisation decisions.
5. WHEN a user is removed from a project, THE Platform SHALL remove the user from the corresponding Cognito_Group.

### Requirement 2: Three-Tier Role Model

**User Story:** As a platform operator, I want three distinct roles (Platform Administrator, Project Administrator, End User), so that access control is granular and follows the principle of least privilege.

#### Acceptance Criteria

1. THE Platform SHALL recognise three roles: Platform_Administrator (member of "Administrators" Cognito_Group), Project_Administrator (member of "ProjectAdmin-{projectId}" Cognito_Group), and End_User (member of "ProjectUser-{projectId}" Cognito_Group).
2. THE Platform SHALL treat Platform_Administrator access as a superset of Project_Administrator access for all projects.
3. THE Platform SHALL treat Project_Administrator access as a superset of End_User access within the same project.
4. WHEN a user holds no Cognito_Group membership for a given project, THE Platform SHALL deny that user access to all resources within that project.

### Requirement 3: Platform Administrator Permissions

**User Story:** As a platform administrator, I want to create and destroy projects and perform all project administrator actions on any project, so that I can manage the entire platform.

#### Acceptance Criteria

1. THE Platform SHALL allow Platform_Administrators to create new projects.
2. THE Platform SHALL allow Platform_Administrators to destroy existing projects.
3. THE Platform SHALL allow Platform_Administrators to add and remove members from any project.
4. THE Platform SHALL allow Platform_Administrators to perform all End_User operations (create, update, destroy, and use clusters) on any project.
5. THE Platform SHALL allow Platform_Administrators to list all projects on the platform.

### Requirement 4: Project Administrator Permissions

**User Story:** As a project administrator, I want to manage membership within my assigned project but not other projects, so that I can onboard and offboard team members without requiring platform administrator intervention.

#### Acceptance Criteria

1. THE Platform SHALL allow Project_Administrators to add End_Users to the project for which the Project_Administrator holds the "ProjectAdmin-{projectId}" Cognito_Group membership.
2. THE Platform SHALL allow Project_Administrators to remove End_Users from the project for which the Project_Administrator holds the "ProjectAdmin-{projectId}" Cognito_Group membership.
3. WHEN a Project_Administrator attempts to add or remove members from a project for which the Project_Administrator does not hold the "ProjectAdmin-{projectId}" Cognito_Group membership, THE Platform SHALL deny the request with an authorisation error.
4. THE Platform SHALL allow Project_Administrators to perform all End_User operations (create, update, destroy, and use clusters) within their assigned project.
5. THE Platform SHALL allow Project_Administrators to set and modify the budget for their assigned project.

### Requirement 5: End User Permissions

**User Story:** As an end user, I want to create, destroy, update, and use clusters within my assigned project, so that I can run HPC workloads.

#### Acceptance Criteria

1. THE Platform SHALL allow End_Users to create clusters within a project for which the End_User holds the "ProjectUser-{projectId}" Cognito_Group membership.
2. THE Platform SHALL allow End_Users to destroy clusters within a project for which the End_User holds the "ProjectUser-{projectId}" Cognito_Group membership.
3. THE Platform SHALL allow End_Users to list and view clusters within a project for which the End_User holds the "ProjectUser-{projectId}" Cognito_Group membership.
4. WHEN an End_User attempts to access clusters in a project for which the End_User holds no Cognito_Group membership, THE Platform SHALL deny the request with an authorisation error.
5. THE Platform SHALL deny End_Users the ability to add or remove project members.

### Requirement 6: Project Administrator Assignment

**User Story:** As a platform administrator, I want to assign project administrator users to projects at creation time or later, so that each project has a designated administrator.

#### Acceptance Criteria

1. WHEN a Platform_Administrator creates a project, THE Platform SHALL allow specifying one or more initial Project_Administrator user IDs.
2. WHEN a Platform_Administrator adds a member to a project with the role "PROJECT_ADMIN", THE Platform SHALL add that user to the "ProjectAdmin-{projectId}" Cognito_Group.
3. WHEN a Platform_Administrator adds a member to a project with the role "PROJECT_USER", THE Platform SHALL add that user to the "ProjectUser-{projectId}" Cognito_Group.
4. THE Platform SHALL allow a Platform_Administrator to change a Project_Member's role between PROJECT_ADMIN and PROJECT_USER by updating the Membership_Record and the corresponding Cognito_Group memberships.


### Requirement 7: Membership Management UI

**User Story:** As a project administrator, I want a user interface where I can associate users with my project and remove those associations, so that I can manage my team without using the API directly.

#### Acceptance Criteria

1. THE Membership_Management_UI SHALL display a list of current Project_Members for the selected project, including each member's user ID, display name, and project role.
2. THE Membership_Management_UI SHALL provide a form to add a new user to the project by specifying the user ID and the project role (PROJECT_ADMIN or PROJECT_USER).
3. THE Membership_Management_UI SHALL provide a control to remove an existing Project_Member from the project.
4. WHEN a Project_Administrator views the Membership_Management_UI, THE Platform SHALL display membership data only for projects where the Project_Administrator holds the "ProjectAdmin-{projectId}" Cognito_Group membership.
5. WHEN a Platform_Administrator views the Membership_Management_UI, THE Platform SHALL allow the Platform_Administrator to manage membership for any project.
6. IF the add-member or remove-member API call fails, THEN THE Membership_Management_UI SHALL display a descriptive error message to the user.

### Requirement 8: POSIX Provisioning on Membership Change

**User Story:** As a project administrator, I want users to be provisioned on active clusters automatically when they are added to or removed from a project, so that access is granted or revoked promptly.

#### Acceptance Criteria

1. WHEN a user is added to a project that has Active_Clusters, THE Platform SHALL trigger POSIX_Provisioning to create the user's Linux account on all Active_Cluster nodes in that project.
2. WHEN a user is removed from a project that has Active_Clusters, THE Platform SHALL trigger POSIX de-provisioning to disable the user's Linux account on all Active_Cluster nodes in that project.
3. IF POSIX_Provisioning fails for one or more Active_Clusters during a membership addition, THEN THE Platform SHALL mark the Membership_Record with a "PENDING_PROPAGATION" status and return the status in the API response.
4. IF POSIX de-provisioning fails for one or more Active_Clusters during a membership removal, THEN THE Platform SHALL log the failure and continue with the membership removal.

### Requirement 9: Scoped POSIX Provisioning

**User Story:** As a platform operator, I want POSIX user provisioning on clusters to be scoped to users who have access to the project, so that only authorised users have Linux accounts on cluster nodes.

#### Acceptance Criteria

1. WHEN generating the EC2 user data script for a cluster in a project, THE POSIX_Provisioning module SHALL create Linux accounts only for users who hold a Membership_Record for that project.
2. THE POSIX_Provisioning module SHALL NOT create Linux accounts for users in the PlatformUsers table who do not hold a Membership_Record for the cluster's project.
3. WHEN propagating a single user to Active_Clusters via SSM Run Command, THE POSIX_Provisioning module SHALL verify that the user holds a Membership_Record for the target project before sending the command.

### Requirement 10: Daily Reconciliation Schedule

**User Story:** As a platform operator, I want a daily scheduled process that audits POSIX user accounts on active clusters against current project membership, so that access drift is detected and corrected automatically.

#### Acceptance Criteria

1. THE Platform SHALL execute the POSIX_Reconciliation Lambda on a daily schedule using an EventBridge rule.
2. WHEN the daily POSIX_Reconciliation runs, THE POSIX_Reconciliation Lambda SHALL scan all Active_Clusters across all projects.
3. FOR EACH Active_Cluster, THE POSIX_Reconciliation Lambda SHALL compare the set of Linux accounts on the cluster node against the current set of Project_Members for that cluster's project.
4. WHEN the POSIX_Reconciliation Lambda detects a user who is a Project_Member but does not have a Linux account on the cluster node, THE POSIX_Reconciliation Lambda SHALL create the missing Linux account.
5. WHEN the POSIX_Reconciliation Lambda detects a Linux account on the cluster node for a user who is no longer a Project_Member, THE POSIX_Reconciliation Lambda SHALL disable that Linux account.
6. THE POSIX_Reconciliation Lambda SHALL continue to process PENDING_PROPAGATION membership records as part of the daily reconciliation run.
7. THE POSIX_Reconciliation Lambda SHALL log a summary of reconciliation actions (accounts created, accounts disabled, errors) for each run.

### Requirement 11: Authorization Enforcement Across All API Endpoints

**User Story:** As a platform operator, I want every API endpoint to enforce role-based authorisation consistently, so that no endpoint can be accessed by unauthorised users.

#### Acceptance Criteria

1. WHEN a request is received at the project management API, THE Authorization_Module SHALL verify the caller is a Platform_Administrator before allowing project creation or destruction.
2. WHEN a request is received at the project membership API, THE Authorization_Module SHALL verify the caller is a Project_Administrator for the target project or a Platform_Administrator before allowing member addition or removal.
3. WHEN a request is received at the cluster operations API, THE Authorization_Module SHALL verify the caller is a Project_Member (End_User, Project_Administrator, or Platform_Administrator) for the target project before allowing cluster operations.
4. WHEN a request is received at the user management API, THE Authorization_Module SHALL verify the caller is a Platform_Administrator before allowing user creation, deactivation, or reactivation.
5. WHEN a request is received at the accounting API, THE Authorization_Module SHALL verify the caller is a Project_Administrator for the target project or a Platform_Administrator before allowing access to accounting data.
6. IF an unauthorised caller attempts any protected operation, THEN THE Platform SHALL return an HTTP 403 response with a descriptive error message.

### Requirement 12: List Projects for Project Administrators

**User Story:** As a project administrator, I want to see the projects I administer in the web portal, so that I can navigate to them and manage their membership and clusters.

#### Acceptance Criteria

1. WHEN a Project_Administrator requests the list of projects, THE Platform SHALL return only the projects for which the caller holds a "ProjectAdmin-{projectId}" Cognito_Group membership.
2. WHEN a Platform_Administrator requests the list of projects, THE Platform SHALL return all projects.
3. WHEN an End_User requests the list of projects, THE Platform SHALL return only the projects for which the End_User holds a "ProjectUser-{projectId}" or "ProjectAdmin-{projectId}" Cognito_Group membership.

### Requirement 13: Remove DynamoDB Role Dependency

**User Story:** As a developer, I want to eliminate the redundant "role" field in DynamoDB user records for authorisation purposes, so that there is a single source of truth for role assignments.

#### Acceptance Criteria

1. THE Platform SHALL retain the "role" field in the PlatformUsers DynamoDB table for display purposes only.
2. THE Authorization_Module in user_management, project_management, cluster_operations, and accounting Lambda packages SHALL derive all authorisation decisions from Cognito_Group claims in the JWT token.
3. WHEN a user's platform role is changed (e.g. from "User" to "Administrator"), THE Platform SHALL update the user's Cognito_Group membership as the authoritative change and update the DynamoDB "role" field for display consistency.

### Requirement 14: Project Deactivation and Reactivation Lifecycle

**User Story:** As a platform administrator, I want project deactivation to revoke all member access while preserving membership records, and reactivation to restore those memberships automatically, so that projects can be temporarily shelved without losing team composition.

#### Acceptance Criteria

1. THE Platform SHALL require all clusters in a project to be destroyed before allowing Project_Deactivation.
2. WHEN a project undergoes Project_Deactivation, THE Platform SHALL delete the "ProjectAdmin-{projectId}" and "ProjectUser-{projectId}" Cognito_Groups from the Cognito_User_Pool, removing all members' role-based access to the project.
3. WHEN a project undergoes Project_Deactivation, THE Platform SHALL retain all Membership_Records (PK=PROJECT#{projectId}, SK=MEMBER#{userId}) in the Projects DynamoDB table.
4. WHEN a project undergoes Project_Reactivation, THE Platform SHALL recreate the "ProjectAdmin-{projectId}" and "ProjectUser-{projectId}" Cognito_Groups in the Cognito_User_Pool.
5. WHEN a project undergoes Project_Reactivation, THE Platform SHALL read all preserved Membership_Records for the project and add each member to the appropriate Cognito_Group based on the role stored in the Membership_Record.
6. IF Cognito_Group deletion fails during Project_Deactivation, THEN THE Platform SHALL log the failure and continue with the remaining deactivation steps.
7. IF restoring a member to a Cognito_Group fails during Project_Reactivation, THEN THE Platform SHALL mark the affected Membership_Record with a "PENDING_RESTORATION" status and log the failure.
