# Bugfix Requirements Document

## Introduction

The previous bugfix spec (`instance-profile-per-cluster`) correctly implemented per-cluster IAM resource management in the Lambda functions (`cluster_creation.py` and `cluster_destruction.py`). However, the CDK Step Functions state machine definitions in `lib/foundation-stack.ts` were never updated to invoke these new Lambda steps. As a result, the `create_iam_resources` and `wait_for_instance_profiles` steps are never called during cluster creation, and `delete_iam_resources` is never called during cluster destruction. This causes cluster creation to fail because `loginInstanceProfileArn` and `computeInstanceProfileArn` are never set in the event, and IAM resources are orphaned on destruction.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a cluster creation Step Functions execution runs THEN the state machine chain is `validate → budget → resolveTemplate → parallel(FSx, PCS) → loginNodes → compute → queue → tag → record → success` with no `CreateIamResources` or `WaitForInstanceProfiles` LambdaInvoke tasks defined or chained

1.2 WHEN the Parallel state completes during cluster creation THEN the `resultSelector` maps `'instanceProfileArn.$': '$[0].instanceProfileArn'` which references the old project-level field that no longer exists in the event payload

1.3 WHEN `create_login_node_group` and `create_compute_node_group` execute THEN they receive empty strings for `loginInstanceProfileArn` and `computeInstanceProfileArn` because these keys were never set by any preceding step in the state machine

1.4 WHEN a cluster destruction Step Functions execution runs THEN the state machine chain is `exportFsx → waitForExport → deletePcs → deleteFsx → recordDestroyed → success` with no `DeleteIamResources` LambdaInvoke task defined or chained

1.5 WHEN a cluster is destroyed THEN the per-cluster IAM roles and instance profiles (`AWSPCS-{projectId}-{clusterName}-login` and `AWSPCS-{projectId}-{clusterName}-compute`) are never cleaned up and remain orphaned in the AWS account

### Expected Behavior (Correct)

2.1 WHEN a cluster creation Step Functions execution runs THEN the state machine SHALL include a `CreateIamResources` LambdaInvoke task that invokes the cluster creation step Lambda with `step: 'create_iam_resources'`, positioned before the Parallel FSx/PCS state

2.2 WHEN a cluster creation Step Functions execution runs THEN the state machine SHALL include a `WaitForInstanceProfiles` LambdaInvoke task (with a retry/wait loop checking `instanceProfilesReady`) that invokes the cluster creation step Lambda with `step: 'wait_for_instance_profiles'`, positioned after `CreateIamResources` and before the Parallel FSx/PCS state

2.3 WHEN the Parallel state completes during cluster creation THEN the `resultSelector` SHALL map `loginInstanceProfileArn` and `computeInstanceProfileArn` from the branch outputs (passed through from the preceding IAM steps) instead of the removed `instanceProfileArn` field

2.4 WHEN `create_login_node_group` executes THEN it SHALL receive a non-empty `loginInstanceProfileArn` value in the event payload that was set by the `create_iam_resources` step earlier in the chain

2.5 WHEN `create_compute_node_group` executes THEN it SHALL receive a non-empty `computeInstanceProfileArn` value in the event payload that was set by the `create_iam_resources` step earlier in the chain

2.6 WHEN a cluster destruction Step Functions execution runs THEN the state machine SHALL include a `DeleteIamResources` LambdaInvoke task that invokes the cluster destruction step Lambda with `step: 'delete_iam_resources'`, positioned after `DeleteFsxFilesystem` and before `RecordClusterDestroyed`

2.7 WHEN the `CreateIamResources` or `WaitForInstanceProfiles` steps fail during cluster creation THEN the error SHALL be caught and routed to the existing rollback handler (`HandleCreationFailure`) consistent with other steps in the chain

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the cluster creation state machine executes THEN the system SHALL CONTINUE TO run `ValidateAndRegisterName`, `CheckBudgetBreach`, `ResolveTemplate`, `CreateFsxFilesystem`, `CheckFsxStatus`, `CreateFsxDra`, `CreatePcsCluster`, `CreateLoginNodeGroup`, `CreateComputeNodeGroup`, `CreatePcsQueue`, `TagResources`, and `RecordCluster` steps in their existing relative order

3.2 WHEN the Parallel FSx/PCS state executes THEN the system SHALL CONTINUE TO run the FSx branch (create → check status → wait loop → DRA) and PCS branch concurrently, merging their outputs via `resultSelector`

3.3 WHEN any creation step fails THEN the system SHALL CONTINUE TO route errors to the `HandleCreationFailure` rollback handler and then to the `CreationFailed` Fail state

3.4 WHEN the cluster destruction state machine executes THEN the system SHALL CONTINUE TO run `CreateFsxExportTask`, `CheckFsxExportStatus` (with wait loop), `DeletePcsResources`, `DeleteFsxFilesystem`, and `RecordClusterDestroyed` steps in their existing relative order

3.5 WHEN the `WaitForInstanceProfiles` step returns `instanceProfilesReady: false` THEN the system SHALL wait and retry (consistent with the existing FSx wait loop pattern) rather than proceeding with empty profile ARNs

3.6 WHEN the CDK stack is synthesized THEN the system SHALL CONTINUE TO produce valid CloudFormation templates for both state machines with correct Lambda ARN references and IAM permissions
