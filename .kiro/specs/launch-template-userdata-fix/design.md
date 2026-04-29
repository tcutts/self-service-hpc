# Launch Template UserData Fix — Bugfix Design

## Overview

Three related defects in the cluster creation pipeline prevent EC2 instances from being properly provisioned at boot time. First, `create_launch_templates()` in `cluster_creation.py` creates launch templates with only `SecurityGroupIds` and `ImageId` — no `UserData` field — so instances boot without executing any provisioning script. Second, `generate_user_data_script()` in `posix_provisioning.py` has no EFS mount logic and does not accept an `efs_filesystem_id` parameter, so even if user data were injected, the EFS home directory at `/home` would never be mounted. Third, manually entered AMI IDs are never validated against EC2 `DescribeImages`, causing late failures during `CreateComputeNodeGroup` instead of failing fast with a clear error.

The fix moves user data generation before launch template creation, adds EFS mount commands to the generated script, base64-encodes the script into the `UserData` field of `LaunchTemplateData`, removes redundant `generate_user_data_script()` calls from node group creation, and adds AMI validation at both the template management API layer and the launch template creation step.

## Glossary

- **Bug_Condition (C)**: The set of conditions under which the three defects manifest — launch templates missing UserData, user data scripts missing EFS mount commands, and AMI IDs not validated before use
- **Property (P)**: The desired correct behavior — launch templates contain base64-encoded user data with EFS mounts, and invalid AMIs are rejected early
- **Preservation**: Existing behaviors that must remain unchanged — security group assignment, image ID selection, template tagging, existing storage mount logic, POSIX user creation, and the parallel provisioning workflow structure
- **`create_launch_templates()`**: Function in `cluster_creation.py` that creates login and compute EC2 launch templates for a cluster
- **`generate_user_data_script()`**: Function in `posix_provisioning.py` that generates a bash provisioning script for EC2 user data
- **`_validate_template_fields()`**: Function in `templates.py` that validates template fields during create/update operations
- **`efsFileSystemId`**: The EFS filesystem ID from the project infrastructure, passed through the Step Functions event payload

## Bug Details

### Bug Condition

The bug manifests across three related defects in the cluster creation pipeline. Defect 1: `create_launch_templates()` builds `LaunchTemplateData` with only `SecurityGroupIds` and `ImageId`, never setting `UserData`. Defect 2: `generate_user_data_script()` does not accept an `efs_filesystem_id` parameter and generates no EFS mount commands. Defect 3: AMI IDs from manual entry are only checked for non-emptiness, not validated against EC2.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type ClusterCreationEvent
  OUTPUT: boolean

  -- Defect 1: Launch template has no UserData
  LET launchTemplateData = buildLaunchTemplateData(input)
  LET missingUserData = "UserData" NOT IN launchTemplateData

  -- Defect 2: EFS mount missing from user data script
  LET userDataScript = generate_user_data_script(input)
  LET missingEfsMount = "mount -t efs" NOT IN userDataScript
                        AND input.efsFileSystemId IS NOT EMPTY

  -- Defect 3: AMI not validated
  LET amiId = input.amiId OR input.loginAmiId
  LET amiNotValidated = amiId IS NOT EMPTY
                        AND amiId NOT VALIDATED via ec2:DescribeImages

  RETURN missingUserData OR missingEfsMount OR amiNotValidated
