# Requirements Document

## Introduction

The HPC self-service platform provisions AWS PCS clusters via a Step Functions state machine (`hpc-cluster-creation`). Currently, Slurm scheduler logs (slurmctld, slurmdbd, slurmrestd) are not collected — they remain on the PCS controller instance and are inaccessible to platform operators. AWS PCS supports vended log delivery, which uses the CloudWatch Logs delivery APIs (`PutDeliverySource`, `PutDeliveryDestination`, `CreateDelivery`) to stream scheduler logs to a CloudWatch Log Group. This is separate from the PCS `CreateCluster` API and must be configured after the cluster reaches the ACTIVE state.

AWS PCS exposes three vended log types:

| Log Type | Description | Record Fields |
|----------|-------------|---------------|
| `PCS_SCHEDULER_LOGS` | slurmctld, slurmdbd, and slurmrestd daemon logs | `resource_id`, `resource_type`, `event_timestamp`, `log_level`, `log_name`, `scheduler_type`, `scheduler_major_version`, `scheduler_patch_version`, `node_type`, `message` |
| `PCS_SCHEDULER_AUDIT_LOGS` | slurmctld audit logs (RPC tracking) | Same as above plus `log_type` |
| `PCS_JOBCOMP_LOGS` | Slurm job completion records | `resource_id`, `resource_type`, `event_timestamp`, `scheduler_type`, `scheduler_major_version`, `fields` |

All three log types are delivered through the same vended log delivery mechanism — no `slurmCustomSettings` or CloudWatch Agent configuration is needed. PCS handles job completion logging natively via the `PCS_JOBCOMP_LOGS` log type.

The console UI creates the following resources per log type:
- One **Delivery Source** per log type (referencing the PCS cluster ARN)
- One **Delivery Destination** per log type (all pointing to the same CloudWatch Log Group)
- One **Delivery** per log type (linking source to destination)

All three deliveries share a single auto-created log group at `/aws/vendedlogs/pcs/cluster/PCS_SCHEDULER_LOGS/{cluster_id}`. Log streams follow the pattern `AWSLogs/PCS/{cluster_id}/{log_name}_{scheduler_major_version}.log` (e.g., `AWSLogs/PCS/pcs_szls0cjyty/slurmctld_25.11.log`). The output format is JSON.

This feature adds automatic configuration of all three vended log deliveries to the cluster creation workflow, so every new cluster gets scheduler, audit, and job completion logs streamed to CloudWatch without manual intervention.

## Glossary

- **Vended_Log_Delivery**: The AWS CloudWatch Logs mechanism for delivering service-generated logs. Uses three API calls per log type: `PutDeliverySource` (registers the PCS cluster as a log source), `PutDeliveryDestination` (registers the CloudWatch Log Group as a destination), and `CreateDelivery` (links source to destination). Requires the `pcs:AllowVendedLogDeliveryForResource` permission on the PCS cluster ARN.
- **PCS_Log_Types**: The three vended log types supported by PCS: `PCS_SCHEDULER_LOGS` (slurmctld/slurmdbd/slurmrestd daemon logs), `PCS_SCHEDULER_AUDIT_LOGS` (slurmctld audit/RPC logs), and `PCS_JOBCOMP_LOGS` (Slurm job completion records). Each requires its own Delivery_Source, Delivery_Destination, and Delivery.
- **Delivery_Source**: A CloudWatch Logs resource created by `PutDeliverySource` that identifies the PCS cluster ARN as the origin of logs. The `logType` field specifies which PCS log type to deliver. The `service` field is `pcs`.
- **Delivery_Destination**: A CloudWatch Logs resource created by `PutDeliveryDestination` that identifies the target CloudWatch Log Group ARN. The `outputFormat` is `json`. Multiple deliveries can share the same log group but each requires its own Delivery_Destination resource.
- **Delivery**: A CloudWatch Logs resource created by `CreateDelivery` that links a Delivery_Source to a Delivery_Destination, activating log flow. Returns a delivery `id` and includes `recordFields` listing the structured fields in each log record.
- **Scheduler_Log_Group**: The CloudWatch Log Group that receives all PCS vended logs for a cluster. The console default uses `/aws/vendedlogs/pcs/cluster/PCS_SCHEDULER_LOGS/{cluster_id}` which requires knowing the PCS cluster ID. For the platform, we use a per-cluster log group at `/hpc-platform/clusters/{projectId}/scheduler-logs/{clusterName}` with 30-day retention, so logs are discoverable by the human-readable project and cluster names.
- **Log_Stream_Pattern**: The CloudWatch Logs stream naming pattern for PCS vended logs: `AWSLogs/PCS/{cluster_id}/{log_name}_{scheduler_major_version}.log` (e.g., `AWSLogs/PCS/pcs_szls0cjyty/slurmctld_25.11.log`). These stream names are controlled by PCS and cannot be customised, but the log group path uses the human-readable cluster name.
- **Cluster_Creation_Step_Lambda**: The Lambda function (`hpc-cluster-creation-steps`) that executes individual steps of the cluster creation workflow, dispatched by the `step_handler` function.
- **CloudWatch_Logs_Client**: A boto3 client for the `logs` service, used to call `PutDeliverySource`, `PutDeliveryDestination`, `CreateDelivery`, and their deletion counterparts.
- **ProjectInfrastructureStack**: The CDK stack (`lib/project-infrastructure-stack.ts`) that provisions per-project infrastructure including VPC, EFS, S3 bucket, security groups, and CloudWatch Log Groups.
- **Cluster_Operations_Construct**: The CDK construct (`lib/constructs/cluster-operations.ts`) that defines cluster-related Lambda functions, Step Functions state machines, IAM policies, and API routes.

