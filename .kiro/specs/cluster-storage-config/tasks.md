# Implementation Plan: Cluster Storage Configuration

## Overview

This plan implements configurable storage modes (FSx for Lustre vs Mountpoint for S3) and compute node scaling overrides for the cluster creation workflow. Changes span the API handler, Step Functions workflow, destruction workflow, frontend form, CDK infrastructure, and documentation.

## Tasks

- [x] 1. Extend the cluster creation handler with storage and scaling validation
  - [x] 1.1 Add `storageMode`, `lustreCapacityGiB`, `minNodes`, and `maxNodes` validation to `_handle_create_cluster` in `lambda/cluster_operations/handler.py`
    - Validate `storageMode` accepts only `"lustre"` or `"mountpoint"`, defaulting to `"mountpoint"` when omitted
    - Validate `lustreCapacityGiB` is >= 1200 and a multiple of 1200 when `storageMode` is `"lustre"`; default to 1200 when omitted
    - Validate `minNodes` >= 0, `maxNodes` >= 1, and `minNodes` <= `maxNodes` when provided
    - Ignore `lustreCapacityGiB` when `storageMode` is `"mountpoint"`
    - Include all new fields in the Step Functions execution payload
    - Persist `storageMode` in the initial CREATING DynamoDB record
    - _Requirements: 1.5, 1.6, 1.7, 2.2, 2.3, 2.4, 2.5, 2.7, 5.2, 5.3, 5.4, 5.5, 9.1_

  - [x] 1.2 Extend `_handle_recreate_cluster` to accept storage and scaling overrides
    - Accept optional `storageMode`, `lustreCapacityGiB`, `minNodes`, `maxNodes` in recreation request body
    - Fall back to the destroyed cluster record's `storageMode` when omitted
    - Apply the same validation rules as cluster creation
    - Include new fields in the Step Functions execution payload
    - _Requirements: 8.1, 8.2, 8.3_

  - [x] 1.3 Include `storageMode` and `lustreCapacityGiB` in the GET cluster detail response
    - Return `storageMode` for all clusters
    - Return `lustreCapacityGiB` only when `storageMode` is `"lustre"`
    - Return effective `minNodes` and `maxNodes` in the response
    - _Requirements: 6.2, 6.3_

  - [x] 1.4 Write unit tests for handler storage and scaling validation
    - Test valid/invalid `storageMode` values, default behaviour
    - Test valid/invalid `lustreCapacityGiB` values, default and ignore behaviour
    - Test valid/invalid `minNodes`/`maxNodes` combinations
    - Test recreation handler fallback logic
    - Test Step Functions payload includes all new fields
    - _Requirements: 1.5, 1.6, 1.7, 2.2, 2.3, 2.4, 2.5, 2.7, 5.2, 5.3, 5.4, 5.5, 8.1, 8.2, 8.3, 9.1_

