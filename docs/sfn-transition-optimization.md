# Step Functions Transition Optimization

## Overview

This document describes the optimization applied to the five HPC-platform Step Functions state machines to reduce state transitions and Lambda invocations, keeping usage within the AWS Free Tier limits (4,000 transitions/month, 1,000,000 Lambda invocations/month).

Two complementary strategies are used:

1. **Lambda consolidation** — consecutive fast Lambda steps that run sequentially without intervening waits are merged into a single Lambda invocation.
2. **Pre-soak Wait states** — a one-time `Wait` state is inserted before each polling loop, calibrated to historical execution data so the first poll is likely to find the resource ready. The existing polling loop is retained as a fallback.

---

## Consolidation Mapping

Each consolidated Lambda handler calls the original step functions sequentially, propagating the accumulated payload between steps. Errors propagate directly (fail-fast) to preserve existing catch-block routing.

### Cluster Creation (`hpc-cluster-creation`)

| Consolidated Step | Replaces | Handler Location |
|---|---|---|
| `consolidated_pre_parallel` | `validate_and_register_name` → `check_budget_breach` → `resolve_template` → `create_iam_resources` | `lambda/cluster_operations/cluster_creation.py` |
| `consolidated_post_parallel` | `resolve_login_node_details` → `create_pcs_queue` → `tag_resources` → `record_cluster` | `lambda/cluster_operations/cluster_creation.py` |

- **Pre-parallel**: 4 original steps → 1 consolidated invocation (eliminates 3 transitions and 3 Lambda invocations)
- **Post-parallel**: 4 original steps → 1 consolidated invocation (eliminates 3 transitions and 3 Lambda invocations)
- Error handling: both consolidated steps route to `HandleCreationFailure` → `CreationFailed`, identical to pre-optimization behaviour
- The `consolidated_post_parallel` handler preserves `_update_step_progress` calls for each sub-step

### Cluster Destruction (`hpc-cluster-destruction`)

| Consolidated Step | Replaces | Handler Location |
|---|---|---|
| `consolidated_delete_resources` | `delete_pcs_cluster_step` → `delete_fsx_filesystem` → (conditionally) `remove_mountpoint_s3_policy` | `lambda/cluster_operations/cluster_destruction.py` |
| `consolidated_cleanup` | `delete_iam_resources` → `delete_launch_templates` → `deregister_cluster_name_step` → `record_cluster_destroyed` | `lambda/cluster_operations/cluster_destruction.py` |

- **Delete resources**: 3–4 original steps → 1 consolidated invocation. The `remove_mountpoint_s3_policy` step executes only when `storageMode == "mountpoint"`.
- **Cleanup**: 4 original steps → 1 consolidated invocation (eliminates 3 transitions and 3 Lambda invocations)
- Error handling: both consolidated steps route to `RecordClusterDestructionFailed` → `DestructionFailed`

### Project Deploy (`hpc-project-deploy`)

| Consolidated Step | Replaces | Handler Location |
|---|---|---|
| `consolidated_pre_loop` | `validate_project_state` → `start_cdk_deploy` | `lambda/project_management/project_deploy.py` |
| `consolidated_post_loop` | `extract_stack_outputs` → `record_infrastructure` | `lambda/project_management/project_deploy.py` |

- **Pre-loop**: 2 original steps → 1 consolidated invocation (eliminates 1 transition and 1 Lambda invocation)
- **Post-loop**: 2 original steps → 1 consolidated invocation (eliminates 1 transition and 1 Lambda invocation)
- Error handling: both consolidated steps route to `HandleDeployFailure` → `DeployFailed`

### Project Update (`hpc-project-update`)

| Consolidated Step | Replaces | Handler Location |
|---|---|---|
| `consolidated_pre_loop` | `validate_update_state` → `start_cdk_update` | `lambda/project_management/project_update.py` |
| `consolidated_post_loop` | `extract_stack_outputs` → `record_updated_infrastructure` | `lambda/project_management/project_update.py` |

- **Pre-loop**: 2 original steps → 1 consolidated invocation (eliminates 1 transition and 1 Lambda invocation)
- **Post-loop**: 2 original steps → 1 consolidated invocation (eliminates 1 transition and 1 Lambda invocation)
- Error handling: both consolidated steps route to `HandleUpdateFailure` → `UpdateFailed`

