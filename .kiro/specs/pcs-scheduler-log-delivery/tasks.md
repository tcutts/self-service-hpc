# Implementation Plan: PCS Scheduler Log Delivery

## Overview

Add automatic configuration of AWS PCS vended log delivery (scheduler, audit, and job completion logs) to the cluster creation workflow, and cleanup of those resources during cluster destruction. Implementation spans the creation step Lambda (Python), the destruction step Lambda (Python), and the CDK construct (TypeScript) for IAM policies and state machine wiring.

## Tasks

- [x] 1. Implement the `configure_scheduler_log_delivery` creation step
  - [x] 1.1 Add `configure_scheduler_log_delivery` function and helpers to `lambda/cluster_operations/cluster_creation.py`
    - Define the `_PCS_LOG_TYPES` configuration list with entries for `PCS_SCHEDULER_LOGS`, `PCS_SCHEDULER_AUDIT_LOGS`, and `PCS_JOBCOMP_LOGS` including suffix and service fields
    - Implement `_create_scheduler_log_group(project_id, cluster_name)` that creates the CloudWatch Log Group at `/hpc-platform/clusters/{projectId}/scheduler-logs/{clusterName}`, sets 30-day retention via `PutRetentionPolicy`, tags with `Project`, and handles `ResourceAlreadyExistsException` for idempotency
    - Implement `_configure_delivery_for_log_type(cluster_name, project_id, cluster_arn, log_group_arn, log_type, suffix)` that calls `PutDeliverySource`, `PutDeliveryDestination`, and `CreateDelivery` with correct naming patterns, handles `ConflictException` for idempotency, and logs INFO for each configured delivery
    - Implement `configure_scheduler_log_delivery(event)` that orchestrates log group creation and delivery configuration for all three log types, logs a summary, and returns the event merged with `schedulerLogGroupName` and `schedulerDeliveryIds`
    - Register `configure_scheduler_log_delivery` in the `_STEP_DISPATCH` dict
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.3, 3.4, 6.1, 6.2, 6.3, 6.4_

  - [x] 1.2 Write property test for log group creation correctness
    - **Property 1: Log group creation correctness**
    - Generate random `projectId` and `clusterName` strings using `st.from_regex(r'[a-z][a-z0-9-]{2,20}')`
    - Mock CloudWatch Logs client and call `configure_scheduler_log_delivery`
    - Verify `CreateLogGroup` called with name `/hpc-platform/clusters/{projectId}/scheduler-logs/{clusterName}`, `PutRetentionPolicy` called with 30 days, `TagLogGroup` called with `{"Project": projectId}`
    - Add to `tests/test_scheduler_log_delivery_properties.py`
    - **Validates: Requirements 1.1, 1.2, 1.4**

  - [x] 1.3 Write property test for delivery configuration completeness
    - **Property 2: Delivery configuration completeness and correctness**
    - Generate random cluster details (clusterName, projectId, pcsClusterArn)
    - Mock CloudWatch Logs client and call `configure_scheduler_log_delivery`
    - Verify `PutDeliverySource` called 3 times with correct `resourceArn`, `logType`, and source name pattern `{clusterName}-{suffix}`
    - Verify `PutDeliveryDestination` called 3 times with same `destinationResourceArn` and destination name pattern `{projectId}-{clusterName}-{suffix}`
    - Verify `CreateDelivery` called 3 times linking correct sources to destinations
    - Add to `tests/test_scheduler_log_delivery_properties.py`
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6**

  - [x] 1.4 Write property test for successful configuration logging
    - **Property 4: Successful configuration logging**
    - Generate random cluster details and call `configure_scheduler_log_delivery` with mocked CloudWatch client
    - Capture log output and verify: 3 INFO entries each containing log type, source name, and delivery ID; 1 summary INFO entry containing cluster name and count 3
    - Add to `tests/test_scheduler_log_delivery_properties.py`
    - **Validates: Requirements 6.1, 6.4**

  - [x] 1.5 Write unit tests for `configure_scheduler_log_delivery`
    - Add `tests/unit/test_configure_scheduler_log_delivery.py`
    - Test step is registered in `_STEP_DISPATCH` as `configure_scheduler_log_delivery`
    - Test missing required payload fields (`pcsClusterArn`, `projectId`, etc.) raises appropriate error
    - Test `CreateLogGroup` raises `ResourceAlreadyExistsException` → step continues without error
    - Test `PutDeliverySource` raises `ConflictException` → step continues, logs INFO
    - Test `CreateDelivery` raises unexpected `ClientError` → step logs ERROR and raises
    - Test all three log types are configured in a single invocation
    - _Requirements: 1.1, 1.3, 2.1, 2.5, 3.1, 6.2, 6.3_

