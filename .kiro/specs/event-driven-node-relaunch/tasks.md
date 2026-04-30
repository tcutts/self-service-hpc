# Implementation Plan: Event-Driven Node Relaunch

## Overview

Replace the 5-minute polling mechanism for login node replacement detection with an event-driven approach using EventBridge. A new Lambda processes EC2 state-change events to update DynamoDB immediately, the existing polling Lambda becomes a 60-minute fallback, and node diagnostic log shipping is added via CloudWatch Agent configuration.

Implementation uses Python for Lambda functions and tests (pytest + Hypothesis), and TypeScript for CDK infrastructure and tests (Jest).

## Tasks

- [x] 1. Create the Login Node Event Handler Lambda
  - [x] 1.1 Create `lambda/cluster_operations/login_node_event.py` with the event handler
    - Implement `handler(event, context)` that processes EC2 Instance State-change Notification events
    - Implement `_get_instance_node_group_tag(instance_id)` to query EC2 DescribeTags for the `aws:pcs:compute-node-group-id` tag
    - Implement `_find_clusters_by_login_node_group(node_group_id)` to scan DynamoDB for ACTIVE clusters matching `loginNodeGroupId`
    - Implement `_resolve_instance_details(instance_id)` to call EC2 DescribeInstances for the public IP
    - Implement `_update_cluster_login_node(project_id, cluster_name, instance_id, public_ip)` to update DynamoDB
    - Handler must filter out instances without PCS tags (DEBUG log), instances matching only `computeNodeGroupId` (DEBUG log), and instances with no matching ACTIVE cluster (DEBUG log)
    - Handler must update all matching cluster records when multiple clusters share a `loginNodeGroupId`
    - Handler must log INFO with instance ID, state, cluster name, project ID, old/new instance IDs, old/new IPs on successful update
    - Handler must never raise unhandled exceptions — always return a response dict
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 5.1, 5.2, 5.3, 5.4, 7.1, 7.2, 7.3, 7.4_

  - [x] 1.2 Write unit tests for the Login Node Event Handler
    - Create `tests/unit/test_login_node_event.py`
    - Test: instance with no PCS tag → skipped, no DynamoDB call
    - Test: valid PCS tag but no matching ACTIVE cluster → skipped
    - Test: PCS tag matches only `computeNodeGroupId` → skipped
    - Test: valid match → DynamoDB updated with new instance ID and IP
    - Test: instance ID and IP unchanged → no update performed
    - Test: EC2 DescribeTags failure → error logged, no DynamoDB update
    - Test: EC2 DescribeInstances failure → error logged, no DynamoDB update
    - Test: DynamoDB update failure → error logged, failure response returned
    - Test: multiple clusters matching same loginNodeGroupId → all updated
    - Follow mocking patterns from `tests/unit/test_login_node_refresh.py`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 5.1, 5.2, 5.3, 5.4_

- [x] 2. Checkpoint — Ensure all unit tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Write property-based tests for the Login Node Event Handler
  - [x] 3.1 Write property test for login-only filtering
    - **Property 1: Login-only filtering**
    - Generate random cluster records with distinct `loginNodeGroupId` and `computeNodeGroupId`. Generate a random tag value. Mock DynamoDB scan to return clusters. Verify: update occurs iff tag matches `loginNodeGroupId` of an ACTIVE cluster; never when tag matches only `computeNodeGroupId`.
    - Create `tests/test_login_node_event_properties.py`
    - Use `st.text()` for IDs, `st.lists()` for cluster records
    - Minimum 100 iterations
    - **Validates: Requirements 2.2, 2.3, 5.1**

  - [x] 3.2 Write property test for update correctness
    - **Property 2: Update correctness**
    - Generate random instance IDs, IPs, and matching cluster records where values differ. Mock EC2 and DynamoDB. Verify: DynamoDB update is called with the new instance ID and IP from the mocked EC2 response.
    - Use `st.from_regex()` for instance IDs (`i-[a-f0-9]{17}`), `st.ip_addresses()` for IPs
    - Minimum 100 iterations
    - **Validates: Requirements 2.4, 2.5**

  - [x] 3.3 Write property test for multi-cluster update
    - **Property 3: Multi-cluster update**
    - Generate 2–5 cluster records sharing the same `loginNodeGroupId`. Mock DynamoDB scan to return all. Verify: DynamoDB update is called once per matching cluster.
    - Use `st.integers(min_value=2, max_value=5)` for cluster count
    - Minimum 100 iterations
    - **Validates: Requirements 5.4**

  - [x] 3.4 Write property test for successful update logging completeness
    - **Property 4: Successful update logging completeness**
    - Generate random event data leading to a successful update. Capture log output. Verify: INFO log contains all required fields (instance_id, state, cluster_name, project_id, old/new instance IDs, old/new IPs).
    - Composite strategy combining instance, cluster, and IP generators
    - Minimum 100 iterations
    - **Validates: Requirements 2.10, 7.1, 7.2**

  - [x] 3.5 Write property test for skip reason logging
    - **Property 5: Skip reason logging**
    - Generate random events for each skip scenario (no tag, no cluster match, compute-only match). Capture log output. Verify: DEBUG log contains a reason string.
    - Use `st.sampled_from()` for skip scenarios
    - Minimum 100 iterations
    - **Validates: Requirements 7.3**

