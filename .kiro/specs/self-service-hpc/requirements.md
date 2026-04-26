# Requirements Document

## Introduction

This document defines the requirements for a self-service High Performance Computing (HPC) platform. The platform enables users to provision ephemeral HPC clusters on demand through a web interface, with project-based isolation, persistent storage, centralized job accounting, and cost controls. Infrastructure is deployed using AWS CDK in TypeScript, HPC clusters use AWS Parallel Computing Service, and serverless technologies are preferred for persistent components to minimise running costs.

## Glossary

- **Web_Portal**: The web-based administration and access interface for the HPC platform, implemented as a serverless application.
- **Administrator**: A platform-level user who manages users, projects, and cluster templates.
- **Project_Administrator**: A user who owns a project, manages project membership, sets budget limits, and has all Project_User rights.
- **Project_User**: A user authorised to create and destroy clusters within a project, and to log into and use those clusters.
- **Project**: A logical grouping of clusters, storage, and users aligned with a business need, providing the security and data governance boundary.
- **Cluster**: An ephemeral HPC cluster provisioned via AWS Parallel Computing Service, created from a Cluster_Template.
- **Cluster_Template**: A predefined configuration specifying instance types, node counts, and software for a particular workload type.
- **Head_Node**: The entry point node of a Cluster, accessible via SSH or DCV, residing on a public or protected subnet.
- **Compute_Node**: A worker node in a Cluster that executes HPC jobs, residing on a private subnet.
- **Home_Directory**: A persistent user home directory shared across all clusters within a single project, implemented using Amazon EFS.
- **Project_Storage**: An S3 bucket providing bulk data storage for a project, accessible as a filesystem from clusters via FSx_for_Lustre.
- **FSx_for_Lustre**: A high-performance filesystem cache with a data repository association to an S3 bucket, used to present Project_Storage to clusters.
- **Slurm_Accounting_Database**: A centralised database storing job accounting records from all clusters, enabling cross-cluster job analysis.
- **Budget_Alert**: An AWS Budgets alert associated with a project cost allocation tag that notifies when spending approaches or exceeds the project budget limit.
- **Cost_Allocation_Tag**: An AWS tag applied to all resources within a project, used for cost tracking and budget enforcement.
- **Cluster_Name**: A unique human-readable identifier for a cluster, unique within a project and globally unique across projects over time.
- **CDK_Stack**: An AWS CDK stack written in TypeScript that defines infrastructure as code for the platform.
- **POSIX_User**: A Linux user account on a Cluster node, mapped to a platform user with a globally unique UID and GID that is consistent across all projects and clusters on the platform.
- **Platform**: The complete self-service HPC application, encompassing all CDK stacks, Lambda functions, web portal, automation scripts, and documentation.

## Requirements

### Requirement 1: Administrator User Management

**User Story:** As an Administrator, I want to add and remove users from the platform, so that I can control who has access to the HPC environment.

#### Acceptance Criteria

1. WHEN an Administrator submits a request to add a user, THE Web_Portal SHALL create the user account and return a confirmation with the user identifier.
2. WHEN an Administrator submits a request to remove a user, THE Web_Portal SHALL deactivate the user account and revoke all active sessions for that user.
3. IF an Administrator attempts to add a user with a duplicate identifier, THEN THE Web_Portal SHALL reject the request and return a descriptive error message.
4. IF a non-Administrator user attempts to add or remove users, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 2: Administrator Project Management

**User Story:** As an Administrator, I want to create and remove projects, so that I can organise HPC resources around business needs.

#### Acceptance Criteria

1. WHEN an Administrator submits a request to create a project, THE Web_Portal SHALL create the project with an associated Cost_Allocation_Tag and return a confirmation.
2. WHEN an Administrator submits a request to remove a project, THE Web_Portal SHALL verify that no active clusters exist in the project before removing the project and its associated resources.
3. IF an Administrator attempts to remove a project that has active clusters, THEN THE Web_Portal SHALL reject the request and list the active clusters that must be destroyed first.
4. IF a non-Administrator user attempts to create or remove projects, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 3: Administrator Cluster Template Management

