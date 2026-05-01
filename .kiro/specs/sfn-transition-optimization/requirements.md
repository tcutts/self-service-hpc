# Requirements Document

## Introduction

This feature optimises the five HPC-platform Step Functions state machines to reduce state transitions and Lambda invocations, keeping usage within the AWS Free Tier (4,000 transitions and 1,000,000 Lambda invocations per month). Two complementary strategies are applied: (1) consolidating consecutive fast Lambda steps into single invocations, and (2) inserting calibrated pre-soak Wait states before polling loops so the first poll is likely to succeed immediately.

## Glossary

- **Optimizer**: The set of CDK construct changes and consolidated Lambda handler logic that implements the transition-reduction strategies
- **Consolidated_Lambda**: A single Lambda invocation that internally executes multiple sequential step functions that were previously separate state machine states
- **Pre_Soak_Wait**: A one-time Wait state inserted before a polling loop, calibrated to historical execution data, so the first poll attempt is likely to find the resource ready
- **Polling_Loop**: A repeating cycle of Lambda invocation → Choice → Wait → Lambda invocation used to check whether an asynchronous AWS resource has reached a target state
- **State_Machine**: An AWS Step Functions state machine that orchestrates a workflow as a series of states
- **Transition**: A single state-to-state movement within a Step Functions execution, counted toward the AWS Free Tier limit of 4,000 per month
- **Step_Handler**: A Python Lambda function that dispatches to individual step functions based on a `step` field in the event payload
- **Cluster_Creation_SM**: The `hpc-cluster-creation` state machine that provisions HPC clusters
- **Cluster_Destruction_SM**: The `hpc-cluster-destruction` state machine that tears down HPC clusters
- **Project_Deploy_SM**: The `hpc-project-deploy` state machine that deploys project infrastructure via CDK/CodeBuild
- **Project_Destroy_SM**: The `hpc-project-destroy` state machine that destroys project infrastructure via CDK/CodeBuild
- **Project_Update_SM**: The `hpc-project-update` state machine that updates project infrastructure via CDK/CodeBuild

## Requirements

### Requirement 1: Consolidate Cluster Creation Pre-Parallel Steps

**User Story:** As a platform operator, I want the four sequential fast steps before the parallel branch in cluster creation to execute as a single Lambda invocation, so that three state transitions and three Lambda invocations are eliminated per cluster creation.

#### Acceptance Criteria

1. WHEN a cluster creation execution begins, THE Consolidated_Lambda SHALL execute validate_and_register_name, check_budget_breach, resolve_template, and create_iam_resources sequentially within a single Lambda invocation
2. WHEN any sub-step within the Consolidated_Lambda fails, THE Consolidated_Lambda SHALL raise the original error with the step name that failed so that the existing catch block routes to the rollback handler
3. THE Consolidated_Lambda SHALL propagate all result fields (registered name, budget status, template fields, IAM resource ARNs) into the state machine payload identically to the original four separate invocations
4. WHEN the consolidated pre-parallel step completes, THE Cluster_Creation_SM SHALL proceed to the parallel provision branch with the same payload structure as before consolidation

### Requirement 2: Consolidate Cluster Creation Post-Parallel Tail Steps

**User Story:** As a platform operator, I want the four sequential fast steps after the node group wait loop (resolve login node details, create PCS queue, tag resources, record cluster) to execute as a single Lambda invocation, so that three state transitions and three Lambda invocations are eliminated per cluster creation.

#### Acceptance Criteria

1. WHEN node groups become active, THE Consolidated_Lambda SHALL execute resolve_login_node_details, create_pcs_queue, tag_resources, and record_cluster sequentially within a single Lambda invocation
2. WHEN any sub-step within the post-parallel Consolidated_Lambda fails, THE Consolidated_Lambda SHALL raise the original error with the step name that failed so that the existing catch block routes to the rollback handler
3. THE Consolidated_Lambda SHALL propagate all result fields (login node IP, instance ID, queue ID, tag results, cluster record) into the state machine payload identically to the original four separate invocations
4. THE Consolidated_Lambda SHALL preserve progress tracking (_update_step_progress calls) for each sub-step within the consolidated invocation

