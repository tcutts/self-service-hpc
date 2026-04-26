# Implementation Plan: Self-Service HPC Platform

## Overview

This plan implements a self-service HPC platform on AWS. Infrastructure is defined using AWS CDK in TypeScript (L2 constructs preferred, L1 for PCS resources). Lambda functions are written in Python. The platform uses a serverless control plane (API Gateway, Lambda, DynamoDB, Cognito) and provisions ephemeral HPC clusters via AWS Parallel Computing Service with per-project VPC isolation, persistent EFS home directories, S3 + FSx for Lustre bulk storage, centralised Slurm accounting, and cost controls via AWS Budgets.

## Tasks

- [x] 1. Initialise CDK project and shared infrastructure
  - [x] 1.1 Initialise CDK TypeScript project with `cdk init app --language typescript`
    - Set up directory structure: `lib/` for stacks, `lambda/` for Python handlers, `test/` for CDK and Lambda tests
    - Configure `cdk.json` with the `thecutts` AWS profile
    - Add dependencies: `aws-cdk-lib`, `constructs`, `@types/node`
    - _Requirements: 16.1, 16.2_

  - [x] 1.2 Create the Platform Foundation stack (Cognito, DynamoDB, API Gateway)
    - Create a Cognito User Pool with email sign-in and an `Administrators` group
    - Create a Cognito User Pool Client for the web portal
    - Create DynamoDB tables: `PlatformUsers` (with StatusIndex GSI), `Projects` (with UserProjectsIndex GSI), `ClusterTemplates`, `Clusters`, `ClusterNameRegistry`
    - Include the POSIX UID atomic counter item in `PlatformUsers`
    - Create an API Gateway REST API with a Cognito authoriser
    - Apply `Project` and `ClusterName` as cost allocation tags via CDK Aspects
    - Configure CloudWatch log retention: 90 days for infrastructure, 365 days for user access logs
    - _Requirements: 13.1, 13.4, 14.2, 14.3, 16.1, 16.2, 16.4_

  - [x] 1.3 Write CDK snapshot tests for the Foundation stack
    - Verify DynamoDB table schemas and GSIs are correct
    - Verify Cognito User Pool configuration
    - Verify API Gateway has Cognito authoriser attached
    - Verify CloudWatch log retention periods (90 days infrastructure, 365 days access)
    - _Requirements: 13.4, 16.1_

- [x] 2. Implement User Management Service
  - [x] 2.1 Create the User Management Lambda function (Python)
    - Implement `POST /users` handler: validate input, allocate POSIX UID/GID via DynamoDB atomic counter (starting at 10000), create Cognito user, store user record in DynamoDB
    - Implement `DELETE /users/{userId}` handler: deactivate user in DynamoDB, disable and sign out Cognito user, revoke sessions
    - Implement `GET /users` and `GET /users/{userId}` handlers
    - Add authorisation checks: only Administrators can create/delete/list users
    - Return structured error responses (AUTHORISATION_ERROR, DUPLICATE_ERROR, VALIDATION_ERROR)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 17.1_

  - [x] 2.2 Wire User Management Lambda to API Gateway in CDK
    - Create the Python Lambda function construct with appropriate IAM permissions for DynamoDB and Cognito
    - Add API Gateway resource and method integrations for `/users` and `/users/{userId}`
    - _Requirements: 16.2, 16.3_

  - [x] 2.3 Write property test: User creation assigns globally unique POSIX identity (Property 1)
    - **Property 1: User creation assigns globally unique POSIX identity**
    - Use Hypothesis to generate sequences of distinct user identifiers and verify all assigned UIDs/GIDs are unique
    - Use moto to mock DynamoDB
    - **Validates: Requirements 1.1, 17.1**

  - [x] 2.4 Write property test: Duplicate user creation is rejected (Property 2)
    - **Property 2: Duplicate user creation is rejected**
    - Use Hypothesis to generate a user identifier, create the user, then attempt to create again and verify rejection with descriptive error
    - **Validates: Requirements 1.3**

  - [x] 2.5 Write property test: Admin-only operations reject non-administrators (Property 3)
    - **Property 3: Admin-only operations reject non-administrators**
    - Use Hypothesis to generate non-admin user contexts and admin-only operations, verify all are rejected with authorisation error
    - **Validates: Requirements 1.4, 2.4, 3.4**

  - [x] 2.6 Write unit tests for User Management Lambda
    - Test user creation happy path and error cases
    - Test user deactivation and session revocation
    - Test POSIX UID/GID atomic counter increment
    - Test authorisation rejection for non-admin callers
    - Use pytest with moto for DynamoDB and Cognito mocks
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 17.1_

