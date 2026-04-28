# Requirements Document

## Introduction

This feature makes cluster storage and compute scaling configurable at deployment time. Currently, every cluster unconditionally creates an FSx for Lustre filesystem (1.2 TiB, SCRATCH_2) with a Data Repository Association to the project S3 bucket, and compute node scaling is fixed to the template-defined minNodes/maxNodes values. This feature allows the user to:

1. Choose whether the cluster uses FSx for Lustre to cache project data, and if so, specify the storage capacity.
2. Override the template's default minimum and maximum compute node counts.
3. When Lustre is not selected, mount the project S3 bucket directly on login and compute nodes using Mountpoint for Amazon S3.

## Glossary

- **Cluster_Creation_Form**: The frontend UI form used to create a new cluster, currently accepting clusterName and templateId.
- **Cluster_Creation_Handler**: The Lambda function (`lambda/cluster_operations/handler.py`) that validates cluster creation requests and starts the Step Functions workflow.
- **Cluster_Creation_Workflow**: The 12-step Step Functions state machine (`lambda/cluster_operations/cluster_creation.py`) that orchestrates cluster provisioning.
- **FSx_for_Lustre**: AWS FSx for Lustre, a high-performance filesystem used as a cache layer with a Data Repository Association to the project S3 bucket.
- **Mountpoint_for_S3**: An open-source FUSE-based file client (Mountpoint for Amazon S3) that mounts an S3 bucket as a local filesystem on Linux instances.
- **Storage_Mode**: A per-cluster configuration choice indicating whether the cluster uses FSx for Lustre (`lustre`) or Mountpoint for Amazon S3 (`mountpoint`) for project data access.
- **Lustre_Capacity**: The storage capacity in GiB for the FSx for Lustre filesystem. Must be a multiple of 1200 GiB (1.2 TiB increments), with a minimum of 1200 GiB.
- **Template**: A cluster template record in the ClusterTemplates DynamoDB table, defining instance types, node counts, and software configuration.
- **Compute_Node_Group**: The PCS compute node group providing elastic scaling for Slurm job execution.
- **Login_Node**: The head node providing SSH/DCV access and job submission.

## Requirements

### Requirement 1: Storage Mode Selection

**User Story:** As a project user, I want to choose whether my cluster uses FSx for Lustre or Mountpoint for Amazon S3 for project data access, so that I can balance performance needs against cost and provisioning time.

#### Acceptance Criteria

1. WHEN a cluster creation request is submitted, THE Cluster_Creation_Form SHALL present a Storage_Mode selector with two options: "FSx for Lustre" and "Mountpoint for Amazon S3".
2. THE Cluster_Creation_Form SHALL default the Storage_Mode selector to "Mountpoint for Amazon S3".
3. WHEN the user selects "FSx for Lustre" as the Storage_Mode, THE Cluster_Creation_Form SHALL display a Lustre_Capacity input field.
4. WHEN the user selects "Mountpoint for Amazon S3" as the Storage_Mode, THE Cluster_Creation_Form SHALL hide the Lustre_Capacity input field.
5. THE Cluster_Creation_Handler SHALL accept an optional `storageMode` field in the cluster creation request body with valid values `lustre` and `mountpoint`.
6. IF the `storageMode` field contains a value other than `lustre` or `mountpoint`, THEN THE Cluster_Creation_Handler SHALL return a VALIDATION_ERROR with HTTP status 400.
7. WHEN the `storageMode` field is omitted from the request body, THE Cluster_Creation_Handler SHALL default the Storage_Mode to `mountpoint`.

### Requirement 2: FSx for Lustre Capacity Configuration

**User Story:** As a project user, I want to specify the size of the FSx for Lustre filesystem when creating a cluster, so that I can provision storage appropriate for my workload.

#### Acceptance Criteria

