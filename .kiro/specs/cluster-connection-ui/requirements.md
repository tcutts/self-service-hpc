# Requirements Document

## Introduction

The cluster detail page in the HPC platform UI currently shows an empty "Connection Information" panel for deployed (ACTIVE) clusters. This is because the login node's IP address and instance ID are never extracted from PCS/EC2 during the cluster creation workflow. Users need to connect to their clusters via SSH, NICE DCV remote desktop, or AWS Systems Manager (SSM) Session Manager command-line sessions. This feature adds the backend logic to discover and store login node connection details during cluster creation, extends the API response with all three connection methods, and updates the frontend to display them with copy-to-clipboard functionality.

## Glossary

- **Cluster_Creation_Workflow**: The Step Functions state machine that orchestrates the multi-step process of creating an HPC cluster, including IAM roles, FSx filesystems, PCS cluster, node groups, queues, and the final DynamoDB record.
- **Login_Node**: The EC2 instance in the login (head) node group of a PCS cluster, placed in a public subnet, used as the entry point for user connections via SSH, DCV, or SSM.
- **Connection_Info**: A computed object in the API response containing formatted connection strings for SSH, DCV, and SSM, derived from the login node's IP address and instance ID stored in DynamoDB.
- **Cluster_Operations_API**: The Lambda-backed REST API that serves cluster CRUD operations, including the GET endpoint that returns cluster details with enriched connection information.
- **Cluster_Detail_Page**: The frontend UI page rendered by `renderClusterDetailPage()` that displays cluster metadata, status, progress, and connection information.
- **PCS**: AWS Parallel Computing Service, the managed HPC service used to create and manage Slurm clusters, node groups, and queues.
- **SSM_Session**: An AWS Systems Manager Session Manager session that provides a browser-based or CLI-based shell to an EC2 instance without requiring open inbound ports or SSH keys.
- **DCV_Session**: A NICE DCV remote desktop session accessed via HTTPS, providing a graphical desktop environment on the login node.
- **Node_Group_Status_Check**: The existing Step Functions step (`check_node_groups_status`) that polls PCS until both login and compute node groups reach ACTIVE status.

## Requirements

### Requirement 1: Extract Login Node Connection Details After Node Groups Become Active

**User Story:** As a platform operator, I want the cluster creation workflow to automatically discover the login node's IP address and EC2 instance ID after the login node group becomes active, so that connection information is available when the cluster reaches ACTIVE status.

#### Acceptance Criteria

1. WHEN the Node_Group_Status_Check confirms all node groups are ACTIVE, THE Cluster_Creation_Workflow SHALL execute a new step that queries PCS for the login node group instances and retrieves the EC2 instance ID of the login node.
2. WHEN the login node EC2 instance ID is known, THE Cluster_Creation_Workflow SHALL query EC2 to retrieve the public IP address of the Login_Node.
3. WHEN the login node IP address and instance ID are successfully retrieved, THE Cluster_Creation_Workflow SHALL pass `loginNodeIp`, `loginNodeInstanceId`, `sshPort`, and `dcvPort` to subsequent steps in the state machine payload.
4. IF the login node group has no running instances, THEN THE Cluster_Creation_Workflow SHALL raise an error that triggers the rollback handler.
5. IF the EC2 DescribeInstances call fails, THEN THE Cluster_Creation_Workflow SHALL raise an error that triggers the rollback handler.

### Requirement 2: Store Login Node Instance ID in Cluster Record

**User Story:** As a platform developer, I want the login node's EC2 instance ID stored in the DynamoDB cluster record, so that the API can construct SSM session commands.

#### Acceptance Criteria

1. WHEN the Cluster_Creation_Workflow records the cluster in DynamoDB, THE Cluster_Creation_Workflow SHALL include the `loginNodeInstanceId` field in the cluster record.
2. THE Cluster_Creation_Workflow SHALL store the `loginNodeIp` field with the public IP address of the Login_Node in the cluster record.
3. IF the `loginNodeInstanceId` is empty at record time, THEN THE Cluster_Creation_Workflow SHALL store an empty string for the `loginNodeInstanceId` field.

### Requirement 3: Extend API Connection Info with SSM Session Command

**User Story:** As a user, I want the cluster API to return an SSM session command alongside SSH and DCV connection strings, so that I can connect to my cluster using Systems Manager without needing SSH keys or open ports.

