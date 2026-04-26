# Cluster Template Resolution Bugfix Design

## Overview

The cluster creation Step Functions workflow fails with a `States.Runtime` error after the `ParallelFsxAndPcs` state completes because no step resolves template fields from the ClusterTemplates DynamoDB table before the parallel state executes. The `resultSelector` references fields like `$[1].loginInstanceType`, `$[1].instanceTypes`, `$[1].maxNodes`, etc., but the PCS branch only adds `pcsClusterId` and `pcsClusterArn` to the event.

The fix adds a new `resolve_template` step function in `cluster_creation.py` that reads the template from DynamoDB and injects the template-driven fields into the event payload. This step is inserted into the Step Functions chain between `check_budget_breach` and `ParallelFsxAndPcs`, and the creation step Lambda is granted read access to the ClusterTemplates table.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — a cluster creation event reaches the `ParallelFsxAndPcs` state without template-driven fields (`loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption`) in the payload
- **Property (P)**: The desired behavior — `resolve_template` reads the ClusterTemplates table and adds all template-driven fields to the event before the parallel state executes
- **Preservation**: Existing step functions (`validate_and_register_name`, `check_budget_breach`, `create_fsx_filesystem`, `create_pcs_cluster`, `create_login_node_group`, `create_compute_node_group`, rollback handler) must continue to work identically
- **`resolve_template`**: The new step function in `cluster_creation.py` that reads the ClusterTemplates DynamoDB table using `templateId` and adds template fields to the event
- **`ClusterTemplates` table**: DynamoDB table storing cluster templates with PK=`TEMPLATE#{templateId}`, SK=`METADATA`, containing fields like `loginInstanceType`, `instanceTypes`, `minNodes`, `maxNodes`
- **`resultSelector`**: The Step Functions JSONPath expression in the `ParallelFsxAndPcs` state that merges FSx and PCS branch outputs into a single flat object

## Bug Details

### Bug Condition

The bug manifests when a cluster creation or recreation request reaches the `ParallelFsxAndPcs` state. The `_handle_create_cluster` and `_handle_recreate_cluster` handlers pass `templateId` in the Step Functions payload, but no step in the workflow resolves the template's fields from the ClusterTemplates DynamoDB table. The `create_pcs_cluster` function only adds `pcsClusterId` and `pcsClusterArn` to the event — it does not read the template. When the parallel state completes, the `resultSelector` tries to extract `$[1].loginInstanceType` and other template fields from the PCS branch output, but they don't exist, causing a `States.Runtime` error.

**Formal Specification:**
```
FUNCTION isBugCondition(event)
  INPUT: event of type dict (Step Functions payload)
  OUTPUT: boolean

  RETURN event HAS KEY "templateId"
         AND event DOES NOT HAVE KEY "loginInstanceType"
         AND event DOES NOT HAVE KEY "instanceTypes"
         AND event DOES NOT HAVE KEY "maxNodes"
         AND event DOES NOT HAVE KEY "minNodes"
         AND event DOES NOT HAVE KEY "purchaseOption"
END FUNCTION
```

### Examples

- **Valid templateId, template exists**: Event has `templateId: "cpu-general"` but no `loginInstanceType`, `instanceTypes`, etc. The `resultSelector` fails with `States.Runtime` because `$[1].loginInstanceType` is not in the PCS branch output. Expected: `resolve_template` reads the template and adds `loginInstanceType: "c7g.medium"`, `instanceTypes: ["c7g.medium"]`, `maxNodes: 10`, `minNodes: 1`, `purchaseOption: "ONDEMAND"`.
- **Valid templateId, template does not exist**: Event has `templateId: "nonexistent"`. Expected: `resolve_template` raises a `ValidationError` so the workflow fails early with a clear error rather than a cryptic `States.Runtime` error.
- **Empty templateId**: Event has `templateId: ""`. Expected: `resolve_template` falls back to sensible defaults (`loginInstanceType: "c7g.medium"`, `instanceTypes: ["c7g.medium"]`, `maxNodes: 10`, `minNodes: 0`, `purchaseOption: "ONDEMAND"`).
- **Missing templateId key**: Event has no `templateId` key at all. Expected: same default fallback behavior as empty templateId.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `validate_and_register_name` must continue to validate cluster name format and register in ClusterNameRegistry
- `check_budget_breach` must continue to check project budget and block creation if breached
- `create_fsx_filesystem`, `check_fsx_status`, `create_fsx_dra` must continue to create and configure FSx filesystems identically
- `create_pcs_cluster` must continue to create PCS clusters with retry logic for ConflictException
- `create_login_node_group` and `create_compute_node_group` must continue to use `event.get(...)` with defaults for template fields
- `handle_creation_failure` must continue to roll back partially created resources on any step failure
- Mouse/API-initiated cluster creation and recreation flows must continue to pass `templateId` in the payload