### Project Destroy (`hpc-project-destroy`)

| Consolidated Step | Replaces | Handler Location |
|---|---|---|
| `consolidated_pre_loop` | `validate_and_check_clusters` → `start_cdk_destroy` | `lambda/project_management/project_destroy.py` |
| `consolidated_post_loop` | `clear_infrastructure` → `archive_project` | `lambda/project_management/project_destroy.py` |

- **Pre-loop**: 2 original steps → 1 consolidated invocation (eliminates 1 transition and 1 Lambda invocation)
- **Post-loop**: 2 original steps → 1 consolidated invocation (eliminates 1 transition and 1 Lambda invocation)
- Error handling: both consolidated steps route to `HandleDestroyFailure` → `DestroyFailed`

---

## Pre-Soak Wait Calibration

Pre-soak Wait states are inserted before polling loops to skip the majority of polling iterations. Each duration is calibrated from historical execution data, targeting approximately the 10th percentile of observed completion times. This ensures the resource is ready on the first poll in the majority of executions while avoiding unnecessary waiting in fast cases.

Wait states in Step Functions are free (no transition cost), making this a zero-cost optimization.

### Calibration Data

| Polling Loop | Historical Wait Iterations | Avg Time per Iteration | Total Historical Wait | Pre-Soak Duration | Expected Remaining Polls |
|---|---|---|---|---|---|
| PCS Cluster (creation) | 10–11 | 30s | 300–333s | **270s** | 0–2 |
| Node Groups (creation) | 10–12 | 30s | 304–366s | **270s** | 0–3 |
| CodeBuild Deploy | 8 | 30s | ~240s | **210s** | 0–1 |
| CodeBuild Update | 4–5 | 30s | 120–150s | **90s** | 0–2 |
| CodeBuild Destroy | ~8 | 30s | ~240s | **210s** | 0–1 |

### How It Works

```
[Trigger Step] → [Pre-Soak Wait N seconds] → [Check Status]
                                                  ├─ Ready → Continue
                                                  └─ Not Ready → [Fallback Wait 30s] → [Check Status] ↻
```

The existing 30-second polling loop is retained as a fallback after the pre-soak wait. If the resource completes during the pre-soak period, the first poll succeeds immediately. If not, the fallback loop handles the remaining wait with the same interval as before optimization.

### Rationale for Each Duration

- **PCS Cluster (270s)**: Historical data shows cluster creation takes 300–333s. A 270s pre-soak covers the bulk of the wait, leaving 0–2 fallback polls (vs. 10–11 polls without pre-soak).
- **Node Groups (270s)**: Historical data shows node group creation takes 304–366s. A 270s pre-soak eliminates 8–10 of the original 10–12 polling iterations.
- **CodeBuild Deploy (210s)**: Historical data shows deploy builds take ~240s. A 210s pre-soak eliminates 6–7 of the original 8 polling iterations.
- **CodeBuild Update (90s)**: Historical data shows update builds take 120–150s. A 90s pre-soak eliminates 2–3 of the original 4–5 polling iterations.
- **CodeBuild Destroy (210s)**: Historical data shows destroy builds take ~240s. A 210s pre-soak eliminates 6–7 of the original 8 polling iterations.

---

## Transition Count Estimates

The tables below show estimated state transitions per execution before and after optimization. Transition savings come from both Lambda consolidation (fewer states) and pre-soak waits (fewer polling iterations).

### Cluster Creation (`hpc-cluster-creation`)

| Component | Before (transitions) | After (transitions) | Savings |
|---|---|---|---|
| Pre-parallel steps | 8 (4 invoke + 4 transitions) | 2 (1 invoke + 1 transition) | 6 |
| PCS cluster polling loop | 30–33 (10–11 iterations × 3) | 0–6 (0–2 iterations × 3) | 24–33 |
| Node group polling loop | 30–36 (10–12 iterations × 3) | 0–9 (0–3 iterations × 3) | 21–36 |
| Post-parallel steps | 8 (4 invoke + 4 transitions) | 2 (1 invoke + 1 transition) | 6 |
| Pre-soak waits (2×) | 0 | 4 (2 wait states × 2 transitions) | -4 |
| **Total estimated savings** | | | **53–77** |
| **Typical total** | **~296–305** | **~225–240** | |