END FUNCTION
```

### Examples

- **Defect 1 — Missing UserData**: A cluster is created with `templateId="cpu-general"`. `create_launch_templates()` creates two launch templates with `LaunchTemplateData={"SecurityGroupIds": ["sg-xxx"], "ImageId": "ami-xxx"}`. Expected: `UserData` field present with base64-encoded provisioning script. Actual: No `UserData` field — instances boot without provisioning.
- **Defect 2 — Missing EFS mount**: `generate_user_data_script()` is called with `project_id="genomics-team"` and the project has `efsFileSystemId="fs-abc123"`. Expected: Script contains `mount -t efs fs-abc123 /home`. Actual: No EFS mount commands in the script; `/home` is never mounted from EFS.
- **Defect 3 — Invalid AMI accepted**: A user creates a template with `amiId="ami-doesnotexist"`. Expected: Backend rejects with a validation error. Actual: Template is saved; cluster creation later fails at `CreateComputeNodeGroup` with "AWS PCS can't find an AMI you specified".
- **Edge case — Auto-detected AMI**: A user clicks "Auto-detect AMI" which calls `get_latest_pcs_ami()`. This AMI is already validated as `available` by the lookup function, so no additional validation is needed.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `create_launch_templates()` SHALL continue to include the correct `SecurityGroupIds` (head node SG for login, compute node SG for compute) in each template
- `create_launch_templates()` SHALL continue to include the correct `ImageId` (login AMI for login template, compute AMI for compute template) in each template
- `create_launch_templates()` SHALL continue to tag templates with project and cluster resource tags
- When a launch template with the same name already exists, the system SHALL continue to adopt the existing template
- `generate_user_data_script()` SHALL continue to generate Mountpoint for S3 mount commands when `storage_mode="mountpoint"`
- `generate_user_data_script()` SHALL continue to generate FSx for Lustre mount commands when `storage_mode="lustre"`
- `generate_user_data_script()` SHALL continue to generate POSIX user creation, generic account disabling, PAM exec logging, and CloudWatch agent commands
- `generate_user_data_script()` SHALL continue to omit storage mount commands when no storage mode is specified
- The cluster creation Step Functions workflow SHALL continue to run launch template creation, storage provisioning, and PCS cluster creation in parallel via the `ParallelProvision` state
- Auto-detected AMIs from `get_latest_pcs_ami()` SHALL continue to work without additional validation since they are already verified as `available`

**Scope:**
All inputs that do NOT involve launch template UserData generation, EFS mount logic, or AMI validation should be completely unaffected by this fix. This includes:
- PCS cluster creation and status polling
- FSx filesystem creation and data repository associations
- IAM role and instance profile lifecycle
- Queue creation and resource tagging
- Cluster destruction workflow
- Template CRUD operations (except AMI validation addition)

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **UserData never set in launch template**: `create_launch_templates()` (line ~620 of `cluster_creation.py`) builds `lt_data` with only `SecurityGroupIds` and `ImageId`. The `generate_user_data_script()` call happens later in `create_login_node_group()` and `create_compute_node_group()`, but its output is stored in the event payload as `userDataScript` and never fed back into the launch template. The launch template creation runs in the `ParallelProvision` branch concurrently with storage provisioning, so the user data must be generated before the parallel state begins.

2. **EFS mount logic absent from script generator**: `generate_user_data_script()` in `posix_provisioning.py` has parameters for S3 Mountpoint and FSx for Lustre storage modes, but no `efs_filesystem_id` parameter and no EFS mount command generation. The CDK stack (`ProjectInfrastructureStack`) creates the EFS filesystem and security groups with NFS port 2049 correctly, but the user data script never uses them.

3. **AMI validation missing**: `_validate_template_fields()` in `templates.py` checks that `ami_id` is a non-empty string but never calls `ec2_client.describe_images()` to verify the AMI exists and is available. Similarly, `create_launch_templates()` uses the AMI ID directly without validation. The `ec2:DescribeImages` permission is already granted to the template management Lambda but not to the cluster creation step Lambda.

4. **Ordering constraint**: The launch template creation step runs inside the `ParallelProvision` state (branch 2: instance profiles → launch templates). User data generation requires DynamoDB access to fetch project members and POSIX identities, plus knowledge of the storage mode and EFS filesystem ID. Since storage provisioning runs in branch 0 of the parallel state, the EFS filesystem ID is available in the event payload before the parallel state begins (it comes from the project infrastructure, not from FSx creation). The user data can therefore be generated before the parallel state, or within the launch template creation step itself.

## Correctness Properties

Property 1: Bug Condition — Launch Templates Contain Base64-Encoded UserData

_For any_ cluster creation event where `create_launch_templates()` is called with a valid project ID, users table, projects table, and EFS filesystem ID, the fixed function SHALL include a `UserData` field in `LaunchTemplateData` containing a base64-encoded MIME multipart script that includes POSIX user provisioning commands, EFS mount commands for `/home`, and storage mount commands appropriate to the storage mode.

**Validates: Requirements 2.1, 2.2, 2.7**

Property 2: Bug Condition — EFS Mount Commands Present in User Data Script

_For any_ call to `generate_user_data_script()` where a non-empty `efs_filesystem_id` is provided, the fixed function SHALL generate EFS mount commands that install `amazon-efs-utils` (if needed), create the `/home` mount point, add an fstab entry, and mount the EFS filesystem at `/home`.

**Validates: Requirements 2.4, 2.6**

Property 3: Bug Condition — AMI Validated Before Use

_For any_ template create/update request where `ami_id` or `login_ami_id` is provided, and for any `create_launch_templates()` call, the fixed code SHALL call EC2 `DescribeImages` to verify the AMI exists in the current region and has state `available`, rejecting the request with a validation error if it does not.

**Validates: Requirements 2.8, 2.9**

Property 4: Preservation — Existing Launch Template Fields Unchanged

_For any_ cluster creation event, the fixed `create_launch_templates()` function SHALL produce launch templates that contain the same `SecurityGroupIds`, `ImageId`, and tags as the original function, preserving all existing launch template configuration.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Property 5: Preservation — Existing User Data Script Content Unchanged

_For any_ call to `generate_user_data_script()` with the same parameters as before the fix (excluding the new `efs_filesystem_id` parameter), the fixed function SHALL produce a script containing the same POSIX user creation commands, generic account disabling commands, PAM exec logging commands, CloudWatch agent commands, and storage mount commands as the original function.

**Validates: Requirements 3.5, 3.6, 3.7, 3.8**

Property 6: Preservation — Workflow Structure Unchanged

_For any_ cluster creation workflow execution, the fixed code SHALL continue to run launch template creation, storage provisioning, and PCS cluster creation in parallel via the `ParallelProvision` state, and SHALL NOT call `generate_user_data_script()` during `create_login_node_group()` or `create_compute_node_group()`.

**Validates: Requirements 2.3, 3.9**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lambda/cluster_operations/posix_provisioning.py`

