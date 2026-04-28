import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as path from 'path';
import { Construct } from 'constructs';

export interface PlatformOperationsProps {
  clustersTable: dynamodb.Table;
  projectsTable: dynamodb.Table;
  platformUsersTable: dynamodb.Table;
  userPool: cognito.UserPool;
  api: apigateway.RestApi;
  cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  sharedLayer: lambda.LayerVersion;
  budgetNotificationTopic: sns.Topic;
  clusterLifecycleNotificationTopic: sns.Topic;
  clusterCreationStateMachine: sfn.StateMachine;
  posixReconciliationLambda: lambda.Function;
}

/**
 * Encapsulates the supporting platform operations: accounting query,
 * budget notification handler, FSx cleanup scheduler, and cluster
 * creation failure detection.
 */
export class PlatformOperations extends Construct {
  /** Lambda function: Accounting Query. */
  public readonly accountingQueryLambda: lambda.Function;
  /** Lambda function: Budget Notification handler. */
  public readonly budgetNotificationLambda: lambda.Function;
  /** Lambda function: FSx Cleanup. */
  public readonly fsxCleanupLambda: lambda.Function;
  /** EventBridge rule: FSx Cleanup schedule. */
  public readonly fsxCleanupScheduleRule: events.Rule;
  /** EventBridge rule: POSIX Reconciliation daily schedule. */
  public readonly posixReconciliationScheduleRule: events.Rule;

