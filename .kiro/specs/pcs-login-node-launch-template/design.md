# PCS Login Node Launch Template Bugfix Design

## Overview

Cluster creation fails with `ValidationException: Launch template id is required` because the `create_launch_templates` Python step handler — which populates `loginLaunchTemplateId` and `computeLaunchTemplateId` — was never wired into the Step Functions state machine definition. The fix adds a `CreateLaunchTemplates` step to the state machine chain and grants the missing EC2 launch template permissions to the Lambda execution role.

## Glossary

- **Bug_Condition (C)**: Any cluster creation workflow execution — the state machine always skips the `create_launch_templates` step because it is not defined in the chain
- **Property (P)**: After the fix, the `CreateLaunchTemplates` step runs between instance profile readiness and `StorageModeChoice`, populating `loginLaunchTemplateId` and `computeLaunchTemplateId` in the event payload
- **Preservation**: All existing state machine steps, IAM permissions, catch/rollback routing, storage mode branching, and API routes must remain unchanged
- **`create_launch_templates`**: The Python handler in `lambda/cluster_operations/cluster_creation.py` that calls `ec2:CreateLaunchTemplate` for login and compute templates, adding their IDs to the event payload
- **`clusterCreationStepLambda`**: The Lambda function (`hpc-cluster-creation-steps`) that executes individual steps of the cluster creation workflow
- **`StorageModeChoice`**: The Step Functions Choice state that branches on `$.storageMode` into lustre (parallel FSx+PCS) or mountpoint (PCS-only) paths

## Bug Details

### Bug Condition

The bug manifests on every cluster creation workflow execution. The `create_launch_templates` handler exists in `_STEP_DISPATCH` in the Python code but has no corresponding `LambdaInvoke` task in the CDK state machine definition. Additionally, the Lambda role lacks `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates` permissions.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type ClusterCreationStateMachineDefinition
  OUTPUT: boolean

  RETURN stateMachineChain DOES NOT CONTAIN step WITH name 'CreateLaunchTemplates'
         OR lambdaRole DOES NOT HAVE permission 'ec2:CreateLaunchTemplate'
         OR lambdaRole DOES NOT HAVE permission 'ec2:DeleteLaunchTemplate'
         OR lambdaRole DOES NOT HAVE permission 'ec2:DescribeLaunchTemplates'