**Scope:**
All inputs that do NOT involve the template resolution step should be completely unaffected by this fix. This includes:
- Events processed by steps before `resolve_template` (validate name, check budget)
- Events processed by steps after the parallel state (create node groups, create queue, tag, record)
- The FSx and PCS branch logic within the parallel state
- The rollback/failure handling chain

## Hypothesized Root Cause

Based on the bug description and code analysis, the root cause is:

1. **Missing template resolution step**: The Step Functions chain goes directly from `check_budget_breach` to `ParallelFsxAndPcs`. No step reads the ClusterTemplates DynamoDB table to resolve template fields. The `_handle_create_cluster` handler in `handler.py` passes `templateId` in the payload but never resolves it.

2. **`resultSelector` assumes fields exist**: The `ParallelFsxAndPcs` state's `resultSelector` references `$[1].loginInstanceType`, `$[1].instanceTypes`, `$[1].maxNodes`, `$[1].minNodes`, `$[1].purchaseOption`, `$[1].loginLaunchTemplateId`, `$[1].loginLaunchTemplateVersion`, `$[1].computeLaunchTemplateId`, `$[1].computeLaunchTemplateVersion`, and `$[1].instanceProfileArn` from the PCS branch output. These fields are never added to the event by `create_pcs_cluster`, which only adds `pcsClusterId` and `pcsClusterArn`.

3. **No environment variable for templates table**: The `clusterCreationStepLambda` in `foundation-stack.ts` does not have `TEMPLATES_TABLE_NAME` in its environment variables, so even if a resolve step existed in the code, it couldn't access the table.

4. **No DynamoDB read grant**: The creation step Lambda is not granted read access to the ClusterTemplates table.

## Correctness Properties

Property 1: Bug Condition - Template Fields Resolved for Valid TemplateId

_For any_ event where `templateId` is a non-empty string that corresponds to an existing record in the ClusterTemplates DynamoDB table, the `resolve_template` function SHALL return the event augmented with the template's `loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, and `purchaseOption` fields matching the values stored in the template record.

**Validates: Requirements 2.1, 2.3**

Property 2: Bug Condition - Default Fallback for Empty/Missing TemplateId

_For any_ event where `templateId` is empty or absent, the `resolve_template` function SHALL return the event augmented with sensible default values: `loginInstanceType` = `"c7g.medium"`, `instanceTypes` = `["c7g.medium"]`, `maxNodes` = `10`, `minNodes` = `0`, `purchaseOption` = `"ONDEMAND"`.

**Validates: Requirements 2.4**

Property 3: Preservation - Existing Step Functions Unchanged

_For any_ event processed by `validate_and_register_name`, `check_budget_breach`, `create_fsx_filesystem`, `create_pcs_cluster`, `create_login_node_group`, `create_compute_node_group`, `create_pcs_queue`, `tag_resources`, or `record_cluster`, the fixed code SHALL produce exactly the same output as the original code, preserving all existing step behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lambda/cluster_operations/cluster_creation.py`

**Function**: `resolve_template` (new)

**Specific Changes**:
1. **Add `TEMPLATES_TABLE_NAME` environment variable**: Read `os.environ.get("TEMPLATES_TABLE_NAME", "ClusterTemplates")` alongside the existing environment variables at module level.

2. **Implement `resolve_template` function**: New step function that:
   - Extracts `templateId` from the event
   - If `templateId` is non-empty, reads the template from ClusterTemplates table using `PK=TEMPLATE#{templateId}`, `SK=METADATA`
   - Raises `ValidationError` if the template is not found (fail fast with a clear error)
   - Adds `loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption` from the template to the event
   - If `templateId` is empty/missing, adds sensible defaults: `loginInstanceType="c7g.medium"`, `instanceTypes=["c7g.medium"]`, `maxNodes=10`, `minNodes=0`, `purchaseOption="ONDEMAND"`
   - Returns the augmented event

3. **Register in `_STEP_DISPATCH`**: Add `"resolve_template": resolve_template` to the dispatch table.

**File**: `lib/foundation-stack.ts`

**Specific Changes**:
4. **Add `TEMPLATES_TABLE_NAME` to creation step Lambda environment**: Add `TEMPLATES_TABLE_NAME: this.clusterTemplatesTable.tableName` to the `clusterCreationStepLambda` environment variables.

