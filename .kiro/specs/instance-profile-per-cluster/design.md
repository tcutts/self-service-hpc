# Instance Profile Per Cluster Bugfix Design

## Overview

The system currently creates a single IAM role and instance profile (`AWSPCS-{projectId}-node`) at the project level in `ProjectInfrastructureStack` (CDK). This profile is stored in the Projects DynamoDB table and shared across every cluster's login and compute node groups. This violates least-privilege and prevents per-cluster permission customisation. The fix moves instance profile creation from CDK (project deploy time) to the cluster creation Lambda workflow (cluster creation time), creating two dedicated profiles per cluster — one for login nodes and one for compute nodes — and cleaning them up during cluster destruction.

## Glossary

- **Bug_Condition (C)**: A cluster creation or destruction event where instance profiles are scoped to the project rather than to the individual cluster and node type
- **Property (P)**: Each cluster SHALL have its own dedicated login and compute instance profiles, created dynamically during cluster creation and deleted during cluster destruction
- **Preservation**: All non-IAM cluster creation behaviour (FSx, PCS cluster, node groups, queues, tagging, DynamoDB records), project deployment behaviour (VPC, EFS, S3, security groups, launch templates), and cluster destruction behaviour (FSx export, PCS resource deletion, DynamoDB status update) must remain unchanged
- **`create_login_node_group`**: Function in `lambda/cluster_operations/cluster_creation.py` that creates the PCS login node group, currently using `event.get("instanceProfileArn", "")` (the project-level profile)
- **`create_compute_node_group`**: Function in `lambda/cluster_operations/cluster_creation.py` that creates the PCS compute node group, currently using `event.get("instanceProfileArn", "")` (the project-level profile)
- **`ProjectInfrastructureStack`**: CDK stack in `lib/project-infrastructure-stack.ts` that currently creates the single project-level IAM role, instance profile, and outputs `InstanceProfileArn`
- **PCS naming requirement**: IAM roles used with PCS must have a name starting with `AWSPCS` or use the IAM path `/aws-pcs/`

## Bug Details

### Bug Condition

The bug manifests when a cluster is created or destroyed. During creation, the handler passes a single project-level `instanceProfileArn` (from the Projects DynamoDB table) to both the login and compute node groups. During destruction, no IAM cleanup occurs because the instance profile is owned by the CDK stack, not the cluster. This means all clusters in a project share the same IAM identity, and per-cluster permission customisation is impossible.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type ClusterLifecycleEvent (creation or destruction)
  OUTPUT: boolean

  IF input.eventType == "CREATION":
    RETURN input.loginInstanceProfileArn == input.projectLevelInstanceProfileArn
           AND input.computeInstanceProfileArn == input.projectLevelInstanceProfileArn
           AND input.loginInstanceProfileArn == input.computeInstanceProfileArn
  ELSE IF input.eventType == "DESTRUCTION":
    RETURN NOT iamResourcesCleanedUp(input.projectId, input.clusterName)
  END IF
