# Implementation Plan: Role-Based Access Control

## Overview

This plan implements a three-tier role model (Platform Administrator, Project Administrator, End User) with Cognito groups as the authoritative source for role assignments. The implementation proceeds incrementally: shared authorization module first, then API endpoint enforcement, membership management, POSIX provisioning scoping, reconciliation upgrade, project lifecycle extensions, frontend UI, CDK infrastructure, and documentation.

## Tasks

- [x] 1. Create shared authorization module
  - [x] 1.1 Create `lambda/shared/authorization.py` with unified role-checking functions
    - Implement `get_caller_identity`, `get_caller_groups`, `is_administrator`, `is_project_admin`, `is_project_user`, `get_admin_project_ids`, `get_member_project_ids`
    - Parse Cognito group names to extract project IDs from `ProjectAdmin-{projectId}` and `ProjectUser-{projectId}` patterns
    - _Requirements: 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 13.2_
  - [x] 1.2 Write unit tests for `lambda/shared/authorization.py`
    - Test each public function with various Cognito group claim formats
    - Test edge cases: empty groups, malformed group names, multiple project memberships
    - _Requirements: 1.3, 2.1, 2.2, 2.3, 2.4_
  - [x] 1.3 Replace per-package `auth.py` files with thin re-exports
    - Update `lambda/user_management/auth.py`, `lambda/project_management/auth.py`, `lambda/cluster_operations/auth.py`, `lambda/accounting/auth.py`, `lambda/template_management/auth.py` to re-export from `authorization`
    - _Requirements: 13.2_

- [x] 2. Enforce authorization across all API endpoints
  - [x] 2.1 Update `lambda/project_management/handler.py` authorization checks
    - Modify `_handle_list_projects` to return scoped results: all projects for Platform Admins, only member projects for Project Admins/End Users (query `UserProjectsIndex` GSI)
    - Ensure project creation/destruction requires Platform Admin
    - Ensure membership operations require Project Admin or Platform Admin
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 4.1, 4.2, 4.3, 11.1, 11.2, 12.1, 12.2, 12.3_
  - [x] 2.2 Update `lambda/cluster_operations/handler.py` authorization checks
    - Verify `is_project_user` (which includes Project Admins and Platform Admins) for all cluster operations
    - _Requirements: 3.4, 4.4, 5.1, 5.2, 5.3, 5.4, 11.3_
  - [x] 2.3 Update `lambda/user_management/handler.py` authorization checks
    - Ensure all user CRUD operations require Platform Admin via the shared authorization module
    - _Requirements: 11.4_
  - [x] 2.4 Update `lambda/accounting/handler.py` authorization checks
    - Ensure accounting data access requires Project Admin for the target project or Platform Admin
    - _Requirements: 11.5_
  - [x] 2.5 Update `lambda/template_management/handler.py` authorization checks
    - Ensure template management uses the shared authorization module
    - _Requirements: 11.6_
  - [x] 2.6 Write unit tests for authorization enforcement in each handler
    - Test that unauthorized callers receive HTTP 403 responses
    - Test scoped project listing for each role tier
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 12.1, 12.2, 12.3_

- [x] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Extend membership management operations
  - [x] 4.1 Add `list_members` function to `lambda/project_management/members.py`
    - Query Projects table for all MEMBER# records under a project
    - Return list of member dicts with userId, displayName, role, addedAt
    - _Requirements: 7.1_
  - [x] 4.2 Add `change_member_role` function to `lambda/project_management/members.py`
    - Remove user from old Cognito group, add to new Cognito group
    - Update the Membership_Record role field in DynamoDB
    - _Requirements: 6.4_
  - [x] 4.3 Add `deprovision_user_from_clusters` function to `lambda/project_management/members.py`
    - Send SSM Run Command to disable the user's Linux account on all active cluster nodes in the project
    - _Requirements: 8.2_
  - [x] 4.4 Update `remove_member` in `lambda/project_management/members.py` to trigger POSIX de-provisioning
    - Call `deprovision_user_from_clusters` when removing a member from a project with active clusters
    - Log failures and continue with removal
    - _Requirements: 8.2, 8.4_
  - [x] 4.5 Add handler routes for new membership endpoints in `lambda/project_management/handler.py`
    - `GET /projects/{projectId}/members` → `_handle_list_members`
    - `PUT /projects/{projectId}/members/{userId}` → `_handle_change_member_role`
    - Enforce Project Admin or Platform Admin authorization for all membership operations
    - _Requirements: 4.1, 4.2, 6.4, 7.1, 11.2_
  - [x] 4.6 Write unit tests for membership management functions
    - Test list_members, change_member_role, deprovision_user_from_clusters
    - Test authorization checks for membership endpoints
    - _Requirements: 4.1, 4.2, 6.4, 7.1, 8.2, 8.4_

- [x] 5. Implement project deactivation and reactivation lifecycle
  - [x] 5.1 Add `deactivate_project` function to `lambda/project_management/lifecycle.py`
    - Verify all clusters are destroyed before allowing deactivation
    - Delete ProjectAdmin-{projectId} and ProjectUser-{projectId} Cognito groups
    - Transition project status from ACTIVE to ARCHIVED
    - Log failures on Cognito group deletion and continue
    - _Requirements: 14.1, 14.2, 14.3, 14.6_
  - [x] 5.2 Add `reactivate_project` function to `lambda/project_management/lifecycle.py`
    - Recreate ProjectAdmin-{projectId} and ProjectUser-{projectId} Cognito groups
    - Read all preserved Membership_Records and add each member to the appropriate Cognito group
    - Mark failed restorations with PENDING_RESTORATION status
    - Transition project status from ARCHIVED to ACTIVE
    - _Requirements: 14.4, 14.5, 14.7_
  - [x] 5.3 Add handler routes for deactivation/reactivation in `lambda/project_management/handler.py`
    - `POST /projects/{projectId}/deactivate` → `_handle_deactivate_project`
    - `POST /projects/{projectId}/reactivate` → `_handle_reactivate_project`
    - Enforce Platform Admin authorization
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_
  - [x] 5.4 Write unit tests for project deactivation and reactivation
    - Test deactivation blocks when active clusters exist
    - Test Cognito group deletion/recreation
    - Test membership restoration and PENDING_RESTORATION marking
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7_

