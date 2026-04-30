# Accessing Clusters

This guide covers how to connect to HPC clusters and submit computational jobs. These operations are available to **Project Users** and **Project Administrators**.

## Prerequisites

- You must be an **active member** of the project that owns the cluster.
- The cluster must be in **ACTIVE** status.
- The project budget must **not be breached** — access is denied if the budget limit has been exceeded.

## Getting Connection Details

**Endpoint:** `GET /projects/{projectId}/clusters/{clusterName}`

The response for an active cluster includes connection information:

```json
{
  "clusterName": "genomics-run-42",
  "status": "ACTIVE",
  "loginNodeIp": "54.123.45.67",
  "connectionInfo": {
    "ssh": "ssh -p 22 jsmith@54.123.45.67",
    "dcv": "https://54.123.45.67:8443",
    "ssm": "aws ssm start-session --target i-0abc123def456789a"
  }
}
```

You can also view connection details in the web portal by navigating to your project's cluster list and selecting the cluster.

## Connecting via SSH

Each cluster's login node (head node) is accessible via SSH on the public or protected subnet.

```bash
ssh -p 22 <your-username>@<login-node-ip>
```

Replace `<your-username>` with your platform user ID (the `userId` assigned when your account was created) and `<login-node-ip>` with the IP address from the cluster details.

### Key Points

- You log in as **your individual POSIX user account**, not as `ec2-user`, `centos`, or `ubuntu`.
- Generic/system accounts are **disabled for interactive login** on all cluster nodes.
- Your POSIX UID and GID are consistent across all clusters on the platform, so file ownership is always correct.
- SSH access is restricted to trusted CIDR ranges configured by the platform administrator.

### First-Time Login

On your first connection, you may need to accept the host key fingerprint. Your initial password is set through Cognito — check your email for the temporary credentials provided when your account was created.

## Connecting via DCV

NICE DCV provides a remote desktop experience for graphical workloads.

1. Open a web browser and navigate to `https://<login-node-ip>:8443`.
2. Log in with your platform user ID and password.
3. You will be connected to a desktop session on the login node.

DCV is particularly useful for:

- Visualising simulation results
- Running graphical pre/post-processing tools
- Interactive debugging

> **Note:** DCV uses a self-signed certificate by default. Your browser may show a security warning — this is expected for the initial deployment.

## Connecting via SSM Session Manager

AWS Systems Manager (SSM) Session Manager provides a secure shell to the login node without requiring open inbound ports or SSH keys. This is useful when SSH access is restricted by network policy or when you prefer not to manage SSH keys.

### Prerequisites

- **AWS CLI v2** — install from [https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html)
- **Session Manager plugin** — install from [https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
- Valid AWS credentials configured for the account where the cluster is deployed.

### Starting a Session

Use the SSM command from the cluster connection details:

```bash
aws ssm start-session --target <instanceId>
```

Replace `<instanceId>` with the login node's EC2 instance ID from the cluster details (e.g. `i-0abc123def456789a`).

### Key Points

- SSM sessions do not require inbound security group rules — traffic is routed through the Systems Manager service.
- You connect as the `ssm-user` by default. To switch to your POSIX user account after connecting, run `sudo su - <your-username>`.
- Session activity can be logged to CloudWatch or S3 if configured by the platform administrator.
- The SSM Agent is automatically verified and started on all cluster login nodes during provisioning. If you encounter a `TargetNotConnected` error, the instance may still be initialising — wait a minute and try again.

## Submitting Jobs with Slurm

Clusters use **Slurm** as the job scheduler. Once connected to the login node via SSH, you can submit and manage jobs using standard Slurm commands. The Slurm binaries (`sinfo`, `squeue`, `sbatch`, etc.) are automatically added to your PATH when you log in.

### Submitting a Batch Job

Create a job script (e.g., `my_job.sh`):

```bash
#!/bin/bash
#SBATCH --job-name=my-simulation
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=4
#SBATCH --time=01:00:00
#SBATCH --output=output_%j.log

echo "Running on $(hostname)"
echo "Job started at $(date)"

# Your computation here
srun ./my_application --input data.dat --output results.dat

echo "Job finished at $(date)"
```

Submit the job:

```bash
sbatch my_job.sh
```

### Common Slurm Commands

| Command | Description |
|---------|-------------|
| `sbatch script.sh` | Submit a batch job |
| `squeue` | View the job queue |
| `squeue -u $USER` | View your jobs only |
| `scancel <job-id>` | Cancel a job |
| `sinfo` | View node and partition status |
| `sacct -j <job-id>` | View job accounting details |
| `srun --pty bash` | Start an interactive session on a compute node |

### Interactive Jobs

For interactive work on compute nodes:

```bash
srun --nodes=1 --ntasks=1 --time=00:30:00 --pty bash
```

This allocates a compute node and opens an interactive shell.

### Job Accounting

Slurm accounting is enabled on all clusters. You can query your job history:

```bash
# View recent jobs
sacct --starttime=2025-01-01

# View detailed job information
sacct -j <job-id> --format=JobID,JobName,Partition,State,ExitCode,Elapsed,MaxRSS

# View job efficiency
seff <job-id>
```

Platform administrators can also query job accounting data across all clusters via the API (`GET /accounting/jobs`).

## Cluster Access Restrictions

| Condition | Access Allowed? |
|-----------|----------------|
| Cluster is ACTIVE, user is a project member, budget OK | Yes |
| Cluster is CREATING | No — cluster not yet ready |
| Cluster is FAILED | No — cluster did not deploy successfully |
| Cluster is DESTROYING or DESTROYED | No — cluster is being or has been removed |
| Project budget is breached | No — access denied until budget is resolved |
| User is not a project member | No — authorisation error |

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| `Connection refused` on SSH | Verify the cluster is in ACTIVE status; check that your IP is in the trusted CIDR range |
| `Permission denied` on SSH | Verify you are using your platform user ID, not `ec2-user` or `ubuntu` |
| `BUDGET_EXCEEDED` error | Contact your Project Administrator to increase the budget limit |
| `TargetNotConnected` on SSM | The instance may still be starting up — wait 1–2 minutes and retry. Verify the cluster is ACTIVE and the instance ID is correct |
| Jobs stuck in `PENDING` | Check `sinfo` for available nodes; the cluster may be scaling up compute nodes |
| Cannot see connection details | The cluster may still be in CREATING status — check the progress endpoint |