- [x] 3. Implement Project Management Service
  - [x] 3.1 Create the Project Management Lambda function (Python)
    - Implement `POST /projects` handler: create project record in DynamoDB with Cost_Allocation_Tag, trigger project infrastructure provisioning (VPC, EFS, S3)
    - Implement `DELETE /projects/{projectId}` handler: verify no active clusters exist (reject with cluster list if any), tear down project infrastructure
    - Implement `GET /projects` and `GET /projects/{projectId}` handlers
    - Implement `POST /projects/{projectId}/members` handler: validate user exists on platform, add membership record, create Cognito group membership
    - Implement `DELETE /projects/{projectId}/members/{userId}` handler: remove membership and Cognito group
    - Implement `PUT /projects/{projectId}/budget` handler: create/update AWS Budget with 80% and 100% SNS notifications
    - Add authorisation checks: Admin for project CRUD, Project Admin for membership and budget
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4_

  - [x] 3.2 Wire Project Management Lambda to API Gateway in CDK
    - Create the Python Lambda function construct with IAM permissions for DynamoDB, Cognito, AWS Budgets, SNS, and CloudFormation/CodeBuild
    - Add API Gateway resource and method integrations for `/projects`, `/projects/{projectId}`, `/projects/{projectId}/members`, `/projects/{projectId}/budget`
    - _Requirements: 16.2, 16.3_

  - [x] 3.3 Write property test: Project deletion is blocked by active clusters (Property 6)
    - **Property 6: Project deletion is blocked by active clusters**
    - Use Hypothesis to generate projects with varying numbers of active/destroyed clusters, verify deletion is blocked when active clusters exist and allowed when none exist
    - **Validates: Requirements 2.2, 2.3**

  - [x] 3.4 Write property test: Project admin operations reject non-project-administrators (Property 4)
    - **Property 4: Project admin operations reject non-project-administrators**
    - Use Hypothesis to generate non-project-admin user contexts and project admin operations, verify rejection
    - **Validates: Requirements 4.4, 5.4**

  - [x] 3.5 Write property test: Non-existent user cannot be added to a project (Property 8)
    - **Property 8: Non-existent user cannot be added to a project**
    - Use Hypothesis to generate user identifiers not present in the platform, verify membership addition is rejected
    - **Validates: Requirements 4.3**

  - [x] 3.6 Write unit tests for Project Management Lambda
    - Test project creation with cost allocation tag
    - Test project deletion blocked by active clusters
    - Test membership add/remove happy path and error cases
    - Test budget creation with 80% and 100% thresholds
    - Test authorisation for all endpoints
    - Use pytest with moto
    - _Requirements: 2.1, 2.2, 2.3, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3_

