# Instance Profile SFN Wiring Bugfix Design

## Overview

The Lambda functions for per-cluster IAM resource management (`create_iam_resources`, `wait_for_instance_profiles`, `delete_iam_resources`) were implemented in the `instance-profile-per-cluster` bugfix, but the CDK Step Functions state machine definitions in `lib/foundation-stack.ts` were never updated to invoke them. The creation state machine skips straight from `resolveTemplate` to the Parallel FSx/PCS state, so `loginInstanceProfileArn` and `computeInstanceProfileArn` are never set. The Parallel state's `resultSelector` still maps the removed `instanceProfileArn` field. The destruction state machine has no `DeleteIamResources` task, so per-cluster IAM resources are orphaned. This fix adds the missing `LambdaInvoke` tasks and wait loop to both state machines and updates the `resultSelector` to forward the correct instance profile ARN fields.

## Glossary

- **Bug_Condition (C)**: A CDK-synthesised state machine definition that is missing the IAM Lambda steps or maps the wrong instance profile field names
- **Property (P)**: The state machine definitions SHALL include `CreateIamResources`, `WaitForInstanceProfiles` (with retry loop), and `DeleteIamResources` tasks wired to the correct Lambda handlers, and the Parallel `resultSelector` SHALL forward `loginInstanceProfileArn` and `computeInstanceProfileArn`
- **Preservation**: All existing state machine steps, their relative ordering, error handling (catch → rollback), wait loops, Parallel branching, and IAM permissions must remain unchanged
- **`clusterCreationStepLambda`**: The Lambda function in `foundation-stack.ts` that handles all cluster creation workflow steps via `cluster_creation.step_handler`
- **`clusterDestructionStepLambda`**: The Lambda function in `foundation-stack.ts` that handles all cluster destruction workflow steps via `cluster_destruction.step_handler`
- **`resultSelector`**: The Step Functions Parallel state configuration that merges branch outputs into a single flat object for downstream steps

## Bug Details

### Bug Condition

The bug manifests when the CDK stack is synthesised and deployed. The creation state machine definition chains `resolveTemplate` directly to `parallelFsxAndPcs` with no `CreateIamResources` or `WaitForInstanceProfiles` tasks in between. The Parallel state's `resultSelector` maps `instanceProfileArn.$: '$[0].instanceProfileArn'` — a field that no longer exists after the per-cluster IAM changes. The destruction state machine chains `deleteFsxFilesystem` directly to `recordClusterDestroyed` with no `DeleteIamResources` task.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type CdkStateMachineDefinition
  OUTPUT: boolean

  IF input.stateMachineType == "CREATION":
    RETURN NOT hasTask(input.definition, "CreateIamResources")
           OR NOT hasTask(input.definition, "WaitForInstanceProfiles")
           OR resultSelectorMaps(input.parallelState, "instanceProfileArn")
           OR NOT resultSelectorMaps(input.parallelState, "loginInstanceProfileArn")
           OR NOT resultSelectorMaps(input.parallelState, "computeInstanceProfileArn")
  ELSE IF input.stateMachineType == "DESTRUCTION":
    RETURN NOT hasTask(input.definition, "DeleteIamResources")
  END IF