END FUNCTION
```

### Examples

- **Cluster A and Cluster B in Project X**: Both clusters use `arn:aws:iam::123456789012:instance-profile/AWSPCS-projX-node`. Cluster A should not share IAM identity with Cluster B.
- **Login vs Compute in Cluster A**: Login node group and compute node group both use the same `AWSPCS-projX-node` profile. Login nodes may need different permissions than compute nodes.
- **Cluster destruction**: When Cluster A is destroyed, the instance profile `AWSPCS-projX-node` is NOT deleted because it belongs to the CDK stack. Orphaned permissions remain active.
- **Cluster recreation**: When a destroyed cluster is recreated, it reuses the stale project-level profile rather than getting a fresh, cluster-scoped profile.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Project deployment SHALL continue to create VPC, EFS, S3 bucket, security groups, launch templates, and CloudWatch log group via CDK
- Cluster creation SHALL continue to create FSx filesystem, PCS cluster, login node group, compute node group, queue, and apply resource tags in the same order
- Cluster destruction SHALL continue to export FSx data to S3, delete PCS resources (node groups, queue, cluster), delete the FSx filesystem, and mark the cluster as DESTROYED in DynamoDB
- Cluster recreation from DESTROYED state SHALL continue to work using the same creation workflow
- The handler.py payload structure SHALL continue to pass infrastructure details to the Step Functions execution
- Mouse/API interactions for listing, getting, and deleting clusters SHALL remain unchanged

**Scope:**
All inputs that do NOT involve IAM instance profile creation, usage, or cleanup should be completely unaffected by this fix. This includes:
- FSx filesystem creation and data repository associations
- PCS cluster creation and configuration
- PCS queue creation
- Resource tagging
- DynamoDB record management
- Budget breach checking
- Cluster name registration
- Template resolution
- SNS lifecycle notifications

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Instance profile created at wrong lifecycle stage**: The instance profile is created in `ProjectInfrastructureStack` (CDK, deploy-time) rather than during cluster creation (runtime). CDK resources are project-scoped by design, so a CDK-created instance profile is inherently project-scoped.

2. **Single profile shared across node types**: `create_login_node_group` and `create_compute_node_group` both read `event.get("instanceProfileArn", "")` — the same project-level ARN passed from `handler.py._lookup_project_infrastructure()`.

3. **No IAM cleanup in destruction workflow**: `cluster_destruction.py` has no step to delete IAM roles or instance profiles. The destruction workflow only handles FSx export, PCS resource deletion, FSx filesystem deletion, and DynamoDB status update.

4. **Project deploy stores a single ARN**: `project_deploy.py.extract_stack_outputs()` reads `InstanceProfileArn` from CloudFormation outputs and `record_infrastructure()` stores it as a single `instanceProfileArn` field in the Projects DynamoDB table.

## Correctness Properties

Property 1: Bug Condition - Per-Cluster Instance Profile Creation

_For any_ cluster creation event, the fixed cluster creation workflow SHALL create two dedicated IAM roles and instance profiles — `AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute` — and pass the respective ARNs to `create_login_node_group` and `create_compute_node_group`, such that no two clusters share the same instance profile and login/compute node types within a cluster have distinct profiles.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 2.7**

Property 2: Bug Condition - Per-Cluster Instance Profile Cleanup

_For any_ cluster destruction event, the fixed cluster destruction workflow SHALL delete the cluster-specific IAM instance profiles and roles (`AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`) as part of the cleanup, leaving no orphaned IAM resources.

**Validates: Requirements 2.5**

Property 3: Preservation - Non-IAM Cluster Creation Behavior

_For any_ cluster creation event, the fixed code SHALL produce the same FSx filesystem, PCS cluster, PCS node groups (aside from the instance profile ARN argument), PCS queue, resource tags, and DynamoDB records as the original code, preserving all non-IAM creation behavior.

**Validates: Requirements 3.1, 3.2, 3.4**

Property 4: Preservation - Non-IAM Cluster Destruction Behavior

_For any_ cluster destruction event, the fixed code SHALL produce the same FSx export, PCS resource deletion, FSx filesystem deletion, and DynamoDB status update as the original code, preserving all non-IAM destruction behavior.

**Validates: Requirements 3.3**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lib/project-infrastructure-stack.ts`