- [x] 4. Implement Cluster Template Management Service
  - [x] 4.1 Create the Cluster Template Management Lambda function (Python)
    - Implement `POST /templates` handler: validate template definition (instance types, node config, software stack), store in DynamoDB
    - Implement `DELETE /templates/{templateId}` handler: remove template from DynamoDB
    - Implement `GET /templates` and `GET /templates/{templateId}` handlers
    - Add authorisation checks: Admin for create/delete, any authenticated user for read
    - Seed two default templates on first deployment: `cpu-general` (e.g., `c7g.medium`) and `gpu-basic` (e.g., `g4dn.xlarge`)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x] 4.2 Wire Cluster Template Lambda to API Gateway in CDK
    - Create the Python Lambda function construct with DynamoDB permissions
    - Add API Gateway resource and method integrations for `/templates` and `/templates/{templateId}`
    - _Requirements: 16.2, 16.3_

  - [x] 4.3 Write property test: Cluster template storage round-trip (Property 7)
    - **Property 7: Cluster template storage round-trip**
    - Use Hypothesis to generate valid template definitions, store and retrieve, verify all fields match
    - **Validates: Requirements 3.1**

  - [x] 4.4 Write unit tests for Cluster Template Lambda
    - Test template CRUD operations
    - Test default template seeding
    - Test authorisation checks
    - Use pytest with moto
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 5. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement Project Infrastructure stack (VPC, EFS, S3, Security Groups)
  - [x] 6.1 Create the Project Infrastructure CDK stack
    - Create a parameterised CDK stack that provisions per-project infrastructure
    - Create a dedicated VPC with public and private subnets (NAT Gateway for private subnet egress)
    - Create an EFS filesystem for home directories with access points per user
    - Create an S3 bucket for project storage (or accept a user-provided bucket ARN), with bucket policy denying access from outside the project boundary
    - Apply the project `Cost_Allocation_Tag` to all resources
    - _Requirements: 9.1, 9.2, 9.3, 10.1, 10.2, 10.4, 11.1, 11.3, 14.1, 14.2_

  - [x] 6.2 Configure security groups in the Project Infrastructure stack
    - Create a Head Node (Login Node) security group: allow SSH (22) and DCV (8443) from a configurable list of trusted CIDR ranges only — no 0.0.0.0/0
    - Create a Compute Node security group: allow traffic only from Head Node SG and other Compute Nodes within the same cluster
    - Create an EFS security group: allow NFS (2049) only from Head Node and Compute Node security groups
    - Create an FSx for Lustre security group: allow Lustre traffic (988) only from Head Node and Compute Node security groups
    - _Requirements: 9.4, 15.1, 15.2, 15.3, 15.4_

  - [x] 6.3 Write property test: No open security groups (Property 16)
    - **Property 16: No open security groups**
    - Synthesise the CDK stack and inspect the CloudFormation template to verify no security group ingress rule has source CIDR 0.0.0.0/0
    - **Validates: Requirements 15.2**

  - [x] 6.4 Write CDK snapshot tests for the Project Infrastructure stack
    - Verify VPC isolation (dedicated VPC per project)
    - Verify S3 bucket policy denies external access
    - Verify EFS access restricted to project security groups
    - Verify FSx security group restricted to project security groups
    - Verify all resources tagged with project Cost_Allocation_Tag
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 15.1, 15.2_

