# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Missing IAM Steps and Stale resultSelector in State Machines
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists in the synthesised CloudFormation
  - **Scoped PBT Approach**: The bug is deterministic (CDK synthesis is pure), so scope the property to the concrete failing cases:
    - The creation state machine definition is missing `CreateIamResources` and `WaitForInstanceProfiles` states
    - The Parallel state's `ResultSelector` maps the stale `instanceProfileArn` field instead of `loginInstanceProfileArn` and `computeInstanceProfileArn`
    - The destruction state machine definition is missing a `DeleteIamResources` state
  - **Test file**: `test/foundation-stack.test.ts` — add a new `describe('Instance Profile SFN Wiring — Bug Condition', ...)` block
  - **Test approach**: Use `Template.fromStack()` (reuse the existing `beforeAll` template) and CDK assertions to inspect the synthesised CloudFormation
  - **Test cases** (each as a separate `it(...)` within the describe block):
    1. Assert the creation state machine definition contains a state named `CreateIamResources` that invokes `clusterCreationStepLambda` with `step: 'create_iam_resources'` (from Bug Condition / isBugCondition: `NOT hasTask(input.definition, "CreateIamResources")`)
    2. Assert the creation state machine definition contains a state named `WaitForInstanceProfiles` that invokes `clusterCreationStepLambda` with `step: 'wait_for_instance_profiles'` (from Bug Condition / isBugCondition: `NOT hasTask(input.definition, "WaitForInstanceProfiles")`)
    3. Assert the creation state machine definition contains a Choice state `AreInstanceProfilesReady` and a Wait state for the instance profile wait loop (from Requirements 2.2, 3.5)
    4. Assert the Parallel state's `ResultSelector` contains `loginInstanceProfileArn` and `computeInstanceProfileArn` and does NOT contain `instanceProfileArn` (from Bug Condition / isBugCondition: `resultSelectorMaps(input.parallelState, "instanceProfileArn")`)
    5. Assert the destruction state machine definition contains a state named `DeleteIamResources` that invokes `clusterDestructionStepLambda` with `step: 'delete_iam_resources'` (from Bug Condition / isBugCondition: `NOT hasTask(input.definition, "DeleteIamResources")`)
  - **Implementation detail**: Parse the `DefinitionString` from the synthesised `AWS::StepFunctions::StateMachine` resources (using the `Fn::Join` resolution pattern already established in the existing Catch-block validation test), then inspect the `States` object for the expected state names and their `Parameters`
  - Run test on UNFIXED code with `npx jest --passWithNoTests`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct — it proves the bug exists)
  - Document counterexamples found (e.g., "CreateIamResources state not found in creation state machine definition", "ResultSelector contains instanceProfileArn instead of loginInstanceProfileArn")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.6_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Existing State Machine Structure Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Test file**: `test/foundation-stack.test.ts` — add a new `describe('Instance Profile SFN Wiring — Preservation', ...)` block
  - **Test approach**: Synthesise the FoundationStack (reuse the existing `beforeAll` template) and use CDK assertions to verify all existing states, transitions, error handling, and `resultSelector` fields are present
  - **Observation-first**: Run on UNFIXED code to confirm these tests PASS, establishing the baseline
  - **Test cases** (each as a separate `it(...)` within the describe block):
    1. Observe: All original creation states exist — `ValidateAndRegisterName`, `CheckBudgetBreach`, `ResolveTemplate`, `ParallelFsxAndPcs`, `CreateFsxFilesystem`, `CheckFsxStatus`, `CreateFsxDra`, `CreatePcsCluster`, `CreateLoginNodeGroup`, `CreateComputeNodeGroup`, `CreatePcsQueue`, `TagResources`, `RecordCluster` (from Preservation Requirements 3.1)
    2. Observe: Error handling states exist — `HandleCreationFailure`, `CreationFailed`, `MarkClusterFailed` with correct Catch routing (from Preservation Requirements 3.3)
    3. Observe: FSx wait loop exists — `IsFsxAvailable` Choice state and `WaitForFsx` Wait state (from Preservation Requirements 3.2)
    4. Observe: All non-IAM `resultSelector` fields in the Parallel state are present — `projectId`, `clusterName`, `templateId`, `createdBy`, `vpcId`, `efsFileSystemId`, `s3BucketName`, `publicSubnetIds`, `privateSubnetIds`, `securityGroupIds`, `fsxFilesystemId`, `fsxDnsName`, `fsxMountName`, `fsxDraId`, `pcsClusterId`, `pcsClusterArn`, `loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption`, `loginLaunchTemplateId`, `computeLaunchTemplateId` (from Preservation Requirements 3.2)
    5. Observe: All original destruction states exist — `CreateFsxExportTask`, `CheckFsxExportStatus`, `DeletePcsResources`, `DeleteFsxFilesystem`, `RecordClusterDestroyed`, `DestructionSucceeded` (from Preservation Requirements 3.4)
    6. Observe: Export wait loop exists — `IsExportComplete` Choice state and `WaitForExport` Wait state (from Preservation Requirements 3.4)
    7. Observe: State machine configuration preserved — both state machines have tracing enabled, 2-hour timeout, correct names (`hpc-cluster-creation`, `hpc-cluster-destruction`) (from Preservation Requirements 3.6)
  - **Implementation detail**: Parse the `DefinitionString` from the synthesised state machine resources (same `Fn::Join` resolution approach as task 1) and check for state names in the `States` object; for `resultSelector`, inspect the `ParallelFsxAndPcs` state's `ResultSelector` property
  - Run tests on UNFIXED code with `npx jest --passWithNoTests`
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Fix: Add IAM Lambda steps to creation and destruction state machines

  - [x] 3.1 Implement the fix in `lib/foundation-stack.ts`
    - Add `CreateIamResources` LambdaInvoke task: invoke `clusterCreationStepLambda` with `step: 'create_iam_resources'`, `payloadResponseOnly: true`, `payload: sfn.TaskInput.fromObject({ step: 'create_iam_resources', payload: sfn.JsonPath.entirePayload })`, `resultPath: '$'`
    - Add `WaitForInstanceProfiles` LambdaInvoke task: invoke `clusterCreationStepLambda` with `step: 'wait_for_instance_profiles'`, same pattern as above
    - Add `WaitForInstanceProfilesPropagation` Wait state: `sfn.WaitTime.duration(cdk.Duration.seconds(10))`
    - Add `AreInstanceProfilesReady` Choice state: check `$.instanceProfilesReady` — when `true` proceed to `parallelFsxAndPcs`, otherwise loop through Wait → re-check (same pattern as FSx wait loop)
    - Add `addCatch(failureChain, catchConfig)` for `createIamResources` and `waitForInstanceProfiles` tasks
    - Update Parallel `resultSelector`: replace `'instanceProfileArn.$': '$[0].instanceProfileArn'` with `'loginInstanceProfileArn.$': '$[0].loginInstanceProfileArn'` and `'computeInstanceProfileArn.$': '$[0].computeInstanceProfileArn'`
    - Update creation chain: `resolveTemplate.next(createIamResources).next(waitForInstanceProfiles).next(isInstanceProfilesReady)` where the Choice routes to `parallelFsxAndPcs` when ready
    - Add `DeleteIamResources` LambdaInvoke task: invoke `clusterDestructionStepLambda` with `step: 'delete_iam_resources'`, same pattern as other destruction tasks
    - Update destruction post-export chain: insert `deleteIamResources` between `deleteFsxFilesystem` and `recordClusterDestroyed`
    - _Bug_Condition: isBugCondition(input) — creation missing CreateIamResources/WaitForInstanceProfiles, resultSelector maps stale instanceProfileArn, destruction missing DeleteIamResources_
    - _Expected_Behavior: Creation chain includes IAM steps before Parallel state with wait loop; resultSelector maps loginInstanceProfileArn and computeInstanceProfileArn; destruction chain includes DeleteIamResources before RecordClusterDestroyed_
    - _Preservation: All existing steps, ordering, error handling, wait loops, resultSelector non-IAM fields, timeouts, tracing, and naming unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Missing IAM Steps and Stale resultSelector in State Machines
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior (IAM states present, correct resultSelector fields)
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1 with `npx jest --passWithNoTests`
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Existing State Machine Structure Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run preservation property tests from step 2 with `npx jest --passWithNoTests`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Checkpoint — Ensure all tests pass
  - Run the full CDK test suite: `npx jest --passWithNoTests`
  - Run the full Python test suite: `cd test/lambda && ../../.venv/bin/python3 -m pytest -x -v`
  - Ensure all tests pass, ask the user if questions arise.
