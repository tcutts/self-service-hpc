# Implementation Plan: Cluster Connection UI

## Overview

This plan implements end-to-end cluster connection information: a new Step Functions step to resolve the login node's IP and instance ID from PCS/EC2, storage of the instance ID in DynamoDB, an extended API response with SSH/DCV/SSM connection strings, a frontend update with copy-to-clipboard, lifecycle notification updates, and documentation. Tasks are ordered so each builds on the previous, with tests close to the code they validate.

## Tasks

- [x] 1. Implement `resolve_login_node_details` step in cluster creation workflow
  - [x] 1.1 Add `resolve_login_node_details` function to `lambda/cluster_operations/cluster_creation.py`
    - Call `pcs_client.list_compute_node_group_instances(clusterIdentifier=pcsClusterId, computeNodeGroupIdentifier=loginNodeGroupId)` to get the login node EC2 instance ID
    - Call `ec2_client.describe_instances(InstanceIds=[instanceId])` to get the public IP address
    - Return `loginNodeInstanceId` and `loginNodeIp` merged into the event payload
    - Raise `InternalError` if instance list is empty, EC2 call fails, or no public IP
    - Call `_update_step_progress` at the start of the function
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 1.2 Register `resolve_login_node_details` in `_STEP_DISPATCH` at the bottom of `cluster_creation.py`
    - _Requirements: 1.1_
  - [x] 1.3 Write unit tests for `resolve_login_node_details`
    - Create `tests/unit/test_resolve_login_node_details.py`
    - Test: PCS returns one instance, EC2 returns public IP → verify `loginNodeInstanceId` and `loginNodeIp` in output
    - Test: PCS returns empty instance list → verify `InternalError` raised
    - Test: EC2 `describe_instances` raises `ClientError` → verify `InternalError` raised
    - Test: EC2 instance has no `PublicIpAddress` → verify `InternalError` raised
    - Mock `pcs_client` and `ec2_client` using `unittest.mock.patch`
    - _Requirements: 1.1, 1.2, 1.4, 1.5_

- [x] 2. Modify `record_cluster` to store `loginNodeInstanceId` and update notification
  - [x] 2.1 Update `record_cluster` in `lambda/cluster_operations/cluster_creation.py`
    - Add `"loginNodeInstanceId": event.get("loginNodeInstanceId", "")` to the `cluster_record` dict
    - Update the notification `connection_details` string to include `SSM: aws ssm start-session --target {instance_id}` when `loginNodeInstanceId` is non-empty
    - _Requirements: 2.1, 2.2, 2.3, 6.1, 6.2_
  - [x] 2.2 Write unit tests for modified `record_cluster`
    - Create `tests/unit/test_record_cluster_connection.py`
    - Test: event with `loginNodeInstanceId` → verify DynamoDB `put_item` includes the field
    - Test: event with empty `loginNodeInstanceId` → verify empty string stored
    - Test: event with non-empty `loginNodeInstanceId` and `loginNodeIp` → verify notification message contains SSH, DCV, and SSM commands
    - Test: event with empty `loginNodeInstanceId` → verify notification message omits SSM command
    - Mock `dynamodb`, `sns_client`, and `_lookup_user_email`
    - _Requirements: 2.1, 2.2, 2.3, 6.1, 6.2_