5. **Grant DynamoDB read on ClusterTemplates table**: Add `this.clusterTemplatesTable.grantReadData(clusterCreationStepLambda)`.

6. **Add `ResolveTemplate` step to state machine**: Create a new `tasks.LambdaInvoke` step for `resolve_template` and insert it into the chain between `checkBudgetBreach` and `parallelFsxAndPcs`. Add a catch handler for rollback.

7. **Update chain definition**: Change from `validateAndRegisterName.next(checkBudgetBreach).next(parallelFsxAndPcs)...` to `validateAndRegisterName.next(checkBudgetBreach).next(resolveTemplate).next(parallelFsxAndPcs)...`.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that call `create_pcs_cluster` with a valid event containing `templateId` and verify that the returned event does NOT contain template-driven fields. This confirms the root cause — `create_pcs_cluster` only adds `pcsClusterId` and `pcsClusterArn`.

**Test Cases**:
1. **PCS output missing template fields**: Call `create_pcs_cluster` with an event containing `templateId: "cpu-general"` and verify the output lacks `loginInstanceType`, `instanceTypes`, `maxNodes`, `minNodes`, `purchaseOption` (will confirm bug on unfixed code)
2. **No resolve_template in dispatch**: Verify that `_STEP_DISPATCH` does not contain a `"resolve_template"` key (will confirm bug on unfixed code)
3. **Event payload from handler**: Verify that `_handle_create_cluster` builds a payload with `templateId` but no template fields (will confirm bug on unfixed code)

**Expected Counterexamples**:
- `create_pcs_cluster` returns event with only `pcsClusterId` and `pcsClusterArn` added — no template fields
- `_STEP_DISPATCH` has no `resolve_template` entry
- The Step Functions chain has no template resolution step between budget check and parallel state

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL event WHERE isBugCondition(event) DO
  result := resolve_template(event)
  ASSERT result HAS KEY "loginInstanceType"
  ASSERT result HAS KEY "instanceTypes"
  ASSERT result HAS KEY "maxNodes"
  ASSERT result HAS KEY "minNodes"
  ASSERT result HAS KEY "purchaseOption"
  IF event.templateId IS NOT EMPTY THEN
    template := getTemplate(event.templateId)
    ASSERT result.loginInstanceType == template.loginInstanceType
    ASSERT result.instanceTypes == template.instanceTypes
    ASSERT result.maxNodes == template.maxNodes
    ASSERT result.minNodes == template.minNodes
  END IF
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL event WHERE NOT isBugCondition(event) DO
  ASSERT validate_and_register_name(event) == validate_and_register_name_original(event)
  ASSERT check_budget_breach(event) == check_budget_breach_original(event)
  ASSERT create_pcs_cluster(event) == create_pcs_cluster_original(event)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for existing step functions, then write property-based tests capturing that behavior. Since we are adding a new function (not modifying existing ones), preservation is largely guaranteed by design — but we verify the CDK chain still includes all original steps.

**Test Cases**:
1. **Existing steps unchanged**: Verify that `validate_and_register_name`, `check_budget_breach`, `create_pcs_cluster` produce identical outputs before and after the fix
2. **Downstream defaults preserved**: Verify that `create_login_node_group` and `create_compute_node_group` still use `event.get(...)` defaults when template fields are absent
3. **Rollback handler preserved**: Verify that `handle_creation_failure` still cleans up resources correctly
4. **CDK chain preserved**: Verify the state machine definition still includes all original steps in the correct order

### Unit Tests

- Test `resolve_template` with a valid `templateId` that exists in the table — verify all fields are added
- Test `resolve_template` with a valid `templateId` that does NOT exist — verify `ValidationError` is raised
- Test `resolve_template` with empty `templateId` — verify defaults are applied
- Test `resolve_template` with missing `templateId` key — verify defaults are applied
- Test `resolve_template` preserves all existing event keys (projectId, clusterName, etc.)
- Test that `_STEP_DISPATCH` contains `"resolve_template"` after the fix
- Test CDK stack includes `ResolveTemplate` step in the state machine chain

### Property-Based Tests

- Generate random valid template records and verify `resolve_template` correctly extracts and adds all template fields to the event
- Generate random events with empty/missing `templateId` and verify defaults are consistently applied
- Generate random events and verify `resolve_template` never removes or modifies existing event keys (preservation of pass-through fields)

### Integration Tests

- Test full creation chain: validate → budget → resolve_template → parallel(FSx, PCS) → login nodes → compute → queue → tag → record
- Test that the `resultSelector` in `ParallelFsxAndPcs` can find all template fields after `resolve_template` runs
- Test cluster recreation flow with template resolution