  constructor(scope: Construct, id: string, props: PlatformOperationsProps) {
    super(scope, id);

    // ---------------------------------------------------------------
    // Accounting Query Lambda Function
    // ---------------------------------------------------------------
    this.accountingQueryLambda = new lambda.Function(this, 'AccountingQueryLambda', {
      functionName: 'hpc-accounting-query',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'accounting')),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        USER_POOL_ID: props.userPool.userPoolId,
      },
      description: 'Queries cross-cluster Slurm job accounting data via SSM Run Command on login nodes',
    });

    // Grant DynamoDB read access on Clusters and Projects tables
    props.clustersTable.grantReadData(this.accountingQueryLambda);
    props.projectsTable.grantReadData(this.accountingQueryLambda);

    // Grant SSM permissions for querying sacct on cluster login nodes
    this.accountingQueryLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ssm:SendCommand',
        'ssm:GetCommandInvocation',
      ],
      resources: ['*'],
    }));

    // Grant Cognito read for authorisation checks
    this.accountingQueryLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:AdminGetUser',
      ],
      resources: [props.userPool.userPoolArn],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Accounting Query Resources
    // ---------------------------------------------------------------
    const accountingResource = props.api.root.addResource('accounting');
    const accountingJobsResource = accountingResource.addResource('jobs');

    const accountingQueryIntegration = new apigateway.LambdaIntegration(this.accountingQueryLambda);

    const cognitoMethodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: props.cognitoAuthorizer,
    };

    // GET /accounting/jobs — query job records across clusters
    accountingJobsResource.addMethod('GET', accountingQueryIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // Budget Notification Lambda Function
    // ---------------------------------------------------------------
    this.budgetNotificationLambda = new lambda.Function(this, 'BudgetNotificationLambda', {
      functionName: 'hpc-budget-notification',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'budget_notification')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        USERS_TABLE_NAME: props.platformUsersTable.tableName,
      },
      description: 'Processes AWS Budgets SNS notifications and updates project budget breach status',
    });

    // Grant DynamoDB read/write on Projects table (to set budgetBreached flag)
    props.projectsTable.grantReadWriteData(this.budgetNotificationLambda);
    // Grant DynamoDB read on PlatformUsers table (to look up admin emails)
    props.platformUsersTable.grantReadData(this.budgetNotificationLambda);

    // Subscribe the Lambda to the budget notification SNS topic
    props.budgetNotificationTopic.addSubscription(
      new snsSubscriptions.LambdaSubscription(this.budgetNotificationLambda),
    );

    // ---------------------------------------------------------------
    // FSx Cleanup Lambda Function (Scheduled)
    // ---------------------------------------------------------------
    this.fsxCleanupLambda = new lambda.Function(this, 'FsxCleanupLambda', {
      functionName: 'hpc-fsx-cleanup',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'fsx_cleanup')),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        SNS_TOPIC_ARN: props.clusterLifecycleNotificationTopic.topicArn,
      },
      description: 'Detects and deletes orphaned FSx for Lustre filesystems on a schedule',
    });

    // Grant DynamoDB read-only access on Clusters table (no write permissions)
    props.clustersTable.grantReadData(this.fsxCleanupLambda);

    // Grant FSx permissions for describing and deleting filesystems and DRAs
    this.fsxCleanupLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'fsx:DescribeFileSystems',
        'fsx:DescribeDataRepositoryAssociations',
        'fsx:DeleteDataRepositoryAssociation',
        'fsx:DeleteFileSystem',
      ],
      resources: ['*'],
    }));

    // Grant SNS publish on the cluster lifecycle notification topic
    props.clusterLifecycleNotificationTopic.grantPublish(this.fsxCleanupLambda);

    // EventBridge rule to trigger FSx cleanup every 6 hours
    this.fsxCleanupScheduleRule = new events.Rule(this, 'FsxCleanupScheduleRule', {
      ruleName: 'hpc-fsx-cleanup-schedule',
      description: 'Triggers the FSx cleanup Lambda every 6 hours to remove orphaned filesystems',
      schedule: events.Schedule.rate(cdk.Duration.hours(6)),
    });

    this.fsxCleanupScheduleRule.addTarget(
      new eventsTargets.LambdaFunction(this.fsxCleanupLambda),
    );

    // ---------------------------------------------------------------
    // POSIX Reconciliation — Daily EventBridge Schedule
    // Triggers the reconciliation Lambda daily at 2 AM UTC to audit
    // POSIX user accounts on active clusters against project membership.
    // ---------------------------------------------------------------
    this.posixReconciliationScheduleRule = new events.Rule(this, 'PosixReconciliationScheduleRule', {
      ruleName: 'hpc-posix-reconciliation-schedule',
      description: 'Triggers the POSIX reconciliation Lambda daily at 2 AM UTC to audit cluster access',
      schedule: events.Schedule.expression('cron(0 2 * * ? *)'),
    });

    this.posixReconciliationScheduleRule.addTarget(
      new eventsTargets.LambdaFunction(props.posixReconciliationLambda),
    );

    // ---------------------------------------------------------------
    // Cluster Creation Failure Handler — EventBridge + Lambda
    // Detects timed-out, failed, or aborted Step Functions executions
    // for the cluster creation state machine and marks the cluster
    // record as FAILED in DynamoDB.
    // ---------------------------------------------------------------
    const clusterCreationFailureHandler = new lambda.Function(this, 'ClusterCreationFailureHandler', {
      functionName: 'hpc-cluster-creation-failure-handler',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'cluster_creation.mark_cluster_failed_from_event',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations')),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
      },
      description: 'Handles Step Functions execution termination events to mark stuck clusters as FAILED',
    });

    // Grant DynamoDB read/write on the Clusters table
    props.clustersTable.grantReadWriteData(clusterCreationFailureHandler);

    // Grant states:DescribeExecution on the cluster creation state machine
    // Execution ARNs use the format arn:aws:states:region:account:execution:smName:execName
    // so we need to construct the resource ARN from the state machine ARN.
    clusterCreationFailureHandler.addToRolePolicy(new iam.PolicyStatement({
      actions: ['states:DescribeExecution'],
      resources: [
        cdk.Arn.format({
          service: 'states',
          resource: 'execution',
          resourceName: `${props.clusterCreationStateMachine.stateMachineName}:*`,
          arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
        }, cdk.Stack.of(this)),
      ],
    }));

    // EventBridge rule matching Step Functions execution status changes
    // for the cluster creation state machine
    const clusterCreationFailureRule = new events.Rule(this, 'ClusterCreationFailureRule', {
      ruleName: 'hpc-cluster-creation-failure',
      description: 'Detects timed-out, failed, or aborted cluster creation executions',
      eventPattern: {
        source: ['aws.states'],
        detailType: ['Step Functions Execution Status Change'],
        detail: {
          stateMachineArn: [props.clusterCreationStateMachine.stateMachineArn],
          status: ['TIMED_OUT', 'FAILED', 'ABORTED'],
        },
      },
    });

    clusterCreationFailureRule.addTarget(
      new eventsTargets.LambdaFunction(clusterCreationFailureHandler),
    );
  }
}
