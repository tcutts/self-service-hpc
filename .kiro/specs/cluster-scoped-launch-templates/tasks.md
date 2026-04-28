# Implementation Plan: Cluster-Scoped Launch Templates

## Overview

Migrate EC2 launch templates from static CDK resources (project-scoped) to dynamic runtime resources (cluster-scoped). This involves removing launch template definitions from the CDK stack and project deploy workflow, then adding creation/deletion logic to the cluster creation and destruction workflows. The implementation follows the existing per-cluster instance profile pattern.

## Tasks

- [x] 1. Remove launch templates from CDK stack and project deploy workflow
  - [x] 1.1 Remove launch template resources and outputs from `lib/project-infrastructure-stack.ts`
    - Remove the `loginLaunchTemplate` and `computeLaunchTemplate` L2 construct declarations
    - Remove the `LoginLaunchTemplateId` and `ComputeLaunchTemplateId` `CfnOutput` entries
    - Remove the corresponding public class properties (`loginLaunchTemplate`, `computeLaunchTemplate`)
    - Verify security groups, VPC, EFS, S3, CloudWatch log group, and all other outputs remain unchanged
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 1.2 Remove launch template extraction and storage from `lambda/project_management/project_deploy.py`
    - In `extract_stack_outputs`: remove extraction of `LoginLaunchTemplateId` and `ComputeLaunchTemplateId` from `output_map`, and remove those keys from the returned event dict
    - In `record_infrastructure`: remove `loginLaunchTemplateId` and `computeLaunchTemplateId` from the DynamoDB `UpdateExpression` and `ExpressionAttributeValues`
    - Verify all other infrastructure fields (VPC, EFS, S3, subnets, security groups) continue to be extracted and stored unchanged
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 1.3 Remove launch template ID passthrough from `lambda/cluster_operations/handler.py`
    - In `_lookup_project_infrastructure`: remove `loginLaunchTemplateId` and `computeLaunchTemplateId` from the returned dict
    - In `_handle_create_cluster`: remove `loginLaunchTemplateId` and `computeLaunchTemplateId` from the SFN execution payload
    - In `_handle_recreate_cluster`: remove `loginLaunchTemplateId` and `computeLaunchTemplateId` from the SFN execution payload
    - Verify all other infrastructure fields (vpcId, efsFileSystemId, s3BucketName, subnets, securityGroupIds) remain in the payload unchanged
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 1.4 Write unit tests for CDK stack, project deploy, and handler removals
    - Test that `ProjectInfrastructureStack` no longer contains launch template resources or outputs (CDK assertions)
    - Test that `extract_stack_outputs` no longer returns `loginLaunchTemplateId` or `computeLaunchTemplateId`
    - Test that `record_infrastructure` no longer writes launch template IDs to DynamoDB
    - Test that `_lookup_project_infrastructure` no longer returns launch template IDs
    - Test that SFN payloads in `_handle_create_cluster` and `_handle_recreate_cluster` no longer contain launch template IDs
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2_

- [x] 2. Checkpoint - Verify removal changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Add launch template creation to cluster creation workflow
  - [x] 3.1 Add `ec2_client` to `lambda/cluster_operations/cluster_creation.py`
    - Add `ec2_client = boto3.client("ec2")` to the AWS clients section
    - _Requirements: 4.1_

  - [x] 3.2 Implement `create_launch_templates` step in `lambda/cluster_operations/cluster_creation.py`
    - Create function `create_launch_templates(event) -> event` that creates two EC2 launch templates via `ec2_client.create_launch_template()`
    - Login template: name `hpc-{projectId}-{clusterName}-login`, security group from `securityGroupIds["headNode"]`, tagged with `build_resource_tags(projectId, clusterName)`
    - Compute template: name `hpc-{projectId}-{clusterName}-compute`, security group from `securityGroupIds["computeNode"]`, tagged with same tags
    - Return event with `loginLaunchTemplateId` and `computeLaunchTemplateId` added
    - On `ClientError`, raise `InternalError` with a descriptive message
    - Register `"create_launch_templates": create_launch_templates` in `_STEP_DISPATCH`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 3.3 Write property test: launch template creation naming and configuration (Property 1)
    - **Property 1: Launch template creation produces correctly named and configured templates**
    - For any valid projectId and clusterName, verify `create_launch_templates` calls `ec2_client.create_launch_template` with correct names (`hpc-{projectId}-{clusterName}-login` and `hpc-{projectId}-{clusterName}-compute`), correct security groups (headNode and computeNode), and correct Project/ClusterName tags
    - Use Hypothesis with `@settings(max_examples=100)`
    - **Validates: Requirements 4.1, 4.2, 4.5**

  - [x] 3.4 Write unit tests for `create_launch_templates`
    - Test successful creation returns event with both template IDs
    - Test `ClientError` raises `InternalError`
    - Test step is registered in `_STEP_DISPATCH`
    - _Requirements: 4.1, 4.2, 4.6_