## Requirements

### Requirement 1: Per-Cluster Scheduler Log Group

**User Story:** As a platform operator, I want scheduler logs stored under a human-readable path that includes the project and cluster name, so that I can find logs without needing to know the PCS cluster ID.

#### Acceptance Criteria

1. WHEN configuring vended log delivery for a new cluster, THE `configure_scheduler_log_delivery` step SHALL create a CloudWatch Log Group named `/hpc-platform/clusters/{projectId}/scheduler-logs/{clusterName}` with a retention period of 30 days.
2. THE step SHALL set the log group's retention policy to 30 days using the `PutRetentionPolicy` API.
3. IF the log group already exists (e.g., from a previous cluster creation attempt or a recreated cluster), THE step SHALL reuse it without error.
4. THE log group SHALL be tagged with the `Project` cost allocation tag matching the project ID.

### Requirement 2: Configure Vended Log Delivery After Cluster Creation

**User Story:** As a platform operator, I want scheduler log delivery configured automatically for every new PCS cluster, so that scheduler, audit, and job completion logs are streamed to CloudWatch without manual intervention.

#### Acceptance Criteria

1. WHEN the PCS cluster reaches the ACTIVE state during the creation workflow, THE Cluster_Creation_Step_Lambda SHALL configure Vended_Log_Delivery for all three PCS_Log_Types: `PCS_SCHEDULER_LOGS`, `PCS_SCHEDULER_AUDIT_LOGS`, and `PCS_JOBCOMP_LOGS`.
2. FOR each PCS log type, THE Cluster_Creation_Step_Lambda SHALL call `PutDeliverySource` with the PCS cluster ARN as the `resourceArn`, the log type as the `logType` value, and a source name following the pattern `{clusterName}-{log_type_suffix}` (e.g., `newbie-scheduler-logs`, `newbie-scheduler-audit-logs`, `newbie-jobcomp-logs`).
3. FOR each PCS log type, THE Cluster_Creation_Step_Lambda SHALL call `PutDeliveryDestination` with the Scheduler_Log_Group ARN as the `destinationResourceArn`, output format `json`, and a destination name following the pattern `{projectId}-{clusterName}-{log_type_suffix}`.
4. FOR each PCS log type, THE Cluster_Creation_Step_Lambda SHALL call `CreateDelivery` to link the Delivery_Source to the Delivery_Destination.
5. IF a `ConflictException` is returned by any delivery API call indicating the resource already exists, THEN THE Cluster_Creation_Step_Lambda SHALL treat the call as successful and continue, supporting idempotent retries.
6. ALL three deliveries SHALL target the same Scheduler_Log_Group. PCS creates separate log streams within the group automatically using the Log_Stream_Pattern.

### Requirement 3: Scheduler Log Delivery Step in the State Machine

**User Story:** As a platform engineer, I want the scheduler log delivery configuration to be a discrete step in the cluster creation state machine, so that it executes at the correct point in the workflow and failures are handled consistently.

#### Acceptance Criteria

