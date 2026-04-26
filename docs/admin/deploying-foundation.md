# Deploying the Foundation Infrastructure

This guide walks administrators through deploying the Self-Service HPC Platform foundation stack, which provisions all shared control-plane resources.

## Prerequisites

Before deploying, ensure the following are installed and configured:

- **Node.js** (v18 or later) and **npm**
- **Python 3.13** and **pip**
- **AWS CDK CLI** (`npm install -g aws-cdk`)
- **AWS CLI** configured with the `thecutts` profile
- **Git** for cloning the repository

Verify your AWS credentials:

```bash
aws sts get-caller-identity --profile thecutts
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
npx cdk bootstrap --profile thecutts
```

### 5. Deploy the Foundation Stack

Use the Makefile for a single-command deployment:

```bash
make deploy
```

Or deploy manually:

```bash
npx cdk deploy HpcFoundationStack --require-approval never --profile thecutts
```

The deployment takes approximately 5–10 minutes. CDK outputs the following values on completion:

- **API Gateway URL** — the base URL for all platform API calls
- **Cognito User Pool ID** — used for authentication configuration
- **Cognito User Pool Client ID** — used by the web portal
- **CloudFront Distribution URL** — the URL for the web portal and documentation

### 6. Verify the Deployment

Check that the API is reachable:

```bash
curl -s https://<api-gateway-url>/prod/health
```

Access the web portal at the CloudFront distribution URL.

Access the documentation at `https://<cloudfront-url>/docs/index.html`.

## Post-Deployment Configuration

### Create the First Administrator

After deployment, create an initial administrator user through the AWS Console:

1. Open the **Amazon Cognito** console.
2. Navigate to the `hpc-platform-users` User Pool.
3. Create a user with a valid email address.
4. Add the user to the `Administrators` group.

Alternatively, use the AWS CLI:

```bash
# Create the user
aws cognito-idp admin-create-user \
  --user-pool-id <user-pool-id> \
  --username admin@example.com \
  --user-attributes Name=email,Value=admin@example.com \
  --profile thecutts

# Add to Administrators group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <user-pool-id> \
  --username admin@example.com \
  --group-name Administrators \
  --profile thecutts
```

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
| `CDK bootstrap required` | Run `npx cdk bootstrap --profile thecutts` |
| `Stack is in ROLLBACK_COMPLETE` | Delete the failed stack in CloudFormation console, then redeploy |
| API returns 403 | Verify the Cognito token is valid and the user is in the correct group |
| CloudFront returns 403 | Check that the S3 bucket policy allows CloudFront OAI access |