- [x] 4. Add launch template deletion to cluster destruction workflow
  - [x] 4.1 Add `ec2_client` to `lambda/cluster_operations/cluster_destruction.py`
    - Add `ec2_client = boto3.client("ec2")` to the AWS clients section
    - _Requirements: 5.1_

  - [x] 4.2 Implement `delete_launch_templates` step in `lambda/cluster_operations/cluster_destruction.py`
    - Create function `delete_launch_templates(event) -> event` that deletes both launch templates by name
    - Use `ec2_client.describe_launch_templates(LaunchTemplateNames=[...])` to resolve names, then `ec2_client.delete_launch_template(LaunchTemplateId=...)` to delete
    - If template not found, log warning and continue (best-effort)
    - Add `launchTemplateCleanupResults` to the returned event
    - Register `"delete_launch_templates": delete_launch_templates` in `_STEP_DISPATCH`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 4.3 Write property test: launch template destruction naming (Property 2)
    - **Property 2: Launch template destruction targets correctly named templates**
    - For any valid projectId and clusterName, verify `delete_launch_templates` attempts to delete templates named `hpc-{projectId}-{clusterName}-login` and `hpc-{projectId}-{clusterName}-compute`
    - Use Hypothesis with `@settings(max_examples=100)`
    - **Validates: Requirements 5.1, 5.2**

  - [x] 4.4 Write unit tests for `delete_launch_templates`
    - Test successful deletion of both templates
    - Test graceful handling when templates do not exist (no error raised)
    - Test step is registered in `_STEP_DISPATCH`
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 5. Checkpoint - Verify creation and destruction changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Add launch template cleanup to creation rollback and wire everything together
  - [x] 6.1 Add launch template cleanup to `handle_creation_failure` in `lambda/cluster_operations/cluster_creation.py`
    - Add a `_cleanup_launch_template(template_name: str) -> str` helper that deletes a launch template by name, catches not-found errors, and returns a result string
    - In `handle_creation_failure`, add launch template cleanup (both login and compute) between IAM cleanup and PCS cleanup steps
    - Use best-effort approach: log warnings on failure, continue with remaining cleanup
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 6.2 Write property test: rollback cleanup naming (Property 3)
    - **Property 3: Rollback cleanup targets correctly named launch templates**
    - For any valid projectId and clusterName, verify `handle_creation_failure` attempts to delete templates named `hpc-{projectId}-{clusterName}-login` and `hpc-{projectId}-{clusterName}-compute`
    - Use Hypothesis with `@settings(max_examples=100)`
    - **Validates: Requirements 6.1**

  - [x] 6.3 Write unit tests for rollback launch template cleanup
    - Test rollback deletes launch templates when they exist
    - Test rollback handles missing launch templates gracefully (no error raised)
    - Test rollback continues to clean up IAM, PCS, and FSx resources unchanged
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 6.4 Write regression unit tests for unchanged behaviour
    - Test that all other creation steps (IAM, FSx, PCS, DynamoDB) remain unchanged
    - Test that all other destruction steps (FSx export, PCS, FSx delete, IAM, DynamoDB) remain unchanged
    - Test step ordering: launch template creation occurs after instance profile propagation and before FSx/PCS node groups
    - Test step ordering: launch template deletion occurs after IAM cleanup and before recording destroyed
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis (already in use in the project) with `@settings(max_examples=100)`
- The implementation follows the existing per-cluster instance profile pattern in `cluster_creation.py`
- Launch template names are deterministic (`hpc-{projectId}-{clusterName}-login/compute`), enabling name-based deletion without storing IDs in DynamoDB