### Requirement 3: Consolidate Cluster Destruction Linear Chains

**User Story:** As a platform operator, I want consecutive fast steps in the cluster destruction workflow that are not separated by wait loops to be consolidated into fewer Lambda invocations, so that state transitions and Lambda invocations are reduced.

#### Acceptance Criteria

1. WHEN PCS sub-resources are confirmed deleted, THE Consolidated_Lambda SHALL execute delete_pcs_cluster, delete_fsx_filesystem, and the storage-mode-dependent cleanup (remove_mountpoint_s3_policy or skip) within a single Lambda invocation
2. WHEN the storage-mode-dependent cleanup completes, THE Consolidated_Lambda SHALL execute delete_iam_resources, delete_launch_templates, deregister_cluster_name, and record_cluster_destroyed within a single Lambda invocation
3. WHEN any sub-step within a destruction Consolidated_Lambda fails, THE Consolidated_Lambda SHALL raise the original error so that the existing catch block routes to the record_cluster_destruction_failed handler
4. THE Consolidated_Lambda SHALL accept the storageMode field from the payload and conditionally execute remove_mountpoint_s3_policy only when storageMode equals "mountpoint"

### Requirement 4: Consolidate Project Deploy Pre-Loop and Post-Loop Steps

**User Story:** As a platform operator, I want the validate-and-start pair and the extract-and-record pair in the project deploy workflow to each execute as single Lambda invocations, so that two state transitions and two Lambda invocations are eliminated per project deployment.

#### Acceptance Criteria

1. WHEN a project deploy execution begins, THE Consolidated_Lambda SHALL execute validate_project_state and start_cdk_deploy sequentially within a single Lambda invocation
2. WHEN the CodeBuild deploy completes, THE Consolidated_Lambda SHALL execute extract_stack_outputs and record_infrastructure sequentially within a single Lambda invocation
3. WHEN any sub-step within a deploy Consolidated_Lambda fails, THE Consolidated_Lambda SHALL raise the original error so that the existing catch block routes to the handle_deploy_failure handler
4. THE Consolidated_Lambda SHALL propagate all result fields into the state machine payload identically to the original separate invocations

### Requirement 5: Consolidate Project Update Pre-Loop and Post-Loop Steps

**User Story:** As a platform operator, I want the validate-and-start pair and the extract-and-record pair in the project update workflow to each execute as single Lambda invocations, so that two state transitions and two Lambda invocations are eliminated per project update.

#### Acceptance Criteria

1. WHEN a project update execution begins, THE Consolidated_Lambda SHALL execute validate_update_state and start_cdk_update sequentially within a single Lambda invocation
2. WHEN the CodeBuild update completes, THE Consolidated_Lambda SHALL execute extract_stack_outputs and record_updated_infrastructure sequentially within a single Lambda invocation
3. WHEN any sub-step within an update Consolidated_Lambda fails, THE Consolidated_Lambda SHALL raise the original error so that the existing catch block routes to the handle_update_failure handler
4. THE Consolidated_Lambda SHALL propagate all result fields into the state machine payload identically to the original separate invocations

### Requirement 6: Consolidate Project Destroy Pre-Loop and Post-Loop Steps

**User Story:** As a platform operator, I want the validate-and-start pair and the clear-and-archive pair in the project destroy workflow to each execute as single Lambda invocations, so that two state transitions and two Lambda invocations are eliminated per project destruction.

#### Acceptance Criteria

1. WHEN a project destroy execution begins, THE Consolidated_Lambda SHALL execute validate_and_check_clusters and start_cdk_destroy sequentially within a single Lambda invocation
2. WHEN the CodeBuild destroy completes, THE Consolidated_Lambda SHALL execute clear_infrastructure and archive_project sequentially within a single Lambda invocation
3. WHEN any sub-step within a destroy Consolidated_Lambda fails, THE Consolidated_Lambda SHALL raise the original error so that the existing catch block routes to the handle_destroy_failure handler
4. THE Consolidated_Lambda SHALL propagate all result fields into the state machine payload identically to the original separate invocations