END FUNCTION
```

### Examples

- **Creation chain missing IAM steps**: The current chain is `validate → budget → resolveTemplate → parallel(FSx, PCS) → loginNodes → ...`. After the fix it should be `validate → budget → resolveTemplate → createIamResources → waitForInstanceProfiles (loop) → parallel(FSx, PCS) → loginNodes → ...`.
- **resultSelector maps stale field**: `'instanceProfileArn.$': '$[0].instanceProfileArn'` references a field that `create_iam_resources` no longer sets. The Lambda now sets `loginInstanceProfileArn` and `computeInstanceProfileArn`.
- **Destruction chain missing IAM cleanup**: The current post-export chain is `deletePcs → deleteFsx → recordDestroyed → success`. After the fix it should be `deletePcs → deleteFsx → deleteIamResources → recordDestroyed → success`.
- **Wait loop missing**: `wait_for_instance_profiles` returns `instanceProfilesReady: false` when profiles haven't propagated yet. Without a Choice/Wait loop in the state machine, the step runs once and proceeds with potentially unready profiles.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The creation state machine SHALL continue to run `ValidateAndRegisterName`, `CheckBudgetBreach`, `ResolveTemplate`, `CreateFsxFilesystem`, `CheckFsxStatus` (with FSx wait loop), `CreateFsxDra`, `CreatePcsCluster`, `CreateLoginNodeGroup`, `CreateComputeNodeGroup`, `CreatePcsQueue`, `TagResources`, and `RecordCluster` in their existing relative order
- The Parallel FSx/PCS state SHALL continue to run the FSx branch and PCS branch concurrently, merging outputs via `resultSelector`
- All existing `resultSelector` field mappings (projectId, clusterName, templateId, createdBy, vpcId, efsFileSystemId, s3BucketName, publicSubnetIds, privateSubnetIds, securityGroupIds, fsxFilesystemId, fsxDnsName, fsxMountName, fsxDraId, pcsClusterId, pcsClusterArn, loginInstanceType, instanceTypes, maxNodes, minNodes, purchaseOption, loginLaunchTemplateId, computeLaunchTemplateId) SHALL remain unchanged
- All existing `addCatch` error handling SHALL continue to route failures to `HandleCreationFailure` → `CreationFailed`, with the `MarkClusterFailed` fallback
- The destruction state machine SHALL continue to run `CreateFsxExportTask`, `CheckFsxExportStatus` (with export wait loop), `DeletePcsResources`, `DeleteFsxFilesystem`, and `RecordClusterDestroyed` in their existing relative order
- The `clusterCreationStepLambda` and `clusterDestructionStepLambda` IAM permissions already include the `iam:*` actions scoped to `AWSPCS-*` resources — these SHALL remain unchanged
- State machine timeouts (2 hours), tracing, and naming SHALL remain unchanged

**Scope:**
All state machine steps that do NOT involve IAM resource creation, waiting, deletion, or instance profile ARN mapping should be completely unaffected by this fix. This includes:
- FSx filesystem creation, status checking, DRA creation
- PCS cluster creation
- Login and compute node group creation (except they now receive the correct ARN fields)
- Queue creation, resource tagging, DynamoDB recording
- FSx export, PCS resource deletion, FSx filesystem deletion
- All error handling and rollback chains

## Hypothesized Root Cause

Based on the bug description and code analysis, the root cause is straightforward:

1. **Missing LambdaInvoke task definitions**: The `instance-profile-per-cluster` bugfix added `create_iam_resources`, `wait_for_instance_profiles`, and `delete_iam_resources` step handlers to the Lambda code and registered them in `_STEP_DISPATCH`, but never added corresponding `tasks.LambdaInvoke` constructs in `foundation-stack.ts`.

2. **Missing chain wiring**: Even if the tasks were defined, they were never inserted into the `.next()` chain. `resolveTemplate.next(parallelFsxAndPcs)` skips directly to the Parallel state. `deleteFsxFilesystem.next(recordClusterDestroyed)` skips directly to the DynamoDB update.

3. **Stale resultSelector mapping**: The Parallel state's `resultSelector` was not updated to replace `instanceProfileArn` with `loginInstanceProfileArn` and `computeInstanceProfileArn`. The old field no longer exists in the event payload after the IAM step changes.

4. **Missing wait loop for instance profiles**: The `wait_for_instance_profiles` step requires a Choice/Wait loop (similar to the FSx wait loop pattern) to poll until `instanceProfilesReady` is `true`. Without this, the state machine would proceed immediately even if profiles haven't propagated.

## Correctness Properties

Property 1: Bug Condition - Creation State Machine IAM Steps

_For any_ CDK synthesis of the foundation stack, the creation state machine definition SHALL include `CreateIamResources` and `WaitForInstanceProfiles` LambdaInvoke tasks that invoke `clusterCreationStepLambda` with steps `create_iam_resources` and `wait_for_instance_profiles` respectively, positioned after `ResolveTemplate` and before `ParallelFsxAndPcs`, with a Choice/Wait loop on `instanceProfilesReady` for the wait step, and both tasks SHALL have `addCatch` routing to the rollback handler.

**Validates: Requirements 2.1, 2.2, 2.7**

Property 2: Bug Condition - Parallel resultSelector Instance Profile ARNs

_For any_ CDK synthesis of the foundation stack, the Parallel state's `resultSelector` SHALL map `loginInstanceProfileArn` and `computeInstanceProfileArn` from branch outputs (passed through from the preceding IAM steps) and SHALL NOT map the removed `instanceProfileArn` field.

**Validates: Requirements 2.3, 2.4, 2.5**

Property 3: Bug Condition - Destruction State Machine IAM Cleanup

_For any_ CDK synthesis of the foundation stack, the destruction state machine definition SHALL include a `DeleteIamResources` LambdaInvoke task that invokes `clusterDestructionStepLambda` with step `delete_iam_resources`, positioned after `DeleteFsxFilesystem` and before `RecordClusterDestroyed`.

**Validates: Requirements 2.6**

Property 4: Preservation - Existing Step Ordering and Error Handling

_For any_ CDK synthesis of the foundation stack, the fixed state machine definitions SHALL preserve all existing steps in their original relative order, all `addCatch` error handling routing to `HandleCreationFailure`, the `MarkClusterFailed` fallback, the FSx wait loop, the export wait loop, and all existing `resultSelector` field mappings (excluding the replaced `instanceProfileArn`).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lib/foundation-stack.ts`