1. WHEN the Storage_Mode is `lustre`, THE Cluster_Creation_Form SHALL display a Lustre_Capacity input field with a default value of 1200 GiB.
2. THE Cluster_Creation_Handler SHALL accept an optional `lustreCapacityGiB` field in the cluster creation request body.
3. IF the `lustreCapacityGiB` value is less than 1200, THEN THE Cluster_Creation_Handler SHALL return a VALIDATION_ERROR with HTTP status 400 indicating the minimum capacity is 1200 GiB.
4. IF the `lustreCapacityGiB` value is not a multiple of 1200, THEN THE Cluster_Creation_Handler SHALL return a VALIDATION_ERROR with HTTP status 400 indicating the capacity must be a multiple of 1200 GiB.
5. WHEN the `lustreCapacityGiB` field is omitted and the Storage_Mode is `lustre`, THE Cluster_Creation_Handler SHALL default the Lustre_Capacity to 1200 GiB.
6. WHEN the Storage_Mode is `lustre`, THE Cluster_Creation_Workflow SHALL create the FSx for Lustre filesystem with the specified Lustre_Capacity as the StorageCapacity parameter.
7. WHEN the Storage_Mode is `mountpoint`, THE Cluster_Creation_Handler SHALL ignore any provided `lustreCapacityGiB` value.

### Requirement 3: Conditional FSx for Lustre Creation

**User Story:** As a project user, I want the cluster creation workflow to skip FSx for Lustre provisioning when I choose Mountpoint for Amazon S3, so that my cluster is created faster and at lower cost.

#### Acceptance Criteria

1. WHEN the Storage_Mode is `lustre`, THE Cluster_Creation_Workflow SHALL create an FSx for Lustre filesystem with a Data Repository Association to the project S3 bucket.
2. WHEN the Storage_Mode is `mountpoint`, THE Cluster_Creation_Workflow SHALL skip the FSx filesystem creation step, the FSx status polling step, and the Data Repository Association creation step.
3. WHEN the Storage_Mode is `mountpoint`, THE Cluster_Creation_Workflow SHALL not create an FSx security group ingress dependency for the cluster.
4. WHEN the Storage_Mode is `mountpoint`, THE Cluster_Creation_Workflow SHALL configure the Login_Node and Compute_Node_Group instances to mount the project S3 bucket using Mountpoint_for_S3 at the `/data` mount path.
5. WHEN the Storage_Mode is `lustre`, THE Cluster_Creation_Workflow SHALL mount the FSx for Lustre filesystem at the `/data` mount path on Login_Node and Compute_Node_Group instances.

### Requirement 4: Mountpoint for Amazon S3 IAM Permissions

**User Story:** As a platform operator, I want clusters using Mountpoint for Amazon S3 to have the correct IAM permissions, so that instances can read from and write to the project S3 bucket securely.

#### Acceptance Criteria

