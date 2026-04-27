# Bugfix Requirements Document

## Introduction

PCS (Parallel Computing Service) requires an IAM instance profile for each compute node group. Currently, a single instance profile (`AWSPCS-{projectId}-node`) is created at the project level in `ProjectInfrastructureStack` and stored in the Projects DynamoDB table. This same instance profile ARN is then passed to every cluster's login node group and compute node group during creation. This is incorrect because different clusters within a project may require different IAM permissions (e.g., access to different S3 paths, different service integrations), and login nodes and compute nodes within the same cluster may also need distinct permissions. Sharing a single instance profile across all clusters and node types creates a least-privilege violation and prevents per-cluster permission customisation.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a project is deployed THEN the system creates a single IAM role and instance profile (`AWSPCS-{projectId}-node`) scoped to the entire project

1.2 WHEN a cluster is created within a project THEN the system uses the project-level `instanceProfileArn` from the Projects DynamoDB table for the login node group (`iamInstanceProfileArn=event.get("instanceProfileArn", "")` in `create_login_node_group`)

1.3 WHEN a cluster is created within a project THEN the system uses the same project-level `instanceProfileArn` for the compute node group (`iamInstanceProfileArn=event.get("instanceProfileArn", "")` in `create_compute_node_group`)

1.4 WHEN multiple clusters are created in the same project THEN all clusters share the identical instance profile, making it impossible to assign different IAM permissions per cluster

1.5 WHEN a cluster is destroyed THEN the system does not clean up any instance profile because the instance profile is owned by the project-level CDK stack, not the cluster

### Expected Behavior (Correct)

2.1 WHEN a cluster is created THEN the system SHALL create a dedicated IAM role and instance profile for the login node group, named `AWSPCS-{projectId}-{clusterName}-login`

2.2 WHEN a cluster is created THEN the system SHALL create a dedicated IAM role and instance profile for the compute node group, named `AWSPCS-{projectId}-{clusterName}-compute`

2.3 WHEN a cluster's login node group is created THEN the system SHALL use the cluster-specific login instance profile ARN (not the project-level one)

2.4 WHEN a cluster's compute node group is created THEN the system SHALL use the cluster-specific compute instance profile ARN (not the project-level one)

2.5 WHEN a cluster is destroyed THEN the system SHALL delete the cluster-specific IAM roles and instance profiles as part of the cleanup workflow

2.6 WHEN a cluster-specific IAM role is created THEN the system SHALL grant it the `pcs:RegisterComputeNodeGroupInstance` permission, the `AmazonSSMManagedInstanceCore` managed policy, and the `CloudWatchAgentServerPolicy` managed policy (matching the current baseline permissions)

2.7 WHEN a cluster-specific IAM role is created THEN the role name SHALL start with `AWSPCS` or the role SHALL use the IAM path `/aws-pcs/` (either satisfies the PCS naming requirement per the [PCS instance profile documentation](https://docs.aws.amazon.com/pcs/latest/userguide/security-instance-profiles.html))

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a project is deployed THEN the system SHALL CONTINUE TO create the VPC, EFS filesystem, S3 bucket, security groups, launch templates, and CloudWatch log group as before

3.2 WHEN a cluster is created THEN the system SHALL CONTINUE TO create the FSx filesystem, PCS cluster, login node group, compute node group, queue, and apply resource tags in the same order

3.3 WHEN a cluster is destroyed THEN the system SHALL CONTINUE TO export FSx data to S3, delete PCS resources (node groups, queue, cluster), delete the FSx filesystem, and mark the cluster as DESTROYED in DynamoDB

3.4 WHEN a cluster is recreated from a DESTROYED state THEN the system SHALL CONTINUE TO create fresh cluster resources using the same creation workflow

3.5 WHEN any IAM role is created for PCS THEN the system SHALL CONTINUE TO satisfy the PCS naming requirement: the role name must start with `AWSPCS` or the role must use the IAM path `/aws-pcs/`
