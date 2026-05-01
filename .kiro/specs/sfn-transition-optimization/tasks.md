# Implementation Plan: SFN Transition Optimization

## Overview

Optimize five HPC-platform Step Functions state machines to reduce state transitions and Lambda invocations by: (1) consolidating consecutive fast Lambda steps into single invocations, and (2) inserting calibrated pre-soak Wait states before polling loops. Implementation proceeds Lambda-handler-first (Python), then CDK construct changes (TypeScript), then documentation.

## Tasks

- [x] 1. Implement consolidated step handlers for cluster creation
  - [x] 1.1 Add `consolidated_pre_parallel` handler to `lambda/cluster_operations/cluster_creation.py`
    - Implement a new function that calls `validate_and_register_name`, `check_budget_breach`, `resolve_template`, and `create_iam_resources` sequentially
    - Each sub-step receives the accumulated payload from prior steps via `{**event, **result}`
    - Exceptions propagate directly (fail-fast, no catch) to preserve existing error routing
    - Register `consolidated_pre_parallel` in `_STEP_DISPATCH`
    - _Requirements: 1.1, 1.2, 1.3, 14.1_

  - [x] 1.2 Add `consolidated_post_parallel` handler to `lambda/cluster_operations/cluster_creation.py`
    - Implement a new function that calls `resolve_login_node_details`, `create_pcs_queue`, `tag_resources`, and `record_cluster` sequentially
    - Preserve `_update_step_progress` calls for each sub-step within the consolidated invocation
    - Register `consolidated_post_parallel` in `_STEP_DISPATCH`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 14.1_

  - [x] 1.3 Write property test for cluster creation pre-parallel output equivalence
    - **Property 1: Cluster creation pre-parallel output equivalence**
    - Use Hypothesis `@st.composite` to generate valid cluster creation event payloads with random `projectId`, `clusterName`, `templateId`, and other fields
    - Mock AWS SDK calls to return deterministic responses based on input
    - Verify `consolidated_pre_parallel(event)` produces the same output as calling the four steps sequentially
    - **Validates: Requirements 1.1, 1.3, 14.1**

  - [x] 1.4 Write property test for cluster creation post-parallel output equivalence
    - **Property 2: Cluster creation post-parallel output equivalence**
    - Generate valid post-parallel event payloads containing fields from all three parallel branches
    - Verify `consolidated_post_parallel(event)` produces the same output as calling the four tail steps sequentially
    - **Validates: Requirements 2.1, 2.3, 14.1**

- [x] 2. Implement consolidated step handlers for cluster destruction
  - [x] 2.1 Add `consolidated_delete_resources` handler to `lambda/cluster_operations/cluster_destruction.py`
    - Implement a new function that calls `delete_pcs_cluster_step`, `delete_fsx_filesystem`, and conditionally `remove_mountpoint_s3_policy` (when `storageMode == "mountpoint"`)
    - Register `consolidated_delete_resources` in `_STEP_DISPATCH`
    - _Requirements: 3.1, 3.4, 14.5_

  - [x] 2.2 Add `consolidated_cleanup` handler to `lambda/cluster_operations/cluster_destruction.py`
    - Implement a new function that calls `delete_iam_resources`, `delete_launch_templates`, `deregister_cluster_name_step`, and `record_cluster_destroyed` sequentially
    - Register `consolidated_cleanup` in `_STEP_DISPATCH`
    - _Requirements: 3.2, 3.3, 14.5_

  - [x] 2.3 Write property test for cluster destruction consolidated delete output equivalence
    - **Property 3: Cluster destruction consolidated delete output equivalence**
    - Generate valid destruction event payloads with `storageMode` in `{"lustre", "mountpoint"}`
    - Verify `consolidated_delete_resources(event)` produces the same output as calling the constituent steps sequentially, including conditional `remove_mountpoint_s3_policy`
    - **Validates: Requirements 3.1, 3.4, 14.5**

  - [x] 2.4 Write property test for cluster destruction consolidated cleanup output equivalence
    - **Property 4: Cluster destruction consolidated cleanup output equivalence**
    - Verify `consolidated_cleanup(event)` produces the same output as calling the four cleanup steps sequentially
    - **Validates: Requirements 3.2, 14.5**

