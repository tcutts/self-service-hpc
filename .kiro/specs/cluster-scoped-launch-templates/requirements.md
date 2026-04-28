# Requirements Document

## Introduction

EC2 launch templates for login and compute nodes are currently created at project deploy time in the CDK infrastructure stack and shared across all clusters within a project. This feature moves launch template creation to cluster creation time, making them cluster-scoped resources. This aligns with the existing per-cluster instance profile pattern and enables future cluster-specific customisation (user data, AMIs, instance metadata options).

## Glossary

- **Project_Infrastructure_Stack**: The CDK stack (`lib/project-infrastructure-stack.ts`) that provisions per-project AWS resources (VPC, EFS, S3, security groups, CloudWatch log group)
- **Cluster_Creation_Workflow**: The Step Functions state machine that creates a cluster, implemented by step handlers in `lambda/cluster_operations/cluster_creation.py`
- **Cluster_Destruction_Workflow**: The Step Functions state machine that destroys a cluster, implemented by step handlers in `lambda/cluster_operations/cluster_destruction.py`
- **Launch_Template**: An EC2 launch template that specifies a security group for PCS compute node groups; two are needed per cluster (login and compute)
- **Project_Deploy_Workflow**: The Step Functions state machine that deploys project infrastructure via CDK, implemented by step handlers in `lambda/project_management/project_deploy.py`
- **Projects_Table**: The DynamoDB table storing project metadata including infrastructure IDs
- **Clusters_Table**: The DynamoDB table storing cluster metadata including resource IDs
- **Cluster_Handler**: The Lambda handler (`lambda/cluster_operations/handler.py`) that routes API requests and starts Step Functions executions for cluster operations
- **EC2_Client**: The AWS EC2 API client used to create and delete launch templates
- **Creation_Rollback_Handler**: The failure handler in the Cluster_Creation_Workflow that cleans up partially created resources when creation fails

## Requirements

### Requirement 1: Remove Launch Templates from Project Infrastructure Stack

**User Story:** As a platform operator, I want launch templates removed from the project CDK stack, so that they are no longer shared across clusters and can be managed per-cluster instead.

#### Acceptance Criteria

1. THE Project_Infrastructure_Stack SHALL NOT define any EC2 launch template resources
2. THE Project_Infrastructure_Stack SHALL NOT emit `LoginLaunchTemplateId` or `ComputeLaunchTemplateId` CloudFormation outputs
3. THE Project_Infrastructure_Stack SHALL continue to define the Head_Node and Compute_Node security groups unchanged

### Requirement 2: Remove Launch Template Extraction from Project Deploy Workflow

**User Story:** As a platform operator, I want the project deploy workflow to stop extracting and storing launch template IDs, so that the project record no longer carries shared template references.

#### Acceptance Criteria

1. THE Project_Deploy_Workflow SHALL NOT extract `LoginLaunchTemplateId` or `ComputeLaunchTemplateId` from CloudFormation stack outputs
2. THE Project_Deploy_Workflow SHALL NOT store `loginLaunchTemplateId` or `computeLaunchTemplateId` in the Projects_Table
3. WHEN a project is deployed, THE Project_Deploy_Workflow SHALL continue to extract and store all other infrastructure IDs (VPC, EFS, S3, subnets, security groups) unchanged

### Requirement 3: Remove Launch Template IDs from Cluster Handler Payload

**User Story:** As a platform operator, I want the cluster handler to stop reading launch template IDs from the project record, so that cluster creation no longer depends on project-level templates.

#### Acceptance Criteria

1. THE Cluster_Handler SHALL NOT read `loginLaunchTemplateId` or `computeLaunchTemplateId` from the Projects_Table
2. THE Cluster_Handler SHALL NOT include `loginLaunchTemplateId` or `computeLaunchTemplateId` in the Step Functions execution payload for cluster creation or recreation
3. THE Cluster_Handler SHALL continue to include all other infrastructure fields (vpcId, efsFileSystemId, s3BucketName, subnets, securityGroupIds) in the Step Functions execution payload unchanged

### Requirement 4: Create Launch Templates During Cluster Creation

**User Story:** As a platform operator, I want launch templates created dynamically during cluster creation, so that each cluster has its own dedicated templates that can be customised independently.

#### Acceptance Criteria

1. WHEN a cluster is being created, THE Cluster_Creation_Workflow SHALL create a login launch template named `hpc-{projectId}-{clusterName}-login` with the Head_Node security group
2. WHEN a cluster is being created, THE Cluster_Creation_Workflow SHALL create a compute launch template named `hpc-{projectId}-{clusterName}-compute` with the Compute_Node security group
3. THE Cluster_Creation_Workflow SHALL create launch templates before creating PCS compute node groups that reference them
4. THE Cluster_Creation_Workflow SHALL pass the dynamically created launch template IDs to the `create_login_node_group` and `create_compute_node_group` steps
5. THE Cluster_Creation_Workflow SHALL tag launch templates with the same Project and ClusterName tags applied to other cluster resources
6. WHEN launch template creation fails, THE Cluster_Creation_Workflow SHALL raise an InternalError with a descriptive message

### Requirement 5: Delete Launch Templates During Cluster Destruction

**User Story:** As a platform operator, I want launch templates cleaned up when a cluster is destroyed, so that orphaned resources do not accumulate.

#### Acceptance Criteria

1. WHEN a cluster is being destroyed, THE Cluster_Destruction_Workflow SHALL delete the login launch template named `hpc-{projectId}-{clusterName}-login`
2. WHEN a cluster is being destroyed, THE Cluster_Destruction_Workflow SHALL delete the compute launch template named `hpc-{projectId}-{clusterName}-compute`
3. IF a launch template does not exist during destruction, THEN THE Cluster_Destruction_Workflow SHALL log a warning and continue without raising an error
4. THE Cluster_Destruction_Workflow SHALL delete launch templates after deleting PCS resources and before recording the cluster as destroyed

### Requirement 6: Clean Up Launch Templates on Creation Failure

**User Story:** As a platform operator, I want launch templates cleaned up during creation rollback, so that failed cluster attempts do not leave orphaned templates.

#### Acceptance Criteria

1. WHEN cluster creation fails, THE Creation_Rollback_Handler SHALL delete any launch templates that were created during the failed attempt
2. IF a launch template does not exist during rollback cleanup, THEN THE Creation_Rollback_Handler SHALL log a warning and continue without raising an error
3. THE Creation_Rollback_Handler SHALL continue to clean up all other resources (IAM, PCS, FSx) unchanged

### Requirement 7: Preserve Existing Cluster Creation Behaviour

**User Story:** As a platform operator, I want all non-launch-template cluster creation behaviour to remain unchanged, so that the migration does not introduce regressions.

#### Acceptance Criteria

1. THE Cluster_Creation_Workflow SHALL continue to create per-cluster IAM roles and instance profiles with the same naming convention and policies
2. THE Cluster_Creation_Workflow SHALL continue to pass the same parameters to PCS `create_compute_node_group` for login and compute nodes, except for the launch template ID source
3. THE Cluster_Creation_Workflow SHALL continue to create FSx filesystems, PCS clusters, PCS queues, and DynamoDB records with the same parameters and behaviour
4. THE Cluster_Destruction_Workflow SHALL continue to delete PCS resources, FSx filesystems, IAM resources, and DynamoDB records with the same parameters and behaviour