**Function**: `generate_user_data_script()`

**Specific Changes**:
1. **Add `efs_filesystem_id` parameter**: Add a new optional `efs_filesystem_id: str = ""` parameter to `generate_user_data_script()`
2. **Add `generate_efs_mount_commands()` function**: Create a new function that generates bash commands to install `amazon-efs-utils`, create `/home` mount point, add an fstab entry for the EFS filesystem, and run `mount -a -t efs`. This should be placed before POSIX user creation in the script so `/home` is available when user home directories are created.
3. **Insert EFS mount commands into script**: In `generate_user_data_script()`, if `efs_filesystem_id` is non-empty, insert the EFS mount commands after the shebang/header and before the POSIX user creation section
4. **Use MIME multipart format**: Wrap the script in MIME multipart format per PCS documentation requirements, with `Content-Type: multipart/mixed` header

---

**File**: `lambda/cluster_operations/cluster_creation.py`

**Function**: `create_launch_templates()`

**Specific Changes**:
1. **Generate user data before creating templates**: Call `generate_user_data_script()` within `create_launch_templates()`, passing the `efs_filesystem_id` from the event payload along with existing parameters (project_id, storage_mode, etc.)
2. **Base64-encode and set UserData**: Base64-encode the generated script and add it as the `UserData` field in `LaunchTemplateData` for both login and compute templates
3. **Add AMI validation**: Call `validate_ami_available()` for both `ami_id` and `login_ami_id` before creating the launch templates, failing fast with a clear error

**Function**: `create_login_node_group()`

**Specific Changes**:
1. **Remove redundant `generate_user_data_script()` call**: Remove the call to `generate_user_data_script()` and the `userDataScript` assignment since user data is now embedded in the launch template

**Function**: `create_compute_node_group()`

**Specific Changes**:
1. **Remove redundant `generate_user_data_script()` call**: Remove the call to `generate_user_data_script()` and the related logging since user data is now embedded in the launch template

---

**File**: `lambda/template_management/templates.py`

**Function**: `_validate_template_fields()`

**Specific Changes**:
1. **Add AMI validation**: After the existing non-empty string check for `ami_id`, call `validate_ami_available(ami_id)` to verify the AMI exists and is available via EC2 `DescribeImages`

**New Function**: `validate_ami_available()`

**Specific Changes**:
1. **Create validation function**: Add a new function that calls `ec2_client.describe_images(ImageIds=[ami_id])` and checks that the AMI exists and has state `available`. Raise `ValidationError` if not found or not available.

---

**File**: `lib/constructs/cluster-operations.ts`

**Specific Changes**:
1. **Add `ec2:DescribeImages` permission**: Add `ec2:DescribeImages` to the cluster creation step Lambda's EC2 policy statement (it already has `ec2:DescribeInstanceTypes` and related permissions, so this is a natural addition)

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bugs on unfixed code, then verify the fixes work correctly and preserve existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the three defects BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write unit tests that exercise `create_launch_templates()`, `generate_user_data_script()`, and `_validate_template_fields()` on the UNFIXED code to observe the missing behaviors.

