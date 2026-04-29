# Bugfix Requirements Document

## Introduction

Launch templates created during cluster creation contain no `UserData` field, which means EC2 instances boot without executing any provisioning scripts. As a result, the EFS home directory filesystem is never mounted at `/home`, POSIX user accounts are never created, generic accounts (ec2-user, centos, ubuntu) are never disabled, and the `/data` storage mount (FSx for Lustre or Mountpoint for S3) is never applied. The `generate_user_data_script()` function in `posix_provisioning.py` produces a complete bash script, but it is only called during node group creation (after launch templates already exist) and its output is stored in the Step Functions event payload as `userDataScript` — it is never injected into the EC2 launch template. Additionally, the generated script has no EFS mount logic at all, despite the infrastructure (EFS filesystem, security groups with NFS port 2049) being fully provisioned by the CDK stack.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `create_launch_templates()` creates login and compute EC2 launch templates THEN the system creates `LaunchTemplateData` containing only `SecurityGroupIds` and `ImageId` with no `UserData` field

1.2 WHEN `create_login_node_group()` executes THEN the system calls `generate_user_data_script()` and stores the result in the event payload as `userDataScript` but does not inject it into the already-created login launch template

1.3 WHEN `create_compute_node_group()` executes THEN the system calls `generate_user_data_script()` and stores the result in the event payload but does not inject it into the already-created compute launch template

1.4 WHEN a cluster node boots using the launch template THEN the system does not mount the EFS filesystem at `/home` because no EFS mount commands exist anywhere in the generated user data script

1.5 WHEN a cluster node boots using the launch template THEN the system does not create POSIX user accounts, does not disable generic accounts, and does not mount `/data` storage because the `UserData` field is empty

1.6 WHEN `generate_user_data_script()` is called with a valid `efsFileSystemId` available in the event THEN the system does not generate any EFS mount commands because the function has no EFS mount logic and does not accept an `efsFileSystemId` parameter

1.7 WHEN a user manually enters an AMI ID in the template creation/edit form THEN the system does not validate that the AMI exists in the deployment region or that its state is `available` — the backend only checks that the string is non-empty

1.8 WHEN `create_launch_templates()` uses an AMI ID from the template THEN the system does not verify the AMI exists or is available before creating the launch template, leading to late failures during `CreateComputeNodeGroup` with "AWS PCS can't find an AMI you specified"

### Expected Behavior (Correct)

2.1 WHEN `create_launch_templates()` creates login and compute EC2 launch templates THEN the system SHALL include a base64-encoded `UserData` field in `LaunchTemplateData` containing the complete provisioning script

2.2 WHEN the user data script is generated for launch templates THEN the system SHALL generate the script BEFORE launch template creation so it can be included in the `LaunchTemplateData`

2.3 WHEN the user data script is generated for launch templates THEN the system SHALL NOT generate the script again during `create_login_node_group()` or `create_compute_node_group()` since it is already embedded in the launch template

2.4 WHEN a cluster node boots using the launch template THEN the system SHALL mount the EFS filesystem at `/home` using `amazon-efs-utils` and the `efsFileSystemId` from the project infrastructure

2.5 WHEN a cluster node boots using the launch template THEN the system SHALL create POSIX user accounts for all project members, disable generic accounts, configure access logging, and mount `/data` storage according to the storage mode

2.6 WHEN `generate_user_data_script()` is called THEN the system SHALL accept an `efs_filesystem_id` parameter and generate EFS mount commands that install `amazon-efs-utils` (if needed) and mount the EFS filesystem at `/home` using the `mount -t efs` helper

2.7 WHEN the user data is set in the launch template THEN the system SHALL base64-encode the script content as required by the EC2 `LaunchTemplateData.UserData` specification

2.8 WHEN a template is created or updated with an `amiId` (or `loginAmiId`) THEN the backend SHALL call EC2 `DescribeImages` to verify the AMI exists in the current region and has state `available`, and SHALL reject the request with a validation error if it does not

2.9 WHEN `create_launch_templates()` is about to use an AMI ID THEN the system SHALL validate the AMI exists and is available before creating the launch template, failing fast with a clear error rather than deferring failure to the PCS `CreateComputeNodeGroup` call

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `create_launch_templates()` creates launch templates THEN the system SHALL CONTINUE TO include the correct `SecurityGroupIds` (head node SG for login, compute node SG for compute) in each template

3.2 WHEN `create_launch_templates()` creates launch templates THEN the system SHALL CONTINUE TO include the correct `ImageId` (login AMI for login template, compute AMI for compute template) in each template

3.3 WHEN `create_launch_templates()` creates launch templates THEN the system SHALL CONTINUE TO tag templates with project and cluster resource tags

3.4 WHEN a launch template with the same name already exists THEN the system SHALL CONTINUE TO adopt the existing template rather than failing

3.5 WHEN `generate_user_data_script()` is called with `storage_mode="mountpoint"` and a valid `s3_bucket_name` THEN the system SHALL CONTINUE TO generate Mountpoint for S3 mount commands for `/data`

3.6 WHEN `generate_user_data_script()` is called with `storage_mode="lustre"` and valid FSx parameters THEN the system SHALL CONTINUE TO generate FSx for Lustre mount commands for `/data`

3.7 WHEN `generate_user_data_script()` is called THEN the system SHALL CONTINUE TO generate POSIX user creation commands, generic account disabling commands, PAM exec logging commands, and CloudWatch agent commands

3.8 WHEN `generate_user_data_script()` is called with no storage mode THEN the system SHALL CONTINUE TO omit storage mount commands from the script

3.9 WHEN the cluster creation Step Functions workflow executes THEN the system SHALL CONTINUE TO run launch template creation, storage provisioning, and PCS cluster creation in parallel via the `ParallelProvision` state

3.10 WHEN the auto-detect AMI button is used in the UI and `get_latest_pcs_ami()` returns an AMI THEN the system SHALL CONTINUE TO populate the AMI field with the result — the auto-detected AMI is already validated as available by the lookup function