- [x] 2. Modify the cluster creation workflow steps
  - [x] 2.1 Update `resolve_template` in `lambda/cluster_operations/cluster_creation.py` to preserve user overrides
    - Only set `minNodes`/`maxNodes` from template when not already provided (None) in the event
    - Preserve `storageMode` and `lustreCapacityGiB` fields from the input payload unchanged
    - _Requirements: 5.6, 5.7, 9.2, 9.3_

  - [x] 2.2 Update `create_fsx_filesystem` to use configurable capacity
    - Use `event.get("lustreCapacityGiB", 1200)` instead of hardcoded `1200` for `StorageCapacity`
    - _Requirements: 2.6_

  - [x] 2.3 Implement `configure_mountpoint_s3_iam` function in `cluster_creation.py`
    - Attach an inline IAM policy named `MountpointS3Access` to both login and compute roles
    - Policy grants `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation`
    - Scope policy to the specific project S3 bucket ARN and its objects (no wildcards)
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 2.4 Add `generate_mountpoint_s3_commands` and `generate_fsx_lustre_mount_commands` to `lambda/cluster_operations/posix_provisioning.py`
    - `generate_mountpoint_s3_commands(s3_bucket_name, mount_path="/data")` installs and mounts S3 via Mountpoint
    - `generate_fsx_lustre_mount_commands(fsx_dns_name, fsx_mount_name, mount_path="/data")` mounts FSx for Lustre
    - Both mount at `/data` path
    - _Requirements: 3.4, 3.5_

  - [x] 2.5 Update `record_cluster` step to persist storage configuration fields
    - Persist `storageMode`, `lustreCapacityGiB` (when lustre), effective `minNodes`, and `maxNodes` in the Clusters DynamoDB record
    - _Requirements: 6.1, 6.4_

  - [x] 2.6 Write unit tests for workflow step modifications
    - Test `resolve_template` preserves user-provided `minNodes`/`maxNodes` and falls back to template values
    - Test `resolve_template` preserves `storageMode` and `lustreCapacityGiB`
    - Test `create_fsx_filesystem` uses `lustreCapacityGiB` from event
    - Test `configure_mountpoint_s3_iam` attaches correct policy to both roles
    - Test `record_cluster` persists all new fields
    - _Requirements: 2.6, 4.1, 4.3, 5.6, 5.7, 6.1, 6.4, 9.2, 9.3_

  - [x] 2.7 Write unit tests for mount command generation
    - Test `generate_mountpoint_s3_commands` produces correct mount-s3 command with bucket name
    - Test `generate_fsx_lustre_mount_commands` produces correct lustre mount command
    - Test both use `/data` as default mount path
    - _Requirements: 3.4, 3.5_

  - [x] 2.8 Write property tests for storage configuration validation
    - **Property 1: Invalid storageMode rejected** — any string not in {"lustre", "mountpoint"} is rejected
    - **Validates: Requirements 1.6**
    - **Property 2: Invalid lustreCapacityGiB rejected** — values < 1200 or not multiples of 1200 are rejected
    - **Validates: Requirements 2.3, 2.4**
    - **Property 3: Valid capacity flows to FSx** — multiples of 1200 >= 1200 are accepted and used as StorageCapacity
    - **Validates: Requirements 2.6**
    - _Requirements: 1.6, 2.3, 2.4, 2.6_

  - [x] 2.9 Write property tests for mount commands and IAM policy
    - **Property 4: Mountpoint S3 commands correct** — generated commands contain the bucket name and mount path
    - **Validates: Requirements 3.4**
    - **Property 5: FSx mount commands correct** — generated commands contain DNS name and mount name
    - **Validates: Requirements 3.5**
    - **Property 6: S3 IAM policy scoped correctly** — policy resource ARNs contain only the specific bucket name
    - **Validates: Requirements 4.1, 4.3**
    - _Requirements: 3.4, 3.5, 4.1, 4.3_

  - [x] 2.10 Write property tests for node scaling and template resolution
    - **Property 7: Invalid node scaling rejected** — minNodes > maxNodes or out-of-range values are rejected
    - **Validates: Requirements 5.3, 5.4, 5.5**
    - **Property 8: resolve_template preserves overrides** — user-provided minNodes/maxNodes/storageMode are never overwritten
    - **Validates: Requirements 5.6, 5.7, 9.2, 9.3**
    - **Property 9: Cluster record round-trip** — storageMode and capacity are persisted and retrievable
    - **Validates: Requirements 6.1, 6.2**
    - _Requirements: 5.3, 5.4, 5.5, 5.6, 5.7, 6.1, 6.2, 9.2, 9.3_

