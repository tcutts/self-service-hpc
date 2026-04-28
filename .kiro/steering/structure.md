---
inclusion: auto
---

# Project Structure — Self-Service HPC Platform

A serverless, multi-tenant HPC platform built with CDK (TypeScript) and Lambda (Python).

## Entry Point

- `bin/self-service-hpc.ts` — CDK app entry point. Instantiates `HpcFoundationStack` and per-project `ProjectInfrastructureStack` stacks. Dynamic project stacks are created via the `PROJECT_ID` env var at synth time.

## CDK Stacks (`lib/`)

- `lib/foundation-stack.ts` — Shared control-plane: Cognito, API Gateway, DynamoDB tables, Lambda functions, Step Functions, SNS topics, CodeBuild, CloudFront web portal. Orchestrates all constructs and wires cross-references.
- `lib/project-infrastructure-stack.ts` — Per-project infrastructure: VPC, EFS, S3 bucket, security groups (head node, compute node, EFS, FSx), CloudWatch log group. Created dynamically by CodeBuild during project deployment.
- `lib/self-service-hpc-stack.ts` — Placeholder/future stack (currently unused).

## CDK Constructs (`lib/constructs/`)

Each construct encapsulates a focused domain. Tests mirror this layout in `test/constructs/`.

| Construct | File | Purpose |
|---|---|---|
| CognitoAuth | `cognito-auth.ts` | User Pool, User Pool Client, admin group |
| DatabaseTables | `database-tables.ts` | DynamoDB tables (PlatformUsers, Projects, ClusterTemplates, Clusters, ClusterNameRegistry) |
| ApiGateway | `api-gateway.ts` | REST API, Cognito authorizer, shared Lambda layer, all API routes |
| NotificationTopics | `notification-topics.ts` | SNS topics (budget, cluster lifecycle) |
| UserManagement | `user-management.ts` | User management Lambda + API routes |
| ProjectManagement | `project-management.ts` | Project management Lambda + API routes |
| TemplateManagement | `template-management.ts` | Cluster template Lambda + API routes |
| ClusterOperations | `cluster-operations.ts` | Cluster operations Lambda + Step Functions (create/destroy workflows) |
| CdkDeployProject | `cdk-deploy-project.ts` | CodeBuild project for CDK deploy/destroy |
| ProjectLifecycle | `project-lifecycle.ts` | Step Functions for project deploy/destroy/update |
| PlatformOperations | `platform-operations.ts` | Accounting, budget notification, FSx cleanup Lambdas |
| WebPortal | `web-portal.ts` | S3 bucket + CloudFront distribution for the frontend |

## Lambda Functions (`lambda/`)

All handlers are Python. Each subdirectory is a separate Lambda deployment unit. Modules within a directory are imported by the handler.

| Directory | Handler | API Routes |
|---|---|---|
| `lambda/user_management/` | `handler.py` | `GET/POST /users`, `GET/DELETE /users/{userId}`, `POST /users/{userId}/reactivate`, batch ops |
| `lambda/project_management/` | `handler.py` | `GET/POST /projects`, `GET/PUT/DELETE /projects/{projectId}`, members, budget, deploy/destroy/update, batch ops |
| `lambda/template_management/` | `handler.py` | `GET/POST /templates`, `GET/PUT/DELETE /templates/{templateId}`, `GET /templates/default-ami`, batch delete |
| `lambda/cluster_operations/` | `handler.py` | `GET/POST /projects/{projectId}/clusters`, `GET/DELETE /projects/{projectId}/clusters/{clusterName}`, recreate, force-fail |
| `lambda/accounting/` | `handler.py` | `GET /accounting/jobs` |
| `lambda/budget_notification/` | `handler.py` | SNS-triggered (not API Gateway) |
| `lambda/fsx_cleanup/` | `handler.py` | EventBridge-scheduled (not API Gateway) |
| `lambda/shared/` | `api_logging.py` | Shared Lambda layer: structured API action logging |

### Lambda module pattern

Each Lambda directory follows a consistent pattern:
- `handler.py` — API Gateway proxy event router, delegates to domain modules
- `auth.py` — Authorisation helpers (Cognito claims, role checks)
- `errors.py` — Domain-specific error classes and `build_error_response()`
- Domain modules (e.g. `clusters.py`, `projects.py`, `users.py`, `templates.py`) — business logic

## Frontend (`frontend/`)

Static SPA served via CloudFront/S3.

- `frontend/index.html` — Main HTML page
- `frontend/js/app.js` — Application logic (Cognito auth, API calls, UI rendering)
- `frontend/js/config.js` — Runtime configuration (Cognito IDs, API URL, polling intervals)
- `frontend/js/table-module.js` — Reusable table component with selection, bulk actions, sorting
- `frontend/css/styles.css` — Styles

## Tests

- `test/constructs/*.test.ts` — CDK construct unit tests (Jest, TypeScript). One test file per construct.
- `test/foundation-stack.test.ts` — Foundation stack integration test.
- `test/frontend/*.test.js` — Frontend unit tests (Jest, jsdom). Includes property-based tests (`*.property.test.js`) using fast-check.
- `test/lambda/**/*.py` — Lambda unit tests (pytest). Mirror the `lambda/` directory structure. Use moto for AWS service mocking.

## Documentation (`docs/`)

User-facing documentation organised by audience:
- `docs/admin/` — Platform administrator guides (deploying, user/project/template management)
- `docs/project-admin/` — Project administrator guides (cluster and project management)
- `docs/user/` — End-user guides (accessing clusters, data management)
- `docs/api/reference.md` — API reference

## Build & Deploy

- `Makefile` — Primary build/deploy interface (`make deploy`, `make test`, `make teardown`, `make purge`)
- `package.json` — Node dependencies and scripts (`npm run build`, `npm test`)
- `requirements.txt` — Python dependencies (boto3, moto, pytest, hypothesis)
- `pyproject.toml` — Pytest configuration
- `jest.config.js` — Jest config with two projects: `backend` (ts-node) and `frontend` (jsdom)
- `cdk.json` — CDK app config, context values (trustedCidrRanges)

## Scripts

- `scripts/teardown_workloads.py` — Destroys all clusters and project stacks (used by `make teardown`)

## Key Conventions

- DynamoDB keys use `PK`/`SK` pattern: `PROJECT#<id>`, `CLUSTER#<name>`, `USER#<id>`, `MEMBER#<id>`, `TEMPLATE#<id>`
- All Lambda handlers call `log_api_action()` from the shared layer for audit logging
- Error handling uses typed exceptions (`AuthorisationError`, `ValidationError`, etc.) mapped to HTTP status codes via `build_error_response()`
- Cluster lifecycle is managed via Step Functions state machines (creation and destruction workflows)
- Project infrastructure is deployed/destroyed via CodeBuild running `cdk deploy`/`cdk destroy`
