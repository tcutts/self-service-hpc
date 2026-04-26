I want to create a self-service HPC environment for users, which consists of the following principal components

- A web interface for administration and access
- Ephemeral HPC clusters
- Storage buckets which outlive the HPC clusters, but which the clusters can access directly from the clusters using FSx for Lustre.
- User home directories accessible from multiple clusters

# Web interface capabilities

## User roles

### Administrator

Administrators can add and remove users
Administrators can add and remove projects
Administrators can define cluster templates

### Project administrators

Project administrators own projects, and can authorise other users to access the projects.  They also have all the rights that project users do.

They set the budget limit for the project.

### Project users

Project users can create and destroy clusters, but not longer lived project-scope filesystems and data stores
project users can log into and use the clusters, unless the budget has been breached.

# User onboarding, offboarding and reactivation

If users are removed, they are just disabled, so their audit records remain.
The UI should include the abiltity to reactivate a disabled user

# Projects 

Projects are aligned with business needs, and provide the security boundary for data governance
A project may have multiple clusters and data stores deployed within it.
Projects should be isolated from each other as much as possible

# Storage

User home directories should be persistent, and shared across clusters within a project, but not across projects.

There should be a mechanism for users to transfer data into and out of the S3 buckets to which they have access.

Project bulk storage should be in S3, and visible as a filesystem to any cluster within the project, but not by other projects, by default.

# Clusters

Clusters should be as ephemeral as possible.  The user should be able to choose from a list of templates, optimised for particular workloads.  For example, a cluster for GROMACS might use low end GPU instances, a general purpose cluster might use hpc7a or graviton family instances.  For the initial proof of concept, we should create two templates using cost effective small instances, one using basic CPU instances and one using low end GPU instances.

Access to the cluster should be by ssh or DCV to the head node.

The cluster must have a unique human-readable name, and its resources tagged with that name.  These cluster names can be re-used within a project, but can never be re-used by other projects.

The compute nodes themselves MUST be on a private subnet so as to avoid allocation of too many public IP addresses.

# Logging

Job logs should be stored in a single central slurm accounting database, shared by all clusters so that administrators can analyse job logs across all clusters

Other infrastructure logs should be sent to cloudwatch as usual

User accesses should be logged in cloud watch.

# Cost controls

All resources deployed in a project should be tagged with a project cost allocation tag, and a budget alert associated with it.

Implementation choices should favour frugality, especially low running costs for components which are persistent.

# Additional thoughts

If the cluster deployment fails, it should be reverted, to avoid spending unnecessary money.

The UI should give visual feedback of progress of the workflow as the cluster is created

The user should be able to navigate away from the UI while their cluster is being created, and come back later to see the status of the deployment

The user should be notified when the cluster creation completes, or if it fails.

The cluster user should be prevented from logging into the head node until the cluster deployment has completed successfully.

# Second set of additional thoughts

There must be a simple script or makefile target to automate deployment
There must be a simple script or makefile target to completely remove all clusters and projects, but leave the foundational infrastructure
There must be a simple script or makefile target to completely purge the application, including the foundational infrastructure

# Documentation

There should be comprehensive documentation aimed at administrators, project administrators and users, and a reference for the APIs.  This documentation should be available both as MD files in this repository and as web pages through the deployed cloudfront distribution.  Topics covered should include:

- deploying the foundation infrastructure
- creating, updating and removing users
- creating, updating and removing projects
- creating, updating and removing clusters
- For users: accessing clusters and submitting jobs
- For users: uploading and downloading data from a project