**Test Cases**:
1. **Missing UserData Test**: Call `create_launch_templates()` with a valid event and assert that the `LaunchTemplateData` passed to `ec2_client.create_launch_template()` contains a `UserData` field (will fail on unfixed code)
2. **Missing EFS Mount Test**: Call `generate_user_data_script()` with a valid `efs_filesystem_id` and assert the output contains `mount -t efs` (will fail on unfixed code — function doesn't accept the parameter)
3. **AMI Not Validated Test**: Call `_validate_template_fields()` with `ami_id="ami-doesnotexist"` and assert it raises `ValidationError` (will fail on unfixed code — only checks non-empty string)
4. **Redundant UserData Generation Test**: Inspect `create_login_node_group()` and `create_compute_node_group()` to confirm they call `generate_user_data_script()` but don't inject the result into the launch template (will confirm the root cause)

**Expected Counterexamples**:
- `create_launch_templates()` creates templates with `LaunchTemplateData` containing no `UserData` key
- `generate_user_data_script()` raises `TypeError` when called with `efs_filesystem_id` parameter (unexpected keyword argument)
- `_validate_template_fields()` accepts any non-empty AMI string without EC2 API validation

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed functions produce the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := create_launch_templates_fixed(input)
  ASSERT "UserData" IN result.LaunchTemplateData
  ASSERT base64_decode(result.LaunchTemplateData.UserData) CONTAINS "mount -t efs"
  ASSERT base64_decode(result.LaunchTemplateData.UserData) CONTAINS "useradd"

  script := generate_user_data_script_fixed(input)
  ASSERT script CONTAINS "amazon-efs-utils"
  ASSERT script CONTAINS "mount -t efs" + input.efsFileSystemId

  ASSERT validate_ami_available(invalid_ami) RAISES ValidationError
  ASSERT validate_ami_available(valid_ami) RETURNS without error
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT create_launch_templates_fixed(input).SecurityGroupIds
         = create_launch_templates_original(input).SecurityGroupIds
  ASSERT create_launch_templates_fixed(input).ImageId
         = create_launch_templates_original(input).ImageId

  ASSERT generate_user_data_script_fixed(input, efs_filesystem_id="")
         CONTAINS same POSIX commands AS generate_user_data_script_original(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many combinations of storage modes, user lists, and template configurations automatically
- It catches edge cases in script generation that manual unit tests might miss
- It provides strong guarantees that existing POSIX provisioning, storage mounts, and launch template fields are unchanged

**Test Plan**: Observe behavior on UNFIXED code first for non-bug inputs (e.g., existing storage mount generation, POSIX user creation), then write property-based tests capturing that behavior.

**Test Cases**:
1. **SecurityGroupIds Preservation**: Verify that for any valid event, the fixed `create_launch_templates()` passes the same security group IDs as the original
2. **ImageId Preservation**: Verify that for any valid event, the fixed `create_launch_templates()` passes the same AMI IDs as the original
3. **POSIX Script Preservation**: Verify that for any valid set of users and storage modes, the fixed `generate_user_data_script()` produces the same POSIX user creation, generic account disabling, and storage mount commands as the original
4. **Template Tag Preservation**: Verify that launch template tag specifications are unchanged

### Unit Tests

- Test `generate_efs_mount_commands()` produces correct install, mkdir, fstab, and mount commands
- Test `generate_user_data_script()` with `efs_filesystem_id` includes EFS mount section
- Test `generate_user_data_script()` without `efs_filesystem_id` omits EFS mount section
- Test `create_launch_templates()` includes base64-encoded `UserData` in `LaunchTemplateData`
- Test `create_launch_templates()` calls `validate_ami_available()` before creating templates
- Test `validate_ami_available()` with valid AMI (state=available) succeeds
- Test `validate_ami_available()` with non-existent AMI raises `ValidationError`
- Test `validate_ami_available()` with unavailable AMI (state=deregistered) raises `ValidationError`
- Test `_validate_template_fields()` calls AMI validation for `ami_id`
- Test `create_login_node_group()` no longer calls `generate_user_data_script()`
- Test `create_compute_node_group()` no longer calls `generate_user_data_script()`
- Test MIME multipart format wrapping of user data script

### Property-Based Tests

- Generate random combinations of project members (0 to N users with valid POSIX UIDs/GIDs) and storage modes, verify the fixed `generate_user_data_script()` produces scripts containing the same POSIX and storage commands as the original when `efs_filesystem_id` is empty
- Generate random valid EFS filesystem IDs and verify the script always contains the correct mount commands
- Generate random event payloads with varying security groups and AMI IDs, verify `create_launch_templates()` preserves `SecurityGroupIds` and `ImageId` while adding `UserData`

### Integration Tests

- Test full cluster creation event flow: verify launch templates created with UserData containing EFS mount and POSIX provisioning commands
- Test template creation with invalid AMI ID is rejected at the API layer
- Test template creation with valid AMI ID succeeds
- Test that the `ParallelProvision` state machine branch structure is unchanged (launch templates still created in branch 2)
