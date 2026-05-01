# Data Management

This guide covers how to upload, download, and manage data within your project. These operations are available to **Project Users** and **Project Administrators**.

## Storage Overview

Each project has two types of persistent storage:

| Storage Type | Technology | Purpose | Persistence |
|-------------|-----------|---------|-------------|
| **Home Directory** | Amazon EFS | Personal files, scripts, configurations | Survives cluster creation/destruction |
| **Project Storage** | Amazon S3 + FSx for Lustre | Large datasets, shared project data | Survives cluster creation/destruction |

Both storage types are **persistent** — your data is preserved when clusters are created or destroyed.

## Home Directories

Each user has a personal home directory within each project they belong to. Home directories are:

- **Mounted automatically** on every cluster in the project at `/home/<username>`
- **Shared across clusters** — files saved on one cluster are visible on all other active clusters in the same project
- **Private by default** — standard POSIX permissions (mode 700) prevent other users from reading your files
- **Backed by Amazon EFS** — durable, elastic storage that scales automatically

> **Note:** EFS home directories are automatically mounted at `/home` on every cluster node at boot time via the EC2 launch template user data script. No manual mount steps are required — when you SSH into a cluster node, your home directory is already available.

### Working with Home Directories

Once connected to a cluster via SSH:

```bash
# Your home directory
cd ~
pwd
# /home/jsmith

# Check available space (EFS scales automatically)
df -h /home/jsmith

# Files you create here persist across cluster lifecycles
mkdir my-scripts
cp my_job.sh my-scripts/
```

### File Permissions

Home directories use standard POSIX file permissions. Each user's files are owned by their unique POSIX UID/GID:

```bash
# View file ownership
ls -la ~/
# drwx------ 3 jsmith jsmith 4096 Jan 15 14:00 .
# -rw-r--r-- 1 jsmith jsmith  220 Jan 15 14:00 .bashrc

# Share a file with other project members
chmod 644 ~/shared-results.txt

# Create a shared directory
mkdir ~/shared
chmod 755 ~/shared
```

## Project Storage (S3 + FSx for Lustre)

Project storage provides high-performance access to large datasets. It consists of:

1. **S3 bucket** — durable, long-term storage for project data
2. **FSx for Lustre** — high-performance filesystem cache mounted on clusters, linked to the S3 bucket via a data repository association

### How It Works

When a cluster is created:

1. An FSx for Lustre filesystem is created with a **data repository association** to the project's S3 bucket.
2. The filesystem is mounted on all cluster nodes (typically at `/fsx` or `/lustre`).
3. Files in the S3 bucket are **automatically imported** into the FSx filesystem.
4. Changes made on the filesystem can be **exported back to S3**.

When a cluster is destroyed:

1. A data repository export task **syncs all changes back to S3**.
2. The FSx for Lustre filesystem is deleted.
3. The S3 bucket and its contents are **preserved**.

### Uploading Data to S3

Upload data to the project S3 bucket before or after creating a cluster:

```bash
# Upload a file
aws s3 cp my-dataset.tar.gz s3://hpc-project-genomics-team-data/ --profile $AWS_PROFILE

# Upload a directory
aws s3 sync ./input-data/ s3://hpc-project-genomics-team-data/input-data/ --profile $AWS_PROFILE

# List bucket contents
aws s3 ls s3://hpc-project-genomics-team-data/ --profile $AWS_PROFILE
```

### Accessing Data on a Cluster

Once connected to a cluster, project storage is available as a mounted filesystem:

```bash
# Navigate to the project storage mount
cd /fsx

# List available data (imported from S3)
ls -la

# Read data for your jobs
cat /fsx/input-data/config.yaml

# Write results (will be exported to S3 on cluster destruction)
cp results.dat /fsx/output/results.dat
```

### Downloading Data from S3

After a cluster is destroyed and data has been exported:

```bash
# Download results
aws s3 cp s3://hpc-project-genomics-team-data/output/results.dat ./results.dat --profile $AWS_PROFILE

# Download a directory
aws s3 sync s3://hpc-project-genomics-team-data/output/ ./output/ --profile $AWS_PROFILE
```

## Data Flow Diagram

```
Upload:   Local machine → S3 bucket → FSx for Lustre (auto-import) → Cluster nodes
Compute:  Cluster nodes read/write FSx for Lustre
Download: Cluster nodes → FSx for Lustre → S3 bucket (export on destroy) → Local machine
```

## Best Practices

### Performance

- **Use FSx for Lustre** (`/fsx`) for large datasets that need high-throughput I/O during computation.
- **Use home directories** (`/home`) for scripts, configuration files, and small outputs.
- **Stage data to S3 before creating a cluster** — this allows FSx to import data during cluster creation, so it is ready when you log in.

### Data Safety

- **Important results should be copied to S3** explicitly, rather than relying solely on the automatic export on cluster destruction.
- **Home directories persist** across cluster lifecycles — use them for files you always need.
- **S3 data persists** after cluster destruction — your project storage bucket is not deleted when clusters are removed.

### Cost Management

- **Delete unused data from S3** to reduce storage costs.
- **Destroy clusters when not in use** — FSx for Lustre filesystems incur costs while active.
- **Use S3 lifecycle policies** for archiving old data to cheaper storage classes (contact your administrator).

## Storage Access by Role

| Storage | Project User | Project Administrator | Administrator |
|---------|-------------|----------------------|---------------|
| Own home directory | Read/Write | Read/Write | Read/Write |
| Other users' home directories | Per POSIX permissions | Per POSIX permissions | Per POSIX permissions |
| Project S3 bucket | Via FSx mount on cluster | Via FSx mount + direct S3 access | Direct S3 access |
| Other projects' storage | No access | No access | No access |

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| `/fsx` is empty after cluster creation | Wait for FSx import to complete; large datasets may take time to appear |
| `Permission denied` on `/fsx` | Check POSIX permissions; FSx inherits permissions from S3 object metadata |
| Cannot upload to S3 | Verify your AWS credentials and that the bucket policy allows access from your principal |
| Data missing after cluster destruction | Check the S3 bucket — the export task runs automatically; verify it completed successfully |
| Home directory not mounted | Verify you are a member of the project; contact your Project Administrator |
