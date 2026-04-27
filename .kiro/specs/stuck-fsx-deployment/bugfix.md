# Bugfix Requirements Document

## Introduction

A cluster deployment can become permanently stuck in CREATING status when the Step Functions execution terminates (due to failure, timeout, or the rollback handler itself failing) without successfully updating the DynamoDB cluster record to FAILED. The UI polls the cluster status every 5 seconds and continues to display "Waiting for FSx" indefinitely because it has no mechanism to detect that the backend workflow has stopped. This leaves users unable to take any action on the stuck cluster (they cannot retry or delete it).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the cluster creation Step Functions execution fails and the rollback handler (`handle_creation_failure`) also fails (throws an exception), THEN the system leaves the DynamoDB cluster record in CREATING status permanently because the catch on `handleCreationFailure` goes directly to the Fail state without updating DynamoDB.

1.2 WHEN the cluster creation Step Functions execution times out (exceeds the 2-hour state machine timeout), THEN the system leaves the DynamoDB cluster record in CREATING status permanently because no handler runs on timeout to update the record.

1.3 WHEN the DynamoDB cluster record is stuck in CREATING status and the Step Functions execution has ended, THEN the UI polls the cluster status every 5 seconds indefinitely, displaying stale progress information (e.g. "Waiting for FSx") with no indication that the workflow has stopped.

1.4 WHEN a cluster is stuck in CREATING status, THEN the user cannot destroy or recreate the cluster because the API rejects destroy requests for clusters not in ACTIVE or FAILED status.

### Expected Behavior (Correct)

2.1 WHEN the cluster creation Step Functions execution fails and the rollback handler also fails, THEN the system SHALL ensure the DynamoDB cluster record is updated to FAILED status with an appropriate error message, so the UI can reflect the terminal state.

2.2 WHEN the cluster creation Step Functions execution times out, THEN the system SHALL detect the stale CREATING record and update it to FAILED status with a timeout error message.

2.3 WHEN the UI is polling a cluster in CREATING status, THEN the system SHALL provide a mechanism for the UI to detect that the backend workflow has stopped and display a failure state to the user rather than polling indefinitely.

2.4 WHEN a cluster has been stuck in CREATING status beyond a reasonable threshold (e.g. the state machine timeout duration), THEN the system SHALL treat the cluster as FAILED so the user can take corrective action (destroy or recreate).

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the cluster creation workflow succeeds (all steps complete normally), THEN the system SHALL CONTINUE TO set the cluster status to ACTIVE with all resource IDs recorded in DynamoDB.

3.2 WHEN the cluster creation workflow fails and the rollback handler succeeds, THEN the system SHALL CONTINUE TO set the cluster status to FAILED with the error message and clean up partially created resources.

3.3 WHEN the UI polls a cluster that is legitimately in CREATING status (workflow still running), THEN the system SHALL CONTINUE TO display accurate progress information (current step, total steps, step description) updated every 5 seconds.

3.4 WHEN a cluster is in ACTIVE or FAILED status, THEN the system SHALL CONTINUE TO allow the user to destroy or recreate the cluster via the existing API endpoints.

3.5 WHEN the FSx polling loop is running normally within the 30-minute window, THEN the system SHALL CONTINUE TO poll every 30 seconds and report progress without prematurely marking the cluster as failed.
