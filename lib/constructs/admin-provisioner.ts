import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { Construct } from 'constructs';

export interface AdminProvisionerProps {
  platformUsersTable: dynamodb.Table;
  userPool: cognito.UserPool;
  adminEmail: string;
}

/**
 * Lambda-backed CloudFormation custom resource that ensures at least one
 * Administrator user exists after Foundation stack deployment.
 *
 * Scans DynamoDB for existing admins and conditionally creates a default
 * admin user in both Cognito and DynamoDB when none is found.
 */
export class AdminProvisioner extends Construct {
  constructor(scope: Construct, id: string, props: AdminProvisionerProps) {
    super(scope, id);

    // Validate that adminEmail is provided at synthesis time
    if (!props.adminEmail) {
      throw new Error(
        'adminEmail is required. Pass it via CDK context: cdk deploy -c adminEmail=ops@company.com',
      );
    }

    // 1. Lambda function for the custom resource handler
    const provisionerLambda = new lambda.Function(this, 'AdminProvisionerLambda', {
      functionName: 'hpc-admin-provisioner',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'admin_provisioner'), {
        exclude: ['__pycache__', '**/__pycache__', '*.pyc'],
      }),
      timeout: cdk.Duration.seconds(60),
      environment: {
        TABLE_NAME: props.platformUsersTable.tableName,
        USER_POOL_ID: props.userPool.userPoolId,
        ADMIN_EMAIL: props.adminEmail,
      },
      description: 'CloudFormation custom resource that provisions the initial admin user',
    });

    // 2. Least-privilege IAM — DynamoDB actions on PlatformUsers table
    provisionerLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:Scan',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
      ],
      resources: [props.platformUsersTable.tableArn],
    }));

    // 3. Least-privilege IAM — Cognito actions on User Pool
    provisionerLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:AdminCreateUser',
        'cognito-idp:AdminAddUserToGroup',
        'cognito-idp:AdminGetUser',
        'cognito-idp:AdminDeleteUser',
      ],
      resources: [props.userPool.userPoolArn],
    }));

    // 4. CfnCustomResource backed by the Lambda function
    const customResource = new cdk.CfnResource(this, 'AdminProvisionerResource', {
      type: 'Custom::AdminProvisioner',
      properties: {
        ServiceToken: provisionerLambda.functionArn,
        TableName: props.platformUsersTable.tableName,
        UserPoolId: props.userPool.userPoolId,
        AdminEmail: props.adminEmail,
      },
    });

    // 5. CfnOutputs for admin credentials (empty when no user created)
    new cdk.CfnOutput(this, 'AdminUserName', {
      value: customResource.getAtt('AdminUserName').toString(),
      description: 'Username of the provisioned admin user',
    });

    new cdk.CfnOutput(this, 'AdminUserPassword', {
      value: customResource.getAtt('AdminUserPassword').toString(),
      description: 'Temporary password for the provisioned admin user',
    });
  }
}
