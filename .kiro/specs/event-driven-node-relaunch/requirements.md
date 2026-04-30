# Requirements Document

## Introduction

The HPC self-service platform currently detects login/head node replacements using a polling-based Lambda function (`hpc-login-node-refresh`) that runs every 5 minutes via an EventBridge schedule rule. This Lambda scans all ACTIVE clusters in DynamoDB, queries EC2 for the current login node instance by tag (`aws:pcs:compute-node-group-id`), and updates the `loginNodeInstanceId` and `loginNodeIp` fields if they have changed. This approach has two problems: (1) there is up to a 5-minute delay before the UI reflects a replaced node's new connection details, and (2) the full-table scan on every invocation is wasteful when most clusters have not experienced a node replacement. Meanwhile, compute node restarts continue to occur because the head node relaunch is not being detected and acted upon promptly.

This feature replaces the polling mechanism with an event-driven approach. An EventBridge rule captures EC2 Instance State-change Notification events (specifically the `running` state) for instances tagged with PCS node group IDs. A new Lambda function receives these events, identifies the affected cluster, resolves the new instance's public IP, and updates DynamoDB immediately. Because AWS PCS tags both login node group instances and compute node group instances with the same `aws:pcs:compute-node-group-id` tag, the Lambda must explicitly verify that the instance's node group ID matches a cluster's `loginNodeGroupId` field and must ignore instances belonging to compute node groups. The scheduled polling Lambda is retained as a fallback safety net but its frequency is reduced from 5 minutes to 60 minutes.

## Glossary

- **Login_Node_Refresh_Lambda**: The existing Lambda function (`hpc-login-node-refresh`) that periodically scans all ACTIVE clusters and reconciles login node instance IDs and IPs in DynamoDB. Currently triggered every 5 minutes by an EventBridge schedule rule.
- **Login_Node_Event_Lambda**: The new Lambda function that receives EC2 instance state-change events via EventBridge and updates the corresponding cluster record in DynamoDB with the new login node instance ID and public IP.
- **Clusters_Table**: The DynamoDB table storing cluster records, keyed by `PK=PROJECT#{projectId}` and `SK=CLUSTER#{clusterName}`. Contains `loginNodeInstanceId`, `loginNodeIp`, `loginNodeGroupId`, and `status` fields among others.
- **EC2_State_Change_Event**: An EventBridge event with source `aws.ec2` and detail-type `EC2 Instance State-change Notification`, emitted automatically by AWS when an EC2 instance transitions between states (pending, running, stopping, stopped, shutting-down, terminated).
- **EventBridge_Rule**: An Amazon EventBridge rule that matches EC2 instance state-change events for instances entering the `running` state and routes them to the Login_Node_Event_Lambda.
- **PCS_Node_Group_Tag**: The EC2 instance tag `aws:pcs:compute-node-group-id` applied by AWS PCS to every instance in a node group. This tag is applied to **both** login node group instances and compute node group instances — the tag name is the same regardless of node group type. Used to correlate an EC2 instance with a specific node group.
- **Login_Node_Group**: A PCS node group that serves as the cluster's head/login node. Each cluster has exactly one login node group. The cluster record stores its ID in the `loginNodeGroupId` field. Login node instances are tagged with `aws:pcs:compute-node-group-id` set to this ID.
- **Compute_Node_Group**: A PCS node group that provides compute capacity for job execution. Each cluster has one or more compute node groups. The cluster record stores the primary compute node group ID in the `computeNodeGroupId` field. Compute node instances are tagged with `aws:pcs:compute-node-group-id` set to this ID. The Login_Node_Event_Lambda must NOT process events from Compute_Node_Group instances.
- **Cluster_Operations_Construct**: The CDK construct (`lib/constructs/cluster-operations.ts`) that defines cluster-related Lambda functions, Step Functions state machines, and EventBridge rules.
- **Node_Diagnostics_Log_Group**: A CloudWatch Log Group per project that receives syslog and PCS bootstrap logs from cluster node instances. Named `/hpc-platform/clusters/{projectId}/node-diagnostics` following the existing log group naming convention. Configured with a 1-day retention period.
- **CloudWatch_Agent**: The Amazon CloudWatch agent pre-installed on PCS node AMIs. Configured via JSON config files and managed with `amazon-cloudwatch-agent-ctl`. The existing `generate_cloudwatch_agent_commands()` function in `posix_provisioning.py` already configures the agent to ship access logs; the node diagnostics configuration extends this with additional log file collection.
- **ProjectInfrastructureStack**: The CDK stack (`lib/project-infrastructure-stack.ts`) that provisions per-project infrastructure including VPC, EFS, S3 bucket, security groups, and CloudWatch Log Groups.

## Requirements

### Requirement 1: EventBridge Rule for EC2 Instance State Changes

**User Story:** As a platform operator, I want EC2 instance state changes to be captured by EventBridge, so that login node replacements trigger an immediate DynamoDB update instead of waiting for the next polling cycle.