**User Story:** As an Administrator, I want to define cluster templates, so that users can provision clusters optimised for specific workloads.

#### Acceptance Criteria

1. WHEN an Administrator submits a cluster template definition specifying instance types, node configuration, and software stack, THE Web_Portal SHALL store the Cluster_Template and make it available for cluster creation.
2. WHEN an Administrator submits a request to remove a Cluster_Template, THE Web_Portal SHALL remove the template from the available list.
3. THE Web_Portal SHALL provide two default Cluster_Templates for the initial proof of concept: one using cost-effective CPU instances and one using low-end GPU instances.
4. IF a non-Administrator user attempts to create or remove cluster templates, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 4: Project Administrator Membership Management

**User Story:** As a Project_Administrator, I want to authorise and remove users from my project, so that I can control who accesses project resources.

#### Acceptance Criteria

1. WHEN a Project_Administrator submits a request to add a user to the project, THE Web_Portal SHALL grant the user Project_User access to that project.
2. WHEN a Project_Administrator submits a request to remove a user from the project, THE Web_Portal SHALL revoke the user's access to that project.
3. IF a Project_Administrator attempts to add a user who is not registered on the platform, THEN THE Web_Portal SHALL reject the request and return a descriptive error message.
4. IF a user who is not a Project_Administrator for the target project attempts to modify project membership, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 5: Project Budget Management

**User Story:** As a Project_Administrator, I want to set a budget limit for my project, so that spending is controlled and stakeholders are alerted before costs exceed the limit.

#### Acceptance Criteria

1. WHEN a Project_Administrator sets a budget limit for a project, THE Web_Portal SHALL create or update the Budget_Alert associated with the project Cost_Allocation_Tag.
2. WHEN project spending reaches 80% of the budget limit, THE Budget_Alert SHALL send a notification to the Project_Administrator.
3. WHEN project spending reaches 100% of the budget limit, THE Budget_Alert SHALL send a notification to the Project_Administrator and to all Administrators.
4. IF a user who is not a Project_Administrator for the target project attempts to modify the budget limit, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 6: Cluster Creation

**User Story:** As a Project_User, I want to create an HPC cluster from a template, so that I can run computational workloads.

#### Acceptance Criteria

1. WHEN a Project_User initiates cluster creation, THE Web_Portal SHALL suggest a unique Cluster_Name derived from the project identifier and a sequential or random suffix, which the user may accept or override.
2. WHEN a Project_User selects a Cluster_Template and provides a Cluster_Name, THE Web_Portal SHALL provision a Cluster using AWS Parallel Computing Service within the project boundary.
3. THE Web_Portal SHALL tag all resources of the provisioned Cluster with the project Cost_Allocation_Tag and the Cluster_Name.
4. THE Web_Portal SHALL place all Compute_Nodes on a private subnet.
5. THE Web_Portal SHALL mount the user's Home_Directory on the Cluster.
6. THE Web_Portal SHALL make Project_Storage accessible from the Cluster via FSx_for_Lustre.
7. IF a Project_User provides a Cluster_Name that has been used by a different project at any time, THEN THE Web_Portal SHALL reject the request and return a descriptive error message.
8. WHEN a Project_User provides a Cluster_Name that was previously used within the same project, THE Web_Portal SHALL allow the cluster creation to proceed.
9. IF the project budget has been breached, THEN THE Web_Portal SHALL reject the cluster creation request and inform the user that the budget limit has been exceeded.

### Requirement 7: Cluster Destruction

**User Story:** As a Project_User, I want to destroy a cluster when I no longer need it, so that resources are released and costs are minimised.

#### Acceptance Criteria

1. WHEN a Project_User submits a request to destroy a Cluster, THE Web_Portal SHALL terminate all Compute_Nodes and the Head_Node of the Cluster.
2. WHEN a Cluster is destroyed, THE Web_Portal SHALL release all associated FSx_for_Lustre filesystems for that Cluster.
3. WHEN a Cluster is destroyed, THE Web_Portal SHALL retain the Home_Directory and Project_Storage.
4. IF a user who is not a Project_User or Project_Administrator for the project attempts to destroy a Cluster, THEN THE Web_Portal SHALL reject the request with an authorisation error.

