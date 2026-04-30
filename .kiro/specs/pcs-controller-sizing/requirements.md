# Requirements Document

## Introduction

AWS PCS clusters have a controller size (SMALL, MEDIUM, or LARGE) that determines the maximum number of instances and jobs the cluster can manage. This size is immutable after cluster creation. Currently, `create_pcs_cluster` hardcodes `size="SMALL"`, which limits every cluster to 32 managed instances regardless of how many compute nodes the user requests via `maxNodes`. If a user requests more than 31 compute nodes (plus the 1 login node = 32 total), the cluster will be unable to manage all of them.

This feature replaces the hardcoded size with a sizing function that selects the appropriate controller tier based on the total managed instance count (`maxNodes` + 1 login node), following the AWS PCS sizing tiers. Requests that exceed the maximum PCS capacity (2,048 managed instances) are rejected with a clear error before any AWS resources are created.

## Glossary

- **PCS_Cluster**: An AWS PCS cluster resource created via `pcs_client.create_cluster()` in the cluster creation workflow.
- **Controller_Size**: The `size` parameter passed to `create_cluster()`. One of `SMALL`, `MEDIUM`, or `LARGE`. Determines the maximum number of managed instances and tracked jobs. Cannot be changed after creation.
- **Total_Managed_Instances**: The total number of EC2 instances the PCS cluster must manage, calculated as `maxNodes + 1` (compute nodes plus the single login node).
- **Max_Nodes**: The maximum number of compute node instances for the cluster, sourced from `event.get("maxNodes", 10)` in the creation workflow.
- **Sizing_Function**: A pure function that accepts a `maxNodes` value and returns the appropriate Controller_Size string, or raises an error if the value exceeds PCS limits.
- **Cluster_Creation_Handler**: The `create_pcs_cluster` function in `lambda/cluster_operations/cluster_creation.py` that creates the AWS PCS cluster.
- **PCS_Size_Tiers**: The AWS PCS controller sizing tiers: SMALL (up to 32 instances, 256 jobs), MEDIUM (up to 512 instances, 8,192 jobs), LARGE (up to 2,048 instances, 16,384 jobs).

## Requirements

### Requirement 1: Dynamic Controller Size Selection

**User Story:** As a platform operator, I want the PCS cluster controller size to be automatically selected based on the requested number of compute nodes, so that the cluster can manage all requested instances without manual sizing.

#### Acceptance Criteria

1. WHEN `maxNodes` + 1 is less than or equal to 32, THE Sizing_Function SHALL return `SMALL`.
2. WHEN `maxNodes` + 1 is greater than 32 and less than or equal to 512, THE Sizing_Function SHALL return `MEDIUM`.
3. WHEN `maxNodes` + 1 is greater than 512 and less than or equal to 2,048, THE Sizing_Function SHALL return `LARGE`.
4. WHEN `maxNodes` + 1 is greater than 2,048, THE Sizing_Function SHALL raise a `ValidationError` with a message indicating that the requested instance count exceeds the maximum PCS cluster capacity of 2,048 managed instances.
5. FOR ALL integer values of `maxNodes` from 1 to 2,047, THE Sizing_Function SHALL return a Controller_Size whose tier capacity is greater than or equal to `maxNodes` + 1. (Correctness property: the selected tier always has sufficient capacity.)
6. FOR ALL integer values of `maxNodes` from 1 to 2,047, THE Sizing_Function SHALL return the smallest Controller_Size whose tier capacity is greater than or equal to `maxNodes` + 1. (Correctness property: the function never over-provisions when a smaller tier suffices.)

### Requirement 2: Integration with Cluster Creation Workflow

**User Story:** As a platform operator, I want the cluster creation workflow to use the dynamically selected controller size, so that every new PCS cluster is created with the correct capacity for its workload.

#### Acceptance Criteria

1. WHEN `create_pcs_cluster` is called, THE Cluster_Creation_Handler SHALL read the `maxNodes` value from the event dict (defaulting to 10 if absent) and pass it to the Sizing_Function.
2. WHEN the Sizing_Function returns a valid Controller_Size, THE Cluster_Creation_Handler SHALL pass that size to `pcs_client.create_cluster()` as the `size` parameter.
3. WHEN the Sizing_Function raises a `ValidationError`, THE Cluster_Creation_Handler SHALL propagate the error without creating any PCS cluster resources.
4. THE Cluster_Creation_Handler SHALL no longer use a hardcoded `size` value.

### Requirement 3: Input Validation for maxNodes

**User Story:** As a platform operator, I want invalid `maxNodes` values to be rejected early, so that the cluster creation workflow fails fast with a clear error instead of creating a misconfigured cluster.

#### Acceptance Criteria

1. WHEN `maxNodes` is less than 1, THE Sizing_Function SHALL raise a `ValidationError` with a message indicating that `maxNodes` must be at least 1.
2. WHEN `maxNodes` is not an integer, THE Sizing_Function SHALL raise a `ValidationError` with a message indicating that `maxNodes` must be an integer.
3. WHEN `maxNodes` is greater than 2,047, THE Sizing_Function SHALL raise a `ValidationError` with a message indicating that the total managed instance count (`maxNodes` + 1 login node) exceeds the PCS maximum of 2,048.

### Requirement 4: Sizing Function as Pure, Testable Unit

**User Story:** As a developer, I want the sizing logic to be a standalone pure function, so that it can be tested independently of the AWS PCS API and reused in other contexts (e.g., cost estimation, validation endpoints).

#### Acceptance Criteria

1. THE Sizing_Function SHALL be a pure function that takes `maxNodes` as input and returns a Controller_Size string or raises a `ValidationError`.
2. THE Sizing_Function SHALL not call any AWS APIs or depend on any external state.
3. FOR ALL valid `maxNodes` values, calling the Sizing_Function twice with the same input SHALL return the same result. (Correctness property: determinism / idempotence.)
4. FOR ALL `maxNodes` values where `maxNodes` + 1 equals a tier boundary (31, 511, 2,047), THE Sizing_Function SHALL return the tier whose capacity matches that boundary exactly. (Edge case: boundary values select the current tier, not the next one.)

### Requirement 5: Documentation Update

**User Story:** As a platform administrator, I want the documentation to describe the controller sizing behavior, so that I understand how `maxNodes` affects the PCS cluster tier and its limits.

#### Acceptance Criteria

1. THE Admin_Documentation SHALL describe the three PCS controller size tiers (SMALL, MEDIUM, LARGE) and their instance and job limits.
2. THE Admin_Documentation SHALL explain that the controller size is automatically selected based on `maxNodes` + 1 (login node).
3. THE Admin_Documentation SHALL state that the controller size cannot be changed after cluster creation.
4. THE Admin_Documentation SHALL document the maximum supported `maxNodes` value (2,047) and the error returned when it is exceeded.
