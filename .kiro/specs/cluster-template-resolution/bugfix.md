# Bugfix Requirements Document

## Introduction

Cluster creation fails when the Step Functions `ParallelFsxAndPcs` state completes. The `resultSelector` expects template-driven fields (`loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption`, `loginLaunchTemplateId`, `loginLaunchTemplateVersion`, `computeLaunchTemplateId`, `computeLaunchTemplateVersion`, `instanceProfileArn`) to be present in the PCS branch output (`$[1]`), but no step in the workflow resolves the cluster template from DynamoDB and injects these fields into the event payload. Both parallel branches complete successfully (FSx filesystem created, PCS cluster created), but the state machine then fails with a `States.Runtime` error because the JSONPath `$[1].loginInstanceType` cannot be found in the input.

This affects both initial cluster creation and recreation of destroyed clusters. The `_handle_create_cluster` and `_handle_recreate_cluster` handlers pass `templateId` in the payload but never resolve the template fields from the `ClusterTemplates` DynamoDB table before starting the state machine execution.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a cluster is created or recreated with a valid `templateId` THEN the system starts the Step Functions execution with only `templateId` in the payload, without resolving the template's fields (`loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption`) from the ClusterTemplates DynamoDB table

1.2 WHEN the `ParallelFsxAndPcs` state completes successfully (both FSx and PCS branches finish) THEN the `resultSelector` fails with a `States.Runtime` error because it references `$[1].loginInstanceType` and other template-driven fields that do not exist in the PCS branch output

1.3 WHEN the `resultSelector` fails THEN the entire cluster creation execution fails despite both the FSx filesystem and PCS cluster having been created successfully, leaving orphaned cloud resources

1.4 WHEN a cluster is created with an empty or missing `templateId` THEN the system has no mechanism to fall back to sensible default values for template fields before the parallel state executes

### Expected Behavior (Correct)

2.1 WHEN a cluster is created or recreated with a valid `templateId` THEN the system SHALL resolve the template from the ClusterTemplates DynamoDB table and add the template fields (`loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption`) to the event payload before the `ParallelFsxAndPcs` state executes

2.2 WHEN the `ParallelFsxAndPcs` state completes successfully THEN the `resultSelector` SHALL find all required template-driven fields in the PCS branch output and merge them into a single flat object for downstream steps

2.3 WHEN template resolution succeeds THEN the downstream steps (`create_login_node_group`, `create_compute_node_group`) SHALL receive the resolved template fields via the event and use them for instance configuration

2.4 WHEN a cluster is created with an empty or missing `templateId` THEN the system SHALL use sensible default values for template fields (`loginInstanceType`: `c7g.medium`, `instanceTypes`: `["c7g.medium"]`, `maxNodes`: `10`, `minNodes`: `0`, `purchaseOption`: `ONDEMAND`) so the workflow can proceed without failure

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the `validate_and_register_name` step executes THEN the system SHALL CONTINUE TO validate cluster name format and register it in the ClusterNameRegistry table

3.2 WHEN the `check_budget_breach` step executes THEN the system SHALL CONTINUE TO check the project budget and block creation if the budget is breached

3.3 WHEN the FSx branch of the parallel state executes THEN the system SHALL CONTINUE TO create the FSx filesystem, wait for availability, and create the data repository association

3.4 WHEN the PCS branch of the parallel state executes THEN the system SHALL CONTINUE TO create the PCS cluster with retry logic for ConflictException

3.5 WHEN any step in the creation workflow fails THEN the system SHALL CONTINUE TO catch the error and route to the rollback handler for cleanup of partially created resources

3.6 WHEN `create_login_node_group` or `create_compute_node_group` receives template fields via `event.get(...)` with defaults THEN the system SHALL CONTINUE TO use those defaults when fields are absent, maintaining backward compatibility