- [x] 3. Implement consolidated step handlers for project lifecycle workflows
  - [x] 3.1 Add `consolidated_pre_loop` and `consolidated_post_loop` handlers to `lambda/project_management/project_deploy.py`
    - `consolidated_pre_loop` calls `validate_project_state` then `start_cdk_deploy` sequentially
    - `consolidated_post_loop` calls `extract_stack_outputs` then `record_infrastructure` sequentially
    - Register both in `STEP_DISPATCH`
    - _Requirements: 4.1, 4.2, 4.3, 14.2_

  - [x] 3.2 Add `consolidated_pre_loop` and `consolidated_post_loop` handlers to `lambda/project_management/project_update.py`
    - `consolidated_pre_loop` calls `validate_update_state` then `start_cdk_update` sequentially
    - `consolidated_post_loop` calls `extract_stack_outputs` then `record_updated_infrastructure` sequentially
    - Register both in `STEP_DISPATCH`
    - _Requirements: 5.1, 5.2, 5.3, 14.3_

  - [x] 3.3 Add `consolidated_pre_loop` and `consolidated_post_loop` handlers to `lambda/project_management/project_destroy.py`
    - `consolidated_pre_loop` calls `validate_and_check_clusters` then `start_cdk_destroy` sequentially
    - `consolidated_post_loop` calls `clear_infrastructure` then `archive_project` sequentially
    - Register both in `STEP_DISPATCH`
    - _Requirements: 6.1, 6.2, 6.3, 14.4_

  - [x] 3.4 Write property test for project lifecycle consolidated pre-loop output equivalence
    - **Property 5: Project lifecycle consolidated pre-loop output equivalence**
    - Generate valid project event payloads and test all three workflows (deploy, update, destroy)
    - Verify each `consolidated_pre_loop(event)` produces the same output as calling the two constituent pre-loop steps sequentially
    - **Validates: Requirements 4.1, 5.1, 6.1, 14.2, 14.3, 14.4**

  - [x] 3.5 Write property test for project lifecycle consolidated post-loop output equivalence
    - **Property 6: Project lifecycle consolidated post-loop output equivalence**
    - Verify each `consolidated_post_loop(event)` produces the same output as calling the two constituent post-loop steps sequentially
    - **Validates: Requirements 4.2, 5.2, 6.2, 14.2, 14.3, 14.4**

  - [x] 3.6 Write property test for error propagation preservation
    - **Property 7: Error propagation preservation**
    - For each consolidated handler, inject a failure at each sub-step position
    - Verify the consolidated handler re-raises an exception of the same type with the original error message, and no subsequent sub-steps are executed
    - **Validates: Requirements 1.2, 2.2, 3.3, 4.3, 5.3, 6.3, 12.1, 12.2, 12.3**

- [x] 4. Checkpoint — Verify all consolidated handlers and property tests
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Update cluster creation CDK state machine in `lib/constructs/cluster-operations.ts`
  - [x] 5.1 Replace pre-parallel sequential states with consolidated invocation
    - Replace the four `LambdaInvoke` states (`ValidateAndRegisterName` → `CheckBudgetBreach` → `ResolveTemplate` → `CreateIamResources`) with a single `LambdaInvoke` dispatching to `consolidated_pre_parallel`
    - Retain `addCatch` configuration routing to `HandleCreationFailure`
    - Chain: `ConsolidatedPreParallel` → `ParallelProvision`
    - _Requirements: 1.1, 1.4, 12.1, 13.1, 13.2_

  - [x] 5.2 Insert pre-soak Wait states for cluster creation polling loops
    - Add a 270-second `Wait` state between `CreatePcsCluster` and the first `CheckPcsClusterStatus` invocation (inside the PCS parallel branch)
    - Add a 270-second `Wait` state between `CreateComputeNodeGroup` and the first `CheckNodeGroupsStatus` invocation
    - Retain existing 30-second polling loops as fallbacks
    - _Requirements: 7.1, 7.2, 7.3, 8.1, 8.2, 8.3_

  - [x] 5.3 Replace post-parallel tail states with consolidated invocation
    - Replace the four `LambdaInvoke` states (`ResolveLoginNodeDetails` → `CreatePcsQueue` → `TagResources` → `RecordCluster`) with a single `LambdaInvoke` dispatching to `consolidated_post_parallel`
    - Retain `addCatch` configuration routing to `HandleCreationFailure`
    - Chain: `AreNodeGroupsActive` (when true) → `ConsolidatedPostParallel` → `CreationSucceeded`
    - _Requirements: 2.1, 12.1, 12.4_

