# Deploying the Foundation Infrastructure

This guide walks administrators through deploying the Self-Service HPC Platform foundation stack, which provisions all shared control-plane resources.

## Prerequisites

Before deploying, ensure the following are installed and configured:

- **Node.js** (v18 or later) and **npm**
- **Python 3.13** and **pip**
- **AWS CDK CLI** (`npm install -g aws-cdk`)
- **AWS CLI** configured with your AWS profile (set in `Makefile` as `AWS_PROFILE`)
- **Git** for cloning the repository

Verify your AWS credentials:

```bash
aws sts get-caller-identity --profile $AWS_PROFILE
```

## What the Foundation Stack Deploys

The `HpcFoundationStack` provisions the following shared resources:

| Resource | Purpose |
|----------|---------|
| Amazon Cognito User Pool | User authentication and role-based access control |
| DynamoDB tables | PlatformUsers, Projects, ClusterTemplates, Clusters, ClusterNameRegistry |
| API Gateway REST API | Platform API with Cognito authoriser |
| Lambda functions | User management, project management, template management, cluster operations, accounting |
| Step Functions state machines | Cluster creation and destruction workflows |
| SNS topics | Budget notifications and cluster lifecycle notifications |
| S3 bucket | Web portal and documentation static assets |
| CloudFront distribution | Content delivery for the web portal and documentation |
| CloudWatch log groups | Infrastructure logs (90-day retention) and API access logs (365-day retention) |

## Step-by-Step Deployment

### 1. Clone the Repository

```bash
git clone <repository-url>
cd self-service-hpc
```

### 2. Install Dependencies

```bash
# Install Node.js dependencies
npm ci

# Create a Python virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Build the CDK Application

```bash
npm run build
```

### 4. Bootstrap CDK (First Time Only)

If this is the first CDK deployment in the target AWS account and region:

```bash
npx cdk bootstrap --profile $AWS_PROFILE
```

### 5. Deploy the Foundation Stack

Deploy the foundation stack with the `adminEmail` context parameter to automatically provision the first administrator:

```bash
npx cdk deploy HpcFoundationStack \
  -c adminEmail=ops@company.com \
  --require-approval never \
  --profile $AWS_PROFILE
```

The `adminEmail` parameter is **optional but recommended** on first deployment. When provided, the stack automatically creates an initial admin user with a temporary password. If omitted, the stack deploys normally without an admin user — you can create one manually afterwards (see [Manual Alternative](#manual-alternative) below).

> **Note:** `make deploy` does not pass CDK context parameters. Use the `cdk deploy` command directly as shown above when you need to supply `adminEmail`.

The deployment takes approximately 5–10 minutes. CDK outputs the following values on completion:

- **API Gateway URL** — the base URL for all platform API calls
- **Cognito User Pool ID** — used for authentication configuration
- **Cognito User Pool Client ID** — used by the web portal
- **CloudFront Distribution URL** — the URL for the web portal and documentation
- **AdminUserName** — the username of the provisioned admin (only when `adminEmail` is provided and no admin exists)
- **AdminUserPassword** — the temporary password for the admin (only when `adminEmail` is provided and no admin exists)

### 6. Verify the Deployment

Check that the API is reachable:

```bash
curl -s https://<api-gateway-url>/prod/health
```

Access the web portal at the CloudFront distribution URL.

Access the documentation at `https://<cloudfront-url>/docs/index.html`.

## Post-Deployment Configuration

### Admin User Provisioning

When the `adminEmail` CDK context parameter is provided during deployment, the Foundation stack automatically provisions an admin user via a CloudFormation custom resource. If `adminEmail` is not provided, the provisioner is not created and the stack deploys without an admin user.

**What happens when `adminEmail` is provided:**

1. The provisioner scans the PlatformUsers DynamoDB table for any existing user with the `Administrator` role.
2. If an administrator already exists, the provisioner completes with no changes.
3. If no administrator is found, the provisioner creates a default `admin` user in both Cognito and DynamoDB with a securely generated temporary password.

**Retrieving the temporary password:**

