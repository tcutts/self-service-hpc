# Requirements Document

## Introduction

This feature adds an automated cleanup mechanism for orphaned FSx for Lustre filesystems. When the cluster creation Step Functions workflow fails partway through, the rollback handler may not successfully delete the FSx filesystem that was created during the workflow. These orphaned filesystems continue to incur costs with no associated active cluster. The Orphaned FSx Cleanup feature provides a scheduled Lambda function that periodically scans for these orphaned resources, cross-references the Clusters DynamoDB table, and deletes any filesystems that belong to clusters in a terminal state or that no longer exist.

## Glossary

- **Cleanup_Lambda**: The AWS Lambda function responsible for detecting and deleting orphaned FSx for Lustre filesystems on a scheduled basis.
- **Clusters_Table**: The DynamoDB table (named "Clusters") that stores cluster records with partition key `PROJECT#{projectId}` and sort key `CLUSTER#{clusterName}`, including a `status` field.
- **Orphaned_Filesystem**: An FSx for Lustre filesystem tagged with `Project` and `ClusterName` tags whose corresponding cluster record in the Clusters_Table either does not exist or has a terminal status (FAILED or DESTROYED).
- **Terminal_Status**: A cluster status indicating the cluster is no longer active or being created. Terminal statuses are: FAILED, DESTROYED.
- **Active_Status**: A cluster status indicating the cluster is operational or being provisioned. Active statuses are: CREATING, ACTIVE.
- **Data_Repository_Association**: An FSx for Lustre resource that links the filesystem to an S3 bucket for data synchronisation. Data repository associations must be deleted before the filesystem can be deleted.
- **Notification_Topic**: The SNS topic used to alert platform administrators when orphaned resources are detected and cleaned up.
- **Cleanup_Schedule**: The EventBridge scheduled rule that triggers the Cleanup_Lambda at a defined interval.

## Requirements

### Requirement 1: Scheduled Execution

**User Story:** As a platform administrator, I want the orphaned filesystem cleanup to run automatically on a schedule, so that orphaned resources are detected and removed without manual intervention.

#### Acceptance Criteria

1. THE Cleanup_Schedule SHALL trigger the Cleanup_Lambda at a configurable interval (default: every 6 hours)
2. WHEN the Cleanup_Schedule triggers, THE Cleanup_Lambda SHALL execute a full scan of FSx for Lustre filesystems in the account and region

### Requirement 2: Orphaned Filesystem Detection

**User Story:** As a platform administrator, I want the system to identify FSx filesystems that belong to clusters that no longer exist or have failed, so that I know which resources are orphaned.

#### Acceptance Criteria

1. WHEN the Cleanup_Lambda executes, THE Cleanup_Lambda SHALL retrieve all FSx for Lustre filesystems that have both a `Project` tag and a `ClusterName` tag
2. WHEN an FSx filesystem has `Project` and `ClusterName` tags, THE Cleanup_Lambda SHALL query the Clusters_Table using partition key `PROJECT#{Project tag value}` and sort key `CLUSTER#{ClusterName tag value}` to retrieve the cluster record
3. WHEN the cluster record does not exist in the Clusters_Table, THE Cleanup_Lambda SHALL classify the filesystem as an Orphaned_Filesystem
4. WHEN the cluster record exists with a Terminal_Status (FAILED or DESTROYED), THE Cleanup_Lambda SHALL classify the filesystem as an Orphaned_Filesystem
5. WHEN the cluster record exists with an Active_Status (CREATING or ACTIVE), THE Cleanup_Lambda SHALL skip the filesystem and not classify it as orphaned

### Requirement 3: Data Repository Association Cleanup

**User Story:** As a platform administrator, I want data repository associations to be removed before filesystem deletion, so that the deletion succeeds without errors.

#### Acceptance Criteria