**Specific Changes**:

1. **Add `CreateIamResources` LambdaInvoke task**: Define a new `tasks.LambdaInvoke` construct that invokes `clusterCreationStepLambda` with `step: 'create_iam_resources'` and `payload: sfn.JsonPath.entirePayload`, using `payloadResponseOnly: true` and `resultPath: '$'`. Place the declaration after `resolveTemplate` and before the FSx/PCS task declarations.

2. **Add `WaitForInstanceProfiles` LambdaInvoke task**: Define a new `tasks.LambdaInvoke` construct that invokes `clusterCreationStepLambda` with `step: 'wait_for_instance_profiles'`, using the same pattern as other tasks.

3. **Add instance profile wait loop**: Create a `sfn.Wait` state (`WaitForInstanceProfiles` with 10-second duration) and a `sfn.Choice` state (`AreInstanceProfilesReady`) that checks `$.instanceProfilesReady`. When `true`, proceed to `parallelFsxAndPcs`. When `false`, loop back through the Wait → check again, following the same pattern as the existing FSx wait loop.

4. **Add `addCatch` for new creation tasks**: Wire `createIamResources.addCatch(failureChain, catchConfig)` and the wait-for-profiles task's `addCatch` to ensure errors route to the rollback handler, consistent with all other creation steps.

5. **Update Parallel `resultSelector`**: Replace `'instanceProfileArn.$': '$[0].instanceProfileArn'` with `'loginInstanceProfileArn.$': '$[0].loginInstanceProfileArn'` and `'computeInstanceProfileArn.$': '$[0].computeInstanceProfileArn'`.

6. **Update creation chain**: Change the chain from `resolveTemplate.next(parallelFsxAndPcs)` to `resolveTemplate.next(createIamResources).next(waitForInstanceProfiles).next(isInstanceProfilesReady)` where the Choice state routes to `parallelFsxAndPcs` when ready. The wait loop connects back through the Wait state to re-check.

