# Bugfix Requirements Document

## Introduction

Cluster creation fails with `ValidationException: Launch template id is required` when the Step Functions state machine reaches the `create_login_node_group` step (and would similarly fail at `create_compute_node_group`). The root cause is that the `create_launch_templates` Python step handler exists and is registered in `_STEP_DISPATCH`, but was never wired into the Step Functions state machine definition in `lib/constructs/cluster-operations.ts`. As a result, `loginLaunchTemplateId` and `computeLaunchTemplateId` are never populated in the event payload, and PCS rejects the node group creation calls. Additionally, the `clusterCreationStepLambda` IAM role is missing `ec2:CreateLaunchTemplate` and `ec2:DeleteLaunchTemplate` permissions required by the handler.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a cluster creation workflow executes THEN the state machine transitions directly from instance profile readiness to `StorageModeChoice` without invoking the `create_launch_templates` step, leaving `loginLaunchTemplateId` and `computeLaunchTemplateId` absent from the event payload

1.2 WHEN the `create_login_node_group` step runs with an empty `loginLaunchTemplateId` THEN the system fails with `ValidationException: Launch template id is required` from the PCS CreateComputeNodeGroup API

1.3 WHEN the `create_compute_node_group` step would run with an empty `computeLaunchTemplateId` THEN the system would fail with the same `ValidationException` (currently unreachable because 1.2 fails first)

1.4 WHEN the `create_launch_templates` Python handler attempts to call `ec2:CreateLaunchTemplate` THEN the call would be denied because the `clusterCreationStepLambda` IAM role lacks `ec2:CreateLaunchTemplate` and `ec2:DeleteLaunchTemplate` permissions

### Expected Behavior (Correct)

2.1 WHEN a cluster creation workflow executes THEN the state machine SHALL invoke a `CreateLaunchTemplates` step (calling the existing `create_launch_templates` handler) after instance profile readiness and before `StorageModeChoice`, populating `loginLaunchTemplateId` and `computeLaunchTemplateId` in the event payload

2.2 WHEN the `create_login_node_group` step runs THEN the system SHALL receive a valid `loginLaunchTemplateId` from the event payload and successfully create the login node group without a `ValidationException`

2.3 WHEN the `create_compute_node_group` step runs THEN the system SHALL receive a valid `computeLaunchTemplateId` from the event payload and successfully create the compute node group without a `ValidationException`

2.4 WHEN the `create_launch_templates` Python handler calls `ec2:CreateLaunchTemplate` THEN the call SHALL succeed because the `clusterCreationStepLambda` IAM role SHALL have `ec2:CreateLaunchTemplate` and `ec2:DeleteLaunchTemplate` permissions

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a cluster creation workflow uses the lustre storage mode THEN the system SHALL CONTINUE TO execute the `ParallelFsxAndPcs` parallel branch (FSx + PCS) and its `resultSelector` SHALL CONTINUE TO correctly map `loginLaunchTemplateId` and `computeLaunchTemplateId` from `$[0]`

3.2 WHEN a cluster creation workflow uses the mountpoint storage mode THEN the system SHALL CONTINUE TO execute the mountpoint PCS branch and the launch template IDs SHALL flow through via `sfn.JsonPath.entirePayload`

3.3 WHEN a cluster creation step fails THEN the system SHALL CONTINUE TO route to the `HandleCreationFailure` rollback handler with the existing catch configuration

3.4 WHEN the rollback handler executes THEN the system SHALL CONTINUE TO clean up launch templates, IAM resources, PCS resources, and FSx resources as already implemented in the Python handler

3.5 WHEN any other creation step (validate name, budget check, resolve template, create IAM resources, wait for instance profiles, create PCS cluster, create queue, tag resources, record cluster) executes THEN the system SHALL CONTINUE TO behave identically to the current implementation
