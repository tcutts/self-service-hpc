import * as cdk from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as path from 'path';
import { Construct } from 'constructs';

export interface ProjectLifecycleProps {
  projectsTable: dynamodb.Table;
  clustersTable: dynamodb.Table;
  cdkDeployProject: codebuild.Project;
  sharedLayer: lambda.LayerVersion;
}

/**
 * Encapsulates the project deploy, destroy, and update step Lambdas and
 * their corresponding Step Functions state machines for the HPC platform.
 *
 * State machine ARN injection into the project management Lambda and
 * grantStartExecution calls are performed by the orchestrator (FoundationStack),
 * NOT this construct.
 */
export class ProjectLifecycle extends Construct {
  /** The project deploy state machine. */
  public readonly projectDeployStateMachine: sfn.StateMachine;
  /** The project destroy state machine. */
  public readonly projectDestroyStateMachine: sfn.StateMachine;
  /** The project update state machine. */
  public readonly projectUpdateStateMachine: sfn.StateMachine;

  constructor(scope: Construct, id: string, props: ProjectLifecycleProps) {
    super(scope, id);

    // ---------------------------------------------------------------
    // Project Deploy Step Lambda
    // ---------------------------------------------------------------
    const projectDeployStepLambda = new lambda.Function(this, 'ProjectDeployStepLambda', {
      functionName: 'hpc-project-deploy-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'project_deploy.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'project_management'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        CODEBUILD_PROJECT_NAME: props.cdkDeployProject.projectName,
      },
      description: 'Executes individual steps of the project deploy workflow',
    });

    // Grant deploy step Lambda permissions
    props.projectsTable.grantReadWriteData(projectDeployStepLambda);

    projectDeployStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'codebuild:StartBuild',
        'codebuild:BatchGetBuilds',
      ],
      resources: [props.cdkDeployProject.projectArn],
    }));

    projectDeployStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudformation:DescribeStacks',
      ],
      resources: [
        `arn:aws:cloudformation:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:stack/HpcProject-*/*`,
      ],
    }));

    // --- Project Deploy State Machine Definition ---

    // Consolidated pre-loop step: validate project state + start CDK deploy
    const consolidatedDeployPreLoop = new tasks.LambdaInvoke(this, 'ConsolidatedPreLoop', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'consolidated_pre_loop',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Pre-soak wait: 210s before first deploy status check (calibrated to ~80th percentile)
    const preSoakWaitDeploy = new sfn.Wait(this, 'PreSoakWaitDeploy', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(210)),
    });

    // Check deploy status (with wait loop)
    const checkDeployStatus = new tasks.LambdaInvoke(this, 'CheckDeployStatus', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_deploy_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForDeploy = new sfn.Wait(this, 'WaitForDeploy', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Consolidated post-loop step: extract stack outputs + record infrastructure
    const consolidatedDeployPostLoop = new tasks.LambdaInvoke(this, 'ConsolidatedPostLoop', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'consolidated_post_loop',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler
    const handleDeployFailure = new tasks.LambdaInvoke(this, 'HandleDeployFailure', {
      lambdaFunction: projectDeployStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_deploy_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const deployFailed = new sfn.Fail(this, 'DeployFailed', {
      cause: 'Project deployment failed',
      error: 'ProjectDeployError',
    });

    const deploySuccess = new sfn.Succeed(this, 'DeploySucceeded');

    // Add catch to all deploy steps for failure handling
    const deployCatchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const deployFailureChain = handleDeployFailure.next(deployFailed);

    consolidatedDeployPreLoop.addCatch(deployFailureChain, deployCatchConfig);
    checkDeployStatus.addCatch(deployFailureChain, deployCatchConfig);
    consolidatedDeployPostLoop.addCatch(deployFailureChain, deployCatchConfig);

    // Deploy wait loop: check status → if not complete, wait → check again
    const deployWaitLoop = waitForDeploy.next(checkDeployStatus);
    const isDeployComplete = new sfn.Choice(this, 'IsDeployComplete')
      .when(sfn.Condition.booleanEquals('$.deployComplete', true), consolidatedDeployPostLoop)
      .otherwise(deployWaitLoop);

    // Chain: ConsolidatedPreLoop → PreSoakWaitDeploy (210s) → CheckDeployStatus → wait loop → ConsolidatedPostLoop → Success
    const deployDefinition = consolidatedDeployPreLoop
      .next(preSoakWaitDeploy)
      .next(checkDeployStatus)
      .next(isDeployComplete);

    // Post-deploy chain (connected via the Choice "when complete" branch)
    consolidatedDeployPostLoop
      .next(deploySuccess);

    this.projectDeployStateMachine = new sfn.StateMachine(this, 'ProjectDeployStateMachine', {
      stateMachineName: 'hpc-project-deploy',
      definitionBody: sfn.DefinitionBody.fromChainable(deployDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // ---------------------------------------------------------------
    // Project Destroy Step Lambda
    // ---------------------------------------------------------------
    const projectDestroyStepLambda = new lambda.Function(this, 'ProjectDestroyStepLambda', {
      functionName: 'hpc-project-destroy-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'project_destroy.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'project_management'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        CLUSTERS_TABLE_NAME: props.clustersTable.tableName,
        CODEBUILD_PROJECT_NAME: props.cdkDeployProject.projectName,
      },
      description: 'Executes individual steps of the project destroy workflow',
    });

    // Grant destroy step Lambda permissions
    props.projectsTable.grantReadWriteData(projectDestroyStepLambda);
    props.clustersTable.grantReadData(projectDestroyStepLambda);

    projectDestroyStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'codebuild:StartBuild',
        'codebuild:BatchGetBuilds',
      ],
      resources: [props.cdkDeployProject.projectArn],
    }));

    // --- Project Destroy State Machine Definition ---
    // Chain: ConsolidatedDestroyPreLoop → PreSoakWaitDestroy (210s) → CheckDestroyStatus → (complete → ConsolidatedDestroyPostLoop → Success | not complete → Wait 30s → CheckDestroyStatus)

    // Consolidated pre-loop step: validate project state + check clusters + start CDK destroy
    const consolidatedDestroyPreLoop = new tasks.LambdaInvoke(this, 'ConsolidatedDestroyPreLoop', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'consolidated_pre_loop',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Pre-soak wait: 210s before first destroy status check (calibrated to ~80th percentile)
    const preSoakWaitDestroy = new sfn.Wait(this, 'PreSoakWaitDestroy', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(210)),
    });

    // Check destroy status (with wait loop)
    const checkDestroyStatus = new tasks.LambdaInvoke(this, 'CheckDestroyStatus', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_destroy_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForDestroy = new sfn.Wait(this, 'WaitForDestroy', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Consolidated post-loop step: clear infrastructure + archive project
    const consolidatedDestroyPostLoop = new tasks.LambdaInvoke(this, 'ConsolidatedDestroyPostLoop', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'consolidated_post_loop',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler
    const handleDestroyFailure = new tasks.LambdaInvoke(this, 'HandleDestroyFailure', {
      lambdaFunction: projectDestroyStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_destroy_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const projectDestroyFailed = new sfn.Fail(this, 'ProjectDestroyFailed', {
      cause: 'Project destruction failed',
      error: 'ProjectDestroyError',
    });

    const projectDestroySuccess = new sfn.Succeed(this, 'ProjectDestroySucceeded');

    // Add catch to all destroy steps for failure handling
    const destroyCatchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const destroyFailureChain = handleDestroyFailure.next(projectDestroyFailed);

    consolidatedDestroyPreLoop.addCatch(destroyFailureChain, destroyCatchConfig);
    checkDestroyStatus.addCatch(destroyFailureChain, destroyCatchConfig);
    consolidatedDestroyPostLoop.addCatch(destroyFailureChain, destroyCatchConfig);

    // Destroy wait loop: check status → if not complete, wait → check again
    const destroyWaitLoop = waitForDestroy.next(checkDestroyStatus);
    const isDestroyComplete = new sfn.Choice(this, 'IsDestroyComplete')
      .when(sfn.Condition.booleanEquals('$.destroyComplete', true), consolidatedDestroyPostLoop)
      .otherwise(destroyWaitLoop);

    // Chain: ConsolidatedDestroyPreLoop → PreSoakWaitDestroy (210s) → CheckDestroyStatus → wait loop → ConsolidatedDestroyPostLoop → Success
    const destroyDefinition = consolidatedDestroyPreLoop
      .next(preSoakWaitDestroy)
      .next(checkDestroyStatus)
      .next(isDestroyComplete);

    // Post-destroy chain (connected via the Choice "when complete" branch)
    consolidatedDestroyPostLoop
      .next(projectDestroySuccess);

    this.projectDestroyStateMachine = new sfn.StateMachine(this, 'ProjectDestroyStateMachine', {
      stateMachineName: 'hpc-project-destroy',
      definitionBody: sfn.DefinitionBody.fromChainable(destroyDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });

    // ---------------------------------------------------------------
    // Project Update Step Lambda
    // ---------------------------------------------------------------
    const projectUpdateStepLambda = new lambda.Function(this, 'ProjectUpdateStepLambda', {
      functionName: 'hpc-project-update-steps',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'project_update.step_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'project_management'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      environment: {
        PROJECTS_TABLE_NAME: props.projectsTable.tableName,
        CODEBUILD_PROJECT_NAME: props.cdkDeployProject.projectName,
      },
      description: 'Executes individual steps of the project update workflow',
    });

    // Grant update step Lambda permissions
    props.projectsTable.grantReadWriteData(projectUpdateStepLambda);

    projectUpdateStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'codebuild:StartBuild',
        'codebuild:BatchGetBuilds',
      ],
      resources: [props.cdkDeployProject.projectArn],
    }));

    projectUpdateStepLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudformation:DescribeStacks',
      ],
      resources: [
        `arn:aws:cloudformation:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:stack/HpcProject-*/*`,
      ],
    }));

    // --- Project Update State Machine Definition ---

    // Consolidated pre-loop step: validate update state + start CDK update
    const consolidatedUpdatePreLoop = new tasks.LambdaInvoke(this, 'ConsolidatedUpdatePreLoop', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'consolidated_pre_loop',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Pre-soak wait: 90s before first update status check (calibrated to ~80th percentile)
    const preSoakWaitUpdate = new sfn.Wait(this, 'PreSoakWaitUpdate', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(90)),
    });

    // Check update status (with wait loop)
    const checkUpdateStatus = new tasks.LambdaInvoke(this, 'CheckUpdateStatus', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'check_update_status',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const waitForUpdate = new sfn.Wait(this, 'WaitForUpdate', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30)),
    });

    // Consolidated post-loop step: extract stack outputs + record updated infrastructure
    const consolidatedUpdatePostLoop = new tasks.LambdaInvoke(this, 'ConsolidatedUpdatePostLoop', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'consolidated_post_loop',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    // Failure handler
    const handleUpdateFailure = new tasks.LambdaInvoke(this, 'HandleUpdateFailure', {
      lambdaFunction: projectUpdateStepLambda,
      payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        'step': 'handle_update_failure',
        'payload': sfn.JsonPath.entirePayload,
      }),
      resultPath: '$',
    });

    const updateFailed = new sfn.Fail(this, 'UpdateFailed', {
      cause: 'Project update failed',
      error: 'ProjectUpdateError',
    });

    const updateSuccess = new sfn.Succeed(this, 'UpdateSucceeded');

    // Add catch to all update steps for failure handling
    const updateCatchConfig: sfn.CatchProps = { resultPath: '$.error' };
    const updateFailureChain = handleUpdateFailure.next(updateFailed);

    consolidatedUpdatePreLoop.addCatch(updateFailureChain, updateCatchConfig);
    checkUpdateStatus.addCatch(updateFailureChain, updateCatchConfig);
    consolidatedUpdatePostLoop.addCatch(updateFailureChain, updateCatchConfig);

    // Update wait loop: check status → if not complete, wait → check again
    const updateWaitLoop = waitForUpdate.next(checkUpdateStatus);
    const isUpdateComplete = new sfn.Choice(this, 'IsUpdateComplete')
      .when(sfn.Condition.booleanEquals('$.updateComplete', true), consolidatedUpdatePostLoop)
      .otherwise(updateWaitLoop);

    // Chain: ConsolidatedUpdatePreLoop → PreSoakWaitUpdate (90s) → CheckUpdateStatus → wait loop → ConsolidatedUpdatePostLoop → Success
    const updateDefinition = consolidatedUpdatePreLoop
      .next(preSoakWaitUpdate)
      .next(checkUpdateStatus)
      .next(isUpdateComplete);

    // Post-update chain (connected via the Choice "when complete" branch)
    consolidatedUpdatePostLoop
      .next(updateSuccess);

    this.projectUpdateStateMachine = new sfn.StateMachine(this, 'ProjectUpdateStateMachine', {
      stateMachineName: 'hpc-project-update',
      definitionBody: sfn.DefinitionBody.fromChainable(updateDefinition),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
    });
  }
}
