# PCS Single-Subnet Fix — Bugfix Design

## Overview

The `create_pcs_cluster` function in `lambda/cluster_operations/cluster_creation.py` passes the full `private_subnet_ids` list to the PCS `CreateCluster` API's `networking.subnetIds` parameter. The PCS `CreateCluster` API requires exactly one subnet, so any VPC with multiple private subnets (the default — CDK creates one per AZ with `maxAzs: 2`) causes a `ValidationException`. The fix is to slice the list to `private_subnet_ids[:1]` so only the first subnet is passed, while leaving all other functions and parameters untouched.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — `create_pcs_cluster` is called with a `privateSubnetIds` list containing more than one subnet
- **Property (P)**: The desired behavior — `create_pcs_cluster` passes exactly one subnet (the first) to the PCS `CreateCluster` API and the cluster is created successfully
- **Preservation**: Existing behavior that must remain unchanged — `create_compute_node_group`, `create_login_node_group`, and `create_fsx_filesystem` subnet handling; retry logic; all non-subnet `CreateCluster` parameters
- **`create_pcs_cluster`**: The function in `lambda/cluster_operations/cluster_creation.py` (line 474) that calls the PCS `CreateCluster` API to provision a Slurm cluster
- **`private_subnet_ids`**: The list of private subnet IDs extracted from `event["privateSubnetIds"]`, typically containing one subnet per availability zone (2 subnets for `maxAzs: 2`)
- **PCS**: AWS Parallel Computing Service — the managed HPC service used for Slurm cluster provisioning

## Bug Details

### Bug Condition

The bug manifests when `create_pcs_cluster` is called with a `privateSubnetIds` list containing more than one subnet. The function passes the entire list to `networking.subnetIds` in the `CreateCluster` API call, but PCS only accepts exactly one subnet for cluster creation. This causes a `ValidationException` that is caught and re-raised as an `InternalError`, failing the cluster creation state machine.

**Formal Specification:**
```
FUNCTION isBugCondition(event)
  INPUT: event of type dict with key "privateSubnetIds"
  OUTPUT: boolean

  private_subnet_ids := event["privateSubnetIds"]
  RETURN LENGTH(private_subnet_ids) > 1
END FUNCTION
```

### Examples

- **2-AZ VPC (typical)**: `privateSubnetIds = ["subnet-aaa", "subnet-bbb"]` → PCS API receives `subnetIds: ["subnet-aaa", "subnet-bbb"]` → `ValidationException: "You can only specify 1 subnet when you create a cluster"` → `InternalError` raised
- **3-AZ VPC**: `privateSubnetIds = ["subnet-aaa", "subnet-bbb", "subnet-ccc"]` → same `ValidationException`
- **1-AZ VPC (not triggered)**: `privateSubnetIds = ["subnet-aaa"]` → PCS API receives `subnetIds: ["subnet-aaa"]` → cluster created successfully (bug condition does not hold)
- **Edge case — empty list**: `privateSubnetIds = []` → PCS API receives `subnetIds: []` → would fail with a different validation error (not related to this bug)

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `create_compute_node_group` must continue to pass the full `private_subnet_ids` list to `CreateComputeNodeGroup` (PCS node group APIs accept multiple subnets for multi-AZ placement)
- `create_login_node_group` must continue to pass the full `public_subnet_ids` list to `CreateComputeNodeGroup` for login node placement
- `create_fsx_filesystem` must continue to use `private_subnet_ids[0]` for single-subnet FSx filesystem creation
- `create_pcs_cluster` retry logic for `ConflictException` must remain unchanged (exponential backoff, up to `_PCS_MAX_RETRIES` attempts)
- All non-subnet parameters passed to `CreateCluster` must remain identical: `clusterName`, `scheduler` (SLURM 24.11), `size` (SMALL), `securityGroupIds`, `slurmConfiguration`, and `tags`

**Scope:**
All inputs that do NOT involve the `networking.subnetIds` parameter of the `CreateCluster` API call should be completely unaffected by this fix. This includes:
- All other cluster creation step functions (FSx, node groups, queue, tagging, recording)
- Mouse/UI interactions that trigger cluster creation (the event payload is unchanged)
- Error handling paths for non-subnet-related failures
- The structure and content of the returned event dict (aside from now succeeding instead of failing)

## Hypothesized Root Cause

Based on the bug description and code analysis, the root cause is straightforward:

1. **Incorrect parameter value at line 504**: The `create_pcs_cluster` function passes `private_subnet_ids` (the full list) to `networking.subnetIds` instead of `private_subnet_ids[:1]` (a single-element list). This is the sole root cause.
   - Line 504: `"subnetIds": private_subnet_ids,` should be `"subnetIds": private_subnet_ids[:1],`
   - The developer likely copied the pattern from `create_compute_node_group` (which correctly passes all subnets) without accounting for the `CreateCluster` API's single-subnet constraint

2. **No other contributing factors**: The VPC configuration (`maxAzs: 2`) is correct — multiple AZs are needed for compute node group placement. The bug is purely in how `create_pcs_cluster` consumes the subnet list.

## Correctness Properties

Property 1: Bug Condition — CreateCluster receives exactly one subnet