1. THE Cluster_Creation_Step_Lambda SHALL expose a new step named `configure_scheduler_log_delivery` that is dispatched by the `step_handler` function.
2. THE `configure_scheduler_log_delivery` step SHALL execute after the PCS cluster is confirmed ACTIVE and before node group creation begins.
3. THE `configure_scheduler_log_delivery` step SHALL receive the `pcsClusterId`, `pcsClusterArn`, `projectId`, and `clusterName` fields from the state machine payload.
4. THE `configure_scheduler_log_delivery` step SHALL receive the `projectId` and `clusterName` fields from the state machine payload and construct the Scheduler_Log_Group name from them.
5. IF the `configure_scheduler_log_delivery` step fails, THEN THE state machine SHALL route to the existing failure handler for rollback, consistent with other step failures.

### Requirement 4: IAM Permissions for Vended Log Delivery

**User Story:** As a platform engineer, I want the cluster creation Lambda to have the minimum IAM permissions required for configuring vended log delivery, so that the feature operates securely following least-privilege principles.

#### Acceptance Criteria

1. THE Cluster_Operations_Construct SHALL grant the Cluster_Creation_Step_Lambda the `logs:PutDeliverySource`, `logs:PutDeliveryDestination`, `logs:CreateDelivery`, `logs:GetDelivery`, `logs:CreateLogGroup`, `logs:PutRetentionPolicy`, and `logs:TagLogGroup` permissions.
2. THE Cluster_Operations_Construct SHALL grant the Cluster_Creation_Step_Lambda the `pcs:AllowVendedLogDeliveryForResource` permission, scoped to PCS cluster resources.
3. THE Cluster_Operations_Construct SHALL grant the Cluster_Creation_Step_Lambda the `logs:DescribeLogGroups` permission to check whether the Scheduler_Log_Group already exists before creating it.

### Requirement 5: Cleanup of Log Delivery on Cluster Destruction

**User Story:** As a platform engineer, I want log delivery resources cleaned up when a cluster is destroyed, so that orphaned delivery sources, destinations, and deliveries do not accumulate.

#### Acceptance Criteria

1. WHEN a cluster is being destroyed, THE cluster destruction workflow SHALL delete the three Delivery resources (created by `CreateDelivery`) associated with the cluster.
2. WHEN a cluster is being destroyed, THE cluster destruction workflow SHALL delete the three Delivery_Destination resources (created by `PutDeliveryDestination`) associated with the cluster.
3. WHEN a cluster is being destroyed, THE cluster destruction workflow SHALL delete the three Delivery_Source resources (created by `PutDeliverySource`) associated with the cluster.
4. THE destruction step SHALL delete resources in the correct order: deliveries first, then destinations, then sources.
5. IF a delivery resource has already been deleted or does not exist, THEN THE destruction step SHALL treat the deletion as successful and continue.
6. WHEN all delivery resources have been deleted, THE destruction step SHALL delete the Scheduler_Log_Group (`/hpc-platform/clusters/{projectId}/scheduler-logs/{clusterName}`) to avoid orphaned empty log groups.
7. THE Cluster_Operations_Construct SHALL grant the cluster destruction step Lambda the `logs:DeleteDelivery`, `logs:DeleteDeliverySource`, `logs:DeleteDeliveryDestination`, `logs:DeleteLogGroup`, `logs:ListDeliveries`, `logs:ListDeliverySources`, and `logs:ListDeliveryDestinations` permissions for cleanup.

### Requirement 6: Observability and Logging

**User Story:** As a platform operator, I want structured logging from the scheduler log delivery step, so that I can monitor delivery configuration and troubleshoot failures in CloudWatch.

#### Acceptance Criteria

1. WHEN the `configure_scheduler_log_delivery` step successfully configures delivery for a Scheduler_Daemon, THE step SHALL log the daemon name, delivery source name, and delivery ID at INFO level.
2. WHEN the `configure_scheduler_log_delivery` step encounters a `ConflictException` indicating a resource already exists, THE step SHALL log the resource name and type at INFO level with a message indicating the existing resource was reused.
3. IF the `configure_scheduler_log_delivery` step encounters an unexpected error from a CloudWatch Logs API call, THEN THE step SHALL log the error details including the API action, resource name, and exception message at ERROR level before raising the exception.
4. WHEN the `configure_scheduler_log_delivery` step completes all three log type configurations, THE step SHALL log a summary at INFO level including the cluster name and the number of deliveries configured.