### Requirement 8: Cluster Access

**User Story:** As a Project_User, I want to access my cluster via SSH or DCV, so that I can interact with the HPC environment.

#### Acceptance Criteria

1. THE Web_Portal SHALL provide SSH connection details for the Head_Node of each active Cluster to authorised Project_Users.
2. THE Web_Portal SHALL provide DCV connection details for the Head_Node of each active Cluster to authorised Project_Users.
3. WHEN a Project_User connects to a Cluster via SSH or DCV, THE Cluster SHALL authenticate the user as their individual POSIX_User account, NOT as a generic shared account such as ec2-user, centos, or ubuntu.
4. THE Cluster SHALL disable interactive login for all generic or system accounts (e.g., ec2-user, centos, ubuntu) so that users cannot bypass individual identity.
5. IF the project budget has been breached, THEN THE Web_Portal SHALL deny login access to the Cluster for Project_Users.
6. IF a user who is not authorised for the project attempts to access a Cluster, THEN THE Web_Portal SHALL deny access.
7. IF a Cluster is not in ACTIVE status (e.g., CREATING, FAILED, DESTROYING), THEN THE Web_Portal SHALL NOT provide login credentials and SHALL inform the user that the cluster is not yet available.

### Requirement 9: Project Isolation

**User Story:** As a Project_Administrator, I want my project to be isolated from other projects, so that data governance and security boundaries are maintained.

#### Acceptance Criteria

1. THE CDK_Stack SHALL deploy each project's resources into a dedicated network boundary so that clusters in one project cannot communicate with clusters in another project.
2. THE CDK_Stack SHALL configure Project_Storage S3 bucket policies to deny access from principals outside the project boundary.
3. THE CDK_Stack SHALL configure Home_Directory access so that home directories in one project are not accessible from another project.
4. THE CDK_Stack SHALL configure FSx_for_Lustre filesystems so that they are accessible only from clusters within the owning project.

### Requirement 10: Home Directory Storage

**User Story:** As a Project_User, I want a persistent home directory that is shared across all clusters in my project, so that my files are available regardless of which cluster I use.

#### Acceptance Criteria

1. WHEN a user is added to a project, THE Web_Portal SHALL provision a Home_Directory for that user within the project.
2. THE Home_Directory SHALL persist across cluster creation and destruction events within the project.
3. WHEN a Cluster is created within a project, THE CDK_Stack SHALL mount the user's Home_Directory on the Cluster so that the user can access the same files from any active Cluster in the project.
4. THE CDK_Stack SHALL restrict Home_Directory access to clusters and users within the owning project.

### Requirement 11: Project Bulk Storage

**User Story:** As a Project_User, I want project bulk storage accessible as a filesystem from any cluster in my project, so that I can work with large datasets efficiently.

#### Acceptance Criteria

1. WHEN a project is created, THE Web_Portal SHALL provision a Project_Storage S3 bucket tagged with the project Cost_Allocation_Tag, or optionally use an S3 bucket provided by the project administrator
2. WHEN a Cluster is created within a project, THE CDK_Stack SHALL create an FSx_for_Lustre filesystem with a data repository association to the Project_Storage S3 bucket and mount it on the Cluster.
3. THE CDK_Stack SHALL configure the Project_Storage S3 bucket policy to deny access from principals outside the project boundary.
4. THE FSx_for_Lustre filesystem SHALL synchronise data back to the Project_Storage S3 bucket when the Cluster is destroyed.

### Requirement 12: Centralised Job Accounting

**User Story:** As an Administrator, I want all job logs stored in a single centralised Slurm accounting database, so that I can analyse job data across all clusters.

#### Acceptance Criteria

1. THE CDK_Stack SHALL deploy a single Slurm_Accounting_Database shared by all clusters across all projects.
2. WHEN a Cluster is created, THE CDK_Stack SHALL configure the Cluster to send Slurm job accounting records to the Slurm_Accounting_Database.
3. THE Web_Portal SHALL provide Administrators with access to query the Slurm_Accounting_Database for job records across all clusters.
4. THE Slurm_Accounting_Database SHALL use a serverless or low-cost persistent database to minimise running costs when no clusters are active.