_For any_ event where `privateSubnetIds` contains one or more subnets, the fixed `create_pcs_cluster` function SHALL pass exactly one subnet (the first element) to the PCS `CreateCluster` API's `networking.subnetIds` parameter, as a single-element list `private_subnet_ids[:1]`.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation — Other functions and parameters unchanged

_For any_ event passed through the cluster creation workflow, the fixed code SHALL produce exactly the same behavior as the original code for all functions other than `create_pcs_cluster`, and within `create_pcs_cluster` all parameters other than `networking.subnetIds` SHALL be passed identically to the PCS `CreateCluster` API.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lambda/cluster_operations/cluster_creation.py`

**Function**: `create_pcs_cluster` (line 474)

**Specific Changes**:
1. **Slice subnet list to single element (line ~504)**: Change `"subnetIds": private_subnet_ids,` to `"subnetIds": private_subnet_ids[:1],` inside the `networking` dict passed to `pcs_client.create_cluster()`. This passes only the first subnet while keeping the value as a list (which the API expects).

No other files or functions require changes. The fix is a single-character edit (`private_subnet_ids` → `private_subnet_ids[:1]`).

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior. All tests use mocked AWS clients (no real API calls).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm that the unfixed code passes multiple subnets to the `CreateCluster` API.

**Test Plan**: Write tests that mock `pcs_client.create_cluster` and capture the arguments it receives. Call `create_pcs_cluster` with multi-subnet events on the UNFIXED code to observe that all subnets are passed.

**Test Cases**:
1. **Two-subnet event**: Call with `privateSubnetIds = ["subnet-a", "subnet-b"]` and verify the mock receives both subnets (will demonstrate the bug on unfixed code)
2. **Three-subnet event**: Call with `privateSubnetIds = ["subnet-a", "subnet-b", "subnet-c"]` and verify the mock receives all three subnets (will demonstrate the bug on unfixed code)

**Expected Counterexamples**:
- The `networking.subnetIds` argument to `create_cluster` contains more than one element
- This confirms the root cause: the full list is passed without slicing

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds (multi-subnet lists), the fixed function passes exactly one subnet to the API.

**Pseudocode:**
```
FOR ALL event WHERE isBugCondition(event) DO
  result := create_pcs_cluster_fixed(event)
  captured_args := mock_pcs_client.create_cluster.call_args
  subnet_ids := captured_args["networking"]["subnetIds"]
  ASSERT LENGTH(subnet_ids) == 1
  ASSERT subnet_ids[0] == event["privateSubnetIds"][0]
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold (single-subnet lists), the fixed function produces the same result as the original function. Also verify that all non-subnet parameters and all other functions are unchanged.

**Pseudocode:**
```
FOR ALL event WHERE NOT isBugCondition(event) DO
  ASSERT create_pcs_cluster_original(event) == create_pcs_cluster_fixed(event)
END FOR

FOR ALL event DO
  captured_args := mock_pcs_client.create_cluster.call_args
  ASSERT captured_args["clusterName"] == event["clusterName"]
  ASSERT captured_args["scheduler"] == {"type": "SLURM", "version": "24.11"}
  ASSERT captured_args["size"] == "SMALL"
  ASSERT captured_args["networking"]["securityGroupIds"] == [event["securityGroupIds"]["computeNode"]]
END FOR
```

**Testing Approach**: Property-based testing is recommended for fix checking and preservation checking because:
- It generates many subnet list configurations automatically
- It catches edge cases like single-element lists, large lists, and lists with duplicate subnet IDs
- It provides strong guarantees that only the `subnetIds` parameter changed

**Test Plan**: Mock the PCS client, capture call arguments, and verify subnet slicing for generated events. Observe behavior on UNFIXED code first to confirm the bug, then verify the fix.

**Test Cases**:
1. **Subnet slicing preservation**: Generate random subnet lists of length 1+ and verify the API always receives exactly one subnet (the first)
2. **Non-subnet parameter preservation**: Generate random events and verify all parameters other than `subnetIds` are passed through unchanged
3. **Return value preservation**: Verify the returned event dict contains `pcsClusterId` and `pcsClusterArn` for all inputs
4. **ConflictException retry preservation**: Verify retry behavior is unchanged by the fix

### Unit Tests

- Test `create_pcs_cluster` with a 2-element subnet list passes only the first subnet after fix
- Test `create_pcs_cluster` with a 1-element subnet list still works correctly
- Test `create_pcs_cluster` ConflictException retry logic is unchanged
- Test `create_pcs_cluster` returns correct event keys on success
- Test `create_compute_node_group` still passes full `private_subnet_ids` list
- Test `create_login_node_group` still passes full `public_subnet_ids` list
- Test `create_fsx_filesystem` still uses `private_subnet_ids[0]`

### Property-Based Tests

- Generate random subnet ID lists (length 1–5) and verify `create_pcs_cluster` always passes exactly one subnet to the API
- Generate random events with varying subnet counts and verify all non-subnet `CreateCluster` parameters are identical
- Generate random events and verify `create_compute_node_group` passes the full subnet list unchanged

### Integration Tests

- Test full cluster creation workflow with mocked AWS clients and a 2-subnet VPC configuration
- Test that the state machine event payload flows correctly through all steps after the fix
- Test error handling when PCS returns non-ConflictException errors (unrelated to subnet count)