The admin username and temporary password are available in the CloudFormation stack outputs after deployment. Retrieve them with the AWS CLI:

```bash
aws cloudformation describe-stacks \
  --stack-name HpcFoundationStack \
  --query "Stacks[0].Outputs" \
  --output table \
  --profile $AWS_PROFILE
```

To retrieve only the temporary password:

```bash
QUERY="Stacks[0].Outputs"
QUERY+="[?OutputKey=='AdminUserPassword']"
QUERY+=".OutputValue"
aws cloudformation describe-stacks \
  --stack-name HpcFoundationStack \
  --query "$QUERY" --output text \
  --profile $AWS_PROFILE
```

**First login flow:**

1. Open the web portal at the CloudFront distribution URL.
2. Log in with username `admin` and the temporary password from the stack outputs.
3. You will be prompted to set a new password (the account is in `FORCE_CHANGE_PASSWORD` state).
4. After resetting the password, you have full administrator access to the platform.

**Idempotent behaviour:**

Redeployments are safe. If an administrator already exists in the PlatformUsers table, the provisioner skips creation entirely. Running `make deploy` or `cdk deploy` multiple times will not:

- Create duplicate admin accounts
- Regenerate or overwrite the temporary password
- Modify any existing user records

**Security considerations:**

The provisioner is designed to prevent abuse through stack updates:

| Concern | Mitigation |
|---------|------------|
| Changing `adminEmail` to create a second admin | The provisioner checks for any existing administrator by role, not by email or userId. If one exists, creation is skipped. |
| Resetting admin credentials via stack update | The provisioner never modifies existing Cognito users or DynamoDB records. |
| Extracting new credentials from stack outputs | When an admin already exists, the stack outputs are empty. |
| Overwriting the `USER#admin` DynamoDB record | A `attribute_not_exists(PK)` condition on all writes prevents overwrites. |

#### Manual Alternative

If `adminEmail` is not provided during deployment, create the first administrator manually through the AWS Console or CLI:

1. Open the **Amazon Cognito** console and navigate to the `hpc-platform-users` User Pool.
2. Create a user with a valid email address.
3. Add the user to the `Administrators` group.

Or use the AWS CLI:

```bash
# Create the user
aws cognito-idp admin-create-user \
  --user-pool-id <user-pool-id> \
  --username admin \
  --user-attributes Name=email,Value=admin@example.com \
  --profile $AWS_PROFILE

# Add to Administrators group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <user-pool-id> \
  --username admin \
  --group-name Administrators \
  --profile $AWS_PROFILE
```

You will also need to create the corresponding DynamoDB record in the PlatformUsers table. The automated provisioner handles this automatically — using it via `adminEmail` is the recommended approach.

Once the first administrator exists, all subsequent user management can be done through the platform API.

### Web Portal Configuration

The web portal configuration (`js/config.js`) is generated automatically during deployment with the correct Cognito User Pool ID, Client ID, API Gateway URL, and region. No manual configuration is required.

## Updating the Platform

To update the platform after code changes:

```bash
make deploy
```

CDK performs incremental updates — only changed resources are modified.

## Teardown and Purge

### Remove Workloads (Keep Foundation)

Destroy all clusters and projects while retaining the foundation infrastructure:

```bash
make teardown
```

This removes all project VPCs, clusters, and DynamoDB records but preserves the Cognito User Pool, DynamoDB table schemas, API Gateway, and CloudFront distribution.

### Complete Removal

Remove everything, including the foundation stack:

```bash
make purge
```

Note: DynamoDB tables and the Cognito User Pool have `RemovalPolicy.RETAIN`, so they persist after stack deletion. Manual cleanup through the AWS Console may be needed.

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| `CDK bootstrap required` | Run `npx cdk bootstrap --profile $AWS_PROFILE` |
| `Stack is in ROLLBACK_COMPLETE` | Delete the failed stack in CloudFormation console, then redeploy |
| API returns 403 | Verify the Cognito token is valid and the user is in the correct group |
| CloudFront returns 403 | Check that the S3 bucket policy allows CloudFront OAI access |