- [x] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Modify the cluster destruction workflow
  - [x] 4.1 Add `remove_mountpoint_s3_policy` function to `lambda/cluster_operations/cluster_destruction.py`
    - Remove the `MountpointS3Access` inline policy from both login and compute roles
    - Handle `NoSuchEntity` gracefully (policy may not exist for lustre clusters)
    - Register the new step in `_STEP_DISPATCH`
    - _Requirements: 7.3_

  - [x] 4.2 Update destruction workflow to conditionally skip FSx steps for mountpoint clusters
    - When `storageMode` is `"mountpoint"`, the existing `create_fsx_export_task` already skips (empty `fsxFilesystemId`)
    - Ensure `delete_fsx_filesystem` also skips when `fsxFilesystemId` is empty
    - Pass `storageMode` through the destruction payload from the handler
    - _Requirements: 7.1, 7.2_

  - [x] 4.3 Update `_handle_delete_cluster` in `handler.py` to include `storageMode` in the destruction payload
    - Read `storageMode` from the cluster record and include it in the Step Functions execution input
    - _Requirements: 7.1, 7.2, 7.3_

  - [x] 4.4 Write unit tests for destruction workflow changes
    - Test `remove_mountpoint_s3_policy` removes policy from both roles
    - Test `remove_mountpoint_s3_policy` handles `NoSuchEntity` gracefully
    - Test FSx export and deletion are skipped when `fsxFilesystemId` is empty
    - Test destruction payload includes `storageMode`
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 5. Update the CDK Step Functions state machine definition
  - [x] 5.1 Add a `StorageModeChoice` Choice state to the creation state machine in `lib/constructs/cluster-operations.ts`
    - After instance profile wait loop, branch on `$.storageMode`
    - When `"lustre"`: execute existing parallel FSx + PCS branch
    - When `"mountpoint"`: execute PCS-only branch (skip FSx creation, polling, and DRA)
    - Add a `ConfigureMountpointS3Iam` Lambda invoke step in the mountpoint branch
    - Both branches converge at the login node group creation step
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 5.2 Add a `RemoveMountpointS3Policy` step to the destruction state machine
    - Add a Choice state that calls `remove_mountpoint_s3_policy` when `storageMode` is `"mountpoint"`
    - Place it before the IAM resource deletion step
    - _Requirements: 7.3_

  - [x] 5.3 Update the `resultSelector` on the parallel state to include `storageMode` and `lustreCapacityGiB`
    - Ensure these fields flow through the parallel state to downstream steps
    - _Requirements: 9.1, 9.2, 9.3_

  - [x] 5.4 Update CDK snapshot tests for the modified state machines
    - Verify the creation state machine includes the `StorageModeChoice` Choice state
    - Verify the destruction state machine includes the `RemoveMountpointS3Policy` step
    - Verify IAM permissions include S3 actions for Mountpoint policy attachment
    - _Requirements: 3.1, 3.2, 7.3_

- [x] 6. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Update the frontend cluster creation form
  - [x] 7.1 Add storage mode radio group, lustre capacity input, and node scaling inputs to the cluster creation form in `frontend/js/app.js`
    - Add a radio group with options "Mountpoint for Amazon S3" (value `mountpoint`, default selected) and "FSx for Lustre" (value `lustre`)
    - Add a number input for Lustre capacity (step=1200, min=1200, default=1200), visible only when `storageMode === "lustre"`
    - Add `minNodes` and `maxNodes` number inputs, pre-populated from template defaults when a template is selected
    - Include all new fields in the POST request body
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 5.1_

  - [x] 7.2 Write frontend tests for the new form controls
    - Verify storage mode radio group renders with `mountpoint` as default
    - Verify lustre capacity field visibility toggles with storage mode selection
    - Verify minNodes/maxNodes fields populate from template selection
    - Verify submit payload includes new fields correctly
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 5.1_

- [x] 8. Update cluster management documentation
  - [x] 8.1 Update `docs/project-admin/cluster-management.md` with storage and scaling configuration
    - Describe the `storageMode` field, valid values (`lustre`, `mountpoint`), and default (`mountpoint`)
    - Describe the `lustreCapacityGiB` field, validation rules (>= 1200, multiple of 1200), and default (1200)
    - Describe the `minNodes` and `maxNodes` override fields and their relationship to template defaults
    - Update the cluster creation request example to include the new optional fields
    - Update the cluster detail response example to include `storageMode`
    - Update the "What Happens" section to describe conditional FSx/Mountpoint behaviour
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

- [x] 9. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Both the API and UI default to `mountpoint` (not `lustre`)
- The destruction workflow already handles empty `fsxFilesystemId` gracefully, so mountpoint clusters require minimal destruction changes
- Property tests use the `hypothesis` library with `@settings(max_examples=100)`
- CDK infrastructure uses TypeScript with L2 constructs; Lambda functions use Python