- [x] 4. Extend CloudWatch Agent commands for node diagnostics
  - [x] 4.1 Update `generate_cloudwatch_agent_commands()` in `posix_provisioning.py`
    - Add a second CloudWatch Agent config file `hpc-node-diagnostics.json` that collects `/var/log/messages` (stream: `{instance_id}/syslog`), `/var/log/cloud-init-output.log` (stream: `{instance_id}/cloud-init-output`), and `/var/log/amazon/pcs/bootstrap.log` (stream: `{instance_id}/pcs-bootstrap`)
    - Target log group: `/hpc-platform/clusters/{project_id}/node-diagnostics`
    - Use `append-config` mode so both configs coexist without overwriting
    - Preserve the existing access log configuration unchanged (backward compatibility)
    - _Requirements: 8.1, 8.2, 8.5, 8.6, 8.7_

  - [x] 4.2 Write unit tests for the updated CloudWatch Agent commands
    - Add tests to `tests/unit/` or extend existing `test/lambda/test_unit_posix_provisioning.py`
    - Test: existing access log config is preserved (backward compatibility)
    - Test: node diagnostics config file is written with correct paths and log group
    - Test: `append-config` mode is used for the diagnostics config
    - _Requirements: 8.1, 8.2, 8.5, 8.6_

  - [x] 4.3 Write property test for CloudWatch Agent diagnostics configuration
    - **Property 6: CloudWatch Agent diagnostics configuration**
    - Generate random project IDs. Call `generate_cloudwatch_agent_commands`. Verify: output contains `/var/log/messages`, `/var/log/cloud-init-output.log`, `/var/log/amazon/pcs/bootstrap.log`, correct log group name, correct stream name patterns, and `append-config`.
    - Add to `tests/test_login_node_event_properties.py` or create a separate file
    - Use `st.from_regex()` for project IDs
    - Minimum 100 iterations
    - **Validates: Requirements 8.1, 8.2, 8.5, 8.6**

- [x] 5. Checkpoint — Ensure all Python tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Update CDK infrastructure
  - [x] 6.1 Add the Login Node Event Handler Lambda and EventBridge rule to `lib/constructs/cluster-operations.ts`
    - Define new Lambda function `hpc-login-node-event-handler` with Python 3.13 runtime, `login_node_event.handler` handler, 30s timeout, 256 MB memory, shared layer
    - Set `CLUSTERS_TABLE_NAME` environment variable
    - Grant DynamoDB read/write on Clusters table
    - Grant `ec2:DescribeInstances` and `ec2:DescribeTags` IAM permissions
    - Create EventBridge rule matching `aws.ec2` source, `EC2 Instance State-change Notification` detail-type, state `running`
    - Add the Lambda as the EventBridge rule target
    - Grant EventBridge permission to invoke the Lambda
    - Expose the Lambda as a public readonly property `loginNodeEventLambda`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 3.1, 3.2, 3.3, 3.4, 3.5, 6.1, 6.2, 6.3, 6.4_

  - [x] 6.2 Update the Login Node Refresh schedule rule in `lib/constructs/cluster-operations.ts`
    - Change `rate(5 minutes)` to `rate(60 minutes)`
    - Update the rule description to reflect its fallback safety net role
    - _Requirements: 4.1, 6.5, 6.6_

  - [x] 6.3 Add the Node Diagnostics Log Group to `lib/project-infrastructure-stack.ts`
    - Create new `logs.LogGroup` with name `/hpc-platform/clusters/${props.projectId}/node-diagnostics`
    - Set retention to `logs.RetentionDays.ONE_DAY`
    - Set removal policy to `cdk.RemovalPolicy.DESTROY`
    - Expose as a public readonly property
    - _Requirements: 8.3, 8.4_

  - [x] 6.4 Write CDK assertion tests for the new infrastructure
    - Add tests to `test/constructs/cluster-operations.test.ts`
    - Test: EventBridge rule exists with correct event pattern (`aws.ec2`, `EC2 Instance State-change Notification`, state `running`)
    - Test: EventBridge rule targets the Login Node Event Handler Lambda
    - Test: Login Node Event Handler Lambda has correct runtime (Python 3.13), timeout (30s), memory (256 MB)
    - Test: Login Node Event Handler Lambda has `CLUSTERS_TABLE_NAME` environment variable
    - Test: Login Node Event Handler Lambda IAM policy includes `ec2:DescribeInstances` and `ec2:DescribeTags`
    - Test: Login Node Refresh schedule rule uses `rate(60 minutes)`
    - Test: Lambda function count is updated to reflect the new function
    - Add tests to `test/project-infrastructure-stack.test.ts`
    - Test: Node Diagnostics Log Group exists with correct name pattern and 1-day retention
    - Test: Node Diagnostics Log Group has DESTROY removal policy
    - _Requirements: 1.1, 1.2, 1.4, 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 6.1, 6.2, 6.3, 6.4, 6.5, 8.3, 8.4_

- [x] 7. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (Properties 1–6)
- Unit tests validate specific examples and edge cases
- CDK assertion tests validate infrastructure correctness
- Python tests use `.venv/bin/pytest`; CDK tests use Jest (`npx jest`)