- [x] 7. Implement Cluster Operations Service
  - [x] 7.1 Create the Cluster Name validation and registry module (Python)
    - Implement cluster name format validation: non-empty, alphanumeric + hyphens + underscores only (`^[a-zA-Z0-9_-]+$`)
    - Implement cluster name suggestion: `{projectId}-{random_suffix}`
    - Implement cluster name registry: DynamoDB conditional put to enforce cross-project uniqueness while allowing same-project reuse
    - _Requirements: 6.1, 6.7, 6.8, 18.1, 18.2, 18.3, 18.4_

  - [x] 7.2 Write property test: Cluster name validation (Property 9)
    - **Property 9: Cluster name validation**
    - Use Hypothesis to generate arbitrary strings, verify the validation function accepts if and only if the string matches `^[a-zA-Z0-9_-]+$`
    - **Validates: Requirements 18.1**

  - [x] 7.3 Write property test: Cluster name cross-project uniqueness (Property 10)
    - **Property 10: Cluster name cross-project uniqueness**
    - Use Hypothesis to generate cluster names and two distinct project IDs, register with project A, attempt from project B, verify rejection
    - **Validates: Requirements 6.7, 18.3**

  - [x] 7.4 Write property test: Cluster name same-project reuse (Property 11)
    - **Property 11: Cluster name same-project reuse**
    - Use Hypothesis to generate cluster names and a project ID, register and re-register within the same project, verify acceptance
    - **Validates: Requirements 6.8, 18.4**

  - [x] 7.5 Write property test: Cluster name registry preserves association (Property 17)
    - **Property 17: Cluster name registry preserves association**
    - Use Hypothesis to generate cluster name and project ID pairs, register them, query back, verify the returned project ID matches
    - **Validates: Requirements 18.2**

  - [x] 7.6 Create the Cluster Creation Step Functions workflow and Lambda handlers (Python)
    - Implement the cluster creation state machine:
      1. Validate cluster name (format + registry check)
      2. Check project budget breach status (DynamoDB consistent read)
      3. Create FSx for Lustre filesystem with data repository association to project S3 bucket
      4. Wait for FSx to become available
      5. Create PCS cluster (Slurm 24.11+, accounting mode STANDARD) using L1 CDK constructs
      6. Create login node compute node group (public subnet, static scaling min 1, on-demand)
      7. Create compute node compute node group (private subnet, elastic scaling)
      8. Create PCS queue linked to compute node group
      9. Tag all resources with project Cost_Allocation_Tag and ClusterName tag
      10. Record cluster details in DynamoDB (status ACTIVE, SSH/DCV connection info)
    - Include retry with exponential backoff for PCS ConflictException (one cluster creating at a time)
    - Include rollback logic on failure: clean up partially created resources, mark cluster as FAILED
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 6.9, 11.2, 12.2, 14.3_

  - [x] 7.7 Create the Cluster Destruction Step Functions workflow and Lambda handlers (Python)
    - Implement the cluster destruction state machine:
      1. Create FSx data repository export task (sync data back to S3)
      2. Wait for export to complete (with failure handling — pause and alert on failure)
      3. Delete PCS compute node groups, queue, and cluster
      4. Delete FSx for Lustre filesystem
      5. Update DynamoDB cluster record (status DESTROYED, destroyedAt timestamp)
    - Retain Home_Directory (EFS) and Project_Storage (S3) after destruction
    - _Requirements: 7.1, 7.2, 7.3, 11.4_

  - [x] 7.8 Create the Cluster Operations API Lambda (Python)
    - Implement `POST /projects/{projectId}/clusters` handler: validate input, start creation Step Functions execution
    - Implement `DELETE /projects/{projectId}/clusters/{clusterName}` handler: validate authorisation, start destruction Step Functions execution
    - Implement `GET /projects/{projectId}/clusters` and `GET /projects/{projectId}/clusters/{clusterName}` handlers: return cluster details including SSH/DCV connection info for active clusters
    - Add authorisation checks: Project User or Project Admin for all cluster operations
    - Check budget breach on cluster access detail requests (deny if breached)
    - _Requirements: 6.1, 6.2, 7.4, 8.1, 8.2, 8.5, 8.6_

  - [x] 7.9 Write property test: Budget breach blocks cluster creation (Property 12)
    - **Property 12: Budget breach blocks cluster creation**
    - Use Hypothesis to generate project states with budget breached, verify cluster creation is rejected
    - **Validates: Requirements 6.9**

  - [x] 7.10 Write property test: Budget breach blocks cluster access (Property 13)
    - **Property 13: Budget breach blocks cluster access**
    - Use Hypothesis to generate project states with budget breached, verify cluster connection detail requests are denied
    - **Validates: Requirements 8.5**

  - [x] 7.11 Write property test: Project-scoped operations reject unauthorised users (Property 5)
    - **Property 5: Project-scoped operations reject unauthorised users**
    - Use Hypothesis to generate unauthorised user contexts and project-scoped operations (cluster destroy, cluster access), verify rejection
    - **Validates: Requirements 7.4, 8.6**

  - [x] 7.12 Write property test: Resource tagging correctness (Property 15)
    - **Property 15: Resource tagging correctness**
    - Use Hypothesis to generate project identifiers and cluster names, verify the tag set includes `Project` = projectId and `ClusterName` = clusterName
    - **Validates: Requirements 14.2, 14.3**

  - [x] 7.13 Write unit tests for Cluster Operations
    - Test cluster name validation and suggestion
    - Test cluster name registry (cross-project rejection, same-project reuse)
    - Test budget breach check before cluster creation
    - Test cluster creation workflow step ordering
    - Test cluster destruction workflow with FSx export
    - Test authorisation for all cluster endpoints
    - Test that non-ACTIVE clusters do not expose connection info
    - Use pytest with moto
    - _Requirements: 6.1, 6.7, 6.8, 6.9, 7.1, 7.2, 7.3, 7.4, 8.7, 18.1, 18.3, 18.4_

  - [x] 7.14 Add step progress tracking to cluster creation workflow
    - Update each step handler in `cluster_creation.py` to write `currentStep`, `totalSteps`, and `stepDescription` to the DynamoDB Clusters record before executing the step logic
    - Define step labels: "Registering cluster name", "Checking budget", "Creating FSx filesystem", "Waiting for FSx", "Creating PCS cluster", "Creating login nodes", "Creating compute nodes", "Creating queue", "Tagging resources", "Finalising"
    - Ensure the GET cluster endpoint returns progress fields (`currentStep`, `totalSteps`, `stepDescription`) for clusters in CREATING status
    - _Requirements: 19.2, 19.3_

  - [x] 7.15 Add cluster lifecycle notifications (SNS)
    - Create an SNS topic for cluster lifecycle notifications (`hpc-cluster-lifecycle-notifications`)
    - On successful cluster creation (end of Step Functions workflow), publish a success notification with cluster name and connection details to the creating user's email (looked up from PlatformUsers table)
    - On cluster creation failure (rollback handler), publish a failure notification with the error description
    - Add the SNS topic to the Foundation stack CDK and grant publish permissions to the cluster operations Lambda
    - _Requirements: 19.1, 19.4, 19.5_

  - [x] 7.16 Write property test: Non-ACTIVE clusters do not expose login credentials (Property 18)
    - **Property 18: Non-ACTIVE clusters do not expose login credentials**
    - Use Hypothesis to generate clusters in non-ACTIVE statuses (CREATING, FAILED, DESTROYING, DESTROYED), verify the GET cluster detail response does not include SSH/DCV connection info
    - **Validates: Requirements 8.7, 19.6**

