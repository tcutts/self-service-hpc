import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { Construct } from 'constructs';

export interface UserManagementProps {
  platformUsersTable: dynamodb.Table;
  userPool: cognito.UserPool;
  api: apigateway.RestApi;
  cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  sharedLayer: lambda.LayerVersion;
}

/**
 * Encapsulates the user management Lambda function, IAM policies,
 * and all /users API Gateway routes for the HPC platform.
 */
export class UserManagement extends Construct {
  /** The user management Lambda function. */
  public readonly lambda: lambda.Function;

  constructor(scope: Construct, id: string, props: UserManagementProps) {
    super(scope, id);

    this.lambda = new lambda.Function(this, 'UserManagementLambda', {
      functionName: 'hpc-user-management',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'user_management'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        USERS_TABLE_NAME: props.platformUsersTable.tableName,
        USER_POOL_ID: props.userPool.userPoolId,
      },
      description: 'Handles user CRUD operations including POSIX UID/GID assignment',
    });

    // Grant DynamoDB read/write access to PlatformUsers table
    props.platformUsersTable.grantReadWriteData(this.lambda);

    // Grant Cognito admin actions on the User Pool
    this.lambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminCreateUser',
        'cognito-idp:AdminDeleteUser',
        'cognito-idp:AdminDisableUser',
        'cognito-idp:AdminEnableUser',
        'cognito-idp:AdminGetUser',
        'cognito-idp:AdminListGroupsForUser',
        'cognito-idp:AdminAddUserToGroup',
        'cognito-idp:AdminRemoveUserFromGroup',
        'cognito-idp:AdminUserGlobalSignOut',
      ],
      resources: [props.userPool.userPoolArn],
    }));

    // ---------------------------------------------------------------
    // API Gateway — User Management Resources
    // ---------------------------------------------------------------
    const usersResource = props.api.root.addResource('users');
    const userIdResource = usersResource.addResource('{userId}');

    const userManagementIntegration = new apigateway.LambdaIntegration(this.lambda);

    const cognitoMethodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: props.cognitoAuthorizer,
    };

    // GET /users — list users
    usersResource.addMethod('GET', userManagementIntegration, cognitoMethodOptions);
    // POST /users — create user
    usersResource.addMethod('POST', userManagementIntegration, cognitoMethodOptions);
    // GET /users/{userId} — get user details
    userIdResource.addMethod('GET', userManagementIntegration, cognitoMethodOptions);
    // DELETE /users/{userId} — deactivate user
    userIdResource.addMethod('DELETE', userManagementIntegration, cognitoMethodOptions);
    // POST /users/{userId}/reactivate — reactivate user
    const reactivateResource = userIdResource.addResource('reactivate');
    reactivateResource.addMethod('POST', userManagementIntegration, cognitoMethodOptions);

    // Batch user operations
    const usersBatchResource = usersResource.addResource('batch');
    // POST /users/batch/deactivate — bulk deactivate users
    const usersBatchDeactivateResource = usersBatchResource.addResource('deactivate');
    usersBatchDeactivateResource.addMethod('POST', userManagementIntegration, cognitoMethodOptions);
    // POST /users/batch/reactivate — bulk reactivate users
    const usersBatchReactivateResource = usersBatchResource.addResource('reactivate');
    usersBatchReactivateResource.addMethod('POST', userManagementIntegration, cognitoMethodOptions);
  }
}