### Requirement 13: Infrastructure and Access Logging

**User Story:** As an Administrator, I want infrastructure and user access events logged to CloudWatch, so that I can monitor platform health and audit user activity.

#### Acceptance Criteria

1. THE CDK_Stack SHALL configure all infrastructure components to send operational logs to Amazon CloudWatch Logs.
2. WHEN a user logs into a Cluster, THE Cluster SHALL log the access event to Amazon CloudWatch Logs including the user identifier, Cluster_Name, and timestamp.
3. WHEN a user performs an action in the Web_Portal, THE Web_Portal SHALL log the action to Amazon CloudWatch Logs including the user identifier, action type, and timestamp.
4. THE CDK_Stack SHALL configure CloudWatch log retention to 90 days for infrastructure logs and 365 days for user access logs.

### Requirement 14: Resource Tagging and Cost Allocation

**User Story:** As an Administrator, I want all project resources tagged with a cost allocation tag, so that I can track and attribute costs to each project.

#### Acceptance Criteria

1. WHEN a resource is deployed within a project, THE CDK_Stack SHALL apply the project Cost_Allocation_Tag to the resource.
2. THE Cost_Allocation_Tag SHALL use the key "Project" and the value set to the project identifier.
3. WHEN a Cluster is created, THE CDK_Stack SHALL apply both the project Cost_Allocation_Tag and a "ClusterName" tag with the Cluster_Name value to all cluster resources.
4. THE CDK_Stack SHALL enable the Cost_Allocation_Tag in AWS Cost Explorer for cost reporting.

### Requirement 15: Security Group Configuration

**User Story:** As an Administrator, I want security groups to follow the principle of least privilege, so that the platform meets AWS Well-Architected Framework security best practices.

#### Acceptance Criteria

1. THE CDK_Stack SHALL configure all security groups with the minimum required ingress and egress rules.
2. THE CDK_Stack SHALL NOT configure any security group with an ingress rule open to 0.0.0.0/0.
3. THE CDK_Stack SHALL configure Head_Node security groups to allow SSH and DCV access only from a defined set of trusted CIDR ranges.
4. THE CDK_Stack SHALL configure Compute_Node security groups to allow traffic only from the Head_Node and other Compute_Nodes within the same Cluster.

### Requirement 16: Infrastructure as Code

**User Story:** As a platform engineer, I want all infrastructure defined as code using AWS CDK in TypeScript, so that deployments are repeatable and auditable.

#### Acceptance Criteria

1. THE CDK_Stack SHALL define all platform infrastructure using AWS CDK in TypeScript.
2. THE CDK_Stack SHALL prefer L2 constructs over L1 constructs for all AWS resources where L2 constructs are available.
3. THE CDK_Stack SHALL implement Lambda functions in Python for all serverless compute within the platform.
4. THE CDK_Stack SHALL use serverless AWS services for persistent platform components where available, to minimise running costs.

### Requirement 17: POSIX User Identity and File Ownership

**User Story:** As a Project_User, I want my files on shared filesystems to be owned by my individual user account, so that file access controls are correct when multiple users share a cluster.

#### Acceptance Criteria

1. WHEN a user is created on the platform, THE Web_Portal SHALL assign the user a globally unique POSIX UID and GID that is consistent across all projects and all clusters on the platform.
2. WHEN a Cluster is created, THE Cluster SHALL create a POSIX_User account for each authorised Project_User with the UID and GID assigned by the Web_Portal.
3. THE Cluster SHALL set ownership of each user's Home_Directory mount to the user's POSIX_User UID and GID.
4. THE Cluster SHALL enforce standard POSIX file permissions on Home_Directory and FSx_for_Lustre mounts so that users cannot read or modify other users' files unless permissions explicitly allow it.
5. WHEN a new user is added to a project that has active clusters, THE Web_Portal SHALL propagate the new POSIX_User account to all active clusters in that project.

### Requirement 18: Cluster Naming Uniqueness

**User Story:** As a Project_User, I want cluster names to be human-readable and unique, so that I can easily identify and reference my clusters.

#### Acceptance Criteria

