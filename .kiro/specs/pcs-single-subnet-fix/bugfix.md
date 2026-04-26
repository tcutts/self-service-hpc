# Bugfix Requirements Document

## Introduction

PCS cluster creation fails with a `ValidationException` when the `create_pcs_cluster` function passes all private subnet IDs from the VPC to the AWS PCS `CreateCluster` API. The PCS `CreateCluster` API requires exactly one subnet, but the code passes the full `private_subnet_ids` list which typically contains multiple subnets (one per availability zone). This causes every cluster creation attempt to fail with the error: *"You can only specify 1 subnet when you create a cluster."*

The fix is isolated to the `create_pcs_cluster` function in `lambda/cluster_operations/cluster_creation.py`. Other functions in the same file that use `private_subnet_ids` either already select a single subnet (e.g., `create_fsx_filesystem` uses `private_subnet_ids[0]`) or call PCS APIs that accept multiple subnets (e.g., `create_compute_node_group` calls `CreateComputeNodeGroup` which supports multi-subnet placement).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `create_pcs_cluster` is called with a `privateSubnetIds` list containing more than one subnet THEN the system raises a `ValidationException` from the PCS `CreateCluster` API with the message "You can only specify 1 subnet when you create a cluster" and cluster creation fails

1.2 WHEN `create_pcs_cluster` is called with a `privateSubnetIds` list containing more than one subnet THEN the system wraps the API error in an `InternalError` and the cluster creation state machine transitions to the failure/rollback path

### Expected Behavior (Correct)

2.1 WHEN `create_pcs_cluster` is called with a `privateSubnetIds` list containing one or more subnets THEN the system SHALL pass only the first subnet from the list to the PCS `CreateCluster` API's `networking.subnetIds` parameter (as a single-element list) and the cluster SHALL be created successfully

2.2 WHEN `create_pcs_cluster` is called with a `privateSubnetIds` list containing more than one subnet THEN the system SHALL not raise a `ValidationException` and SHALL return the `pcsClusterId` and `pcsClusterArn` in the event payload for subsequent steps

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `create_pcs_cluster` is called with a `privateSubnetIds` list containing exactly one subnet THEN the system SHALL CONTINUE TO pass that subnet to the PCS `CreateCluster` API and create the cluster successfully

3.2 WHEN `create_compute_node_group` is called with a `privateSubnetIds` list containing multiple subnets THEN the system SHALL CONTINUE TO pass all subnets to the PCS `CreateComputeNodeGroup` API (which accepts multiple subnets for compute node placement across availability zones)

3.3 WHEN `create_login_node_group` is called with a `publicSubnetIds` list THEN the system SHALL CONTINUE TO pass all public subnets to the PCS `CreateComputeNodeGroup` API for login node placement

3.4 WHEN `create_fsx_filesystem` is called with a `privateSubnetIds` list THEN the system SHALL CONTINUE TO use only the first subnet (`private_subnet_ids[0]`) for FSx filesystem creation

3.5 WHEN `create_pcs_cluster` encounters a `ConflictException` from the PCS API THEN the system SHALL CONTINUE TO retry with exponential backoff up to the configured maximum retries

3.6 WHEN `create_pcs_cluster` is called THEN the system SHALL CONTINUE TO pass the same `clusterName`, `scheduler`, `size`, `securityGroupIds`, `slurmConfiguration`, and `tags` parameters unchanged to the PCS `CreateCluster` API
