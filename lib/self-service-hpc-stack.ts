import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export class SelfServiceHpcStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Platform foundation resources will be added in subsequent tasks
  }
}
