# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Stuck CREATING Cluster After Execution Termination
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to the concrete failing cases:
    - Case A: `handle_creation_failure` raises an exception (rollback handler failure) — the catch goes to the Fail state without updating DynamoDB
    - Case B: `_record_failed_cluster` raises `ClientError` during rollback — DynamoDB write fails, function throws, catch goes to Fail state
    - Case C: Cluster record stuck in CREATING with `createdAt` older than 2 hours (simulating state machine timeout) — no mechanism transitions it to FAILED
    - Case D: API rejects destroy on a CREATING cluster — `_handle_delete_cluster` returns ConflictError for status not in (ACTIVE, FAILED)
  - Test file: `test/lambda/test_stuck_fsx_bug_condition.py`
  - For each case, assert that the cluster record SHOULD be in FAILED status (expected behavior from design)
  - Use Hypothesis to generate random `projectId`/`clusterName` strings and `createdAt` timestamps older than 2 hours
  - Mock DynamoDB, Step Functions, and IAM clients using `unittest.mock`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found: DynamoDB cluster record remains in CREATING status after Step Functions execution has terminated
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Normal Creation and Failure Flows Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Observe on UNFIXED code**:
    - Observe: Successful cluster creation flow sets status to ACTIVE with all resource IDs in DynamoDB
    - Observe: When `handle_creation_failure` succeeds, cluster status is set to FAILED with error message and resources cleaned up
    - Observe: A cluster legitimately in CREATING status (workflow still running) continues to show accurate progress (currentStep, totalSteps, stepDescription)
    - Observe: Clusters in ACTIVE or FAILED status can be destroyed via `_handle_delete_cluster`
    - Observe: FSx polling within the 30-minute window reports progress without premature failure
  - Test file: `test/lambda/test_stuck_fsx_preservation.py`
  - Write property-based tests using Hypothesis:
    - Property: For all valid cluster creation payloads where all steps succeed, `_record_active_cluster` is called and status becomes ACTIVE
    - Property: For all failure scenarios where `handle_creation_failure` succeeds, `_record_failed_cluster` is called and status becomes FAILED with an error message
    - Property: For all clusters in CREATING status with `createdAt` within the last 2 hours, the polling response includes progress fields (currentStep, totalSteps, stepDescription)
    - Property: For all clusters in ACTIVE or FAILED status, `_handle_delete_cluster` starts the destruction workflow (returns 202)
    - Property: For all FSx poll attempts where `fsxPollCount < 60` and status is not terminal, `check_fsx_status` returns without raising
  - Mock DynamoDB, Step Functions, FSx, IAM, and PCS clients
  - Verify tests pass on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Fix for stuck CREATING cluster after Step Functions execution termination

  - [x] 3.1 Add last-resort DynamoDB UpdateItem task before Fail state in Step Functions
    - File: `lib/foundation-stack.ts`
    - Insert a new `tasks.CallAwsService` task (`MarkClusterFailed`) that uses the DynamoDB `UpdateItem` SDK integration (not a Lambda) to set `status = "FAILED"` and `errorMessage` on the cluster record
    - Chain: `handleCreationFailure` catch → `MarkClusterFailed` → `CreationFailed` (Fail state)
    - The UpdateItem should set `status = "FAILED"`, `errorMessage = "Cluster creation failed — rollback handler encountered an error"`, and `updatedAt` to the current timestamp
    - Use `sfn.JsonPath` to extract `projectId` and `clusterName` from the state machine payload for the DynamoDB key
    - Since this is a direct SDK call (not Lambda), it has minimal failure surface
    - _Bug_Condition: isBugCondition(input) where cluster.status == "CREATING" AND execution.status IN ["FAILED", "TIMED_OUT", "ABORTED"]_
    - _Expected_Behavior: cluster record updated to FAILED with error message_
    - _Preservation: Normal success path (ACTIVE) and normal rollback path (FAILED via handle_creation_failure) are not affected — MarkClusterFailed only runs when handleCreationFailure itself fails_
    - _Requirements: 2.1_

  - [x] 3.2 Add EventBridge rule and Lambda handler for timed-out/failed executions
    - File: `lib/foundation-stack.ts`
    - Create an EventBridge rule matching Step Functions execution status change events for the cluster creation state machine with `status: ["TIMED_OUT", "FAILED", "ABORTED"]`
    - Target a new Lambda function (`hpc-cluster-creation-failure-handler`) that extracts the execution ARN from the event, describes the execution to get the input payload, and calls `_record_failed_cluster`
    - Grant the Lambda function `states:DescribeExecution` permission on the state machine ARN
    - Grant the Lambda function DynamoDB read/write on the Clusters table
    - File: `lambda/cluster_operations/cluster_creation.py`
    - Add a new `mark_cluster_failed_from_event` function that:
      1. Extracts the execution ARN from the EventBridge event
      2. Calls `sfn_client.describe_execution()` to get the input payload
      3. Parses `projectId` and `clusterName` from the input
      4. Checks if the cluster record is still in CREATING status (idempotency guard)
      5. Calls `_record_failed_cluster` to transition to FAILED
    - Register the new function in `_STEP_DISPATCH` or as a separate handler entry point
    - _Bug_Condition: isBugCondition(input) where execution times out or fails without DynamoDB update_
    - _Expected_Behavior: EventBridge detects termination and Lambda updates cluster to FAILED_
    - _Preservation: EventBridge rule only fires on terminal execution states; running executions are not affected_
    - _Requirements: 2.2, 2.4_

  - [x] 3.3 Add staleness detection to UI polling
    - File: `frontend/js/config.js`
    - Add `clusterCreationTimeoutMs: 9000000` (2.5 hours in milliseconds) configuration value
    - File: `frontend/js/app.js`
    - In the cluster list rendering and cluster detail polling logic, when a cluster has `status === "CREATING"`:
      1. Compare `Date.now()` against `new Date(cluster.createdAt).getTime()`
      2. If the elapsed time exceeds `CONFIG.clusterCreationTimeoutMs`, display a warning badge ("Creation may have failed") and show a "Mark as Failed" action button
      3. The "Mark as Failed" button calls the new force-fail API endpoint (task 3.4)
    - Do NOT prematurely stop polling — the staleness detection is a UI hint, not a status change
    - _Bug_Condition: UI polls indefinitely for a cluster stuck in CREATING_
    - _Expected_Behavior: UI detects staleness and offers user a recovery action_
    - _Preservation: Clusters in CREATING status within the timeout window continue to show normal progress_
    - _Requirements: 2.3, 2.4_

  - [x] 3.4 Add force-fail API endpoint
    - File: `lambda/cluster_operations/handler.py`
    - Add a new route: `POST /projects/{projectId}/clusters/{clusterName}/fail`
    - Handler `_handle_force_fail_cluster`:
      1. Validate authorisation (caller must be a project member)
      2. Retrieve the cluster record — raise NotFoundError if missing
      3. Verify the cluster is in CREATING status — raise ConflictError otherwise
      4. Optionally check that the Step Functions execution is no longer running (describe latest execution for this cluster)
      5. Update the DynamoDB record: set `status = "FAILED"`, `errorMessage = "Manually marked as failed by user"`, `updatedAt`
      6. Return 200 with success message
    - File: `lib/foundation-stack.ts`
    - Add the API Gateway resource and method: `POST /projects/{projectId}/clusters/{clusterName}/fail` with Cognito authorizer, integrated with the cluster operations Lambda
    - _Bug_Condition: User cannot take action on a stuck CREATING cluster_
    - _Expected_Behavior: User can force-transition a stuck cluster to FAILED_
    - _Preservation: Endpoint only works on CREATING clusters; ACTIVE/FAILED/DESTROYED clusters are rejected_
    - _Requirements: 2.4_

  - [x] 3.5 Update cluster management documentation
    - File: `docs/project-admin/cluster-management.md`
    - Add a "Stuck Cluster Recovery" section documenting:
      - How the system automatically detects stuck clusters (EventBridge timeout detection, last-resort DynamoDB update)
      - How the UI displays a staleness warning after 2.5 hours
      - How to use the "Mark as Failed" button to manually recover a stuck cluster
      - What happens after marking a cluster as failed (can destroy or recreate)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.6 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Stuck CREATING Cluster After Execution Termination
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied:
      - Case A: Rollback handler failure → MarkClusterFailed SDK task updates DynamoDB to FAILED
      - Case B: DynamoDB write failure in rollback → MarkClusterFailed SDK task updates DynamoDB to FAILED
      - Case C: State machine timeout → EventBridge + Lambda handler updates DynamoDB to FAILED
      - Case D: Force-fail endpoint allows user to transition stuck CREATING cluster to FAILED
    - Run bug condition exploration test from step 1: `test/lambda/test_stuck_fsx_bug_condition.py`
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.7 Verify preservation tests still pass
    - **Property 2: Preservation** - Normal Creation and Failure Flows Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2: `test/lambda/test_stuck_fsx_preservation.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full test suite to verify no regressions
  - Verify `test/lambda/test_stuck_fsx_bug_condition.py` passes (bug is fixed)
  - Verify `test/lambda/test_stuck_fsx_preservation.py` passes (no regressions)
  - Run `test/foundation-stack.test.ts` to verify CDK synthesis still works with the new state machine changes
  - Ensure all tests pass, ask the user if questions arise.