1. THE Web_Portal SHALL validate that a Cluster_Name is a non-empty string containing only alphanumeric characters, hyphens, and underscores.
2. THE Web_Portal SHALL maintain a registry of all Cluster_Names ever used, associated with their project identifier.
3. WHEN a Project_User requests a Cluster_Name that has been used by a different project, THE Web_Portal SHALL reject the request and return a descriptive error message indicating the name is reserved.
4. WHEN a Project_User requests a Cluster_Name that was previously used within the same project, THE Web_Portal SHALL allow the request.

### Requirement 19: Cluster Lifecycle Feedback and Resilience

**User Story:** As a Project_User, I want to see the progress of my cluster deployment, be notified when it completes or fails, and know that failed deployments are cleaned up automatically, so that I can work efficiently and avoid unnecessary costs.

#### Acceptance Criteria

1. IF a cluster deployment fails at any step, THE Web_Portal SHALL automatically revert all partially created resources for that cluster to avoid unnecessary spending, and SHALL mark the cluster status as FAILED.
2. WHEN a cluster is being created, THE Web_Portal SHALL display the current deployment step and progress to the user in the Web_Portal UI.
3. THE Web_Portal SHALL allow the user to navigate away from the cluster creation page and return later to see the current status of the deployment.
4. WHEN a cluster deployment completes successfully, THE Web_Portal SHALL notify the creating user (e.g., via in-app notification or email).
5. WHEN a cluster deployment fails, THE Web_Portal SHALL notify the creating user with a description of the failure.
6. THE Cluster SHALL prevent user login via SSH or DCV until the cluster deployment has completed successfully and the cluster status is ACTIVE.

### Requirement 20: Deployment Automation

**User Story:** As a platform engineer, I want simple automation targets for deploying, tearing down workloads, and purging the entire platform, so that I can manage the platform lifecycle efficiently without manual multi-step procedures.

#### Acceptance Criteria

1. THE Platform SHALL provide a script or Makefile target that automates the full deployment of the foundation infrastructure and platform components in a single invocation.
2. THE Platform SHALL provide a script or Makefile target that removes all clusters and projects while retaining the foundation infrastructure (Cognito, DynamoDB tables, API Gateway, CloudFront distribution).
3. THE Platform SHALL provide a script or Makefile target that completely purges the application, including all clusters, projects, and the foundation infrastructure.
4. WHEN the deployment automation target is invoked, THE Platform SHALL deploy the foundation CDK stack and all dependent resources in the correct dependency order.
5. WHEN the teardown automation target is invoked, THE Platform SHALL destroy all project infrastructure stacks and remove all cluster and project records from DynamoDB before returning success.
6. WHEN the purge automation target is invoked, THE Platform SHALL destroy all project infrastructure stacks, remove all cluster and project records, and then destroy the foundation CDK stack.
7. IF the teardown or purge target encounters an active cluster, THEN THE Platform SHALL destroy the cluster before removing the project infrastructure.

### Requirement 21: Platform Documentation

**User Story:** As an Administrator, Project_Administrator, or Project_User, I want comprehensive documentation covering all platform operations, so that I can deploy, manage, and use the platform without relying on tribal knowledge.

#### Acceptance Criteria

1. THE Platform SHALL provide documentation in Markdown format stored in the repository.
2. THE Platform SHALL serve the documentation as web pages through the deployed CloudFront distribution alongside the Web_Portal.
3. THE Platform SHALL provide documentation covering the following topics: deploying the foundation infrastructure, creating and updating and removing users, creating and updating and removing projects, creating and updating and removing clusters, accessing clusters and submitting jobs, and uploading and downloading data from a project.
4. THE Platform SHALL provide API reference documentation describing all Web_Portal API endpoints, their request and response formats, required authorisation roles, and error codes.
5. THE Platform SHALL organise documentation by target audience: Administrator guides, Project_Administrator guides, and Project_User guides.
6. WHEN the platform is deployed, THE CloudFront distribution SHALL serve the documentation pages at a well-known path prefix so that users can access documentation from the Web_Portal.
7. WHEN documentation source files in the repository are updated and redeployed, THE CloudFront distribution SHALL serve the updated documentation.