1. WHEN the Cleanup_Lambda identifies an Orphaned_Filesystem, THE Cleanup_Lambda SHALL retrieve all data repository associations for that filesystem
2. WHEN data repository associations exist for an Orphaned_Filesystem, THE Cleanup_Lambda SHALL delete each data repository association before attempting to delete the filesystem
3. WHEN a data repository association deletion fails, THE Cleanup_Lambda SHALL log the failure with the association ID and filesystem ID and skip deletion of that filesystem

### Requirement 4: Orphaned Filesystem Deletion

**User Story:** As a platform administrator, I want orphaned FSx filesystems to be automatically deleted, so that the platform does not accumulate unnecessary costs.

#### Acceptance Criteria

1. WHEN all data repository associations for an Orphaned_Filesystem have been successfully deleted, THE Cleanup_Lambda SHALL delete the FSx filesystem
2. WHEN an FSx filesystem deletion fails, THE Cleanup_Lambda SHALL log the error with the filesystem ID, project ID, and cluster name, and continue processing remaining orphaned filesystems
3. WHEN an FSx filesystem is successfully deleted, THE Cleanup_Lambda SHALL log the deletion with the filesystem ID, project ID, and cluster name

### Requirement 5: Cleanup Action Logging

**User Story:** As a platform administrator, I want detailed logs of all cleanup actions, so that I can audit what was deleted and troubleshoot any issues.

#### Acceptance Criteria

1. WHEN the Cleanup_Lambda starts execution, THE Cleanup_Lambda SHALL log the start time and the total number of FSx for Lustre filesystems discovered
2. WHEN the Cleanup_Lambda classifies a filesystem as orphaned, THE Cleanup_Lambda SHALL log the filesystem ID, the associated project ID, the cluster name, and the reason for classification (cluster not found or terminal status)
3. WHEN the Cleanup_Lambda completes execution, THE Cleanup_Lambda SHALL log a summary including the total filesystems scanned, the number classified as orphaned, the number successfully deleted, and the number that failed deletion

### Requirement 6: Administrator Notification

**User Story:** As a platform administrator, I want to receive notifications when orphaned resources are found and cleaned up, so that I am aware of cleanup activity and potential issues in the cluster creation workflow.

#### Acceptance Criteria

1. WHEN the Cleanup_Lambda deletes one or more Orphaned_Filesystems, THE Cleanup_Lambda SHALL publish a notification to the Notification_Topic
2. THE notification message SHALL include the count of deleted filesystems, and for each deleted filesystem: the filesystem ID, the project ID, and the cluster name
3. WHEN the Cleanup_Lambda finds no orphaned filesystems, THE Cleanup_Lambda SHALL NOT publish a notification
4. WHEN the Cleanup_Lambda encounters errors during cleanup, THE notification message SHALL include a summary of errors alongside successful deletions

### Requirement 7: Least-Privilege Permissions

**User Story:** As a security engineer, I want the cleanup Lambda to operate with minimal permissions, so that the blast radius of any compromise is limited.

#### Acceptance Criteria

1. THE Cleanup_Lambda SHALL have read-only access to the Clusters_Table (GetItem operation only)
2. THE Cleanup_Lambda SHALL have permission to describe FSx filesystems and their data repository associations
3. THE Cleanup_Lambda SHALL have permission to delete FSx data repository associations and filesystems
4. THE Cleanup_Lambda SHALL have permission to publish messages to the Notification_Topic
5. THE Cleanup_Lambda SHALL NOT have write access to the Clusters_Table

### Requirement 8: Error Resilience

**User Story:** As a platform administrator, I want the cleanup process to be resilient to individual failures, so that one problematic filesystem does not prevent cleanup of other orphaned resources.

#### Acceptance Criteria

1. IF a single filesystem lookup or deletion fails, THEN THE Cleanup_Lambda SHALL log the error and continue processing the remaining filesystems
2. IF the Clusters_Table is unreachable, THEN THE Cleanup_Lambda SHALL log the error and terminate execution without deleting any filesystems
3. IF the FSx API is unreachable during the initial filesystem scan, THEN THE Cleanup_Lambda SHALL log the error and terminate execution