**Changes**:
1. **Remove project-level IAM role and instance profile**: Delete the `PcsNodeRole` IAM role, the `PcsInstanceProfile` CfnInstanceProfile, and the `InstanceProfileArn` CfnOutput. Remove the `pcsInstanceProfile` public property.
2. **Grant the cluster operations Lambda permission to manage IAM**: The Lambda execution role needs `iam:CreateRole`, `iam:DeleteRole`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:CreateInstanceProfile`, `iam:DeleteInstanceProfile`, `iam:AddRoleToInstanceProfile`, `iam:RemoveRoleFromInstanceProfile`, `iam:PassRole`, and `iam:GetInstanceProfile` permissions, scoped to resources matching `AWSPCS-*` to maintain least-privilege.

**File**: `lambda/cluster_operations/cluster_creation.py`

**Function**: New function `create_iam_resources` (new Step 2b, between budget check and FSx creation)

**Specific Changes**:
1. **Add `create_iam_resources` step function**: Creates two IAM roles (`AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`), attaches `pcs:RegisterComputeNodeGroupInstance` inline policy and `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy` managed policies, creates instance profiles, and adds roles to profiles. Adds `loginInstanceProfileArn` and `computeInstanceProfileArn` to the event.
2. **Add `wait_for_instance_profiles` step function**: Instance profiles can take a few seconds to propagate in IAM. This step polls `iam:GetInstanceProfile` until both profiles are available, with exponential backoff.
3. **Modify `create_login_node_group`**: Change `iamInstanceProfileArn=event.get("instanceProfileArn", "")` to `iamInstanceProfileArn=event.get("loginInstanceProfileArn", "")`.
4. **Modify `create_compute_node_group`**: Change `iamInstanceProfileArn=event.get("instanceProfileArn", "")` to `iamInstanceProfileArn=event.get("computeInstanceProfileArn", "")`.
5. **Update `TOTAL_STEPS` and `STEP_LABELS`**: Increment total steps and add labels for the new IAM steps.
6. **Update `_STEP_DISPATCH`**: Register the new step functions.
7. **Update `handle_creation_failure`**: Add IAM cleanup (delete instance profiles and roles) to the rollback handler.

**File**: `lambda/cluster_operations/cluster_destruction.py`

**Function**: New function `delete_iam_resources` (new Step 4b, after FSx deletion and before DynamoDB record update)

**Specific Changes**:
1. **Add `delete_iam_resources` step function**: Removes roles from instance profiles, deletes instance profiles, detaches managed policies, deletes inline policies, and deletes IAM roles. Uses best-effort approach (log and continue on failure) consistent with existing PCS cleanup pattern.
2. **Update `_STEP_DISPATCH`**: Register the new step function.

**File**: `lambda/cluster_operations/handler.py`

**Specific Changes**:
1. **Remove `instanceProfileArn` from the creation payload**: The `_handle_create_cluster` and `_handle_recreate_cluster` functions currently pass `infra["instanceProfileArn"]` to the Step Functions payload. Remove this field since instance profiles will be created dynamically.
2. **Optionally remove `instanceProfileArn` from `_lookup_project_infrastructure`**: The field is no longer needed for cluster creation, but can be left for backward compatibility during migration.

**File**: `lambda/project_management/project_deploy.py`

**Specific Changes**:
1. **Remove `instanceProfileArn` extraction and storage**: The `extract_stack_outputs` function reads `InstanceProfileArn` from CloudFormation outputs and `record_infrastructure` stores it. These references should be removed since the CDK stack will no longer output this value.

**File**: `docs/project-admin/cluster-management.md`

**Specific Changes**:
1. **Document per-cluster IAM**: Update documentation to reflect that each cluster now gets its own IAM roles and instance profiles, created automatically during cluster creation and cleaned up during destruction.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that exercise the cluster creation and destruction workflows and assert that instance profiles are cluster-scoped. Run these tests on the UNFIXED code to observe failures and understand the root cause.

**Test Cases**:
1. **Shared Profile Test**: Create two clusters in the same project and assert they receive different instance profile ARNs (will fail on unfixed code — both get the same project-level ARN)
2. **Login vs Compute Test**: Create a cluster and assert the login node group and compute node group receive different instance profile ARNs (will fail on unfixed code — both get the same ARN)
3. **Destruction Cleanup Test**: Destroy a cluster and assert the cluster-specific IAM resources are deleted (will fail on unfixed code — no IAM cleanup occurs)
4. **Profile Naming Test**: Create a cluster and assert the instance profile names follow `AWSPCS-{projectId}-{clusterName}-{login|compute}` pattern (will fail on unfixed code — name is `AWSPCS-{projectId}-node`)

**Expected Counterexamples**:
- `create_login_node_group` and `create_compute_node_group` both receive the same `instanceProfileArn` value from the event
- `cluster_destruction.py` has no IAM cleanup step — the `_STEP_DISPATCH` table contains no IAM-related entries
- Possible causes: project-level CDK creation, single ARN in event payload, missing destruction step

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  IF input.eventType == "CREATION":
    result := create_iam_resources_fixed(input)
    ASSERT result.loginInstanceProfileArn != result.computeInstanceProfileArn
    ASSERT result.loginInstanceProfileArn CONTAINS input.clusterName
    ASSERT result.computeInstanceProfileArn CONTAINS input.clusterName
    ASSERT roleHasPolicy(result.loginRoleName, "pcs:RegisterComputeNodeGroupInstance")
    ASSERT roleHasManagedPolicy(result.loginRoleName, "AmazonSSMManagedInstanceCore")
    ASSERT roleHasManagedPolicy(result.loginRoleName, "CloudWatchAgentServerPolicy")
  ELSE IF input.eventType == "DESTRUCTION":
    result := delete_iam_resources_fixed(input)
    ASSERT NOT instanceProfileExists(input.projectId, input.clusterName, "login")
    ASSERT NOT instanceProfileExists(input.projectId, input.clusterName, "compute")
  END IF
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT create_fsx_filesystem_original(input) = create_fsx_filesystem_fixed(input)
  ASSERT create_pcs_cluster_original(input) = create_pcs_cluster_fixed(input)
  ASSERT create_pcs_queue_original(input) = create_pcs_queue_fixed(input)
  ASSERT tag_resources_original(input) = tag_resources_fixed(input)
  ASSERT record_cluster_original(input) = record_cluster_fixed(input)
  ASSERT delete_pcs_resources_original(input) = delete_pcs_resources_fixed(input)
  ASSERT delete_fsx_filesystem_original(input) = delete_fsx_filesystem_fixed(input)
  ASSERT record_cluster_destroyed_original(input) = record_cluster_destroyed_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for non-IAM operations, then write property-based tests capturing that behavior.

**Test Cases**:
1. **FSx Creation Preservation**: Observe that FSx filesystem creation works correctly on unfixed code, then write test to verify this continues after fix
2. **PCS Cluster Creation Preservation**: Observe that PCS cluster creation works correctly on unfixed code, then write test to verify this continues after fix
3. **PCS Destruction Preservation**: Observe that PCS resource deletion works correctly on unfixed code, then write test to verify this continues after fix
4. **DynamoDB Record Preservation**: Observe that cluster DynamoDB records are written correctly on unfixed code, then write test to verify this continues after fix

### Unit Tests

- Test `create_iam_resources` creates two distinct roles and instance profiles with correct naming
- Test `create_iam_resources` attaches the correct policies (inline `pcs:RegisterComputeNodeGroupInstance`, managed `AmazonSSMManagedInstanceCore`, managed `CloudWatchAgentServerPolicy`)
- Test `create_iam_resources` role names start with `AWSPCS` (PCS naming requirement)
- Test `delete_iam_resources` removes instance profiles and roles in correct order
- Test `delete_iam_resources` handles already-deleted resources gracefully (idempotent)
- Test `create_login_node_group` uses `loginInstanceProfileArn` from event
- Test `create_compute_node_group` uses `computeInstanceProfileArn` from event
- Test `handle_creation_failure` cleans up IAM resources during rollback
- Test handler.py no longer passes `instanceProfileArn` in the Step Functions payload

### Property-Based Tests

- Generate random valid projectId/clusterName pairs and verify `create_iam_resources` always produces two distinct, correctly-named instance profiles with the required policies
- Generate random cluster destruction events and verify `delete_iam_resources` always attempts cleanup of both login and compute IAM resources
- Generate random cluster creation events and verify non-IAM steps (FSx, PCS, queue, tags, DynamoDB) produce identical results regardless of whether instance profiles are project-scoped or cluster-scoped

### Integration Tests

- Test full cluster creation flow with per-cluster instance profiles end-to-end
- Test cluster destruction flow including IAM cleanup end-to-end
- Test cluster recreation flow creates fresh instance profiles for the new cluster
- Test that two clusters in the same project get independent instance profiles