- [x] 3. Extend `_handle_get_cluster` API handler with SSM connection info
  - [x] 3.1 Update `_handle_get_cluster` in `lambda/cluster_operations/handler.py`
    - Read `instance_id = cluster.get("loginNodeInstanceId", "")` from the cluster record
    - Add `"ssm": f"aws ssm start-session --target {instance_id}" if instance_id else ""` to the `connectionInfo` dict
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [x] 3.2 Write unit tests for modified `_handle_get_cluster`
    - Create `tests/unit/test_get_cluster_connection_info.py`
    - Test: ACTIVE cluster with IP and instance ID → verify all three `connectionInfo` fields populated
    - Test: ACTIVE cluster with empty IP and empty instance ID → verify all fields are empty strings
    - Test: ACTIVE cluster with IP but no instance ID → verify `ssh` and `dcv` populated, `ssm` empty
    - Test: Non-ACTIVE cluster → verify `connectionInfo` not in response
    - Mock `get_cluster`, `check_budget_breach`, and `is_project_user`
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 4. Checkpoint — Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Write property-based tests for connectionInfo formatting and notification content
  - [x] 5.1 Write property test for connectionInfo field formatting
    - **Property 1: Connection info fields are correctly formatted for any valid inputs**
    - Create `tests/test_connection_info_properties.py`
    - Use Hypothesis to generate random IPv4 addresses, instance IDs matching `i-[a-f0-9]{17}`, SSH ports (1–65535), DCV ports (1–65535), and empty-string variants
    - Extract the `connectionInfo` construction logic into a testable helper function (e.g. `build_connection_info(login_ip, instance_id, ssh_port, dcv_port)`) in `handler.py` or a shared module
    - Verify `ssh` field equals `ssh -p {port} <username>@{ip}` when IP is non-empty, empty otherwise
    - Verify `dcv` field equals `https://{ip}:{port}` when IP is non-empty, empty otherwise
    - Verify `ssm` field equals `aws ssm start-session --target {id}` when instance ID is non-empty, empty otherwise
    - Keep examples low (max 50) per steering rules
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
  - [x] 5.2 Write property test for lifecycle notification content
    - **Property 2: Lifecycle notification contains all applicable connection strings**
    - Create `tests/test_notification_properties.py`
    - Use Hypothesis to generate random connection details (IP, instance ID, ports) including empty-string cases
    - Extract the notification message construction logic into a testable helper function (e.g. `build_notification_message(...)`) in `cluster_creation.py`
    - Verify message contains SSH command when IP is non-empty, omits it when empty
    - Verify message contains DCV URL when IP is non-empty, omits it when empty
    - Verify message contains SSM command when instance ID is non-empty, omits it when empty
    - Keep examples low (max 50) per steering rules
    - **Validates: Requirements 6.1, 6.2**

- [x] 6. Update CDK state machine to insert `ResolveLoginNodeDetails` step
  - [x] 6.1 Add `ResolveLoginNodeDetails` LambdaInvoke task in `lib/constructs/cluster-operations.ts`
    - Create a new `tasks.LambdaInvoke` task named `ResolveLoginNodeDetails` using `clusterCreationStepLambda` with step `resolve_login_node_details`
    - Add `.addCatch(failureChain, catchConfig)` for error handling
    - _Requirements: 1.1_
  - [x] 6.2 Insert `ResolveLoginNodeDetails` into the state machine chain
    - Modify the `areNodeGroupsActive` choice: when `nodeGroupsActive` is true, route to `ResolveLoginNodeDetails` instead of `createPcsQueue`
    - Chain `ResolveLoginNodeDetails` → `createPcsQueue`
    - _Requirements: 1.1, 1.3_
  - [x] 6.3 Add `pcs:ListComputeNodeGroupInstances` and `ec2:DescribeInstances` to the creation step Lambda's IAM policy
    - Add `pcs:ListComputeNodeGroupInstances` to the existing PCS policy statement on `clusterCreationStepLambda`
    - Add `ec2:DescribeInstances` to the existing EC2 policy statement on `clusterCreationStepLambda`
    - _Requirements: 1.1, 1.2_

- [x] 7. Update frontend connection details section
  - [x] 7.1 Update `renderClusterDetailPage` connection info block in `frontend/js/app.js`
    - Replace the existing `connection-info` rendering block with a new implementation
    - Render three connection methods: SSH (code block + copy button), DCV (clickable link), SSM (code block + copy button)
    - Show fallback message "Connection details are not yet available" when `connectionInfo` is empty or all fields are empty strings
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - [x] 7.2 Implement copy-to-clipboard functionality in `frontend/js/app.js`
    - Add a `copyToClipboard(text, buttonElement)` function
    - Use `navigator.clipboard.writeText(text)` with a fallback to selecting the text in the adjacent code element
    - Show a brief "Copied!" toast or inline indicator on success
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [x] 7.3 Add CSS styles for connection methods and copy buttons in `frontend/css/styles.css`
    - Add `.connection-method` styles for individual connection method rows
    - Add `.copy-btn` styles for copy-to-clipboard buttons
    - Add `.copy-toast` styles for the brief confirmation indicator
    - _Requirements: 4.1, 5.4_

- [x] 8. Update user documentation
  - [x] 8.1 Update `docs/user/accessing-clusters.md`
    - Add a "Connecting via SSM Session Manager" section with prerequisites (AWS CLI v2, Session Manager plugin) and the CLI command
    - Update the example API response JSON to include the `ssm` field in `connectionInfo`
    - Ensure all three connection methods (SSH, DCV, SSM) are described
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The `resolve_login_node_details` step reuses the existing `hpc-cluster-creation-steps` Lambda via the step dispatcher pattern — no new Lambda function is needed
- Frontend changes use vanilla JS/HTML/CSS consistent with the existing codebase