- [x] 8. Wire Cluster Operations to API Gateway and Step Functions in CDK
  - Create Step Functions state machines for cluster creation and destruction workflows
  - Create the Cluster Operations Lambda with IAM permissions for DynamoDB, PCS, FSx, EFS, Step Functions, SSM, and SNS
  - Add API Gateway resource and method integrations for `/projects/{projectId}/clusters` and `/projects/{projectId}/clusters/{clusterName}`
  - Grant Step Functions execution role permissions for PCS, FSx, EC2, tagging, and DynamoDB (for step progress updates)
  - Create the cluster lifecycle SNS topic and wire it to the cluster operations Lambda
  - _Requirements: 16.1, 16.2, 16.3, 19.4, 19.5_

- [x] 9. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement POSIX User Provisioning
  - [x] 10.1 Create the POSIX user provisioning module (Python)
    - Implement EC2 launch template user data script generation:
      - Fetch project user list from DynamoDB (via Lambda-backed API or SSM parameter)
      - Create POSIX user accounts with correct UID/GID
      - Set home directory ownership on EFS mount
      - Disable interactive login for generic accounts (ec2-user, centos, ubuntu)
    - Implement SSM Run Command handler for propagating new users to active cluster nodes
    - Include retry logic (up to 3 retries with exponential backoff) for SSM failures
    - Mark user as `PENDING_PROPAGATION` if propagation fails after retries
    - _Requirements: 8.3, 8.4, 17.2, 17.3, 17.4, 17.5_

  - [x] 10.2 Integrate POSIX provisioning into cluster creation and membership workflows
    - Update cluster creation Step Functions to include user data script in launch templates
    - Update project membership Lambda to trigger SSM Run Command for active clusters when a new user is added
    - Create a periodic reconciliation Lambda to retry `PENDING_PROPAGATION` users
    - _Requirements: 17.2, 17.5_

  - [x] 10.3 Write unit tests for POSIX User Provisioning
    - Test user data script generation with multiple users
    - Test generic account disabling logic
    - Test SSM propagation retry logic
    - Test PENDING_PROPAGATION fallback
    - Use pytest with moto
    - _Requirements: 8.3, 8.4, 17.2, 17.3, 17.4, 17.5_

- [x] 11. Implement Accounting Query Service
  - [x] 11.1 Create the Accounting Query Lambda function (Python)
    - Implement `GET /accounting/jobs` handler: use SSM Run Command to execute `sacct` queries on login nodes of active clusters
    - Implement project-scoped query: `GET /accounting/jobs?projectId={projectId}`
    - Aggregate results across clusters and return structured response
    - Add authorisation checks: Admin for cross-cluster queries, Project Admin for project-scoped queries
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

  - [x] 11.2 Wire Accounting Lambda to API Gateway in CDK
    - Create the Python Lambda function construct with SSM and DynamoDB permissions
    - Add API Gateway resource and method integrations for `/accounting/jobs`
    - _Requirements: 16.2, 16.3_

  - [x] 11.3 Write unit tests for Accounting Query Lambda
    - Test SSM command construction for sacct queries
    - Test result aggregation from multiple clusters
    - Test authorisation checks
    - Use pytest with moto
    - _Requirements: 12.3_