#### Acceptance Criteria

1. THE Cluster_Operations_Construct SHALL create an EventBridge_Rule that matches EC2_State_Change_Event events where the instance state is `running`.
2. WHEN an EC2 instance enters the `running` state, THE EventBridge_Rule SHALL route the event to the Login_Node_Event_Lambda as the target.
3. THE EventBridge_Rule SHALL have a descriptive name following the existing naming convention (e.g., `hpc-login-node-state-change`).
4. THE EventBridge_Rule SHALL use the event pattern source `aws.ec2` and detail-type `EC2 Instance State-change Notification` with detail state `running`.

### Requirement 2: Event-Driven Login Node Update Lambda

**User Story:** As a platform operator, I want a Lambda function that processes EC2 state-change events and updates DynamoDB with new login node details, so that the UI reflects accurate connection information within seconds of a node replacement.

#### Acceptance Criteria

1. WHEN the Login_Node_Event_Lambda receives an EC2_State_Change_Event for an instance entering the `running` state, THE Login_Node_Event_Lambda SHALL query the instance's tags to retrieve the `aws:pcs:compute-node-group-id` tag value.
2. WHEN the instance has a valid PCS_Node_Group_Tag, THE Login_Node_Event_Lambda SHALL query the Clusters_Table to find the ACTIVE cluster record whose `loginNodeGroupId` field matches the tag value.
3. WHEN the PCS_Node_Group_Tag value matches a cluster record's `computeNodeGroupId` field instead of its `loginNodeGroupId` field, THE Login_Node_Event_Lambda SHALL log the event at DEBUG level and take no further action, because the instance belongs to a Compute_Node_Group.
4. WHEN a matching cluster record is found by `loginNodeGroupId`, THE Login_Node_Event_Lambda SHALL resolve the instance's public IP address using EC2 DescribeInstances.
5. WHEN the instance ID or public IP differs from the values stored in the Clusters_Table, THE Login_Node_Event_Lambda SHALL update the `loginNodeInstanceId` and `loginNodeIp` fields in the matching cluster record.
6. WHEN the instance does not have a PCS_Node_Group_Tag, THE Login_Node_Event_Lambda SHALL log the event at DEBUG level and take no further action.
7. WHEN no matching ACTIVE cluster record is found where `loginNodeGroupId` equals the PCS_Node_Group_Tag value, THE Login_Node_Event_Lambda SHALL log the event at DEBUG level and take no further action.
8. IF the EC2 DescribeInstances call fails, THEN THE Login_Node_Event_Lambda SHALL log the error at ERROR level and return without updating DynamoDB.
9. IF the DynamoDB update fails, THEN THE Login_Node_Event_Lambda SHALL log the error at ERROR level and return a failure response.
10. WHEN the Login_Node_Event_Lambda successfully updates a cluster record, THE Login_Node_Event_Lambda SHALL log the cluster name, old instance ID, new instance ID, old IP, and new IP at INFO level.

### Requirement 3: Lambda IAM Permissions and Configuration

**User Story:** As a platform engineer, I want the event-driven Lambda to have the minimum required IAM permissions, so that the function operates securely following least-privilege principles.

#### Acceptance Criteria

1. THE Login_Node_Event_Lambda SHALL have read and write access to the Clusters_Table for querying cluster records and updating login node details.
2. THE Login_Node_Event_Lambda SHALL have `ec2:DescribeInstances` and `ec2:DescribeTags` permissions to resolve instance details and tags.
3. THE Login_Node_Event_Lambda SHALL receive the `CLUSTERS_TABLE_NAME` environment variable containing the Clusters_Table name.
4. THE Login_Node_Event_Lambda SHALL use the Python 3.13 runtime and the shared Lambda layer, consistent with existing Lambda functions in the Cluster_Operations_Construct.
5. THE Login_Node_Event_Lambda SHALL have a timeout of 30 seconds and memory size of 256 MB.

### Requirement 4: Reduce Polling Frequency of Existing Refresh Lambda

**User Story:** As a platform operator, I want the existing polling Lambda to run less frequently now that event-driven updates handle the common case, so that unnecessary DynamoDB scans and EC2 API calls are reduced.

#### Acceptance Criteria

1. THE Login_Node_Refresh_Lambda schedule rule SHALL be updated from a 5-minute rate to a 60-minute rate.
2. THE Login_Node_Refresh_Lambda SHALL continue to scan all ACTIVE clusters and update login node details when changes are detected, serving as a fallback safety net.
3. THE Login_Node_Refresh_Lambda handler logic, DynamoDB update format, and EC2 query logic SHALL remain unchanged.

### Requirement 5: Cluster Record Lookup by Login Node Group ID

**User Story:** As a developer, I want an efficient way to find a cluster record by its `loginNodeGroupId`, so that the event-driven Lambda can quickly identify which cluster a replaced login node instance belongs to while ignoring compute node instances.