7. **Add `DeleteIamResources` LambdaInvoke task**: Define a new `tasks.LambdaInvoke` construct that invokes `clusterDestructionStepLambda` with `step: 'delete_iam_resources'`, using the same pattern as other destruction tasks.

8. **Update destruction post-export chain**: Change from `deleteFsxFilesystem.next(recordClusterDestroyed)` to `deleteFsxFilesystem.next(deleteIamResources).next(recordClusterDestroyed)`.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code (missing tasks in synthesised CloudFormation), then verify the fix produces correct state machine definitions and preserves existing structure.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write CDK assertion tests that synthesise the `FoundationStack` and inspect the CloudFormation template for the presence of the IAM-related Step Functions tasks. Run these tests on the UNFIXED code to observe failures.

**Test Cases**:
1. **Missing CreateIamResources Task**: Assert the creation state machine definition contains a state named `CreateIamResources` with `step: 'create_iam_resources'` (will fail on unfixed code)
2. **Missing WaitForInstanceProfiles Task**: Assert the creation state machine definition contains a state named `WaitForInstanceProfiles` with `step: 'wait_for_instance_profiles'` (will fail on unfixed code)
3. **Stale resultSelector Field**: Assert the Parallel state's `ResultSelector` does NOT contain `instanceProfileArn` and DOES contain `loginInstanceProfileArn` and `computeInstanceProfileArn` (will fail on unfixed code)
4. **Missing DeleteIamResources Task**: Assert the destruction state machine definition contains a state named `DeleteIamResources` with `step: 'delete_iam_resources'` (will fail on unfixed code)

**Expected Counterexamples**:
- The synthesised CloudFormation template contains no `CreateIamResources` or `WaitForInstanceProfiles` states in the creation state machine
- The Parallel state's `ResultSelector` contains `instanceProfileArn` instead of `loginInstanceProfileArn`/`computeInstanceProfileArn`
- The destruction state machine contains no `DeleteIamResources` state

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed CDK code produces the expected state machine definitions.

**Pseudocode:**
```
FOR ALL synthesis WHERE isBugCondition(synthesis) DO
  template := synthesise(FoundationStack)
  creationDef := extractStateMachineDefinition(template, "hpc-cluster-creation")
  destructionDef := extractStateMachineDefinition(template, "hpc-cluster-destruction")

  ASSERT hasState(creationDef, "CreateIamResources")
  ASSERT hasState(creationDef, "WaitForInstanceProfiles")
  ASSERT hasState(creationDef, "AreInstanceProfilesReady")  // Choice state
  ASSERT hasState(creationDef, "WaitForInstanceProfilesPropagation")  // Wait state
  ASSERT stateInvokesStep(creationDef, "CreateIamResources", "create_iam_resources")
  ASSERT stateInvokesStep(creationDef, "WaitForInstanceProfiles", "wait_for_instance_profiles")
  ASSERT parallelResultSelectorContains(creationDef, "loginInstanceProfileArn")
  ASSERT parallelResultSelectorContains(creationDef, "computeInstanceProfileArn")
  ASSERT NOT parallelResultSelectorContains(creationDef, "instanceProfileArn")

  ASSERT hasState(destructionDef, "DeleteIamResources")
  ASSERT stateInvokesStep(destructionDef, "DeleteIamResources", "delete_iam_resources")
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed CDK code produces the same state machine structure as the original.

**Pseudocode:**
```
FOR ALL synthesis WHERE NOT isBugCondition(synthesis) DO
  template := synthesise(FoundationStack)
  creationDef := extractStateMachineDefinition(template, "hpc-cluster-creation")

  // All original states still present
  ASSERT hasState(creationDef, "ValidateAndRegisterName")
  ASSERT hasState(creationDef, "CheckBudgetBreach")
  ASSERT hasState(creationDef, "ResolveTemplate")
  ASSERT hasState(creationDef, "ParallelFsxAndPcs")
  ASSERT hasState(creationDef, "CreateLoginNodeGroup")
  ASSERT hasState(creationDef, "CreateComputeNodeGroup")
  ASSERT hasState(creationDef, "CreatePcsQueue")
  ASSERT hasState(creationDef, "TagResources")
  ASSERT hasState(creationDef, "RecordCluster")
  ASSERT hasState(creationDef, "HandleCreationFailure")
  ASSERT hasState(creationDef, "MarkClusterFailed")

  // All original resultSelector fields preserved
  ASSERT parallelResultSelectorContains(creationDef, "projectId")
  ASSERT parallelResultSelectorContains(creationDef, "fsxFilesystemId")
  ASSERT parallelResultSelectorContains(creationDef, "pcsClusterId")
  ASSERT parallelResultSelectorContains(creationDef, "loginLaunchTemplateId")
  ASSERT parallelResultSelectorContains(creationDef, "computeLaunchTemplateId")