- [x] 12. Implement Budget Management integration
  - [x] 12.1 Create the Budget Notification handler Lambda (Python)
    - Implement SNS message handler: parse AWS Budgets notification, update `budgetBreached` flag in DynamoDB Projects table
    - Handle 80% threshold: notify Project Admin via SNS/email
    - Handle 100% threshold: notify Project Admin and all Administrators, set `budgetBreached = true`
    - Use DynamoDB consistent reads for budget breach checks
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 12.2 Wire Budget notification Lambda and SNS topic in CDK
    - Create SNS topic for budget notifications
    - Create the Budget notification Lambda with DynamoDB and SNS permissions
    - Subscribe the Lambda to the SNS topic
    - _Requirements: 16.2, 16.3_

  - [x] 12.3 Write unit tests for Budget Notification Lambda
    - Test 80% threshold notification handling
    - Test 100% threshold notification and budgetBreached flag update
    - Test consistent read for budget breach status
    - Use pytest with moto
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 13. Implement Logging and Monitoring
  - [x] 13.1 Create API action logging middleware (Python)
    - Implement a Lambda middleware/decorator that logs every API action to CloudWatch Logs with user identifier, action type, and timestamp
    - Apply the middleware to all API Lambda handlers
    - _Requirements: 13.2, 13.3_

  - [x] 13.2 Configure cluster access logging in CDK
    - Add `pam_exec` or `auditd` configuration to cluster launch template user data for SSH/DCV login event logging
    - Configure CloudWatch agent on cluster nodes to forward access logs
    - Set CloudWatch Log Group retention: 365 days for user access logs
    - _Requirements: 13.1, 13.2, 13.4_

  - [x] 13.3 Write property test: API action logging contains required fields (Property 14)
    - **Property 14: API action logging contains required fields**
    - Use Hypothesis to generate user identifiers, action types, and timestamps, verify log entries contain all required fields
    - **Validates: Requirements 13.3**

  - [x] 13.4 Write unit tests for logging middleware
    - Test log entry format includes user identifier, action type, and timestamp
    - Test middleware applies to all API actions
    - Use pytest
    - _Requirements: 13.3_

- [x] 14. Implement Web Portal static frontend
  - [x] 14.1 Create the static web portal and deploy via CloudFront + S3
    - Create an S3 bucket for static web assets
    - Create a CloudFront distribution with the S3 bucket as origin
    - Scaffold a minimal frontend (HTML/JS or lightweight framework) that authenticates via Cognito and calls the API Gateway endpoints
    - Include pages for: user management, project management, cluster template management, cluster operations, accounting queries
    - Cluster operations page: display real-time deployment progress (step number, step description, progress bar) by polling the GET cluster endpoint. Allow the user to navigate away and return to see current status.
    - Cluster operations page: show in-app notification when a cluster transitions from CREATING to ACTIVE or FAILED
    - Cluster detail page: only show SSH/DCV connection info for ACTIVE clusters; show progress or error info for other statuses
    - _Requirements: 1.1, 2.1, 3.1, 6.1, 6.2, 8.1, 8.2, 8.7, 12.3, 19.2, 19.3, 19.6_

  - [x] 14.2 Wire CloudFront and S3 hosting in CDK
    - Create the S3 bucket with website hosting configuration
    - Create CloudFront distribution with OAI for S3 access
    - Output the CloudFront URL
    - _Requirements: 16.1, 16.2_

- [x] 15. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. Integration wiring and end-to-end validation
  - [x] 16.1 Wire all stacks together in the CDK app entry point
    - Create the main CDK app in `bin/` that instantiates the Foundation stack, and parameterises Project Infrastructure stacks
    - Ensure cross-stack references (API Gateway URL, DynamoDB table names, Cognito pool ID) are passed via stack outputs or SSM parameters
    - Configure CDK Aspects to enforce tagging across all stacks
    - _Requirements: 14.1, 14.4, 16.1, 16.2_

  - [x] 16.2 Create default cluster template seeding as a CDK custom resource
    - Implement a custom resource Lambda that seeds the two default templates (`cpu-general`, `gpu-basic`) into DynamoDB on stack deployment
    - _Requirements: 3.3_

  - [x] 16.3 Write integration tests for end-to-end workflows
    - Test user creation → project creation → member addition → cluster creation → cluster access → cluster destruction flow
    - Test budget alert configuration and breach blocking
    - Test cluster name uniqueness across projects
    - Use pytest with moto or localstack
    - _Requirements: 1.1, 2.1, 4.1, 6.2, 6.7, 6.9, 7.1, 8.1_