### Requirement 7: Pre-Soak Wait for PCS Cluster Creation Polling Loop

**User Story:** As a platform operator, I want a calibrated wait inserted before the PCS cluster status polling loop, so that the first poll is likely to find the cluster active and 8-9 polling iterations are eliminated.

#### Acceptance Criteria

1. WHEN the PCS cluster creation API call completes, THE Cluster_Creation_SM SHALL enter a Pre_Soak_Wait state of 270 seconds before the first check_pcs_cluster_status invocation
2. WHEN the Pre_Soak_Wait completes, THE Cluster_Creation_SM SHALL invoke check_pcs_cluster_status and proceed through the existing Polling_Loop if the cluster is not yet active
3. THE Cluster_Creation_SM SHALL retain the existing 30-second Polling_Loop as a fallback after the Pre_Soak_Wait

### Requirement 8: Pre-Soak Wait for Node Group Creation Polling Loop

**User Story:** As a platform operator, I want a calibrated wait inserted before the node group status polling loop, so that the first poll is likely to find node groups active and 8-10 polling iterations are eliminated.

#### Acceptance Criteria

1. WHEN the compute node group creation step completes, THE Cluster_Creation_SM SHALL enter a Pre_Soak_Wait state of 270 seconds before the first check_node_groups_status invocation
2. WHEN the Pre_Soak_Wait completes, THE Cluster_Creation_SM SHALL invoke check_node_groups_status and proceed through the existing Polling_Loop if node groups are not yet active
3. THE Cluster_Creation_SM SHALL retain the existing 30-second Polling_Loop as a fallback after the Pre_Soak_Wait

### Requirement 9: Pre-Soak Wait for CodeBuild Deploy Polling Loop

**User Story:** As a platform operator, I want a calibrated wait inserted before the CodeBuild deploy status polling loop, so that the first poll is likely to find the build complete and 6-7 polling iterations are eliminated.

#### Acceptance Criteria

1. WHEN the start_cdk_deploy step completes (within the consolidated pre-loop Lambda), THE Project_Deploy_SM SHALL enter a Pre_Soak_Wait state of 210 seconds before the first check_deploy_status invocation
2. WHEN the Pre_Soak_Wait completes, THE Project_Deploy_SM SHALL invoke check_deploy_status and proceed through the existing Polling_Loop if the build is not yet complete
3. THE Project_Deploy_SM SHALL retain the existing 30-second Polling_Loop as a fallback after the Pre_Soak_Wait

### Requirement 10: Pre-Soak Wait for CodeBuild Update Polling Loop

**User Story:** As a platform operator, I want a calibrated wait inserted before the CodeBuild update status polling loop, so that the first poll is likely to find the build complete and 2-3 polling iterations are eliminated.

#### Acceptance Criteria

1. WHEN the start_cdk_update step completes (within the consolidated pre-loop Lambda), THE Project_Update_SM SHALL enter a Pre_Soak_Wait state of 90 seconds before the first check_update_status invocation
2. WHEN the Pre_Soak_Wait completes, THE Project_Update_SM SHALL invoke check_update_status and proceed through the existing Polling_Loop if the build is not yet complete
3. THE Project_Update_SM SHALL retain the existing 30-second Polling_Loop as a fallback after the Pre_Soak_Wait

### Requirement 11: Pre-Soak Wait for CodeBuild Destroy Polling Loop

**User Story:** As a platform operator, I want a calibrated wait inserted before the CodeBuild destroy status polling loop, so that polling iterations are reduced when the destroy build completes.

#### Acceptance Criteria