#### Acceptance Criteria

1. WHEN the Cluster_Operations_API returns an ACTIVE cluster with a non-empty `loginNodeInstanceId`, THE Cluster_Operations_API SHALL include an `ssm` field in the Connection_Info object containing the command `aws ssm start-session --target <instanceId>`.
2. WHEN the Cluster_Operations_API returns an ACTIVE cluster with a non-empty `loginNodeIp`, THE Cluster_Operations_API SHALL include an `ssh` field in the Connection_Info object containing the command `ssh -p <sshPort> <username>@<loginNodeIp>`.
3. WHEN the Cluster_Operations_API returns an ACTIVE cluster with a non-empty `loginNodeIp`, THE Cluster_Operations_API SHALL include a `dcv` field in the Connection_Info object containing the URL `https://<loginNodeIp>:<dcvPort>`.
4. WHEN the Cluster_Operations_API returns an ACTIVE cluster with an empty `loginNodeIp` and empty `loginNodeInstanceId`, THE Cluster_Operations_API SHALL return an empty Connection_Info object.

### Requirement 4: Display All Connection Methods in the Cluster Detail Page

**User Story:** As a user, I want the cluster detail page to display SSH, DCV, and SSM connection options clearly, so that I can choose the connection method that suits my needs.

#### Acceptance Criteria

1. WHEN the Cluster_Detail_Page renders an ACTIVE cluster with Connection_Info, THE Cluster_Detail_Page SHALL display a "Connection Details" section with separate entries for each available connection method (SSH, DCV, SSM).
2. WHEN the Connection_Info contains an `ssh` field, THE Cluster_Detail_Page SHALL display the SSH command in a code-formatted block with a label "SSH".
3. WHEN the Connection_Info contains a `dcv` field, THE Cluster_Detail_Page SHALL display the DCV URL as a clickable link with a label "DCV (Remote Desktop)".
4. WHEN the Connection_Info contains an `ssm` field, THE Cluster_Detail_Page SHALL display the SSM command in a code-formatted block with a label "SSM Session Manager".
5. WHEN the Connection_Info is empty or all fields are empty strings, THE Cluster_Detail_Page SHALL display a message "Connection details are not yet available" in the Connection Details section.

### Requirement 5: Copy-to-Clipboard for Connection Commands

**User Story:** As a user, I want to copy connection commands to my clipboard with a single click, so that I can quickly paste them into my terminal.

#### Acceptance Criteria

1. WHEN the Cluster_Detail_Page displays an SSH connection command, THE Cluster_Detail_Page SHALL render a copy button adjacent to the command text.
2. WHEN the Cluster_Detail_Page displays an SSM connection command, THE Cluster_Detail_Page SHALL render a copy button adjacent to the command text.
3. WHEN a user activates a copy button, THE Cluster_Detail_Page SHALL copy the associated command text to the system clipboard.
4. WHEN the clipboard copy operation succeeds, THE Cluster_Detail_Page SHALL display a brief visual confirmation (toast or inline indicator) that the text was copied.
5. IF the clipboard API is unavailable, THEN THE Cluster_Detail_Page SHALL select the command text for manual copying.

### Requirement 6: Include SSM Connection in Lifecycle Notification

**User Story:** As a user, I want the cluster-ready email notification to include the SSM session command alongside SSH and DCV details, so that I have all connection options in one place.

#### Acceptance Criteria

1. WHEN the Cluster_Creation_Workflow sends a cluster-ready notification and the `loginNodeInstanceId` is non-empty, THE Cluster_Creation_Workflow SHALL include the SSM session command in the notification message.
2. WHEN the Cluster_Creation_Workflow sends a cluster-ready notification and the `loginNodeIp` is non-empty, THE Cluster_Creation_Workflow SHALL include the SSH and DCV connection details in the notification message.

### Requirement 7: Update User Documentation

**User Story:** As a user, I want the "Accessing Clusters" documentation to describe all three connection methods including SSM, so that I can follow clear instructions for any connection type.

#### Acceptance Criteria

1. THE documentation SHALL include a section describing how to connect via SSM Session Manager, including the CLI command and prerequisites (AWS CLI, Session Manager plugin).
2. THE documentation SHALL describe all three connection methods: SSH, DCV, and SSM Session Manager.
3. THE documentation SHALL update the example API response to include the `ssm` field in the `connectionInfo` object.
