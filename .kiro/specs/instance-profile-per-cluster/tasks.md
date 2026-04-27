# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Shared Instance Profile Across Clusters and Node Types
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to cluster creation events where `create_login_node_group` and `create_compute_node_group` are called
  - Create test file `test/lambda/test_property_instance_profile_per_cluster_bug.py`
  - Load `cluster_creation` module using `_load_module_from` pattern from conftest (same as existing PBT tests)
  - Generate random valid `projectId` (strategy: `proj-[a-z0-9]{4,10}`) and `clusterName` (strategy: `[a-z][a-z0-9\-]{2,20}`)
  - **Test A — Login and compute get distinct profiles**: For any cluster creation event, assert that `create_login_node_group` passes a different `iamInstanceProfileArn` than `create_compute_node_group`. On unfixed code both read `event.get("instanceProfileArn", "")` so they receive the same value — test FAILS
  - **Test B — Profile ARNs contain cluster name**: For any cluster creation event, assert that the `iamInstanceProfileArn` passed to `create_login_node_group` contains the cluster name and ends with `-login`, and the one passed to `create_compute_node_group` contains the cluster name and ends with `-compute`. On unfixed code the ARN is `AWSPCS-{projectId}-node` — test FAILS
  - Mock `pcs_client.create_compute_node_group` and inspect `iamInstanceProfileArn` kwarg in each call
  - Patch `_update_step_progress` and `generate_user_data_script` to avoid DynamoDB/side-effect calls
  - Use `@settings(max_examples=10, deadline=None)` to keep tests fast
  - Run test on UNFIXED code: `cd test/lambda && ../../.venv/bin/python3 -m pytest test_property_instance_profile_per_cluster_bug.py -x -v`
  - **EXPECTED OUTCOME**: Test FAILS (this is correct — it proves the bug exists)
  - Document counterexamples found (e.g., "both login and compute node groups receive identical instanceProfileArn='arn:...AWSPCS-proj-abc-node'")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-IAM Cluster Creation and Destruction Behavior
  - **IMPORTANT**: Follow observation-first methodology
  - Create test file `test/lambda/test_property_instance_profile_per_cluster_preservation.py`
  - Load `cluster_creation` and `cluster_destruction` modules using `_load_module_from` pattern from conftest
  - **Test A — FSx creation preserved**: For any valid event, `create_fsx_filesystem` passes the same `FileSystemType`, `StorageCapacity`, `SubnetIds`, `SecurityGroupIds`, and `LustreConfiguration` to the FSx API regardless of instance profile changes. Observe on unfixed code, then assert.
  - **Test B — PCS cluster creation preserved**: For any valid event, `create_pcs_cluster` passes the same `clusterName`, `scheduler`, `size`, `networking`, `slurmConfiguration`, and `tags` to the PCS API. Observe on unfixed code, then assert.
  - **Test C — PCS queue creation preserved**: For any valid event, `create_pcs_queue` passes the same `clusterIdentifier`, `queueName`, `computeNodeGroupConfigurations`, and `tags` to the PCS API. Observe on unfixed code, then assert.
  - **Test D — Non-IAM node group params preserved**: For any valid event, `create_login_node_group` and `create_compute_node_group` pass the same `subnetIds`, `purchaseOption`, `scalingConfiguration`, `instanceConfigs`, `customLaunchTemplate`, and `tags` to the PCS API (everything except `iamInstanceProfileArn`). Observe on unfixed code, then assert.
  - **Test E — Cluster destruction PCS cleanup preserved**: For any valid destruction event, `delete_pcs_resources` calls `delete_compute_node_group`, `delete_queue`, and `delete_cluster` in the same order with the same arguments. Observe on unfixed code, then assert.
  - **Test F — DynamoDB record_cluster_destroyed preserved**: For any valid destruction event, `record_cluster_destroyed` sets status to `DESTROYED` and writes `destroyedAt`. Observe on unfixed code, then assert.
  - Use `@settings(max_examples=10, deadline=None)` to keep tests fast
  - Run tests on UNFIXED code: `cd test/lambda && ../../.venv/bin/python3 -m pytest test_property_instance_profile_per_cluster_preservation.py -x -v`
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix for shared instance profile across clusters and node types

  - [x] 3.1 Remove project-level IAM role, instance profile, and InstanceProfileArn output from CDK
    - In `lib/project-infrastructure-stack.ts`, delete the `PcsNodeRole` IAM role construct, the `PcsInstanceProfile` CfnInstanceProfile construct, and the `InstanceProfileArn` CfnOutput
    - Remove the `pcsInstanceProfile` public property from the class
    - Leave all other resources (VPC, EFS, S3, security groups, launch templates, log group) unchanged
    - _Bug_Condition: isBugCondition(input) where instance profile is created at project level in CDK_
    - _Expected_Behavior: No project-level instance profile; profiles created per-cluster at runtime_
    - _Preservation: VPC, EFS, S3, security groups, launch templates, log group unchanged_
    - _Requirements: 1.1, 2.1, 2.2, 3.1_

  - [x] 3.2 Add `create_iam_resources` step to cluster creation workflow
    - In `lambda/cluster_operations/cluster_creation.py`, add a new function `create_iam_resources(event)` that:
      - Creates two IAM roles: `AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute` with `ec2.amazonaws.com` as the trusted principal
      - Adds inline policy granting `pcs:RegisterComputeNodeGroupInstance` on `*`
      - Attaches `AmazonSSMManagedInstanceCore` and `CloudWatchAgentServerPolicy` managed policies
      - Creates two instance profiles with the same names as the roles
      - Adds each role to its respective instance profile
      - Returns event augmented with `loginInstanceProfileArn` and `computeInstanceProfileArn`
    - Register `create_iam_resources` in `_STEP_DISPATCH`
    - Update `TOTAL_STEPS` from 10 to 12 and update `STEP_LABELS` to include the new IAM steps
    - _Bug_Condition: isBugCondition(input) where single project-level profile is used_
    - _Expected_Behavior: Two distinct cluster-scoped profiles created per cluster_
    - _Preservation: All other creation steps unchanged_
    - _Requirements: 2.1, 2.2, 2.6, 2.7_

  - [x] 3.3 Add `wait_for_instance_profiles` step to cluster creation workflow
    - In `lambda/cluster_operations/cluster_creation.py`, add a new function `wait_for_instance_profiles(event)` that:
      - Calls `iam.get_instance_profile` for both login and compute profiles
      - Returns `event` with `instanceProfilesReady: True/False`
      - Handles `NoSuchEntity` by returning `instanceProfilesReady: False`
    - Register `wait_for_instance_profiles` in `_STEP_DISPATCH`
    - _Bug_Condition: Instance profiles need propagation time after creation_
    - _Expected_Behavior: Step Functions waits until profiles are available_
    - _Requirements: 2.1, 2.2_

  - [x] 3.4 Modify `create_login_node_group` to use `loginInstanceProfileArn`
    - Change `iamInstanceProfileArn=event.get("instanceProfileArn", "")` to `iamInstanceProfileArn=event.get("loginInstanceProfileArn", "")`
    - _Bug_Condition: Login node group uses project-level profile_
    - _Expected_Behavior: Login node group uses cluster-specific login profile_
    - _Requirements: 2.3_

  - [x] 3.5 Modify `create_compute_node_group` to use `computeInstanceProfileArn`
    - Change `iamInstanceProfileArn=event.get("instanceProfileArn", "")` to `iamInstanceProfileArn=event.get("computeInstanceProfileArn", "")`
    - _Bug_Condition: Compute node group uses project-level profile_
    - _Expected_Behavior: Compute node group uses cluster-specific compute profile_
    - _Requirements: 2.4_

  - [x] 3.6 Add `delete_iam_resources` step to cluster destruction workflow
    - In `lambda/cluster_operations/cluster_destruction.py`, add a new function `delete_iam_resources(event)` that:
      - For each of login and compute: removes role from instance profile, deletes instance profile, detaches managed policies, deletes inline policies, deletes IAM role
      - Uses best-effort approach (log and continue on `NoSuchEntity` or `ClientError`) consistent with existing PCS cleanup pattern
      - Derives resource names from `projectId` and `clusterName`: `AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`
    - Register `delete_iam_resources` in `_STEP_DISPATCH`
    - _Bug_Condition: No IAM cleanup during cluster destruction_
    - _Expected_Behavior: Cluster-specific IAM resources deleted during destruction_
    - _Preservation: FSx export, PCS deletion, DynamoDB update unchanged_
    - _Requirements: 2.5_

  - [x] 3.7 Update `handler.py` to stop passing `instanceProfileArn`
    - In `lambda/cluster_operations/handler.py`, remove `"instanceProfileArn": infra["instanceProfileArn"]` from the Step Functions payload in both `_handle_create_cluster` and `_handle_recreate_cluster`
    - Optionally remove `instanceProfileArn` from `_lookup_project_infrastructure` return dict
    - _Bug_Condition: Handler passes project-level instanceProfileArn to creation workflow_
    - _Expected_Behavior: Handler no longer passes instanceProfileArn; creation workflow creates its own_
    - _Requirements: 1.2, 1.3, 2.1, 2.2_

  - [x] 3.8 Update `project_deploy.py` to stop extracting/storing `instanceProfileArn`
    - In `lambda/project_management/project_deploy.py`:
      - Remove `instance_profile_arn = output_map.get("InstanceProfileArn", "")` from `extract_stack_outputs`
      - Remove `"instanceProfileArn": instance_profile_arn` from the return dict
      - Remove `instanceProfileArn = :ipa` from the `record_infrastructure` UpdateExpression and its ExpressionAttributeValues entry
    - _Bug_Condition: Project deploy stores project-level instanceProfileArn in DynamoDB_
    - _Expected_Behavior: No instanceProfileArn stored at project level_
    - _Requirements: 1.1_

  - [x] 3.9 Grant cluster operations Lambda IAM permissions for IAM management
    - In `lib/project-infrastructure-stack.ts` (or the stack that defines the cluster operations Lambda role), add an IAM policy statement granting:
      - `iam:CreateRole`, `iam:DeleteRole`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:CreateInstanceProfile`, `iam:DeleteInstanceProfile`, `iam:AddRoleToInstanceProfile`, `iam:RemoveRoleFromInstanceProfile`, `iam:PassRole`, `iam:GetInstanceProfile`
      - Scoped to resources matching `arn:aws:iam::*:role/AWSPCS-*` and `arn:aws:iam::*:instance-profile/AWSPCS-*`
    - This is added to the self-service-hpc-stack or foundation-stack where the Lambda execution role is defined
    - _Bug_Condition: Lambda has no IAM management permissions_
    - _Expected_Behavior: Lambda can create/delete IAM resources scoped to AWSPCS-*_
    - _Requirements: 2.1, 2.2, 2.5, 2.6_

  - [x] 3.10 Update `handle_creation_failure` for IAM rollback
    - In `lambda/cluster_operations/cluster_creation.py`, update `handle_creation_failure` to add IAM cleanup before the existing PCS/FSx cleanup:
      - For each of login and compute: remove role from instance profile, delete instance profile, detach managed policies, delete inline policies, delete IAM role
      - Use best-effort approach consistent with existing cleanup helpers
      - Derive resource names from `projectId` and `clusterName`
    - _Bug_Condition: Rollback does not clean up IAM resources_
    - _Expected_Behavior: IAM resources cleaned up on creation failure_
    - _Requirements: 2.5_

  - [x] 3.11 Update CDK tests for removed IAM resources
    - In `test/project-infrastructure-stack.test.ts`, remove or update assertions that reference the `PcsNodeRole`, `PcsInstanceProfile`, or `InstanceProfileArn` output
    - Add assertions confirming these resources are no longer present in the synthesized template
    - _Requirements: 3.1_

  - [x] 3.12 Update documentation
    - In `docs/project-admin/cluster-management.md`, document that each cluster now gets its own dedicated IAM roles and instance profiles (`AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`), created automatically during cluster creation and cleaned up during cluster destruction
    - Note that this enables future per-cluster permission customisation
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.13 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Per-Cluster Instance Profiles
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior (distinct login/compute profiles per cluster)
    - When this test passes, it confirms the expected behavior is satisfied
    - Run: `cd test/lambda && ../../.venv/bin/python3 -m pytest test_property_instance_profile_per_cluster_bug.py -x -v`
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.14 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-IAM Behavior Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run: `cd test/lambda && ../../.venv/bin/python3 -m pytest test_property_instance_profile_per_cluster_preservation.py -x -v`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all preservation tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 4. Checkpoint — Ensure all tests pass
  - Run full test suite: `../../.venv/bin/python3 -m pytest test/lambda/ -x -v` and `npx jest --passWithNoTests`
  - Ensure all property tests pass (both bug condition and preservation)
  - Ensure all existing unit tests pass (no regressions)
  - Ensure CDK tests pass
  - Ask the user if questions arise
