import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as path from 'path';
import { Construct } from 'constructs';

export interface ProjectManagementProps {
  projectsTable: dynamodb.Table;
  clustersTable: dynamodb.Table;
  platformUsersTable: dynamodb.Table;
  userPool: cognito.UserPool;
  api: apigateway.RestApi;
  cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  sharedLayer: lambda.LayerVersion;
  budgetNotificationTopic: sns.Topic;
}

/**
 * Encapsulates the project management Lambda function, IAM policies,
 * and all /projects API Gateway routes for the HPC platform.
 */
export class ProjectManagement extends Construct {
  /** The project management Lambda function. */
  public readonly lambda: lambda.Function;
  /** The {projectId} API Gateway resource, needed by ClusterOperations. */
  public readonly projectIdResource: apigateway.Resource;

  constructor(scope: Construct, id: string, props: ProjectManagementProps) {
    super(scope, id);

    this.lambda = new lambda.Function(this, 'ProjectManagementLambda', {
      functionName: 'hpc-project-management',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'project_management'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        USERS_TABLE_NAME: props.platformUsersTable.tableName,
        USER_POOL_ID: props.userPool.userPoolId,
        BUDGET_SNS_TOPIC_ARN: props.budgetNotificationTopic.topicArn,
      },
      description: 'Handles project CRUD, membership management, and budget configuration',
    });

    // Grant DynamoDB read/write on Projects table
    props.projectsTable.grantReadWriteData(this.lambda);
    // Grant DynamoDB read on Clusters table (for checking active clusters on deletion)
    props.clustersTable.grantReadData(this.lambda);
    // Grant DynamoDB read on PlatformUsers table (for validating user existence on membership add)
    props.platformUsersTable.grantReadData(this.lambda);

    // Grant Cognito admin actions for group management
    this.lambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminAddUserToGroup',
        'cognito-idp:AdminRemoveUserFromGroup',
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:CreateGroup',
        'cognito-idp:DeleteGroup',
        'cognito-idp:GetGroup',
        'cognito-idp:ListUsersInGroup',
      ],
      resources: [props.userPool.userPoolArn],
    }));

    // Grant AWS Budgets permissions for creating/updating project budgets
    this.lambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'budgets:CreateBudget',
        'budgets:ModifyBudget',
        'budgets:ViewBudget',
        'budgets:CreateNotification',
        'budgets:UpdateNotification',
        'budgets:DeleteNotification',
        'budgets:CreateSubscriber',
        'budgets:DeleteSubscriber',
      ],
      resources: ['*'],
    }));

    // Grant SNS publish for budget notification topic
    props.budgetNotificationTopic.grantPublish(this.lambda);

    // Grant STS get caller identity (required by the Budgets API for account ID)
    this.lambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sts:GetCallerIdentity'],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Project Management Resources
    // ---------------------------------------------------------------
    const projectsResource = props.api.root.addResource('projects');
    this.projectIdResource = projectsResource.addResource('{projectId}');
    const membersResource = this.projectIdResource.addResource('members');
    const memberUserIdResource = membersResource.addResource('{userId}');
    const budgetResource = this.projectIdResource.addResource('budget');

    const projectManagementIntegration = new apigateway.LambdaIntegration(this.lambda);

    const cognitoMethodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: props.cognitoAuthorizer,
    };

    // POST /projects — create project
    projectsResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // GET /projects — list projects
    projectsResource.addMethod('GET', projectManagementIntegration, cognitoMethodOptions);
    // GET /projects/{projectId} — get project details
    this.projectIdResource.addMethod('GET', projectManagementIntegration, cognitoMethodOptions);
    // DELETE /projects/{projectId} — delete project
    this.projectIdResource.addMethod('DELETE', projectManagementIntegration, cognitoMethodOptions);
    // GET /projects/{projectId}/members — list members
    membersResource.addMethod('GET', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/members — add member
    membersResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // PUT /projects/{projectId}/members/{userId} — change member role
    memberUserIdResource.addMethod('PUT', projectManagementIntegration, cognitoMethodOptions);
    // DELETE /projects/{projectId}/members/{userId} — remove member
    memberUserIdResource.addMethod('DELETE', projectManagementIntegration, cognitoMethodOptions);
    // PUT /projects/{projectId}/budget — set budget
    budgetResource.addMethod('PUT', projectManagementIntegration, cognitoMethodOptions);
    // PUT /projects/{projectId} — edit project (budget only)
    this.projectIdResource.addMethod('PUT', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/deactivate — deactivate project
    const deactivateResource = this.projectIdResource.addResource('deactivate');
    deactivateResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/reactivate — reactivate project
    const reactivateResource = this.projectIdResource.addResource('reactivate');
    reactivateResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/deploy — deploy project infrastructure
    const deployResource = this.projectIdResource.addResource('deploy');
    deployResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/destroy — destroy project infrastructure
    const destroyResource = this.projectIdResource.addResource('destroy');
    destroyResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/update — update project infrastructure
    const updateResource = this.projectIdResource.addResource('update');
    updateResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);

    // Batch project operations
    const projectsBatchResource = projectsResource.addResource('batch');
    // POST /projects/batch/update — bulk update projects
    const projectsBatchUpdateResource = projectsBatchResource.addResource('update');
    projectsBatchUpdateResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/batch/deploy — bulk deploy projects
    const projectsBatchDeployResource = projectsBatchResource.addResource('deploy');
    projectsBatchDeployResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
    // POST /projects/batch/destroy — bulk destroy projects
    const projectsBatchDestroyResource = projectsBatchResource.addResource('destroy');
    projectsBatchDestroyResource.addMethod('POST', projectManagementIntegration, cognitoMethodOptions);
  }
}