### Cluster Destruction (`hpc-cluster-destruction`)

| Component | Before (transitions) | After (transitions) | Savings |
|---|---|---|---|
| Delete resources chain | 8–10 (4–5 invoke + transitions) | 2 (1 invoke + 1 transition) | 6–8 |
| Cleanup chain | 8 (4 invoke + 4 transitions) | 2 (1 invoke + 1 transition) | 6 |
| **Total estimated savings** | | | **12–14** |
| **Typical total** | **~65–74** | **~53–60** | |

### Project Deploy (`hpc-project-deploy`)

| Component | Before (transitions) | After (transitions) | Savings |
|---|---|---|---|
| Pre-loop steps | 4 (2 invoke + 2 transitions) | 2 (1 invoke + 1 transition) | 2 |
| CodeBuild polling loop | 24 (8 iterations × 3) | 0–3 (0–1 iterations × 3) | 21–24 |
| Post-loop steps | 4 (2 invoke + 2 transitions) | 2 (1 invoke + 1 transition) | 2 |
| Pre-soak wait | 0 | 2 (1 wait state × 2 transitions) | -2 |
| **Total estimated savings** | | | **23–26** |
| **Typical total** | **~103** | **~77–80** | |

### Project Update (`hpc-project-update`)

| Component | Before (transitions) | After (transitions) | Savings |
|---|---|---|---|
| Pre-loop steps | 4 (2 invoke + 2 transitions) | 2 (1 invoke + 1 transition) | 2 |
| CodeBuild polling loop | 12–15 (4–5 iterations × 3) | 0–6 (0–2 iterations × 3) | 6–15 |
| Post-loop steps | 4 (2 invoke + 2 transitions) | 2 (1 invoke + 1 transition) | 2 |
| Pre-soak wait | 0 | 2 (1 wait state × 2 transitions) | -2 |
| **Total estimated savings** | | | **8–17** |
| **Typical total** | **~67–76** | **~55–63** | |

### Project Destroy (`hpc-project-destroy`)

| Component | Before (transitions) | After (transitions) | Savings |
|---|---|---|---|
| Pre-loop steps | 4 (2 invoke + 2 transitions) | 2 (1 invoke + 1 transition) | 2 |
| CodeBuild polling loop | 24 (~8 iterations × 3) | 0–3 (0–1 iterations × 3) | 21–24 |
| Post-loop steps | 4 (2 invoke + 2 transitions) | 2 (1 invoke + 1 transition) | 2 |
| Pre-soak wait | 0 | 2 (1 wait state × 2 transitions) | -2 |
| **Total estimated savings** | | | **23–26** |
| **Typical total** | **~65** | **~39–42** | |

### Combined Monthly Impact

Assuming a typical monthly workload of mixed cluster and project operations, the optimization reduces total transitions significantly. The exact savings depend on workload mix, but each execution benefits from both consolidation (fixed savings) and pre-soak waits (variable savings based on resource readiness timing).

---

## Design Decisions

### Why Lambda Consolidation Over Express Workflows

Express Workflows were considered but rejected because:
- Express Workflows have a 5-minute execution limit, incompatible with cluster creation (~15 min)
- The existing standard workflows are deployed and battle-tested
- The step handler dispatch pattern already exists — consolidation adds a new dispatch key that calls existing functions sequentially, minimising new code

### Why Pre-Soak Waits Over Longer Polling Intervals

Increasing the polling interval (e.g., from 30s to 60s) was considered but rejected because:
- It would increase worst-case latency when resources complete just after a poll
- Pre-soak waits eliminate the majority of polls without affecting responsiveness once the resource is likely ready
- Wait states are free in Step Functions (no transition cost)

### Error Handling Preservation

Consolidated handlers use a fail-fast pattern — exceptions propagate directly without being caught. This preserves the existing `addCatch` routing in each state machine. When a consolidated handler fails partway through, some sub-steps will have completed and their side effects persist. This is identical to pre-optimization behaviour, and existing rollback handlers already account for partial execution.
