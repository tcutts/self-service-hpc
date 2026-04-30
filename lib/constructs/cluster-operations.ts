import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as path from 'path';
import { Construct } from 'constructs';

export interface ClusterOperationsProps {
  clustersTable: dynamodb.Table;
  projectsTable: dynamodb.Table;
  clusterNameRegistryTable: dynamodb.Table;
  platformUsersTable: dynamodb.Table;
  clusterTemplatesTable: dynamodb.Table;
  userPool: cognito.UserPool;
  cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  sharedLayer: lambda.LayerVersion;
  clusterLifecycleNotificationTopic: sns.Topic;
  projectIdResource: apigateway.Resource;
}

/**
 * Encapsulates the cluster operations Lambda, cluster creation/destruction
 * step Lambdas, both state machines, all associated IAM policies, and the
 * cluster API routes for the HPC platform.
 */
export class ClusterOperations extends Construct {
  /** The cluster operations Lambda function. */
  public readonly clusterOperationsLambda: lambda.Function;
  /** The POSIX reconciliation Lambda function. */
  public readonly posixReconciliationLambda: lambda.Function;
  /** The login node refresh Lambda function. */
  public readonly loginNodeRefreshLambda: lambda.Function;
  /** The login node event handler Lambda function. */
  public readonly loginNodeEventLambda: lambda.Function;
  /** The cluster creation state machine. */
  public readonly clusterCreationStateMachine: sfn.StateMachine;
  /** The cluster destruction state machine. */
  public readonly clusterDestructionStateMachine: sfn.StateMachine;

  constructor(scope: Construct, id: string, props: ClusterOperationsProps) {
    super(scope, id);

    // ---------------------------------------------------------------
    // Cluster Operations Lambda Function
    // ---------------------------------------------------------------
    this.clusterOperationsLambda = new lambda.Function(this, 'ClusterOperationsLambda', {
      functionName: 'hpc-cluster-operations',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        CLUSTER_NAME_REGISTRY_TABLE_NAME: props.clusterNameRegistryTable.tableName,
        USERS_TABLE_NAME: props.platformUsersTable.tableName,
        CREATION_STATE_MACHINE_ARN: '', // set after state machine creation by orchestrator
        DESTRUCTION_STATE_MACHINE_ARN: '', // set after state machine creation by orchestrator
        CLUSTER_LIFECYCLE_SNS_TOPIC_ARN: props.clusterLifecycleNotificationTopic.topicArn,
        USER_POOL_ID: props.userPool.userPoolId,
      },
      description: 'Handles cluster CRUD operations and orchestrates creation/destruction via Step Functions',
    });

    // Grant DynamoDB read/write on Clusters, Projects, ClusterNameRegistry, and PlatformUsers tables
    props.clustersTable.grantReadWriteData(this.clusterOperationsLambda);
    props.projectsTable.grantReadData(this.clusterOperationsLambda);
    props.clusterNameRegistryTable.grantReadWriteData(this.clusterOperationsLambda);
    props.platformUsersTable.grantReadData(this.clusterOperationsLambda);

    // Grant SNS publish for cluster lifecycle notifications
    props.clusterLifecycleNotificationTopic.grantPublish(this.clusterOperationsLambda);