1. WHEN the start_cdk_destroy step completes (within the consolidated pre-loop Lambda), THE Project_Destroy_SM SHALL enter a Pre_Soak_Wait state of 210 seconds before the first check_destroy_status invocation
2. WHEN the Pre_Soak_Wait completes, THE Project_Destroy_SM SHALL invoke check_destroy_status and proceed through the existing Polling_Loop if the build is not yet complete
3. THE Project_Destroy_SM SHALL retain the existing 30-second Polling_Loop as a fallback after the Pre_Soak_Wait

### Requirement 12: Preserve Error Handling and Rollback Behaviour

**User Story:** As a platform operator, I want all existing error handling, catch blocks, and rollback behaviour to continue working correctly after optimisation, so that failed workflows still clean up resources and record failure status.

#### Acceptance Criteria

1. WHEN a consolidated step fails in the Cluster_Creation_SM, THE Cluster_Creation_SM SHALL route to the HandleCreationFailure handler and then to the CreationFailed terminal state, identically to the pre-optimisation behaviour
2. WHEN a consolidated step fails in the Cluster_Destruction_SM, THE Cluster_Destruction_SM SHALL route to the RecordClusterDestructionFailed handler and then to the DestructionFailed terminal state, identically to the pre-optimisation behaviour
3. WHEN a consolidated step fails in any project State_Machine, THE State_Machine SHALL route to the respective failure handler (HandleDeployFailure, HandleDestroyFailure, or HandleUpdateFailure) and then to the respective Failed terminal state
4. WHEN the rollback handler in the Cluster_Creation_SM itself fails, THE Cluster_Creation_SM SHALL route through the MarkClusterFailed DynamoDB direct-update state before reaching the CreationFailed terminal state

### Requirement 13: Preserve Parallel Branch Structure in Cluster Creation

**User Story:** As a platform operator, I want the parallel provisioning branch (storage, PCS cluster, launch templates) to remain parallel after optimisation, so that cluster creation time is not increased.

#### Acceptance Criteria

1. THE Cluster_Creation_SM SHALL execute the storage branch, PCS cluster branch, and launch template branch concurrently within a Parallel state after the consolidated pre-parallel step
2. THE Cluster_Creation_SM SHALL merge results from all three parallel branches using the same resultSelector mapping as before optimisation

### Requirement 14: Maintain Functional Equivalence

**User Story:** As a platform operator, I want the optimised state machines to produce identical outcomes (DynamoDB records, created AWS resources, error messages) as the original state machines, so that no user-facing behaviour changes.

#### Acceptance Criteria

1. FOR ALL valid cluster creation inputs, THE Cluster_Creation_SM SHALL produce the same DynamoDB cluster record, PCS cluster, node groups, queue, FSx filesystem (or Mountpoint S3 configuration), IAM resources, and launch templates as the pre-optimisation state machine
2. FOR ALL valid project deploy inputs, THE Project_Deploy_SM SHALL produce the same DynamoDB project record and CloudFormation stack outputs as the pre-optimisation state machine
3. FOR ALL valid project update inputs, THE Project_Update_SM SHALL produce the same DynamoDB project record and CloudFormation stack outputs as the pre-optimisation state machine
4. FOR ALL valid project destroy inputs, THE Project_Destroy_SM SHALL produce the same DynamoDB project record archival and CloudFormation stack deletion as the pre-optimisation state machine
5. FOR ALL valid cluster destruction inputs, THE Cluster_Destruction_SM SHALL produce the same resource deletions, name deregistration, and DynamoDB record update as the pre-optimisation state machine

### Requirement 15: Document Optimisation Changes

**User Story:** As a platform operator, I want the optimisation changes documented, so that future maintainers understand the consolidation mapping and pre-soak calibration rationale.

#### Acceptance Criteria

1. WHEN the optimisation is deployed, THE Optimizer SHALL include documentation in `docs/` that maps each consolidated Lambda step to the original individual steps it replaces
2. THE Optimizer SHALL document the historical execution data used to calibrate each Pre_Soak_Wait duration and the rationale for the chosen values
