# Implementation Plan: Orphaned FSx Cleanup

## Overview

Implement a scheduled Lambda function that detects and deletes orphaned FSx for Lustre filesystems. The implementation follows a pure-function core with thin I/O shell pattern: pure classification and summary logic in `cleanup.py` (property-testable), I/O functions for AWS API calls (unit-testable with moto), and a handler that orchestrates the workflow. CDK infrastructure adds the Lambda, EventBridge schedule, and least-privilege IAM permissions to the existing FoundationStack.

## Tasks

- [x] 1. Create the FSx cleanup Lambda package with pure-function core
  - [x] 1.1 Create `lambda/fsx_cleanup/__init__.py` and `lambda/fsx_cleanup/cleanup.py` with pure functions
    - Implement `filter_tagged_filesystems(filesystems)` — filters to filesystems with both `Project` and `ClusterName` tags
    - Implement `classify_filesystem(filesystem_tags, cluster_record)` — returns `(is_orphaned, reason)` tuple
    - Implement `build_cleanup_summary(total_scanned, orphaned, deleted, failed)` — returns summary dict with consistent counts
    - Implement `build_notification_message(deleted, failed)` — returns `(subject, message_body)` tuple for SNS
    - _Requirements: 2.1, 2.3, 2.4, 2.5, 5.3, 6.2, 6.4_

  - [x] 1.2 Write property test: tag filtering correctness
    - **Property 1: Tag filtering correctness**
    - **Validates: Requirements 2.1**
    - Create `test/lambda/test_property_fsx_cleanup_tag_filter.py`
    - Generate arbitrary lists of filesystem dicts with random tag combinations
    - Assert `filter_tagged_filesystems` returns exactly those with both `Project` and `ClusterName` tags

  - [x] 1.3 Write property test: orphan classification correctness
    - **Property 2: Orphan classification correctness**
    - **Validates: Requirements 2.3, 2.4, 2.5**
    - Create `test/lambda/test_property_fsx_cleanup_classification.py`
    - Generate arbitrary tag dicts and cluster record states (None, terminal, active)
    - Assert `classify_filesystem` returns `is_orphaned=True` iff cluster record is missing or terminal

  - [x] 1.4 Write property test: summary counts consistency
    - **Property 4: Summary counts consistency**
    - **Validates: Requirements 5.3**
    - Create `test/lambda/test_property_fsx_cleanup_summary.py`
    - Generate arbitrary counts for scanned, orphaned, deleted, failed
    - Assert `total_orphaned == total_deleted + total_failed` and `total_tagged >= total_orphaned` and `total_scanned >= total_tagged`

  - [x] 1.5 Write property test: notification message completeness
    - **Property 5: Notification message completeness**
    - **Validates: Requirements 6.2**
    - Create `test/lambda/test_property_fsx_cleanup_notification.py`
    - Generate arbitrary lists of deleted/failed filesystem records
    - Assert message body contains every filesystem ID, project ID, and cluster name