- [x] 6. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Scope POSIX provisioning to project members
  - [x] 7.1 Update `generate_user_data_script` in `lambda/cluster_operations/posix_provisioning.py`
    - Ensure only users with a Membership_Record for the project get Linux accounts created
    - Do not create accounts for users in PlatformUsers who lack membership
    - _Requirements: 9.1, 9.2_
  - [x] 7.2 Update `propagate_user_to_clusters` in `lambda/cluster_operations/posix_provisioning.py`
    - Add membership verification before sending SSM commands
    - _Requirements: 9.3_
  - [x] 7.3 Write unit tests for scoped POSIX provisioning
    - Test that non-members are excluded from user data scripts
    - Test that propagation is skipped for non-members
    - _Requirements: 9.1, 9.2, 9.3_

- [x] 8. Upgrade POSIX reconciliation to full daily audit
  - [x] 8.1 Rewrite `lambda/cluster_operations/posix_reconciliation.py` for full membership audit
    - Scan all ACTIVE clusters across all projects
    - For each cluster, compare Linux accounts on the node (via SSM) against current project membership
    - Create missing accounts for members without Linux accounts
    - Disable Linux accounts for non-members
    - Continue processing PENDING_PROPAGATION and PENDING_RESTORATION records
    - Log summary of actions (created, disabled, errors)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_
  - [x] 8.2 Write unit tests for the reconciliation Lambda
    - Test drift detection and correction logic
    - Test handling of PENDING_PROPAGATION records
    - Test summary logging
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

- [x] 9. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Update CDK infrastructure
  - [x] 10.1 Update `lib/constructs/api-gateway.ts` to include `authorization.py` in the shared Lambda layer
    - Ensure the bundling logic copies `authorization.py` into the layer's `python/` directory
    - _Requirements: 13.2_
  - [x] 10.2 Update `lib/constructs/project-management.ts` to add new API Gateway routes
    - Add `GET /projects/{projectId}/members`
    - Add `PUT /projects/{projectId}/members/{userId}`
    - Add `POST /projects/{projectId}/deactivate`
    - Add `POST /projects/{projectId}/reactivate`
    - Add `ListUsersInGroup` Cognito permission for reactivation
    - _Requirements: 7.1, 6.4, 14.2, 14.4_
  - [x] 10.3 Update `lib/constructs/cluster-operations.ts` for reconciliation Lambda permissions
    - Add SSM `SendCommand` and `GetCommandInvocation` permissions to the reconciliation Lambda
    - _Requirements: 10.3, 10.4, 10.5_
  - [x] 10.4 Update `lib/constructs/platform-operations.ts` for daily reconciliation schedule
    - Change the EventBridge rule to a daily `cron(0 2 * * ? *)` schedule
    - _Requirements: 10.1_
  - [x] 10.5 Write CDK construct tests for infrastructure changes
    - Test new API Gateway routes are synthesized
    - Test IAM permissions are correct
    - Test EventBridge schedule is daily
    - _Requirements: 10.1, 13.2, 14.2, 14.4_

- [x] 11. Implement membership management frontend UI
  - [x] 11.1 Add members page to `frontend/js/app.js`
    - Create a "Members" tab visible to Platform Admins on all projects and Project Admins on their projects
    - Hide the tab from End Users
    - _Requirements: 7.4, 7.5_
  - [x] 11.2 Implement members table and management controls
    - Display members list (userId, displayName, role, addedAt)
    - Add member form (userId input, role dropdown: PROJECT_ADMIN or PROJECT_USER)
    - Remove member button per row
    - Role change dropdown per row
    - Display error messages on API failures
    - _Requirements: 7.1, 7.2, 7.3, 7.6_
  - [x] 11.3 Write frontend unit tests for membership UI
    - Test visibility rules for different roles
    - Test add/remove member interactions
    - Test error display on API failures
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

- [x] 12. Update documentation
  - [x] 12.1 Update `docs/admin/user-management.md` with three-tier role model
    - Document Platform Administrator, Project Administrator, and End User roles
    - Document Cognito groups as the authoritative role source
    - _Requirements: 1.1, 1.2, 2.1, 13.1_
  - [x] 12.2 Update `docs/project-admin/project-management.md` with membership management
    - Document how to add/remove members, change roles
    - Document project deactivation and reactivation
    - _Requirements: 4.1, 4.2, 6.4, 14.1, 14.2, 14.4_
  - [x] 12.3 Update `docs/api/reference.md` with new API endpoints
    - Document GET /projects/{projectId}/members
    - Document PUT /projects/{projectId}/members/{userId}
    - Document POST /projects/{projectId}/deactivate
    - Document POST /projects/{projectId}/reactivate
    - _Requirements: 7.1, 6.4, 14.2, 14.4_

- [x] 13. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- The shared authorization module is the foundation — all subsequent tasks depend on it
- CDK infrastructure changes are grouped together to minimize deployment iterations
- Documentation updates are last to capture the final implemented behavior