- [x] 6. Update cluster destruction CDK state machine in `lib/constructs/cluster-operations.ts`
  - [x] 6.1 Replace post-PCS-deletion linear chains with consolidated invocations
    - Replace `DeletePcsCluster` → `DeleteFsxFilesystem` → `StorageModeDestroyChoice` → (`RemoveMountpointS3Policy` | skip) with a single `ConsolidatedDeleteResources` `LambdaInvoke`
    - Replace `DeleteIamResources` → `DeleteLaunchTemplates` → `DeregisterClusterName` → `RecordClusterDestroyed` with a single `ConsolidatedCleanup` `LambdaInvoke`
    - Retain `addCatch` configurations routing to `RecordClusterDestructionFailed`
    - Chain: `ArePcsSubResourcesDeleted` (when true) → `ConsolidatedDeleteResources` → `ConsolidatedCleanup` → `DestructionSucceeded`
    - _Requirements: 3.1, 3.2, 3.3, 12.2_

- [x] 7. Update project lifecycle CDK state machines in `lib/constructs/project-lifecycle.ts`
  - [x] 7.1 Update project deploy state machine
    - Replace `ValidateProjectState` → `StartCdkDeploy` with a single `ConsolidatedPreLoop` `LambdaInvoke` dispatching to `consolidated_pre_loop`
    - Insert a 210-second pre-soak `Wait` state between `ConsolidatedPreLoop` and the first `CheckDeployStatus`
    - Replace `ExtractStackOutputs` → `RecordInfrastructure` with a single `ConsolidatedPostLoop` `LambdaInvoke` dispatching to `consolidated_post_loop`
    - Retain `addCatch` configurations routing to `HandleDeployFailure`
    - Retain existing 30-second polling loop as fallback
    - _Requirements: 4.1, 4.2, 4.3, 9.1, 9.2, 9.3, 12.3_

  - [x] 7.2 Update project update state machine
    - Replace `ValidateUpdateState` → `StartCdkUpdate` with a single `ConsolidatedPreLoop` `LambdaInvoke` dispatching to `consolidated_pre_loop`
    - Insert a 90-second pre-soak `Wait` state between `ConsolidatedPreLoop` and the first `CheckUpdateStatus`
    - Replace `ExtractUpdateStackOutputs` → `RecordUpdatedInfrastructure` with a single `ConsolidatedPostLoop` `LambdaInvoke` dispatching to `consolidated_post_loop`
    - Retain `addCatch` configurations routing to `HandleUpdateFailure`
    - Retain existing 30-second polling loop as fallback
    - _Requirements: 5.1, 5.2, 5.3, 10.1, 10.2, 10.3, 12.3_

  - [x] 7.3 Update project destroy state machine
    - Replace `ValidateAndCheckClusters` → `StartCdkDestroy` with a single `ConsolidatedPreLoop` `LambdaInvoke` dispatching to `consolidated_pre_loop`
    - Insert a 210-second pre-soak `Wait` state between `ConsolidatedPreLoop` and the first `CheckDestroyStatus`
    - Replace `ClearInfrastructure` → `ArchiveProject` with a single `ConsolidatedPostLoop` `LambdaInvoke` dispatching to `consolidated_post_loop`
    - Retain `addCatch` configurations routing to `HandleDestroyFailure`
    - Retain existing 30-second polling loop as fallback
    - _Requirements: 6.1, 6.2, 6.3, 11.1, 11.2, 11.3, 12.3_

- [x] 8. Checkpoint — Verify CDK synth and all state machine structures
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Create optimization documentation
  - [x] 9.1 Create `docs/sfn-transition-optimization.md`
    - Document the consolidation mapping: which consolidated step replaces which original steps for all five state machines
    - Document the pre-soak calibration rationale with historical execution data (PCS cluster 270s, node groups 270s, CodeBuild deploy 210s, CodeBuild update 90s, CodeBuild destroy 210s)
    - Include transition count estimates before and after optimization for each state machine
    - _Requirements: 15.1, 15.2_

- [x] 10. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties (output equivalence of consolidated vs sequential execution)
- Unit tests validate specific examples and edge cases
- Python handlers are implemented first (tasks 1–3) so CDK changes (tasks 5–7) can reference the new step names
- The Hypothesis library (already in the project) is used for property-based testing