- [x] 17. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. Implement Deployment Automation (Makefile)
  - [x] 18.1 Create the Makefile with `deploy`, `teardown`, and `purge` targets
    - Create a `Makefile` at the repository root with three targets:
      - `deploy`: `npm ci` → `pip install -r requirements.txt` into `.venv/` → `npm run build` → `npx cdk deploy HpcFoundationStack --require-approval never --profile thecutts` → deploy any `HpcProject-*` stacks
      - `teardown`: invoke `scripts/teardown_workloads.py` to destroy all clusters and projects, then clean DynamoDB records, leaving the foundation stack intact
      - `purge`: run teardown, then `npx cdk destroy HpcFoundationStack --force --profile thecutts`
    - Include helper targets: `build`, `test` (runs both CDK and Python tests), `synth`
    - _Requirements: 20.1, 20.2, 20.3, 20.4_

  - [x] 18.2 Create the teardown workloads helper script (Python)
    - Create `scripts/teardown_workloads.py` that:
      - Scans DynamoDB Clusters table for ACTIVE/CREATING clusters and destroys them (calls PCS/FSx cleanup APIs or invokes the destruction Step Functions workflow)
      - Scans DynamoDB Projects table for all project records and destroys each project CDK stack (`cdk destroy HpcProject-{projectId} --force`)
      - Removes all cluster, project, membership, and cluster name registry records from DynamoDB
      - Handles errors gracefully: logs failures, continues with remaining resources, reports all failures at the end
      - Retries CDK stack destroy once after 30s on failure
    - Use boto3 with the `thecutts` profile
    - _Requirements: 20.2, 20.3, 20.5, 20.6, 20.7_

  - [x] 18.3 Write unit tests for the teardown helper script
    - Test DynamoDB scan logic for active clusters and projects
    - Test error handling (cluster destruction failure continues with remaining)
    - Test retry logic for CDK stack destroy
    - Use pytest with moto
    - _Requirements: 20.5, 20.7_

  - [x] 18.4 Write smoke tests for Makefile targets
    - Verify the Makefile contains `deploy`, `teardown`, and `purge` targets (parse the Makefile)
    - Verify `make deploy` runs without syntax errors (dry-run or `--just-print`)
    - _Requirements: 20.1, 20.2, 20.3_

- [x] 19. Implement Platform Documentation
  - [x] 19.1 Create the documentation directory structure and content
    - Create `docs/` directory at the repository root with the following structure:
      - `docs/index.html` — documentation landing page with navigation and client-side Markdown renderer (marked.js)
      - `docs/admin/deploying-foundation.md` — deploying the foundation infrastructure
      - `docs/admin/user-management.md` — creating, updating, and removing users
      - `docs/admin/project-management.md` — creating, updating, and removing projects
      - `docs/project-admin/project-management.md` — managing project membership and budgets
      - `docs/project-admin/cluster-management.md` — creating, updating, and removing clusters
      - `docs/user/accessing-clusters.md` — accessing clusters via SSH/DCV and submitting jobs
      - `docs/user/data-management.md` — uploading and downloading data from a project
      - `docs/api/reference.md` — API reference: all endpoints, request/response formats, authorisation roles, error codes
    - Each document should be comprehensive and audience-appropriate
    - _Requirements: 21.1, 21.3, 21.4, 21.5_

  - [x] 19.2 Wire documentation deployment to CloudFront via CDK
    - Add an `s3deploy.BucketDeployment` construct in the Foundation stack to deploy `docs/` to the web portal S3 bucket under the `docs/` prefix
    - Configure CloudFront cache invalidation for `/docs/*` on redeployment
    - Verify documentation is accessible at `https://{cloudfront-domain}/docs/index.html`
    - _Requirements: 21.2, 21.6, 21.7_

  - [x] 19.3 Write CDK tests for documentation deployment
    - Verify the BucketDeployment construct includes the `docs/` source
    - Verify the destination key prefix is `docs/`
    - Verify CloudFront invalidation paths include `/docs/*`
    - _Requirements: 21.2, 21.6_

  - [x] 19.4 Write smoke tests for documentation files
    - Verify all required documentation files exist in the `docs/` directory
    - Verify each file is non-empty and contains a Markdown heading
    - Verify the `docs/index.html` landing page exists and contains navigation links
    - _Requirements: 21.1, 21.3_

- [x] 20. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis (Python)
- Unit tests use pytest with moto for AWS service mocking
- CDK infrastructure uses TypeScript with L2 constructs (L1 for PCS resources only)
- Lambda functions are implemented in Python
- All AWS operations use the `thecutts` profile
- Security groups never allow 0.0.0.0/0 ingress — trusted CIDR ranges are configurable