END FUNCTION
```

### Examples

- **Lustre cluster creation**: State machine transitions from `WaitForInstanceProfiles` → `AreInstanceProfilesReady` → `StorageModeChoice` → `ParallelFsxAndPcs` → `CreateLoginNodeGroup`. The `CreateLoginNodeGroup` step fails with `ValidationException: Launch template id is required` because `loginLaunchTemplateId` was never set.
- **Mountpoint cluster creation**: Same flow but via the mountpoint branch. `CreateLoginNodeGroup` fails identically because `loginLaunchTemplateId` is absent.
- **Expected behavior after fix**: State machine transitions from `AreInstanceProfilesReady` → `CreateLaunchTemplates` → `StorageModeChoice`. Both `loginLaunchTemplateId` and `computeLaunchTemplateId` are present in the payload when `CreateLoginNodeGroup` and `CreateComputeNodeGroup` execute.
- **Edge case — CreateLaunchTemplates failure**: If the EC2 API call fails, the catch handler routes to `HandleCreationFailure` for rollback, same as all other steps.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- All existing state machine steps (ValidateAndRegisterName, CheckBudgetBreach, ResolveTemplate, CreateIamResources, WaitForInstanceProfiles, StorageModeChoice, ParallelFsxAndPcs, mountpoint branch, CreateLoginNodeGroup, CreateComputeNodeGroup, CreatePcsQueue, TagResources, RecordCluster) must continue to function identically
- The `ParallelFsxAndPcs` `resultSelector` must continue to correctly map `loginLaunchTemplateId` and `computeLaunchTemplateId` from `$[0]`
- The mountpoint branch must continue to pass the entire payload through
- All existing catch handlers routing to `HandleCreationFailure` must remain unchanged
- The destruction state machine must remain completely unchanged
- All existing IAM permissions on `clusterCreationStepLambda` must remain unchanged
- All API Gateway routes and integrations must remain unchanged

**Scope:**
The fix is strictly additive — one new `LambdaInvoke` step, one new catch handler, and three new IAM actions added to an existing policy statement. No existing code is modified or removed.

## Hypothesized Root Cause

Based on the bug description and code analysis, the root cause is clear:

1. **Missing State Machine Step**: The `create_launch_templates` handler was implemented in `cluster_creation.py` and registered in `_STEP_DISPATCH`, but the corresponding `LambdaInvoke` task was never added to the CDK state machine definition in `lib/constructs/cluster-operations.ts`. The chain goes directly from `waitForInstanceProfiles` → `areInstanceProfilesReady` → `storageModeChoice`, skipping launch template creation entirely.

2. **Missing IAM Permissions**: The `clusterCreationStepLambda` role has EC2 permissions for `DescribeSubnets`, `DescribeSecurityGroups`, `DescribeVpcs`, `CreateNetworkInterface`, `DescribeNetworkInterfaces`, and `CreateTags`, but is missing `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates` which the handler needs.

3. **Missing Catch Handler**: Since the step doesn't exist, there is no catch handler for it. When added, it needs the same `addCatch(failureChain, catchConfig)` pattern used by all other steps.

## Correctness Properties

Property 1: Bug Condition - CreateLaunchTemplates Step Exists in State Machine

_For any_ synthesized CloudFormation template from the CDK stack, the cluster creation state machine definition SHALL contain a `CreateLaunchTemplates` state that invokes the `clusterCreationStepLambda` with `step: 'create_launch_templates'`.

**Validates: Requirements 2.1**

Property 2: Bug Condition - EC2 Launch Template Permissions Granted

_For any_ synthesized CloudFormation template from the CDK stack, the `clusterCreationStepLambda` IAM role SHALL have a policy statement granting `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates` permissions.

**Validates: Requirements 2.4**

Property 3: Preservation - Existing State Machine Steps Unchanged

_For any_ synthesized CloudFormation template from the CDK stack, all previously existing state machine states (ValidateAndRegisterName, CheckBudgetBreach, ResolveTemplate, CreateIamResources, WaitForInstanceProfiles, StorageModeChoice, ParallelFsxAndPcs, CreateLoginNodeGroup, CreateComputeNodeGroup, CreatePcsQueue, TagResources, RecordCluster, HandleCreationFailure) SHALL continue to exist with their original configuration.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

Property 4: Preservation - Existing IAM Permissions Unchanged

_For any_ synthesized CloudFormation template from the CDK stack, all previously existing IAM policy statements on the `clusterCreationStepLambda` role SHALL remain present and unchanged.

**Validates: Requirements 3.5**

## Fix Implementation

### Changes Required

**File**: `lib/constructs/cluster-operations.ts`

**Specific Changes**:

1. **Add CreateLaunchTemplates LambdaInvoke step**: Create a new `tasks.LambdaInvoke` task named `CreateLaunchTemplates` that invokes `clusterCreationStepLambda` with `step: 'create_launch_templates'` and `payload: sfn.JsonPath.entirePayload`, using `resultPath: '$'` to merge the output (which adds `loginLaunchTemplateId` and `computeLaunchTemplateId`) back into the payload. Place this definition near the other step definitions (after `waitForInstanceProfilesPropagation` and before the FSx steps).

2. **Add catch handler**: Call `createLaunchTemplates.addCatch(failureChain, catchConfig)` alongside the existing catch handler registrations.

3. **Insert into chain**: Modify the chain so that `areInstanceProfilesReady` routes to `createLaunchTemplates` instead of `storageModeChoice`, and `createLaunchTemplates` routes to `storageModeChoice`. The updated flow becomes: `... → AreInstanceProfilesReady → CreateLaunchTemplates → StorageModeChoice → ...`

4. **Add EC2 launch template IAM permissions**: Add `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates` to the existing EC2 policy statement on `clusterCreationStepLambda` (the one that already has `ec2:DescribeSubnets`, `ec2:DescribeSecurityGroups`, etc.).

5. **No changes to destruction state machine**: The destruction handler already cleans up launch templates via `ec2:DeleteLaunchTemplate` in the rollback handler. The destruction step Lambda already has the necessary permissions through its existing IAM policies (or will use the creation step Lambda's rollback handler).

## Testing Strategy

### Validation Approach

The testing strategy uses CDK template assertions to verify the synthesized CloudFormation output. Since this is an infrastructure-as-code bug (missing step in state machine definition + missing IAM permissions), the tests operate on the synthesized template rather than runtime behavior.

### Exploratory Bug Condition Checking

**Goal**: Confirm the bug exists in the current code by verifying the state machine definition lacks the `CreateLaunchTemplates` step and the IAM policy lacks launch template permissions.

**Test Plan**: Write CDK assertion tests that check for the presence of `CreateLaunchTemplates` in the state machine definition string and `ec2:CreateLaunchTemplate` in IAM policies. Run on unfixed code to observe failures.

**Test Cases**:
1. **Missing Step Test**: Assert state machine definition contains `CreateLaunchTemplates` (will fail on unfixed code)
2. **Missing Permission Test**: Assert IAM policy contains `ec2:CreateLaunchTemplate` (will fail on unfixed code)
3. **Missing Catch Handler Test**: Assert `CreateLaunchTemplates` has error handling routing to failure (will fail on unfixed code)

**Expected Counterexamples**:
- State machine definition string does not contain `CreateLaunchTemplates`
- No IAM policy statement includes `ec2:CreateLaunchTemplate`

### Fix Checking

**Goal**: Verify that the fixed CDK code produces a CloudFormation template with the `CreateLaunchTemplates` step correctly wired and permissions granted.

**Pseudocode:**
```
FOR ALL synthesized templates from the fixed CDK stack DO
  definition := template.stateMachine['hpc-cluster-creation'].definitionString
  ASSERT 'CreateLaunchTemplates' IN definition
  ASSERT 'create_launch_templates' IN definition
  ASSERT definition has CreateLaunchTemplates BETWEEN AreInstanceProfilesReady AND StorageModeChoice
  ASSERT iamPolicy CONTAINS 'ec2:CreateLaunchTemplate'
  ASSERT iamPolicy CONTAINS 'ec2:DeleteLaunchTemplate'
  ASSERT iamPolicy CONTAINS 'ec2:DescribeLaunchTemplates'