1. WHEN the Storage_Mode is `mountpoint`, THE Cluster_Creation_Workflow SHALL attach an inline IAM policy to the login and compute IAM roles granting `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, and `s3:GetBucketLocation` on the project S3 bucket and its objects.
2. WHEN the Storage_Mode is `lustre`, THE Cluster_Creation_Workflow SHALL not attach the Mountpoint S3 inline policy to the login and compute IAM roles.
3. THE Cluster_Creation_Workflow SHALL scope the Mountpoint S3 IAM policy to the specific project S3 bucket ARN and its object ARN, not to wildcard resources.

### Requirement 5: Compute Node Scaling Overrides

**User Story:** As a project user, I want to override the template's default minimum and maximum compute node counts when creating a cluster, so that I can right-size the cluster for my specific workload.

#### Acceptance Criteria

1. WHEN a template is selected in the Cluster_Creation_Form, THE Cluster_Creation_Form SHALL display the template's minNodes and maxNodes values as editable input fields pre-populated with the template defaults.
2. THE Cluster_Creation_Handler SHALL accept optional `minNodes` and `maxNodes` fields in the cluster creation request body.
3. IF the `minNodes` value is provided and is less than 0, THEN THE Cluster_Creation_Handler SHALL return a VALIDATION_ERROR with HTTP status 400.
4. IF the `maxNodes` value is provided and is less than 1, THEN THE Cluster_Creation_Handler SHALL return a VALIDATION_ERROR with HTTP status 400.
5. IF the `minNodes` value exceeds the `maxNodes` value, THEN THE Cluster_Creation_Handler SHALL return a VALIDATION_ERROR with HTTP status 400.
6. WHEN `minNodes` or `maxNodes` fields are omitted from the request body, THE Cluster_Creation_Workflow SHALL use the values from the resolved Template.
7. WHEN `minNodes` and `maxNodes` fields are provided in the request body, THE Cluster_Creation_Workflow SHALL use the provided values instead of the Template values for the Compute_Node_Group scaling configuration.

### Requirement 6: Cluster Record Storage Configuration

**User Story:** As a project user, I want to see the storage configuration of my cluster, so that I can verify the cluster was created with the correct settings.

#### Acceptance Criteria

1. THE Cluster_Creation_Workflow SHALL persist the `storageMode` and `lustreCapacityGiB` (when applicable) fields in the Clusters DynamoDB table record.
2. WHEN a cluster detail is retrieved via the GET endpoint, THE Cluster_Creation_Handler SHALL include the `storageMode` field in the response.
3. WHEN the Storage_Mode is `lustre`, THE Cluster_Creation_Handler SHALL include the `lustreCapacityGiB` field in the cluster detail response.
4. THE Cluster_Creation_Workflow SHALL persist the effective `minNodes` and `maxNodes` values (whether from the template or user overrides) in the Clusters DynamoDB table record.

### Requirement 7: Cluster Destruction with Storage Mode Awareness

**User Story:** As a project user, I want cluster destruction to clean up the correct resources based on the storage mode, so that no orphaned resources remain.

#### Acceptance Criteria

1. WHEN a cluster with Storage_Mode `lustre` is destroyed, THE Cluster_Destruction_Workflow SHALL execute the FSx data export task and delete the FSx filesystem.
2. WHEN a cluster with Storage_Mode `mountpoint` is destroyed, THE Cluster_Destruction_Workflow SHALL skip the FSx export task and FSx filesystem deletion steps.
3. WHEN a cluster with Storage_Mode `mountpoint` is destroyed, THE Cluster_Destruction_Workflow SHALL remove the Mountpoint S3 inline IAM policy from the login and compute roles before deleting the roles.

### Requirement 8: Cluster Recreation with Storage Configuration

**User Story:** As a project user, I want to be able to specify storage configuration when recreating a destroyed cluster, so that I can change the storage mode or capacity on recreation.

#### Acceptance Criteria

1. THE Cluster_Recreation_Handler SHALL accept optional `storageMode`, `lustreCapacityGiB`, `minNodes`, and `maxNodes` fields in the recreation request body.
2. WHEN the `storageMode` field is omitted from the recreation request body, THE Cluster_Recreation_Handler SHALL use the `storageMode` from the destroyed cluster record.
3. WHEN the `minNodes` or `maxNodes` fields are omitted from the recreation request body, THE Cluster_Recreation_Handler SHALL use the values from the resolved Template.

### Requirement 9: Step Functions Payload Propagation

**User Story:** As a platform operator, I want the storage and scaling configuration to flow through the entire Step Functions workflow, so that each step has access to the configuration it needs.

#### Acceptance Criteria

1. THE Cluster_Creation_Handler SHALL include `storageMode`, `lustreCapacityGiB`, `minNodes`, and `maxNodes` fields in the Step Functions execution input payload.
2. THE Cluster_Creation_Workflow resolve_template step SHALL preserve user-provided `minNodes` and `maxNodes` overrides and not overwrite them with template values.
3. THE Cluster_Creation_Workflow resolve_template step SHALL preserve the `storageMode` and `lustreCapacityGiB` fields from the input payload.

### Requirement 10: Documentation Updates

**User Story:** As a project user, I want the cluster management documentation to describe the new storage and scaling options, so that I can understand how to use the new configuration capabilities.

#### Acceptance Criteria

1. THE cluster management documentation SHALL describe the `storageMode` field, its valid values, and its default behaviour.
2. THE cluster management documentation SHALL describe the `lustreCapacityGiB` field, its validation rules, and its default value.
3. THE cluster management documentation SHALL describe the `minNodes` and `maxNodes` override fields and their relationship to template defaults.
4. THE cluster management documentation SHALL update the cluster creation request example to include the new optional fields.
5. THE cluster management documentation SHALL update the cluster detail response example to include the `storageMode` field.
