# Bugfix Requirements Document

## Introduction

PCS login nodes enter an infinite crash loop because the generated user data script uses `set -euo pipefail`, causing any single command failure (e.g., EFS mount, S3 Mountpoint install) to abort the entire script. When cloud-final fails, PCS health checks mark the node as unhealthy and terminate it. The replacement node hits the same failure, creating a continuous terminate-and-replace cycle. The fix must make the user data script resilient to individual section failures while still logging errors for diagnosis.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN any command in the generated user data script fails (e.g., `yum install -y amazon-efs-utils`, `mount -a -t efs`, `yum install -y mountpoint-s3`, or `mount-s3`) THEN the system aborts the entire script immediately due to `set -euo pipefail`, causing cloud-final to report failure

1.2 WHEN cloud-final fails on a PCS login node THEN the system triggers PCS health check failure, which terminates the instance and launches a replacement that also crashes, creating an infinite terminate-and-replace loop

1.3 WHEN the EFS mount section fails (package unavailable or stunnel dependency missing for TLS) THEN the system prevents all subsequent sections from executing, including user account creation, access logging configuration, and storage mounts

1.4 WHEN the Mountpoint for S3 section fails (package unavailable or mount command fails) THEN the system prevents all subsequent sections from executing, including the final provisioning-complete echo

1.5 WHEN the FSx for Lustre mount section fails (lustre client unavailable or mount command fails) THEN the system prevents all subsequent sections from executing

1.6 WHEN a mount operation fails THEN the system provides no diagnostic logging about which specific section failed or why, making troubleshooting difficult from CloudWatch or EC2 console output

### Expected Behavior (Correct)

2.1 WHEN any individual section of the generated user data script fails (EFS mount, S3 mount, FSx mount, package install) THEN the system SHALL log the error clearly and continue executing the remaining sections of the script

2.2 WHEN one or more sections of the user data script fail THEN the system SHALL still exit with code 0 so that cloud-final reports success and PCS does not terminate the node

2.3 WHEN the EFS mount section fails THEN the system SHALL log the failure with the section name and error details, and SHALL continue to execute user account creation, generic account disabling, access logging, and storage mount sections

2.4 WHEN the Mountpoint for S3 section fails THEN the system SHALL log the failure with the section name and error details, and SHALL continue to execute any remaining sections

2.5 WHEN the FSx for Lustre mount section fails THEN the system SHALL log the failure with the section name and error details, and SHALL continue to execute any remaining sections

2.6 WHEN any section fails THEN the system SHALL output a clear summary at the end of the script indicating which sections succeeded and which failed, enabling diagnosis from CloudWatch logs or EC2 console output

### Unchanged Behavior (Regression Prevention)

3.1 WHEN all commands in the user data script succeed (packages available, mounts succeed, user creation succeeds) THEN the system SHALL CONTINUE TO execute all sections in the same order and produce the same final node state as before the fix

3.2 WHEN an EFS filesystem ID is provided THEN the system SHALL CONTINUE TO install amazon-efs-utils, create the mount point, add the fstab entry, and attempt the EFS mount

3.3 WHEN storage mode is "mountpoint" with an S3 bucket name THEN the system SHALL CONTINUE TO install mountpoint-s3, create the mount point, mount the bucket, add the rc.local entry, and set rc.local permissions

3.4 WHEN storage mode is "lustre" with FSx DNS and mount names THEN the system SHALL CONTINUE TO install the lustre client, create the mount point, mount the filesystem, and add the fstab entry

3.5 WHEN project members exist THEN the system SHALL CONTINUE TO create POSIX user accounts with the correct UID/GID and set home directory ownership

3.6 WHEN generic accounts (ec2-user, centos, ubuntu) exist on the node THEN the system SHALL CONTINUE TO disable interactive login for those accounts

3.7 WHEN the script is generated THEN the system SHALL CONTINUE TO wrap it in MIME multipart format for PCS launch template compatibility

3.8 WHEN SSM Agent commands are generated THEN the system SHALL CONTINUE TO ensure the SSM Agent is installed and running as the first section of the script