- [x] 2. Checkpoint - Verify creation step
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implement the `cleanup_scheduler_log_delivery` destruction step
  - [x] 3.1 Add `cleanup_scheduler_log_delivery` function and helpers to `lambda/cluster_operations/cluster_destruction.py`
    - Implement `_delete_deliveries_by_name(source_names)` that lists deliveries matching source names and deletes them, handling `ResourceNotFoundException`
    - Implement `_delete_delivery_destinations(destination_names)` that deletes delivery destinations by name, handling `ResourceNotFoundException`
    - Implement `_delete_delivery_sources(source_names)` that deletes delivery sources by name, handling `ResourceNotFoundException`
    - Implement `_delete_scheduler_log_group(project_id, cluster_name)` that deletes the log group, handling `ResourceNotFoundException`
    - Implement `cleanup_scheduler_log_delivery(event)` that orchestrates deletion in the correct order: deliveries → destinations → sources → log group
    - Add `cleanup_scheduler_log_delivery` as the first step in the `consolidated_cleanup` steps list (before `delete_iam_resources`)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 3.2 Write property test for cleanup ordering and completeness
    - **Property 3: Cleanup ordering and completeness**
    - Generate random cluster details and mock `ListDeliveries`/`ListDeliverySources`/`ListDeliveryDestinations` to return matching resources
    - Call `cleanup_scheduler_log_delivery` and verify: all `DeleteDelivery` calls precede all `DeleteDeliveryDestination` calls, which precede all `DeleteDeliverySource` calls, which precede `DeleteLogGroup`
    - Add to `tests/test_scheduler_log_delivery_properties.py`
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6**

  - [x] 3.3 Write unit tests for `cleanup_scheduler_log_delivery`
    - Add `tests/unit/test_cleanup_scheduler_log_delivery.py`
    - Test all delivery resources exist → deleted in correct order (deliveries, destinations, sources, log group)
    - Test some delivery resources already deleted (`ResourceNotFoundException`) → step continues
    - Test log group does not exist → step continues without error
    - Test `DeleteDelivery` raises unexpected error → error propagates
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [x] 4. Checkpoint - Verify destruction step
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Add CDK infrastructure changes
  - [x] 5.1 Add IAM policies and state machine step to `lib/constructs/cluster-operations.ts`
    - Add IAM policy for the creation step Lambda granting `logs:PutDeliverySource`, `logs:PutDeliveryDestination`, `logs:CreateDelivery`, `logs:GetDelivery`, `logs:CreateLogGroup`, `logs:PutRetentionPolicy`, `logs:TagLogGroup`, `logs:DescribeLogGroups`
    - Add IAM policy for the creation step Lambda granting `pcs:AllowVendedLogDeliveryForResource`
    - Add IAM policy for the destruction step Lambda granting `logs:DeleteDelivery`, `logs:DeleteDeliverySource`, `logs:DeleteDeliveryDestination`, `logs:DeleteLogGroup`, `logs:ListDeliveries`, `logs:ListDeliverySources`, `logs:ListDeliveryDestinations`
    - Add `ConfigureSchedulerLogDelivery` Lambda invoke step to the creation state machine, inserted after the PCS cluster ACTIVE check and before node group creation
    - Add catch block on the new step routing to the existing failure handler chain
    - _Requirements: 3.2, 3.5, 4.1, 4.2, 4.3, 5.7_

  - [x] 5.2 Write CDK assertion tests for the new infrastructure
    - Add tests to `test/constructs/cluster-operations.test.ts`
    - Assert `ConfigureSchedulerLogDelivery` step exists in the creation state machine with correct step name
    - Assert the step has a catch block routing to the failure handler
    - Assert creation step Lambda IAM policy includes all required `logs:*` and `pcs:AllowVendedLogDeliveryForResource` permissions
    - Assert destruction step Lambda IAM policy includes all required `logs:*` cleanup permissions
    - _Requirements: 3.2, 3.5, 4.1, 4.2, 4.3, 5.7_

- [x] 6. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major component
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases
- The design uses Python for Lambda code and TypeScript for CDK — no language selection needed
- The creation step follows the existing `_STEP_DISPATCH` dict pattern in `cluster_creation.py`
- The cleanup step follows the existing `consolidated_cleanup` sequential-steps pattern in `cluster_destruction.py`