    // Grant Cognito read for authorisation checks
    this.clusterOperationsLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:AdminGetUser',
      ],
      resources: [props.userPool.userPoolArn],
    }));

    // ---------------------------------------------------------------
    // Step Functions — Cluster Creation Step Lambda
    // ---------------------------------------------------------------
    const clusterCreationStepLambda = new lambda.Function(this, 'ClusterCreationStepLambda', {
      functionName: 'hpc-cluster-creation-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'cluster_creation.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        CLUSTER_NAME_REGISTRY_TABLE_NAME: props.clusterNameRegistryTable.tableName,
        USERS_TABLE_NAME: props.platformUsersTable.tableName,
        CLUSTER_LIFECYCLE_SNS_TOPIC_ARN: props.clusterLifecycleNotificationTopic.topicArn,
        TEMPLATES_TABLE_NAME: props.clusterTemplatesTable.tableName,
      },
      description: 'Executes individual steps of the cluster creation workflow',
    });

    // Grant creation step Lambda broad permissions for PCS, FSx, DynamoDB, SNS, tagging
    props.clustersTable.grantReadWriteData(clusterCreationStepLambda);
    props.projectsTable.grantReadData(clusterCreationStepLambda);
    props.clusterNameRegistryTable.grantReadWriteData(clusterCreationStepLambda);
    props.platformUsersTable.grantReadData(clusterCreationStepLambda);
    props.clusterTemplatesTable.grantReadData(clusterCreationStepLambda);
    props.clusterLifecycleNotificationTopic.grantPublish(clusterCreationStepLambda);

    // SNS subscribe permission for lifecycle notifications
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sns:Subscribe'],
      resources: [props.clusterLifecycleNotificationTopic.topicArn],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'pcs:GetCluster',
        'pcs:CreateCluster',
        'pcs:CreateComputeNodeGroup',
        'pcs:CreateQueue',
        'pcs:DescribeCluster',
        'pcs:DescribeComputeNodeGroup',
        'pcs:DescribeQueue',
        'pcs:GetComputeNodeGroup',
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:TagResource',
        'pcs:ListComputeNodeGroupInstances',
      ],
      resources: ['*'],
    }));

    // PCS CreateCluster creates a Secrets Manager secret for the Slurm auth
    // key using the caller's credentials, so the Lambda needs these permissions.
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'secretsmanager:CreateSecret',
        'secretsmanager:TagResource',
      ],
      resources: ['arn:aws:secretsmanager:*:*:secret:pcs!slurm-secret-*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateFileSystem',
        'fsx:DescribeFileSystems',
        'fsx:DeleteFileSystem',
        'fsx:TagResource',
        'fsx:CreateDataRepositoryAssociation',
        'fsx:DescribeDataRepositoryAssociations',
      ],
      resources: ['*'],
    }));

    // S3 permissions required by FSx
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        's3:Get*',
        's3:List*',
        's3:PutObject',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:DescribeSubnets',
        'ec2:DescribeSecurityGroups',
        'ec2:GetSecurityGroupsForVpc',
        'ec2:DescribeVpcs',
        'ec2:CreateNetworkInterface',
        'ec2:DescribeNetworkInterfaces',
        'ec2:CreateTags',
        'ec2:CreateLaunchTemplate',
        'ec2:DeleteLaunchTemplate',
        'ec2:DescribeLaunchTemplates',
        'ec2:DescribeLaunchTemplateVersions',
        'ec2:DescribeInstanceTypes',
        'ec2:DescribeInstanceTypeOfferings',
        'ec2:DescribeImages',
        'ec2:DescribeInstances',
        'ec2:RunInstances',
        'ec2:CreateFleet',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'tag:TagResources',
        'tag:UntagResources',
      ],
      resources: ['*'],
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'iam:PassRole',
        'iam:GetRole',
      ],
      resources: ['*'],
    }));

    // PCS and Spot service-linked roles
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:CreateServiceLinkedRole'],
      resources: [
        'arn:aws:iam::*:role/aws-service-role/pcs.amazonaws.com/AWSServiceRoleFor*',
        'arn:aws:iam::*:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleFor*',
      ],
      conditions: {
        'StringLike': {
          'iam:AWSServiceName': [
            'pcs.amazonaws.com',
            'spot.amazonaws.com',
          ],
        },
      },
    }));

    // FSx for Lustre service-linked roles
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:CreateServiceLinkedRole'],
      resources: ['arn:aws:iam::*:role/aws-service-role/fsx.amazonaws.com/*'],
      conditions: {
        'StringLike': {
          'iam:AWSServiceName': 'fsx.amazonaws.com',
        },
      },
    }));

    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'iam:CreateServiceLinkedRole',
        'iam:AttachRolePolicy',
        'iam:PutRolePolicy',
      ],
      resources: ['arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*'],
      conditions: {
        'StringLike': {
          'iam:AWSServiceName': 's3.data-source.lustre.fsx.amazonaws.com',
        },
      },
    }));

    // IAM management permissions for per-cluster instance profile lifecycle
    clusterCreationStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'iam:CreateRole',
        'iam:DeleteRole',
        'iam:TagRole',
        'iam:UntagRole',
        'iam:AttachRolePolicy',
        'iam:DetachRolePolicy',
        'iam:PutRolePolicy',
        'iam:DeleteRolePolicy',
        'iam:CreateInstanceProfile',
        'iam:DeleteInstanceProfile',
        'iam:TagInstanceProfile',
        'iam:AddRoleToInstanceProfile',
        'iam:RemoveRoleFromInstanceProfile',
        'iam:PassRole',
        'iam:GetInstanceProfile',
      ],
      resources: [
        'arn:aws:iam::*:role/AWSPCS-*',
        'arn:aws:iam::*:instance-profile/AWSPCS-*',
      ],
    }));

    // ---------------------------------------------------------------
    // Step Functions — Cluster Destruction Step Lambda
    // ---------------------------------------------------------------
    const clusterDestructionStepLambda = new lambda.Function(this, 'ClusterDestructionStepLambda', {
      functionName: 'hpc-cluster-destruction-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'cluster_destruction.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        CLUSTER_NAME_REGISTRY_TABLE_NAME: props.clusterNameRegistryTable.tableName,
      },
      description: 'Executes individual steps of the cluster destruction workflow',
    });

    // Grant destruction step Lambda permissions
    props.clustersTable.grantReadWriteData(clusterDestructionStepLambda);
    props.clusterNameRegistryTable.grantReadWriteData(clusterDestructionStepLambda);

    clusterDestructionStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:DescribeCluster',
        'pcs:GetComputeNodeGroup',
        'pcs:GetQueue',
      ],
      resources: ['*'],
    }));

    clusterDestructionStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateDataRepositoryTask',
        'fsx:DescribeDataRepositoryTasks',
        'fsx:DeleteFileSystem',
        'fsx:DescribeFileSystems',
      ],
      resources: ['*'],
    }));

    // EC2 launch template cleanup permissions
    clusterDestructionStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:DeleteLaunchTemplate',
        'ec2:DescribeLaunchTemplates',
      ],
      resources: ['*'],
    }));

    // IAM management permissions for per-cluster instance profile cleanup
    clusterDestructionStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'iam:CreateRole',
        'iam:DeleteRole',
        'iam:TagRole',
        'iam:UntagRole',
        'iam:AttachRolePolicy',
        'iam:DetachRolePolicy',
        'iam:PutRolePolicy',
        'iam:DeleteRolePolicy',
        'iam:CreateInstanceProfile',
        'iam:DeleteInstanceProfile',
        'iam:AddRoleToInstanceProfile',
        'iam:RemoveRoleFromInstanceProfile',
        'iam:PassRole',
        'iam:GetInstanceProfile',
      ],
      resources: [
        'arn:aws:iam::*:role/AWSPCS-*',
        'arn:aws:iam::*:instance-profile/AWSPCS-*',
      ],
    }));

    // ---------------------------------------------------------------
    // Cluster Creation State Machine Definition
    // ---------------------------------------------------------------

    // Step 1: Validate and register cluster name
    const validateAndRegisterName = new tasks.LambdaInvoke(this, 'ValidateAndRegisterName', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'validate_and_register_name',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Check budget breach
    const checkBudgetBreach = new tasks.LambdaInvoke(this, 'CheckBudgetBreach', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_budget_breach',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2b: Resolve template fields from ClusterTemplates table
    const resolveTemplate = new tasks.LambdaInvoke(this, 'ResolveTemplate', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'resolve_template',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2c: Create per-cluster IAM resources (roles + instance profiles)
    const createIamResources = new tasks.LambdaInvoke(this, 'CreateIamResources', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_iam_resources',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2d: Wait for instance profiles to propagate
    const waitForInstanceProfiles = new tasks.LambdaInvoke(this, 'WaitForInstanceProfiles', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'wait_for_instance_profiles',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForInstanceProfilesPropagation = new sfn.Wait(this, 'WaitForInstanceProfilesPropagation', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(10)),
    });

    // Step 2e: Create launch templates for login and compute nodes
    const createLaunchTemplates = new tasks.LambdaInvoke(this, 'CreateLaunchTemplates', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_launch_templates',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 3: Create FSx filesystem
    const createFsxFilesystem = new tasks.LambdaInvoke(this, 'CreateFsxFilesystem', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_fsx_filesystem',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 4: Check FSx status (with wait loop)
    const checkFsxStatus = new tasks.LambdaInvoke(this, 'CheckFsxStatus', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_fsx_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForFsx = new sfn.Wait(this, 'WaitForFsx', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 4b: Create Data Repository Association (after FSx is available)
    const createFsxDra = new tasks.LambdaInvoke(this, 'CreateFsxDra', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_fsx_dra',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Create PCS cluster
    const createPcsCluster = new tasks.LambdaInvoke(this, 'CreatePcsCluster', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_pcs_cluster',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5b: Check PCS cluster status (with wait loop)
    const checkPcsClusterStatus = new tasks.LambdaInvoke(this, 'CheckPcsClusterStatus', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_pcs_cluster_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForPcsCluster = new sfn.Wait(this, 'WaitForPcsCluster', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 6: Create login node group
    const createLoginNodeGroup = new tasks.LambdaInvoke(this, 'CreateLoginNodeGroup', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_login_node_group',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 7: Create compute node group
    const createComputeNodeGroup = new tasks.LambdaInvoke(this, 'CreateComputeNodeGroup', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_compute_node_group',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 7b: Check node group statuses (with wait loop)
    const checkNodeGroupsStatus = new tasks.LambdaInvoke(this, 'CheckNodeGroupsStatus', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_node_groups_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForNodeGroups = new sfn.Wait(this, 'WaitForNodeGroups', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 7c: Resolve login node details (IP + instance ID)
    const resolveLoginNodeDetails = new tasks.LambdaInvoke(this, 'ResolveLoginNodeDetails', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'resolve_login_node_details',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 8: Create PCS queue
    const createPcsQueue = new tasks.LambdaInvoke(this, 'CreatePcsQueue', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_pcs_queue',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 9: Tag resources
    const tagResources = new tasks.LambdaInvoke(this, 'TagResources', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'tag_resources',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 10: Record cluster in DynamoDB
    const recordCluster = new tasks.LambdaInvoke(this, 'RecordCluster', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_cluster',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Rollback handler on failure
    const handleCreationFailure = new tasks.LambdaInvoke(this, 'HandleCreationFailure', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_creation_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const creationFailed = new sfn.Fail(this, 'CreationFailed', {
      cause: 'Cluster creation failed',
      error: 'ClusterCreationError',
    });

    // Last-resort DynamoDB UpdateItem task: when the rollback handler
    // itself fails, this direct SDK call marks the cluster as FAILED
    // so the record never stays stuck in CREATING.
    const markClusterFailed = new tasks.CallAwsService(this, 'MarkClusterFailed', {
      service: 'dynamodb',
      action: 'updateItem',
      parameters: {
        TableName: props.clustersTable.tableName,
        Key: {
          PK: { S: sfn.JsonPath.format('PROJECT#{}', sfn.JsonPath.stringAt('$.projectId')) },
          SK: { S: sfn.JsonPath.format('CLUSTER#{}', sfn.JsonPath.stringAt('$.clusterName')) },
        },
        UpdateExpression: 'SET #s = :status, #err = :errorMsg, #ua = :updatedAt',
        ExpressionAttributeNames: {
          '#s': 'status',
          '#err': 'errorMessage',
          '#ua': 'updatedAt',
        },
        ExpressionAttributeValues: {
          ':status': { S: 'FAILED' },
          ':errorMsg': { S: 'Cluster creation failed \u2014 rollback handler encountered an error' },
          ':updatedAt': { S: sfn.JsonPath.stringAt('$.State.EnteredTime') },
        },
      },
      iamResources: [props.clustersTable.tableArn],
      resultPath: '$.markFailedResult',
    });

    // If the MarkClusterFailed SDK call itself fails, proceed to the Fail state anyway
    markClusterFailed.addCatch(creationFailed, { resultPath: '$.error' });
    markClusterFailed.next(creationFailed);

    const creationSuccess = new sfn.Succeed(this, 'CreationSucceeded');

    // Add catch to all steps for rollback
    const catchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const failureChain = handleCreationFailure.next(creationFailed);

    // If the rollback handler itself fails, route through MarkClusterFailed
    handleCreationFailure.addCatch(markClusterFailed, { resultPath: '$.error' });

    validateAndRegisterName.addCatch(failureChain, catchConfig);
    checkBudgetBreach.addCatch(failureChain, catchConfig);
    resolveTemplate.addCatch(failureChain, catchConfig);
    createIamResources.addCatch(failureChain, catchConfig);
    createLoginNodeGroup.addCatch(failureChain, catchConfig);
    createComputeNodeGroup.addCatch(failureChain, catchConfig);
    checkNodeGroupsStatus.addCatch(failureChain, catchConfig);
    resolveLoginNodeDetails.addCatch(failureChain, catchConfig);
    createPcsQueue.addCatch(failureChain, catchConfig);
    tagResources.addCatch(failureChain, catchConfig);
    recordCluster.addCatch(failureChain, catchConfig);

    // FSx wait loop: check status → if not available, wait → check again
    const fsxWaitLoop = waitForFsx.next(checkFsxStatus);
    const isFsxAvailable = new sfn.Choice(this, 'IsFsxAvailable')
      .when(sfn.Condition.booleanEquals('$.fsxAvailable', true), createFsxDra)
      .otherwise(fsxWaitLoop);

    // PCS cluster wait loop: check status → if not active, wait → check again
    const pcsWaitLoop = waitForPcsCluster.next(checkPcsClusterStatus);
    const isPcsClusterActive = new sfn.Choice(this, 'IsPcsClusterActive')
      .when(sfn.Condition.booleanEquals('$.pcsClusterActive', true), new sfn.Pass(this, 'PcsClusterReady'))
      .otherwise(pcsWaitLoop);

    // Node group wait loop: check status → if not active, wait → check again
    const nodeGroupWaitLoop = waitForNodeGroups.next(checkNodeGroupsStatus);
    const areNodeGroupsActive = new sfn.Choice(this, 'AreNodeGroupsActive')
      .when(sfn.Condition.booleanEquals('$.nodeGroupsActive', true), resolveLoginNodeDetails)
      .otherwise(nodeGroupWaitLoop);

    // --- Storage branch: choice between lustre (FSx) and mountpoint (S3 IAM) ---

    const configureMountpointS3Iam = new tasks.LambdaInvoke(this, 'ConfigureMountpointS3Iam', {
      lambdaFunction: clusterCreationStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'configure_mountpoint_s3_iam',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Catch handlers for states inside the Parallel are not needed —
    // the Parallel state's own addCatch covers errors from all branches.

    // Lustre storage sub-branch: create FSx → wait for available → create DRA
    const lustreStorageBranch = createFsxFilesystem
      .next(checkFsxStatus)
      .next(isFsxAvailable);

    // Storage mode choice within the storage branch of the Parallel state
    const storageModeChoice = new sfn.Choice(this, 'StorageModeChoice')
      .when(sfn.Condition.stringEquals('$.storageMode', 'lustre'), lustreStorageBranch)
      .otherwise(configureMountpointS3Iam);

    // PCS branch: create cluster → wait for active
    const pcsBranch = createPcsCluster
      .next(checkPcsClusterStatus)
      .next(isPcsClusterActive);

    // Instance profile + launch template branch: wait for propagation → create templates
    const instanceProfileWaitLoop = waitForInstanceProfilesPropagation.next(waitForInstanceProfiles);
    const areInstanceProfilesReady = new sfn.Choice(this, 'AreInstanceProfilesReady')
      .when(sfn.Condition.booleanEquals('$.instanceProfilesReady', true), createLaunchTemplates)
      .otherwise(instanceProfileWaitLoop);

    const launchTemplateBranch = waitForInstanceProfiles
      .next(areInstanceProfilesReady);

    // --- Parallel execution: storage, PCS cluster, and launch templates all run concurrently ---

    const parallelProvision = new sfn.Parallel(this, 'ParallelProvision', {
      comment: 'Provision storage, create PCS cluster, and prepare launch templates in parallel',
      resultSelector: {
        // Branch 0: storage
        'projectId.$': '$[0].projectId',
        'clusterName.$': '$[0].clusterName',
        'templateId.$': '$[0].templateId',
        'createdBy.$': '$[0].createdBy',
        'vpcId.$': '$[0].vpcId',
        'efsFileSystemId.$': '$[0].efsFileSystemId',
        's3BucketName.$': '$[0].s3BucketName',
        'publicSubnetIds.$': '$[0].publicSubnetIds',
        'privateSubnetIds.$': '$[0].privateSubnetIds',
        'securityGroupIds.$': '$[0].securityGroupIds',
        'fsxFilesystemId.$': '$[0].fsxFilesystemId',
        'fsxDnsName.$': '$[0].fsxDnsName',
        'fsxMountName.$': '$[0].fsxMountName',
        'fsxDraId.$': '$[0].fsxDraId',
        'storageMode.$': '$[0].storageMode',
        'lustreCapacityGiB.$': '$[0].lustreCapacityGiB',
        // Branch 1: PCS cluster
        'pcsClusterId.$': '$[1].pcsClusterId',
        'pcsClusterArn.$': '$[1].pcsClusterArn',
        // Template-driven fields (from any branch — all start with same payload)
        'loginInstanceType.$': '$[0].loginInstanceType',
        'instanceTypes.$': '$[0].instanceTypes',
        'maxNodes.$': '$[0].maxNodes',
        'minNodes.$': '$[0].minNodes',
        'purchaseOption.$': '$[0].purchaseOption',
        // Branch 2: instance profiles + launch templates
        'loginInstanceProfileArn.$': '$[2].loginInstanceProfileArn',
        'computeInstanceProfileArn.$': '$[2].computeInstanceProfileArn',
        'loginLaunchTemplateId.$': '$[2].loginLaunchTemplateId',
        'computeLaunchTemplateId.$': '$[2].computeLaunchTemplateId',
        // AMI IDs (needed by node group creation as fallback)
        'amiId.$': '$[0].amiId',
        'loginAmiId.$': '$[0].loginAmiId',
      },
      resultPath: '$',
    });

    parallelProvision.branch(storageModeChoice, pcsBranch, launchTemplateBranch);
    parallelProvision.addCatch(failureChain, catchConfig);

    // Chain: validate → budget → resolve template → create IAM → parallel(storage + PCS + launch templates)
    //   → login nodes → compute → queue → tag → record → success
    const creationDefinition = validateAndRegisterName
      .next(checkBudgetBreach)
      .next(resolveTemplate)
      .next(createIamResources)
      .next(parallelProvision);

    // Post-parallel chain: all branches converge here
    const postBranchChain = createLoginNodeGroup
      .next(createComputeNodeGroup)
      .next(checkNodeGroupsStatus)
      .next(areNodeGroupsActive);

    // Continue after node groups are active: resolve login node → queue → tag → record → success
    resolveLoginNodeDetails.next(createPcsQueue);

    createPcsQueue
      .next(tagResources)
      .next(recordCluster)
      .next(creationSuccess);

    parallelProvision.next(createLoginNodeGroup);

    this.clusterCreationStateMachine = new sfn.StateMachine(this, 'ClusterCreationStateMachine', {
      stateMachineName: 'hpc-cluster-creation',
      definitionBody: sfn.DefinitionBody.fromChainable(creationDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // ---------------------------------------------------------------
    // Cluster Destruction State Machine Definition
    // ---------------------------------------------------------------

    // Step 1: Create FSx export task
    const createFsxExportTask = new tasks.LambdaInvoke(this, 'CreateFsxExportTask', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'create_fsx_export_task',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 2: Check FSx export status (with wait loop)
    const checkFsxExportStatus = new tasks.LambdaInvoke(this, 'CheckFsxExportStatus', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_fsx_export_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForExport = new sfn.Wait(this, 'WaitForExport', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(60)),
    });

    // Step 3: Delete PCS resources (initiate sub-resource deletions)
    const deletePcsResources = new tasks.LambdaInvoke(this, 'DeletePcsResources', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_pcs_resources',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 3b: Check PCS sub-resource deletion status (with wait loop)
    const checkPcsDeletionStatus = new tasks.LambdaInvoke(this, 'CheckPcsDeletionStatus', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_pcs_deletion_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForPcsDeletion = new sfn.Wait(this, 'WaitForPcsDeletion', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Step 3c: Delete PCS cluster (after sub-resources confirmed deleted)
    const deletePcsCluster = new tasks.LambdaInvoke(this, 'DeletePcsCluster', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_pcs_cluster',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 4: Delete FSx filesystem
    const deleteFsxFilesystem = new tasks.LambdaInvoke(this, 'DeleteFsxFilesystem', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_fsx_filesystem',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5: Record cluster as destroyed
    const recordClusterDestroyed = new tasks.LambdaInvoke(this, 'RecordClusterDestroyed', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_cluster_destroyed',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const destructionSuccess = new sfn.Succeed(this, 'DestructionSucceeded');

    // Step 5b: Delete per-cluster IAM resources (roles + instance profiles)
    const deleteIamResources = new tasks.LambdaInvoke(this, 'DeleteIamResources', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_iam_resources',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step 5c: Delete per-cluster launch templates
    const deleteLaunchTemplates = new tasks.LambdaInvoke(this, 'DeleteLaunchTemplates', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'delete_launch_templates',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Step: Remove Mountpoint S3 inline policy (mountpoint clusters only)
    const removeMountpointS3Policy = new tasks.LambdaInvoke(this, 'RemoveMountpointS3Policy', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'remove_mountpoint_s3_policy',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$.removePolicyResult',
    });

    // Step: Deregister cluster name from ClusterNameRegistry
    const deregisterClusterName = new tasks.LambdaInvoke(this, 'DeregisterClusterName', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'deregister_cluster_name',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler: record DESTRUCTION_FAILED status in DynamoDB before failing
    const recordClusterDestructionFailed = new tasks.LambdaInvoke(this, 'RecordClusterDestructionFailed', {
      lambdaFunction: clusterDestructionStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'record_cluster_destruction_failed',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$.destructionFailedResult',
    });

    // Terminal fail state for destruction errors
    const destructionFailed = new sfn.Fail(this, 'DestructionFailed', {
      cause: 'Cluster destruction failed',
      error: 'ClusterDestructionError',
    });

    // Route failure handler to the terminal fail state
    recordClusterDestructionFailed.next(destructionFailed);

    // If the failure handler itself fails, proceed to the fail state anyway
    recordClusterDestructionFailed.addCatch(destructionFailed, { resultPath: '$.error' });

    // Storage mode choice for destruction: remove S3 policy before IAM cleanup for mountpoint clusters
    const storageModeDestroyChoice = new sfn.Choice(this, 'StorageModeDestroyChoice')
      .when(sfn.Condition.stringEquals('$.storageMode', 'mountpoint'), removeMountpointS3Policy)
      .otherwise(deleteIamResources);

    // Error catching — all failure paths route through RecordClusterDestructionFailed
    const destructionCatchConfig: sfn.CatchProps = { resultPath: '$.error' };

    // Catch errors on FSx export steps
    createFsxExportTask.addCatch(recordClusterDestructionFailed, destructionCatchConfig);
    checkFsxExportStatus.addCatch(recordClusterDestructionFailed, destructionCatchConfig);

    // Catch errors on PCS deletion steps
    deletePcsResources.addCatch(recordClusterDestructionFailed, destructionCatchConfig);
    checkPcsDeletionStatus.addCatch(recordClusterDestructionFailed, destructionCatchConfig);
    deletePcsCluster.addCatch(recordClusterDestructionFailed, destructionCatchConfig);

    // Export wait loop: check status → if not complete, wait → check again
    const exportWaitLoop = waitForExport.next(checkFsxExportStatus);
    const isExportComplete = new sfn.Choice(this, 'IsExportComplete')
      .when(sfn.Condition.booleanEquals('$.exportComplete', true), deletePcsResources)
      .otherwise(exportWaitLoop);

    // PCS deletion wait loop: check status → if not all deleted, wait → check again
    const pcsDeletionWaitLoop = waitForPcsDeletion.next(checkPcsDeletionStatus);
    const arePcsSubResourcesDeleted = new sfn.Choice(this, 'ArePcsSubResourcesDeleted')
      .when(sfn.Condition.booleanEquals('$.pcsSubResourcesDeleted', true), deletePcsCluster)
      .otherwise(pcsDeletionWaitLoop);

    // Chain: steps 1-2 → export wait loop → steps 3-5 → success
    const destructionDefinition = createFsxExportTask
      .next(checkFsxExportStatus)
      .next(isExportComplete);

    // Post-export chain: DeletePcsResources → CheckPcsDeletionStatus → wait loop → DeletePcsCluster → FSx → StorageMode → IAM → LaunchTemplates → DeregisterClusterName → Record → success
    deletePcsResources
      .next(checkPcsDeletionStatus)
      .next(arePcsSubResourcesDeleted);

    deletePcsCluster
      .next(deleteFsxFilesystem)
      .next(storageModeDestroyChoice);

    // Both branches converge at deleteIamResources
    removeMountpointS3Policy.next(deleteIamResources);
    deleteIamResources
      .next(deleteLaunchTemplates)
      .next(deregisterClusterName)
      .next(recordClusterDestroyed)
      .next(destructionSuccess);

    this.clusterDestructionStateMachine = new sfn.StateMachine(this, 'ClusterDestructionStateMachine', {
      stateMachineName: 'hpc-cluster-destruction',
      definitionBody: sfn.DefinitionBody.fromChainable(destructionDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // Grant Step Functions execution roles permissions for PCS, FSx, EC2, tagging, DynamoDB
    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'pcs:CreateCluster',
        'pcs:CreateComputeNodeGroup',
        'pcs:CreateQueue',
        'pcs:DescribeCluster',
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:TagResource',
      ],
      resources: ['*'],
    }));

    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateFileSystem',
        'fsx:DescribeFileSystems',
        'fsx:DeleteFileSystem',
        'fsx:TagResource',
        'fsx:CreateDataRepositoryAssociation',
        'fsx:DescribeDataRepositoryAssociations',
      ],
      resources: ['*'],
    }));

    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'ec2:DescribeSubnets',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeVpcs',
        'ec2:RunInsances',
        'ec2:CreateTags',
        'ec2:CreateFleet',
        'ec2:CreateNetworkInterface',
        'ec2:DescribeImages',
        'ec2:DescribeInstanceTypes',
        'ec2:DescribeInstanceTypeOfferings',
        'ec2:DescribeLaunchTemplates',
        'ec2:DescribeLaunchTemplateVersions',
        'ec2:GetSecurityGroupsForVpc',
        'ec2:DescribeCapacityReservations',
      ],
      resources: ['*'],
    }));

    this.clusterCreationStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'tag:TagResources',
        'tag:UntagResources',
      ],
      resources: ['*'],
    }));

    this.clusterDestructionStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'pcs:DeleteCluster',
        'pcs:DeleteComputeNodeGroup',
        'pcs:DeleteQueue',
        'pcs:DescribeCluster',
        'pcs:GetComputeNodeGroup',
        'pcs:GetQueue',
      ],
      resources: ['*'],
    }));

    this.clusterDestructionStateMachine.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'fsx:CreateDataRepositoryTask',
        'fsx:DescribeDataRepositoryTasks',
        'fsx:DeleteFileSystem',
        'fsx:DescribeFileSystems',
      ],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Cluster Operations Resources
    // ---------------------------------------------------------------
    const clustersResource = props.projectIdResource.addResource('clusters');
    const clusterNameResource = clustersResource.addResource('{clusterName}');

    const clusterOperationsIntegration = new apigateway.LambdaIntegration(this.clusterOperationsLambda);

    const cognitoMethodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: props.cognitoAuthorizer,
    };

    // POST /projects/{projectId}/clusters — create cluster
    clustersResource.addMethod('POST', clusterOperationsIntegration, cognitoMethodOptions);
    // GET /projects/{projectId}/clusters — list clusters
    clustersResource.addMethod('GET', clusterOperationsIntegration, cognitoMethodOptions);
    // GET /projects/{projectId}/clusters/{clusterName} — get cluster details
    clusterNameResource.addMethod('GET', clusterOperationsIntegration, cognitoMethodOptions);
    // DELETE /projects/{projectId}/clusters/{clusterName} — destroy cluster
    clusterNameResource.addMethod('DELETE', clusterOperationsIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/clusters/{clusterName}/recreate — recreate destroyed cluster
    const recreateResource = clusterNameResource.addResource('recreate');
    recreateResource.addMethod('POST', clusterOperationsIntegration, cognitoMethodOptions);
    // POST /projects/{projectId}/clusters/{clusterName}/fail — force-fail stuck cluster
    const failResource = clusterNameResource.addResource('fail');
    failResource.addMethod('POST', clusterOperationsIntegration, cognitoMethodOptions);

    // ---------------------------------------------------------------
    // POSIX Reconciliation Lambda Function
    // ---------------------------------------------------------------
    this.posixReconciliationLambda = new lambda.Function(this, 'PosixReconciliationLambda', {
      functionName: 'hpc-posix-reconciliation',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'posix_reconciliation.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        USERS_TABLE_NAME: props.platformUsersTable.tableName,
      },
      description: 'Daily POSIX reconciliation — audits Linux accounts on active clusters against project membership',
    });

    // Grant DynamoDB read access on Clusters, Projects, and PlatformUsers tables
    props.clustersTable.grantReadData(this.posixReconciliationLambda);
    props.projectsTable.grantReadWriteData(this.posixReconciliationLambda);
    props.platformUsersTable.grantReadData(this.posixReconciliationLambda);

    // Grant SSM permissions for querying and managing accounts on cluster nodes
    this.posixReconciliationLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ssm:SendCommand',
        'ssm:GetCommandInvocation',
      ],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // Login Node Refresh Lambda Function (Scheduled)
    // Periodically re-resolves login node instance IDs and IPs for
    // all active clusters.  If PCS replaces a login node, this Lambda
    // detects the change and updates DynamoDB so connection details
    // shown in the UI stay current.
    // ---------------------------------------------------------------
    this.loginNodeRefreshLambda = new lambda.Function(this, 'LoginNodeRefreshLambda', {
      functionName: 'hpc-login-node-refresh',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'login_node_refresh.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
      },
      description: 'Fallback safety net: refreshes login node instance IDs and IPs for active clusters',
    });

    // Grant DynamoDB read/write on Clusters table (read to scan, write to update)
    props.clustersTable.grantReadWriteData(this.loginNodeRefreshLambda);

    // Grant EC2 DescribeInstances to resolve current login node instances
    this.loginNodeRefreshLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeInstances'],
      resources: ['*'],
    }));

    // EventBridge rule to trigger login node refresh every 60 minutes as a fallback safety net
    new events.Rule(this, 'LoginNodeRefreshScheduleRule', {
      ruleName: 'hpc-login-node-refresh-schedule',
      description: 'Fallback safety net: triggers the login node refresh Lambda every 60 minutes to reconcile any missed event-driven updates',
      schedule: events.Schedule.rate(cdk.Duration.minutes(60)),
    }).addTarget(
      new eventsTargets.LambdaFunction(this.loginNodeRefreshLambda),
    );

    // ---------------------------------------------------------------
    // Login Node Event Handler Lambda Function (Event-Driven)
    // Processes EC2 Instance State-change Notification events from
    // EventBridge.  When a login node instance enters the "running"
    // state, this Lambda resolves the new instance details and updates
    // the corresponding cluster record in DynamoDB immediately.
    // ---------------------------------------------------------------
    this.loginNodeEventLambda = new lambda.Function(this, 'LoginNodeEventLambda', {
      functionName: 'hpc-login-node-event-handler',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'login_node_event.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'cluster_operations'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
      },
      description: 'Processes EC2 state-change events to update login node details in DynamoDB immediately',
    });

    // Grant DynamoDB read/write on Clusters table (scan to find cluster, write to update)
    props.clustersTable.grantReadWriteData(this.loginNodeEventLambda);

    // Grant EC2 permissions to resolve instance tags and details
    this.loginNodeEventLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:DescribeInstances',
        'ec2:DescribeTags',
      ],
      resources: ['*'],
    }));

    // EventBridge rule to capture EC2 instances entering the "running" state
    const loginNodeStateChangeRule = new events.Rule(this, 'LoginNodeStateChangeRule', {
      ruleName: 'hpc-login-node-state-change',
      description: 'Routes EC2 instance running state-change events to the login node event handler',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Instance State-change Notification'],
        detail: {
          state: ['running'],
        },
      },
    });

    loginNodeStateChangeRule.addTarget(
      new eventsTargets.LambdaFunction(this.loginNodeEventLambda),
    );
  }
}