END FOR
```

### Preservation Checking

**Goal**: Verify that all existing state machine steps, IAM permissions, and infrastructure remain unchanged.

**Pseudocode:**
```
FOR ALL synthesized templates from the fixed CDK stack DO
  ASSERT template.resourceCount('AWS::Lambda::Function') = 5
  ASSERT template.resourceCount('AWS::StepFunctions::StateMachine') = 2
  ASSERT definition CONTAINS 'StorageModeChoice'
  ASSERT definition CONTAINS 'ValidateAndRegisterName'
  ASSERT definition CONTAINS 'ParallelFsxAndPcs'
  ASSERT existingIamPermissions ARE ALL STILL PRESENT
  ASSERT template.resourceCount('AWS::ApiGateway::Resource') = 19
END FOR
```

**Testing Approach**: CDK template assertions provide deterministic verification of the synthesized output. Since the fix is purely additive (new step + new permissions), preservation is verified by confirming all existing test assertions continue to pass.

**Test Cases**:
1. **Existing Step Preservation**: All existing tests in `cluster-operations.test.ts` continue to pass unchanged
2. **Resource Count Preservation**: Lambda function count, state machine count, and API Gateway resource count remain the same
3. **Permission Preservation**: All existing IAM permission assertions continue to pass

### Unit Tests

- Test that the creation state machine definition contains `CreateLaunchTemplates`
- Test that the definition contains the step payload `create_launch_templates`
- Test that `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates` are in IAM policy
- Test that all existing tests continue to pass (preservation)

### Property-Based Tests

Not applicable for this bugfix. The fix is a CDK infrastructure change verified through deterministic template assertions. The input space is a single synthesized CloudFormation template, not a domain of runtime inputs.

### Integration Tests

- Verify the full CDK stack synthesizes without errors after the fix
- Verify the state machine definition is valid JSON with the new step correctly positioned
- Verify end-to-end cluster creation flow works with launch templates (manual verification)