END FOR
```

**Testing Approach**: CDK assertion-based testing is the primary approach since the bug is entirely in CDK infrastructure code. The `Template.fromStack()` API lets us inspect the synthesised CloudFormation template to verify state machine structure without deploying.

**Test Plan**: Synthesise the FoundationStack and use CDK assertions to verify all existing states, transitions, error handling, and `resultSelector` fields are preserved alongside the new IAM tasks.

**Test Cases**:
1. **Existing Creation Steps Preserved**: Verify all original creation state machine states are present in the synthesised template
2. **Existing Destruction Steps Preserved**: Verify all original destruction state machine states are present
3. **Error Handling Preserved**: Verify `HandleCreationFailure` and `MarkClusterFailed` states exist with correct transitions
4. **FSx Wait Loop Preserved**: Verify the `IsFsxAvailable` Choice state and `WaitForFsx` Wait state exist
5. **Export Wait Loop Preserved**: Verify the `IsExportComplete` Choice state and `WaitForExport` Wait state exist
6. **resultSelector Non-IAM Fields Preserved**: Verify all non-IAM fields in the Parallel `resultSelector` are unchanged

### Unit Tests

- Test that the synthesised creation state machine contains `CreateIamResources` and `WaitForInstanceProfiles` LambdaInvoke states
- Test that `CreateIamResources` invokes `clusterCreationStepLambda` with `step: 'create_iam_resources'`
- Test that `WaitForInstanceProfiles` invokes `clusterCreationStepLambda` with `step: 'wait_for_instance_profiles'`
- Test that the instance profile wait loop has a Choice state checking `instanceProfilesReady` and a Wait state
- Test that the Parallel `resultSelector` contains `loginInstanceProfileArn` and `computeInstanceProfileArn`
- Test that the Parallel `resultSelector` does NOT contain `instanceProfileArn`
- Test that the destruction state machine contains `DeleteIamResources` invoking `step: 'delete_iam_resources'`
- Test that `DeleteIamResources` is positioned between `DeleteFsxFilesystem` and `RecordClusterDestroyed`
- Test that `CreateIamResources` has `addCatch` routing to the rollback handler

### Property-Based Tests

- Generate random state machine synthesis configurations and verify the creation chain always includes IAM steps between `ResolveTemplate` and `ParallelFsxAndPcs`
- Generate random synthesis configurations and verify the destruction chain always includes `DeleteIamResources` between `DeleteFsxFilesystem` and `RecordClusterDestroyed`
- Generate random synthesis configurations and verify all original `resultSelector` fields (excluding `instanceProfileArn`) are preserved

### Integration Tests

- Synthesise the full FoundationStack and verify both state machines produce valid CloudFormation
- Verify the synthesised template passes `cdk synth` without errors
- Verify the creation state machine definition JSON contains the correct step ordering end-to-end
- Verify the destruction state machine definition JSON contains the correct step ordering end-to-end
