import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export class SelfServiceHpcStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Platform foundation resources will be added in subsequent tasks

    // -----------------------------------------------------------------
    // PUT /templates/{templateId} — Update cluster template (admin only)
    //
    // When the full API Gateway and Lambda resources are wired into this
    // stack, add a PUT method on the /templates/{templateId} resource
    // following the same pattern as PUT /projects/{projectId} in the
    // FoundationStack.
    //
    // Required CDK configuration:
    //
    //   const templateManagementIntegration = new apigateway.LambdaIntegration(
    //     templateManagementLambda,
    //   );
    //
    //   const cognitoMethodOptions: apigateway.MethodOptions = {
    //     authorizationType: apigateway.AuthorizationType.COGNITO,
    //     authorizer: cognitoAuthorizer,
    //   };
    //
    //   // PUT /templates/{templateId} — update template (admin only in handler)
    //   templateIdResource.addMethod('PUT', templateManagementIntegration, cognitoMethodOptions);
    //
    // The other template routes (GET, POST, DELETE) are currently defined
    // in FoundationStack (lib/foundation-stack.ts). When this stack takes
    // over API Gateway ownership, add the PUT method alongside them.
    // The handler dispatches PUT requests to _handle_update_template
    // in lambda/template_management/handler.py.
    //
    // Validates: Requirement 6.1
    // -----------------------------------------------------------------
  }
}