#### Acceptance Criteria

1. WHEN the Login_Node_Event_Lambda needs to find a cluster by node group ID, THE Login_Node_Event_Lambda SHALL scan the Clusters_Table with a filter expression matching `loginNodeGroupId` equal to the PCS_Node_Group_Tag value and `status` equal to `ACTIVE`. The filter SHALL NOT match against the `computeNodeGroupId` field.
2. WHEN the scan returns exactly one matching cluster record, THE Login_Node_Event_Lambda SHALL use that record for the update.
3. WHEN the scan returns zero matching cluster records (indicating the PCS_Node_Group_Tag value does not correspond to any active cluster's login node group), THE Login_Node_Event_Lambda SHALL log at DEBUG level and take no further action.
4. WHEN the scan returns more than one matching cluster record, THE Login_Node_Event_Lambda SHALL log a warning and update all matching records.

### Requirement 6: CDK Infrastructure Updates

**User Story:** As a platform engineer, I want the CDK construct to define the new EventBridge rule and Lambda function alongside the existing cluster operations infrastructure, so that the event-driven mechanism is deployed and managed consistently.

#### Acceptance Criteria

1. THE Cluster_Operations_Construct SHALL define the Login_Node_Event_Lambda as a new Lambda function with the function name `hpc-login-node-event-handler`.
2. THE Cluster_Operations_Construct SHALL define the EventBridge_Rule and add the Login_Node_Event_Lambda as its target.
3. THE Cluster_Operations_Construct SHALL grant the EventBridge_Rule permission to invoke the Login_Node_Event_Lambda.
4. THE Cluster_Operations_Construct SHALL expose the Login_Node_Event_Lambda as a public readonly property for testing and cross-construct references.
5. THE Cluster_Operations_Construct SHALL update the existing `LoginNodeRefreshScheduleRule` schedule from `rate(5 minutes)` to `rate(60 minutes)`.
6. THE Cluster_Operations_Construct SHALL update the description of the `LoginNodeRefreshScheduleRule` to reflect its new role as a fallback safety net.

### Requirement 7: Observability and Logging

**User Story:** As a platform operator, I want structured logging from the event-driven Lambda, so that I can monitor login node replacements and troubleshoot issues in CloudWatch.

#### Acceptance Criteria

1. WHEN the Login_Node_Event_Lambda processes an event, THE Login_Node_Event_Lambda SHALL log the EC2 instance ID and state from the event at INFO level.
2. WHEN the Login_Node_Event_Lambda updates a cluster record, THE Login_Node_Event_Lambda SHALL log the project ID, cluster name, previous instance ID, new instance ID, previous IP, and new IP at INFO level.
3. WHEN the Login_Node_Event_Lambda skips an event due to a missing PCS_Node_Group_Tag, no matching cluster by `loginNodeGroupId`, or because the instance belongs to a Compute_Node_Group, THE Login_Node_Event_Lambda SHALL log the reason at DEBUG level.
4. WHEN the Login_Node_Event_Lambda encounters an error, THE Login_Node_Event_Lambda SHALL log the error details including the EC2 instance ID and the exception message at ERROR level.

### Requirement 8: Node Diagnostic Log Shipping to CloudWatch

**User Story:** As a platform operator, I want cluster node syslog and PCS bootstrap logs shipped to CloudWatch, so that I can diagnose login node crashes and relaunch failures without needing SSH access to the instance.

#### Acceptance Criteria

1. THE `generate_cloudwatch_agent_commands` function in `posix_provisioning.py` SHALL configure the CloudWatch_Agent to collect the syslog file (`/var/log/messages`) and ship it to the Node_Diagnostics_Log_Group with a log stream name of `{instance_id}/syslog`.
2. THE `generate_cloudwatch_agent_commands` function SHALL configure the CloudWatch_Agent to collect the cloud-init output log (`/var/log/cloud-init-output.log`) and ship it to the Node_Diagnostics_Log_Group with a log stream name of `{instance_id}/cloud-init-output`.
3. THE ProjectInfrastructureStack SHALL create the Node_Diagnostics_Log_Group with the name `/hpc-platform/clusters/{projectId}/node-diagnostics` and a retention period of 1 day.
4. THE ProjectInfrastructureStack SHALL set the removal policy of the Node_Diagnostics_Log_Group to DESTROY, because the short retention period and diagnostic nature of the logs make them disposable.
5. THE `generate_cloudwatch_agent_commands` function SHALL accept the `project_id` parameter and use it to construct both the existing access log group name and the new node diagnostics log group name.
6. THE CloudWatch_Agent configuration SHALL use `append-config` mode so that the node diagnostics configuration coexists with the existing access log configuration without overwriting it.
7. WHEN a node instance boots, THE user data script SHALL configure the CloudWatch_Agent to begin shipping syslog and cloud-init output logs within the same boot sequence that configures access log shipping.