- [x] 2. Checkpoint — Ensure all pure-function tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implement I/O functions and Lambda handler
  - [x] 3.1 Add I/O functions to `lambda/fsx_cleanup/cleanup.py`
    - Implement `scan_fsx_filesystems()` — paginated `describe_file_systems` calls filtered to `LUSTRE` type
    - Implement `lookup_cluster_record(project_id, cluster_name)` — DynamoDB `get_item` on Clusters table
    - Implement `delete_filesystem_dras(filesystem_id)` — describe and delete all DRAs for a filesystem
    - Implement `delete_filesystem(filesystem_id)` — delete an FSx filesystem
    - Implement `publish_notification(subject, message)` — publish to SNS topic
    - _Requirements: 1.2, 2.2, 3.1, 3.2, 3.3, 4.1, 6.1_

  - [x] 3.2 Create `lambda/fsx_cleanup/handler.py` with the EventBridge handler
    - Implement `handler(event, context)` that orchestrates the full cleanup workflow
    - Call `scan_fsx_filesystems`, `filter_tagged_filesystems`, classify each filesystem, delete orphans (DRAs first, then filesystem), build summary, publish notification if deletions occurred
    - Implement fail-fast on infrastructure unavailability (FSx API or DynamoDB unreachable)
    - Implement best-effort per-filesystem processing with error isolation
    - Return cleanup result dict with counts and error details
    - _Requirements: 1.2, 2.1–2.5, 3.1–3.3, 4.1–4.3, 5.1–5.3, 6.1–6.4, 8.1–8.3_

  - [x] 3.3 Write property test: error resilience
    - **Property 3: Error resilience — individual failures do not block remaining processing**
    - **Validates: Requirements 4.2, 8.1**
    - Create `test/lambda/test_property_fsx_cleanup_resilience.py`
    - Generate sets of orphaned filesystems with some marked to fail deletion
    - Assert all filesystems are attempted and `attempted + dra_failures == total_orphaned`

  - [x] 3.4 Write unit tests for I/O functions and handler
    - Create `test/lambda/test_unit_fsx_cleanup.py`
    - Add conftest helpers: `reload_fsx_cleanup_modules()`, `fsx_cleanup_env` fixture with moto mocks for FSx, DynamoDB Clusters table, and SNS topic
    - Test `scan_fsx_filesystems` handles pagination correctly
    - Test DRA deletion before filesystem deletion ordering
    - Test DRA failure skips filesystem deletion
    - Test SNS notification sent when deletions occur
    - Test no notification when no orphans found
    - Test notification includes error details when failures occur
    - Test handler returns correct summary dict
    - Test fail-fast when DynamoDB is unreachable
    - Test fail-fast when FSx API is unreachable during initial scan
    - _Requirements: 1.2, 2.1–2.5, 3.1–3.3, 4.1–4.3, 5.1–5.3, 6.1–6.4, 8.1–8.3_

- [x] 4. Checkpoint — Ensure all Lambda tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Add CDK infrastructure for the FSx cleanup Lambda
  - [x] 5.1 Add the FSx cleanup Lambda, EventBridge rule, and IAM permissions to `lib/foundation-stack.ts`
    - Import `aws-events` and `aws-events-targets` modules
    - Create the `fsxCleanupLambda` Lambda function with Python 3.13 runtime, `handler.handler` entry point, 5-minute timeout, code from `lambda/fsx_cleanup/`, shared layer, and environment variables (`CLUSTERS_TABLE_NAME`, `SNS_TOPIC_ARN`, `AWS_REGION`)
    - Create the `fsxCleanupScheduleRule` EventBridge rule with `rate(6 hours)` schedule targeting the Lambda
    - Grant DynamoDB `GetItem` read-only access on the Clusters table (use `grantReadData` or scoped policy)
    - Grant FSx permissions: `fsx:DescribeFileSystems`, `fsx:DescribeDataRepositoryAssociations`, `fsx:DeleteDataRepositoryAssociation`, `fsx:DeleteFileSystem`
    - Grant SNS publish on the existing `clusterLifecycleNotificationTopic`
    - Ensure NO DynamoDB write permissions are granted on the Clusters table
    - _Requirements: 1.1, 7.1–7.5_

  - [x] 5.2 Write CDK infrastructure tests for the FSx cleanup resources
    - Add tests to `test/foundation-stack.test.ts`
    - Test Lambda created with correct runtime (Python 3.13), handler (`handler.handler`), and timeout (5 min)
    - Test EventBridge rule with `rate(6 hours)` schedule expression
    - Test IAM policy grants only required FSx permissions
    - Test no DynamoDB write permissions on Clusters table
    - Test SNS publish permission granted on cluster lifecycle topic
    - Test Lambda environment variables are set correctly
    - _Requirements: 1.1, 7.1–7.5_

- [x] 6. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases using pytest + moto
- CDK infrastructure tests use the existing Jest test setup
- Python Lambda code goes in `lambda/fsx_cleanup/`, tests in `test/lambda/`
- Follow existing `conftest.py` patterns for test infrastructure (module reloading, class-scoped fixtures)
