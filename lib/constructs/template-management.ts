import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { Construct } from 'constructs';

export interface TemplateManagementProps {
  clusterTemplatesTable: dynamodb.Table;
  api: apigateway.RestApi;
  cognitoAuthorizer: apigateway.CognitoUserPoolsAuthorizer;
  sharedLayer: lambda.LayerVersion;
}

/**
 * Encapsulates the template management Lambda function, IAM policies,
 * and all /templates API Gateway routes for the HPC platform.
 */
export class TemplateManagement extends Construct {
  /** The template management Lambda function. */
  public readonly lambda: lambda.Function;

  constructor(scope: Construct, id: string, props: TemplateManagementProps) {
    super(scope, id);

    this.lambda = new lambda.Function(this, 'TemplateManagementLambda', {
      functionName: 'hpc-template-management',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'template_management')),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        TEMPLATES_TABLE_NAME: props.clusterTemplatesTable.tableName,
      },
      description: 'Handles cluster template CRUD operations',
    });

    // Grant DynamoDB read/write access to ClusterTemplates table
    props.clusterTemplatesTable.grantReadWriteData(this.lambda);

    // Grant EC2 DescribeImages for PCS sample AMI lookup
    this.lambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeImages'],
      resources: ['*'],
    }));

    // ---------------------------------------------------------------
    // API Gateway — Template Management Resources
    // ---------------------------------------------------------------
    const templatesResource = props.api.root.addResource('templates');
    const defaultAmiResource = templatesResource.addResource('default-ami');
    const templateIdResource = templatesResource.addResource('{templateId}');

    const templateManagementIntegration = new apigateway.LambdaIntegration(this.lambda);

    const cognitoMethodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizer: props.cognitoAuthorizer,
    };

    // POST /templates — create template (admin only in handler)
    templatesResource.addMethod('POST', templateManagementIntegration, cognitoMethodOptions);
    // GET /templates — list templates (any authenticated user in handler)
    templatesResource.addMethod('GET', templateManagementIntegration, cognitoMethodOptions);
    // GET /templates/default-ami — look up latest PCS sample AMI (any authenticated user)
    defaultAmiResource.addMethod('GET', templateManagementIntegration, cognitoMethodOptions);
    // GET /templates/{templateId} — get template details (any authenticated user in handler)
    templateIdResource.addMethod('GET', templateManagementIntegration, cognitoMethodOptions);
    // DELETE /templates/{templateId} — delete template (admin only in handler)
    templateIdResource.addMethod('DELETE', templateManagementIntegration, cognitoMethodOptions);
    // PUT /templates/{templateId} — update template (admin only in handler)
    templateIdResource.addMethod('PUT', templateManagementIntegration, cognitoMethodOptions);

    // Batch template operations
    const templatesBatchResource = templatesResource.addResource('batch');
    // POST /templates/batch/delete — bulk delete templates
    const templatesBatchDeleteResource = templatesBatchResource.addResource('delete');
    templatesBatchDeleteResource.addMethod('POST', templateManagementIntegration, cognitoMethodOptions);
  }
}
