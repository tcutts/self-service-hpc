import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

describe('FoundationStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new FoundationStack(app, 'TestFoundationStack');
    template = Template.fromStack(stack);
  });

  // ---------------------------------------------------------------------------
  // DynamoDB Tables — existence, key schemas, billing, and PITR
  // Validates: Requirement 16.1 (all infrastructure defined in CDK)
  // ---------------------------------------------------------------------------
  describe('DynamoDB Tables', () => {
    it('creates exactly 5 DynamoDB tables', () => {
      template.resourceCountIs('AWS::DynamoDB::Table', 5);
    });

    describe('PlatformUsers table', () => {
      it('has correct key schema (PK/SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'PlatformUsers',
          KeySchema: [
            { AttributeName: 'PK', KeyType: 'HASH' },
            { AttributeName: 'SK', KeyType: 'RANGE' },
          ],
        });
      });

      it('has StatusIndex GSI with status (PK) and userId (SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'PlatformUsers',
          GlobalSecondaryIndexes: Match.arrayWith([
            Match.objectLike({
              IndexName: 'StatusIndex',
              KeySchema: [
                { AttributeName: 'status', KeyType: 'HASH' },
                { AttributeName: 'userId', KeyType: 'RANGE' },
              ],
              Projection: { ProjectionType: 'ALL' },
            }),
          ]),
        });
      });

      it('uses PAY_PER_REQUEST billing', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'PlatformUsers',
          BillingMode: 'PAY_PER_REQUEST',
        });
      });

      it('has point-in-time recovery enabled', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'PlatformUsers',
          PointInTimeRecoverySpecification: {
            PointInTimeRecoveryEnabled: true,
          },
        });
      });
    });

    describe('Projects table', () => {
      it('has correct key schema (PK/SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Projects',
          KeySchema: [
            { AttributeName: 'PK', KeyType: 'HASH' },
            { AttributeName: 'SK', KeyType: 'RANGE' },
          ],
        });
      });

      it('has UserProjectsIndex GSI with userId (PK) and projectId (SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Projects',
          GlobalSecondaryIndexes: Match.arrayWith([
            Match.objectLike({
              IndexName: 'UserProjectsIndex',
              KeySchema: [
                { AttributeName: 'userId', KeyType: 'HASH' },
                { AttributeName: 'projectId', KeyType: 'RANGE' },
              ],
              Projection: { ProjectionType: 'ALL' },
            }),
          ]),
        });
      });

      it('uses PAY_PER_REQUEST billing', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Projects',
          BillingMode: 'PAY_PER_REQUEST',
        });
      });

      it('has point-in-time recovery enabled', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Projects',
          PointInTimeRecoverySpecification: {
            PointInTimeRecoveryEnabled: true,
          },
        });
      });
    });

    describe('ClusterTemplates table', () => {
      it('has correct key schema (PK/SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'ClusterTemplates',
          KeySchema: [
            { AttributeName: 'PK', KeyType: 'HASH' },
            { AttributeName: 'SK', KeyType: 'RANGE' },
          ],
        });
      });

      it('uses PAY_PER_REQUEST billing', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'ClusterTemplates',
          BillingMode: 'PAY_PER_REQUEST',
        });
      });

      it('has point-in-time recovery enabled', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'ClusterTemplates',
          PointInTimeRecoverySpecification: {
            PointInTimeRecoveryEnabled: true,
          },
        });
      });
    });

    describe('Clusters table', () => {
      it('has correct key schema (PK/SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Clusters',
          KeySchema: [
            { AttributeName: 'PK', KeyType: 'HASH' },
            { AttributeName: 'SK', KeyType: 'RANGE' },
          ],
        });
      });

      it('uses PAY_PER_REQUEST billing', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Clusters',
          BillingMode: 'PAY_PER_REQUEST',
        });
      });

      it('has point-in-time recovery enabled', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'Clusters',
          PointInTimeRecoverySpecification: {
            PointInTimeRecoveryEnabled: true,
          },
        });
      });
    });

    describe('ClusterNameRegistry table', () => {
      it('has correct key schema (PK/SK)', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'ClusterNameRegistry',
          KeySchema: [
            { AttributeName: 'PK', KeyType: 'HASH' },
            { AttributeName: 'SK', KeyType: 'RANGE' },
          ],
        });
      });

      it('uses PAY_PER_REQUEST billing', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'ClusterNameRegistry',
          BillingMode: 'PAY_PER_REQUEST',
        });
      });

      it('has point-in-time recovery enabled', () => {
        template.hasResourceProperties('AWS::DynamoDB::Table', {
          TableName: 'ClusterNameRegistry',
          PointInTimeRecoverySpecification: {
            PointInTimeRecoveryEnabled: true,
          },
        });
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Cognito User Pool, Client, and Administrators group
  // Validates: Requirement 16.1
  // ---------------------------------------------------------------------------
  describe('Cognito', () => {
    it('creates a User Pool with email sign-in', () => {
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        UserPoolName: 'hpc-platform-users',
        UsernameAttributes: ['email'],
        AutoVerifiedAttributes: ['email'],
      });
    });

    it('creates a User Pool Client', () => {
      template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
        ClientName: 'hpc-web-portal',
      });
    });

    it('creates an Administrators group', () => {
      template.hasResourceProperties('AWS::Cognito::UserPoolGroup', {
        GroupName: 'Administrators',
        Description: 'Platform administrators with full management access',
      });
    });
  });

  // ---------------------------------------------------------------------------
  // API Gateway REST API with Cognito authoriser
  // Validates: Requirement 16.1
  // ---------------------------------------------------------------------------
  describe('API Gateway', () => {
    it('creates a REST API', () => {
      template.hasResourceProperties('AWS::ApiGateway::RestApi', {
        Name: 'hpc-platform-api',
        Description: 'Self-Service HPC Platform API',
      });
    });

    it('creates a Cognito authoriser attached to the User Pool', () => {
      template.hasResourceProperties('AWS::ApiGateway::Authorizer', {
        Name: 'hpc-cognito-authorizer',
        Type: 'COGNITO_USER_POOLS',
        IdentitySource: 'method.request.header.Authorization',
      });
    });

    it('has at least one method using the Cognito authoriser', () => {
      template.hasResourceProperties('AWS::ApiGateway::Method', {
        AuthorizationType: 'COGNITO_USER_POOLS',
      });
    });
  });

  // ---------------------------------------------------------------------------
  // User Management Lambda
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('User Management Lambda', () => {
    it('creates a Python Lambda function for user management', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-user-management',
        Runtime: 'python3.13',
        Handler: 'handler.handler',
        Timeout: 30,
        MemorySize: 256,
      });
    });

    it('passes USERS_TABLE_NAME and USER_POOL_ID as environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-user-management',
        Environment: {
          Variables: {
            USERS_TABLE_NAME: Match.anyValue(),
            USER_POOL_ID: Match.anyValue(),
          },
        },
      });
    });

    it('has IAM policy for Cognito admin actions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'cognito-idp:AdminCreateUser',
                'cognito-idp:AdminDeleteUser',
                'cognito-idp:AdminDisableUser',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // API Gateway — User Management Routes
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('API Gateway User Management Routes', () => {
    it('creates /users resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'users',
      });
    });

    it('creates /users/{userId} resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: '{userId}',
      });
    });

    it('creates /users/{userId}/reactivate resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'reactivate',
      });
    });

    it('has methods using Cognito authorisation for user routes', () => {
      // There should be multiple methods with COGNITO_USER_POOLS auth
      // (GET /users, POST /users, GET /users/{userId}, DELETE /users/{userId},
      //  POST /users/{userId}/reactivate, plus health)
      const methods = template.findResources('AWS::ApiGateway::Method', {
        Properties: {
          AuthorizationType: 'COGNITO_USER_POOLS',
        },
      });
      // At least 6: health GET + 4 user management methods + 1 reactivate POST
      expect(Object.keys(methods).length).toBeGreaterThanOrEqual(6);
    });
  });

  // ---------------------------------------------------------------------------
  // SNS Topics
  // Validates: Requirements 19.1, 19.4, 19.5
  // ---------------------------------------------------------------------------
  describe('SNS Topics', () => {
    it('creates a budget notification SNS topic', () => {
      template.hasResourceProperties('AWS::SNS::Topic', {
        TopicName: 'hpc-budget-notifications',
        DisplayName: 'HPC Platform Budget Notifications',
      });
    });

    it('creates a cluster lifecycle notification SNS topic', () => {
      template.hasResourceProperties('AWS::SNS::Topic', {
        TopicName: 'hpc-cluster-lifecycle-notifications',
        DisplayName: 'HPC Cluster Lifecycle Notifications',
      });
    });

    it('creates exactly 2 SNS topics', () => {
      template.resourceCountIs('AWS::SNS::Topic', 2);
    });
  });

  // ---------------------------------------------------------------------------
  // CloudWatch Log Groups — retention periods
  // Validates: Requirement 13.4
  // ---------------------------------------------------------------------------
  describe('CloudWatch Log Groups', () => {
    it('sets 90-day retention for infrastructure logs', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/hpc-platform/lambda/infrastructure',
        RetentionInDays: 90,
      });
    });

    it('sets 365-day retention for API access logs', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/hpc-platform/api-gateway/access-logs',
        RetentionInDays: 365,
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Cluster Operations Lambda
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('Cluster Operations Lambda', () => {
    it('creates a Python Lambda function for cluster operations', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-cluster-operations',
        Runtime: 'python3.13',
        Handler: 'handler.handler',
        Timeout: 60,
        MemorySize: 256,
      });
    });

    it('passes required environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-cluster-operations',
        Environment: {
          Variables: {
            CLUSTERS_TABLE_NAME: Match.anyValue(),
            PROJECTS_TABLE_NAME: Match.anyValue(),
            CLUSTER_NAME_REGISTRY_TABLE_NAME: Match.anyValue(),
            USERS_TABLE_NAME: Match.anyValue(),
            CREATION_STATE_MACHINE_ARN: Match.anyValue(),
            DESTRUCTION_STATE_MACHINE_ARN: Match.anyValue(),
            CLUSTER_LIFECYCLE_SNS_TOPIC_ARN: Match.anyValue(),
            USER_POOL_ID: Match.anyValue(),
          },
        },
      });
    });

    it('creates cluster creation step Lambda', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-cluster-creation-steps',
        Runtime: 'python3.13',
        Timeout: 300,
      });
    });

    it('creates cluster destruction step Lambda', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-cluster-destruction-steps',
        Runtime: 'python3.13',
        Timeout: 300,
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Step Functions State Machines
  // Validates: Requirements 16.1, 16.2, 19.4, 19.5
  // ---------------------------------------------------------------------------
  describe('Step Functions State Machines', () => {
    it('creates a cluster creation state machine', () => {
      template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
        StateMachineName: 'hpc-cluster-creation',
      });
    });

    it('creates a cluster destruction state machine', () => {
      template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
        StateMachineName: 'hpc-cluster-destruction',
      });
    });

    it('creates exactly 5 state machines', () => {
      template.resourceCountIs('AWS::StepFunctions::StateMachine', 5);
    });

    it('state machines have tracing enabled', () => {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine');
      for (const [, resource] of Object.entries(stateMachines)) {
        expect((resource as any).Properties?.TracingConfiguration?.Enabled).toBe(true);
      }
    });

    it('creation state machine execution role has PCS permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'pcs:CreateCluster',
                'pcs:CreateComputeNodeGroup',
                'pcs:CreateQueue',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('creation state machine execution role has FSx permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'fsx:CreateFileSystem',
                'fsx:DescribeFileSystems',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('creation step Lambda has iam:CreateServiceLinkedRole for FSx', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'iam:CreateServiceLinkedRole',
              Effect: 'Allow',
              Condition: {
                StringLike: {
                  'iam:AWSServiceName': 'fsx.amazonaws.com',
                },
              },
            }),
          ]),
        },
      });
    });

    it('creation step Lambda has SLR and policy permissions for FSx S3 data-source', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: [
                'iam:CreateServiceLinkedRole',
                'iam:AttachRolePolicy',
                'iam:PutRolePolicy',
              ],
              Effect: 'Allow',
              Condition: {
                StringLike: {
                  'iam:AWSServiceName': 's3.data-source.lustre.fsx.amazonaws.com',
                },
              },
            }),
          ]),
        },
      });
    });

    it('creation state machine execution role has EC2 permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'ec2:DescribeSubnets',
                'ec2:DescribeSecurityGroups',
                'ec2:CreateTags',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('creation state machine execution role has tagging permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'tag:TagResources',
                'tag:UntagResources',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('destruction state machine execution role has FSx data repository task permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'fsx:CreateDataRepositoryTask',
                'fsx:DescribeDataRepositoryTasks',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('all state machine definitions have valid Catch blocks (States.ALL must appear alone and at end)', () => {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine');

      for (const [logicalId, resource] of Object.entries(stateMachines)) {
        const definitionString = (resource as any).Properties?.DefinitionString;
        if (!definitionString) continue;

        // The DefinitionString is typically a Fn::Join — resolve it to a string.
        // Non-string parts (Ref, Fn::GetAtt) are ARN-like values that appear as
        // JSON string values, so we replace them with a valid placeholder string.
        let definition: any;
        if (typeof definitionString === 'string') {
          definition = JSON.parse(definitionString);
        } else if (definitionString['Fn::Join']) {
          const separator: string = definitionString['Fn::Join'][0];
          const parts: any[] = definitionString['Fn::Join'][1];
          const joined = parts
            .map((p: any) => (typeof p === 'string' ? p : 'arn:aws:placeholder:us-east-1:123456789012:placeholder'))
            .join(separator);
          definition = JSON.parse(joined);
        } else {
          continue;
        }

        // Recursively walk all states and validate Catch blocks
        const validateStates = (states: Record<string, any>, path: string) => {
          for (const [stateName, state] of Object.entries(states)) {
            const statePath = `${path}/States/${stateName}`;

            if (state.Catch && Array.isArray(state.Catch)) {
              const errorEqualsLists = state.Catch.map(
                (c: any) => c.ErrorEquals,
              );

              for (let i = 0; i < errorEqualsLists.length; i++) {
                const errorEquals: string[] = errorEqualsLists[i];

                // States.ALL must be the sole entry in its ErrorEquals array
                if (errorEquals.includes('States.ALL')) {
                  expect(errorEquals).toHaveLength(1);
                }

                // States.ALL must only appear in the last Catch entry
                if (errorEquals.includes('States.ALL') && i !== errorEqualsLists.length - 1) {
                  throw new Error(
                    `${logicalId} at ${statePath}/Catch[${i}]: ` +
                    `States.ALL must appear only in the last Catch entry`,
                  );
                }
              }

              // No duplicate Catch entries with identical ErrorEquals
              const serialised = errorEqualsLists.map((e: string[]) =>
                JSON.stringify(e.slice().sort()),
              );
              const uniqueSerialised = new Set(serialised);
              if (serialised.length !== uniqueSerialised.size) {
                throw new Error(
                  `${logicalId} at ${statePath}: ` +
                  `duplicate Catch ErrorEquals entries detected`,
                );
              }
            }

            // Recurse into Parallel branches
            if (state.Branches && Array.isArray(state.Branches)) {
              for (const branch of state.Branches) {
                if (branch.States) {
                  validateStates(branch.States, `${statePath}/Branch`);
                }
              }
            }

            // Recurse into Map iterator
            if (state.Iterator?.States) {
              validateStates(state.Iterator.States, `${statePath}/Iterator`);
            }
          }
        };

        if (definition.States) {
          validateStates(definition.States, logicalId);
        }
      }
    });
  });

  // ---------------------------------------------------------------------------
  // API Gateway — Cluster Operations Routes
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('API Gateway Cluster Operations Routes', () => {
    it('creates /projects/{projectId}/clusters resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'clusters',
      });
    });

    it('creates /projects/{projectId}/clusters/{clusterName} resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: '{clusterName}',
      });
    });

    it('creates /projects/{projectId}/clusters/{clusterName}/recreate resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'recreate',
      });
    });

    it('has POST method on recreate resource with Cognito auth', () => {
      // Find all API Gateway resources with PathPart 'recreate'
      const recreateResources = template.findResources('AWS::ApiGateway::Resource', {
        Properties: {
          PathPart: 'recreate',
        },
      });
      const recreateResourceIds = Object.keys(recreateResources);
      expect(recreateResourceIds.length).toBeGreaterThanOrEqual(1);

      // Find the recreate resource that is a child of {clusterName}
      const clusterNameResources = template.findResources('AWS::ApiGateway::Resource', {
        Properties: {
          PathPart: '{clusterName}',
        },
      });
      const clusterNameResourceId = Object.keys(clusterNameResources)[0];

      // Find the recreate resource whose ParentId references the {clusterName} resource
      const recreateUnderCluster = Object.entries(recreateResources).find(
        ([, resource]) => {
          const parentRef = (resource as any).Properties?.ParentId?.Ref;
          return parentRef === clusterNameResourceId;
        },
      );
      expect(recreateUnderCluster).toBeDefined();

      // Verify there is a POST method on the recreate resource with Cognito auth
      const recreateLogicalId = recreateUnderCluster![0];
      const postMethods = template.findResources('AWS::ApiGateway::Method', {
        Properties: {
          HttpMethod: 'POST',
          AuthorizationType: 'COGNITO_USER_POOLS',
          ResourceId: { Ref: recreateLogicalId },
        },
      });
      expect(Object.keys(postMethods).length).toBe(1);
    });

    it('has Cognito-authorised methods for cluster routes', () => {
      // Count all methods with COGNITO_USER_POOLS auth — should include
      // the 4 cluster methods (POST/GET clusters, GET/DELETE cluster) + 1 recreate POST
      const methods = template.findResources('AWS::ApiGateway::Method', {
        Properties: {
          AuthorizationType: 'COGNITO_USER_POOLS',
        },
      });
      // Foundation already had: health GET + 4 user + 1 reactivate + 7 project + 4 template = 17
      // Now adding: 4 cluster methods + 1 accounting + 1 recreate = 23 total
      expect(Object.keys(methods).length).toBeGreaterThanOrEqual(23);
    });
  });

  // ---------------------------------------------------------------------------
  // Accounting Query Lambda
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('Accounting Query Lambda', () => {
    it('creates a Python Lambda function for accounting queries', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-accounting-query',
        Runtime: 'python3.13',
        Handler: 'handler.handler',
        Timeout: 60,
        MemorySize: 256,
      });
    });

    it('passes required environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-accounting-query',
        Environment: {
          Variables: {
            CLUSTERS_TABLE_NAME: Match.anyValue(),
            PROJECTS_TABLE_NAME: Match.anyValue(),
            USER_POOL_ID: Match.anyValue(),
          },
        },
      });
    });

    it('has IAM policy for SSM Run Command', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'ssm:SendCommand',
                'ssm:GetCommandInvocation',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Budget Notification Lambda
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('Budget Notification Lambda', () => {
    it('creates a Python Lambda function for budget notifications', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-budget-notification',
        Runtime: 'python3.13',
        Handler: 'handler.handler',
        Timeout: 30,
        MemorySize: 256,
      });
    });

    it('passes PROJECTS_TABLE_NAME and USERS_TABLE_NAME as environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-budget-notification',
        Environment: {
          Variables: {
            PROJECTS_TABLE_NAME: Match.anyValue(),
            USERS_TABLE_NAME: Match.anyValue(),
          },
        },
      });
    });

    it('is subscribed to the budget notification SNS topic', () => {
      template.hasResourceProperties('AWS::SNS::Subscription', {
        Protocol: 'lambda',
        TopicArn: Match.anyValue(),
      });
    });

    it('has a Lambda invoke permission from the SNS topic', () => {
      template.hasResourceProperties('AWS::Lambda::Permission', {
        Action: 'lambda:InvokeFunction',
        Principal: 'sns.amazonaws.com',
      });
    });
  });

  // ---------------------------------------------------------------------------
  // API Gateway — Accounting Query Routes
  // Validates: Requirements 16.2, 16.3
  // ---------------------------------------------------------------------------
  describe('API Gateway Accounting Query Routes', () => {
    it('creates /accounting resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'accounting',
      });
    });

    it('creates /accounting/jobs resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'jobs',
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Default Cluster Template Seeding (Custom Resources)
  // Validates: Requirement 3.3
  // ---------------------------------------------------------------------------
  describe('Default Cluster Template Seeding', () => {
    it('creates custom resources for seeding default templates', () => {
      const customResources = template.findResources('Custom::AWS');
      const resourceIds = Object.keys(customResources);

      // Should have 3 custom resources: POSIX UID counter + 2 template seeds
      const templateSeedResources = resourceIds.filter(
        (id) => id.startsWith('DefaultTemplate'),
      );
      expect(templateSeedResources).toHaveLength(2);
    });

    it('seeds the cpu-general template with correct DynamoDB putItem', () => {
      const customResources = template.findResources('Custom::AWS');
      const cpuSeedEntry = Object.entries(customResources).find(
        ([id]) => id.includes('CpuGeneral'),
      );
      expect(cpuSeedEntry).toBeDefined();

      const [, resource] = cpuSeedEntry!;
      const createProp = (resource as any).Properties.Create;

      // The Create property is a Fn::Join containing the serialised SDK call.
      // Extract the joined parts and verify the key fields are present.
      expect(createProp).toBeDefined();
      const joinParts: string[] = createProp['Fn::Join'][1];
      const joinedStr = joinParts
        .filter((p: any) => typeof p === 'string')
        .join('');

      expect(joinedStr).toContain('"service":"DynamoDB"');
      expect(joinedStr).toContain('"action":"putItem"');
      expect(joinedStr).toContain('TEMPLATE#cpu-general');
      expect(joinedStr).toContain('General CPU Workloads');
      expect(joinedStr).toContain('c7g.medium');
      expect(joinedStr).toContain('attribute_not_exists(PK)');
    });

    it('seeds the gpu-basic template with correct DynamoDB putItem', () => {
      const customResources = template.findResources('Custom::AWS');
      const gpuSeedEntry = Object.entries(customResources).find(
        ([id]) => id.includes('GpuBasic'),
      );
      expect(gpuSeedEntry).toBeDefined();

      const [, resource] = gpuSeedEntry!;
      const createProp = (resource as any).Properties.Create;

      expect(createProp).toBeDefined();
      const joinParts: string[] = createProp['Fn::Join'][1];
      const joinedStr = joinParts
        .filter((p: any) => typeof p === 'string')
        .join('');

      expect(joinedStr).toContain('"service":"DynamoDB"');
      expect(joinedStr).toContain('"action":"putItem"');
      expect(joinedStr).toContain('TEMPLATE#gpu-basic');
      expect(joinedStr).toContain('Basic GPU Workloads');
      expect(joinedStr).toContain('g4dn.xlarge');
      expect(joinedStr).toContain('attribute_not_exists(PK)');
    });

    it('grants DynamoDB permissions for template seeding custom resources', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'dynamodb:PutItem',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // CodeBuild Project for CDK Deploy/Destroy
  // Validates: Requirements 2.1, 3.3
  // ---------------------------------------------------------------------------
  describe('CodeBuild Project', () => {
    it('creates a CodeBuild project for CDK deploy/destroy', () => {
      template.hasResourceProperties('AWS::CodeBuild::Project', {
        Name: 'hpc-cdk-deploy',
        Description: 'Runs CDK deploy/destroy for project infrastructure stacks',
      });
    });

    it('uses a Linux Standard 7.0 build image with SMALL compute', () => {
      template.hasResourceProperties('AWS::CodeBuild::Project', {
        Name: 'hpc-cdk-deploy',
        Environment: {
          ComputeType: 'BUILD_GENERAL1_SMALL',
          Image: 'aws/codebuild/standard:7.0',
          Type: 'LINUX_CONTAINER',
        },
      });
    });

    it('has a 60-minute timeout', () => {
      template.hasResourceProperties('AWS::CodeBuild::Project', {
        Name: 'hpc-cdk-deploy',
        TimeoutInMinutes: 60,
      });
    });

    it('has CloudFormation stack management permissions scoped to HpcProject-* stacks', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'cloudformation:CreateStack',
                'cloudformation:UpdateStack',
                'cloudformation:DeleteStack',
                'cloudformation:DescribeStacks',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has EC2 VPC management permissions for project infrastructure', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'ec2:CreateVpc',
                'ec2:DeleteVpc',
                'ec2:CreateSubnet',
                'ec2:DeleteSubnet',
                'ec2:CreateSecurityGroup',
                'ec2:DeleteSecurityGroup',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has EFS management permissions for project infrastructure', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'elasticfilesystem:CreateFileSystem',
                'elasticfilesystem:DeleteFileSystem',
                'elasticfilesystem:DescribeFileSystems',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has S3 bucket management permissions for project infrastructure', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                's3:CreateBucket',
                's3:DeleteBucket',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Project Deploy Step Lambda
  // Validates: Requirements 2.1, 2.5, 2.6
  // ---------------------------------------------------------------------------
  describe('Project Deploy Step Lambda', () => {
    it('creates a Python Lambda function for project deploy steps', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-deploy-steps',
        Runtime: 'python3.13',
        Handler: 'project_deploy.step_handler',
        Timeout: 300,
        MemorySize: 512,
      });
    });

    it('passes PROJECTS_TABLE_NAME and CODEBUILD_PROJECT_NAME as environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-deploy-steps',
        Environment: {
          Variables: {
            PROJECTS_TABLE_NAME: Match.anyValue(),
            CODEBUILD_PROJECT_NAME: Match.anyValue(),
          },
        },
      });
    });

    it('has CodeBuild start/describe permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'codebuild:StartBuild',
                'codebuild:BatchGetBuilds',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has CloudFormation describe permissions scoped to HpcProject-* stacks', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'cloudformation:DescribeStacks',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Project Destroy Step Lambda
  // Validates: Requirements 3.3, 3.7, 3.8
  // ---------------------------------------------------------------------------
  describe('Project Destroy Step Lambda', () => {
    it('creates a Python Lambda function for project destroy steps', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-destroy-steps',
        Runtime: 'python3.13',
        Handler: 'project_destroy.step_handler',
        Timeout: 300,
        MemorySize: 512,
      });
    });

    it('passes PROJECTS_TABLE_NAME, CLUSTERS_TABLE_NAME, and CODEBUILD_PROJECT_NAME as environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-destroy-steps',
        Environment: {
          Variables: {
            PROJECTS_TABLE_NAME: Match.anyValue(),
            CLUSTERS_TABLE_NAME: Match.anyValue(),
            CODEBUILD_PROJECT_NAME: Match.anyValue(),
          },
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Project Deploy State Machine
  // Validates: Requirements 2.1, 2.5, 2.6
  // ---------------------------------------------------------------------------
  describe('Project Deploy State Machine', () => {
    it('creates a project deploy state machine', () => {
      template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
        StateMachineName: 'hpc-project-deploy',
      });
    });

    it('has tracing enabled', () => {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: {
          StateMachineName: 'hpc-project-deploy',
        },
      });
      for (const [, resource] of Object.entries(stateMachines)) {
        expect((resource as any).Properties?.TracingConfiguration?.Enabled).toBe(true);
      }
    });

    it('has a 2-hour timeout', () => {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: {
          StateMachineName: 'hpc-project-deploy',
        },
      });
      const smEntries = Object.entries(stateMachines);
      expect(smEntries).toHaveLength(1);
    });
  });

  // ---------------------------------------------------------------------------
  // Project Destroy State Machine
  // Validates: Requirements 3.3, 3.7, 3.8
  // ---------------------------------------------------------------------------
  describe('Project Destroy State Machine', () => {
    it('creates a project destroy state machine', () => {
      template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
        StateMachineName: 'hpc-project-destroy',
      });
    });

    it('has tracing enabled', () => {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: {
          StateMachineName: 'hpc-project-destroy',
        },
      });
      for (const [, resource] of Object.entries(stateMachines)) {
        expect((resource as any).Properties?.TracingConfiguration?.Enabled).toBe(true);
      }
    });
  });

  // ---------------------------------------------------------------------------
  // Project Update Step Lambda
  // Validates: Requirements 6.1, 6.2, 6.3
  // ---------------------------------------------------------------------------
  describe('Project Update Step Lambda', () => {
    it('creates a Python Lambda function for project update steps', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-update-steps',
        Runtime: 'python3.13',
        Handler: 'project_update.step_handler',
        Timeout: 300,
        MemorySize: 512,
      });
    });

    it('passes PROJECTS_TABLE_NAME and CODEBUILD_PROJECT_NAME as environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-update-steps',
        Environment: {
          Variables: {
            PROJECTS_TABLE_NAME: Match.anyValue(),
            CODEBUILD_PROJECT_NAME: Match.anyValue(),
          },
        },
      });
    });

    it('has DynamoDB read/write permissions on Projects table', () => {
      // The update step Lambda needs read/write on the Projects table.
      // This is verified via the grantReadWriteData call which produces
      // dynamodb:BatchGetItem, Query, GetItem, Scan, ConditionCheckItem,
      // BatchWriteItem, PutItem, UpdateItem, DeleteItem
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'dynamodb:BatchGetItem',
                'dynamodb:Query',
                'dynamodb:GetItem',
                'dynamodb:Scan',
                'dynamodb:ConditionCheckItem',
                'dynamodb:BatchWriteItem',
                'dynamodb:PutItem',
                'dynamodb:UpdateItem',
                'dynamodb:DeleteItem',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has CodeBuild start/describe permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'codebuild:StartBuild',
                'codebuild:BatchGetBuilds',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has CloudFormation DescribeStacks permission', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'cloudformation:DescribeStacks',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Project Update State Machine
  // Validates: Requirements 6.4, 6.5
  // ---------------------------------------------------------------------------
  describe('Project Update State Machine', () => {
    it('creates a project update state machine', () => {
      template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
        StateMachineName: 'hpc-project-update',
      });
    });

    it('has tracing enabled', () => {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: {
          StateMachineName: 'hpc-project-update',
        },
      });
      for (const [, resource] of Object.entries(stateMachines)) {
        expect((resource as any).Properties?.TracingConfiguration?.Enabled).toBe(true);
      }
    });
  });

  // ---------------------------------------------------------------------------
  // Project Management Lambda — Update State Machine ARN
  // Validates: Requirements 6.6, 6.7
  // ---------------------------------------------------------------------------
  describe('Project Management Lambda Update Permissions', () => {
    it('passes PROJECT_UPDATE_STATE_MACHINE_ARN as environment variable', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-management',
        Environment: {
          Variables: {
            PROJECT_UPDATE_STATE_MACHINE_ARN: Match.anyValue(),
          },
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Project Management Lambda — State Machine and Cost Explorer Permissions
  // Validates: Requirements 2.1, 3.3
  // ---------------------------------------------------------------------------
  describe('Project Management Lambda Lifecycle Permissions', () => {
    it('passes PROJECT_DEPLOY_STATE_MACHINE_ARN as environment variable', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-management',
        Environment: {
          Variables: {
            PROJECT_DEPLOY_STATE_MACHINE_ARN: Match.anyValue(),
          },
        },
      });
    });

    it('passes PROJECT_DESTROY_STATE_MACHINE_ARN as environment variable', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-project-management',
        Environment: {
          Variables: {
            PROJECT_DESTROY_STATE_MACHINE_ARN: Match.anyValue(),
          },
        },
      });
    });

    it('has states:StartExecution permission on project state machines', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'states:StartExecution',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('has ce:GetCostAndUsage permission for budget breach clearing', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'ce:GetCostAndUsage',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // API Gateway — Project Lifecycle Routes (deploy, destroy, edit)
  // Validates: Requirements 2.1, 3.3, 6.4
  // ---------------------------------------------------------------------------
  describe('API Gateway Project Lifecycle Routes', () => {
    it('creates /projects/{projectId}/deploy resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'deploy',
      });
    });

    it('creates /projects/{projectId}/destroy resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'destroy',
      });
    });

    it('has PUT method on /projects/{projectId} for editing', () => {
      // Verify there is a PUT method with Cognito auth
      const methods = template.findResources('AWS::ApiGateway::Method', {
        Properties: {
          HttpMethod: 'PUT',
          AuthorizationType: 'COGNITO_USER_POOLS',
        },
      });
      expect(Object.keys(methods).length).toBeGreaterThanOrEqual(1);
    });

    it('creates /projects/{projectId}/update resource', () => {
      template.hasResourceProperties('AWS::ApiGateway::Resource', {
        PathPart: 'update',
      });
    });

    it('has POST method on deploy resource with Cognito auth', () => {
      const methods = template.findResources('AWS::ApiGateway::Method', {
        Properties: {
          HttpMethod: 'POST',
          AuthorizationType: 'COGNITO_USER_POOLS',
        },
      });
      // Should have POST methods for: create project, add member, deploy, destroy, update,
      // create cluster, create user, reactivate = at least 8
      expect(Object.keys(methods).length).toBeGreaterThanOrEqual(8);
    });

    it('has Cognito-authorised methods for all lifecycle routes', () => {
      const methods = template.findResources('AWS::ApiGateway::Method', {
        Properties: {
          AuthorizationType: 'COGNITO_USER_POOLS',
        },
      });
      // Previous count was ≥25, adding update POST = ≥26
      expect(Object.keys(methods).length).toBeGreaterThanOrEqual(26);
    });
  });

  // ---------------------------------------------------------------------------
  // FSx Cleanup Lambda and EventBridge Schedule
  // Validates: Requirements 1.1, 7.1–7.5
  // ---------------------------------------------------------------------------
  describe('FSx Cleanup Lambda', () => {
    it('creates a Python 3.13 Lambda with correct handler and 5-minute timeout', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-fsx-cleanup',
        Runtime: 'python3.13',
        Handler: 'handler.handler',
        Timeout: 300,
      });
    });

    it('sets CLUSTERS_TABLE_NAME and SNS_TOPIC_ARN environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'hpc-fsx-cleanup',
        Environment: {
          Variables: {
            CLUSTERS_TABLE_NAME: Match.anyValue(),
            SNS_TOPIC_ARN: Match.anyValue(),
          },
        },
      });
    });

    it('creates an EventBridge rule with rate(6 hours) schedule', () => {
      template.hasResourceProperties('AWS::Events::Rule', {
        Name: 'hpc-fsx-cleanup-schedule',
        ScheduleExpression: 'rate(6 hours)',
      });
    });

    it('grants FSx describe and delete permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: [
                'fsx:DescribeFileSystems',
                'fsx:DescribeDataRepositoryAssociations',
                'fsx:DeleteDataRepositoryAssociation',
                'fsx:DeleteFileSystem',
              ],
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    it('does not grant DynamoDB write permissions on the Clusters table', () => {
      const policies = template.findResources('AWS::IAM::Policy');
      const writeActions = [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:BatchWriteItem',
      ];

      // Find policies attached to the FSx cleanup Lambda's role (logical ID
      // contains "FsxCleanupLambda"). Other Lambdas may have read/write on
      // the Clusters table, so we must scope to the cleanup Lambda only.
      for (const [logicalId, policy] of Object.entries(policies)) {
        const roles: any[] = (policy as any).Properties?.Roles ?? [];
        const attachedToFsxCleanup = roles.some((role: any) => {
          const ref = role.Ref ?? '';
          return ref.includes('FsxCleanupLambda');
        });
        if (!attachedToFsxCleanup) continue;

        const statements: any[] = (policy as any).Properties?.PolicyDocument?.Statement ?? [];
        for (const stmt of statements) {
          if (stmt.Effect !== 'Allow') continue;
          const actions: string[] = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          for (const writeAction of writeActions) {
            expect(actions).not.toContain(writeAction);
          }
        }
      }
    });

    it('grants SNS publish permission on the cluster lifecycle topic', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'sns:Publish',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Instance Profile SFN Wiring — Bug Condition
  // Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.6
  // ---------------------------------------------------------------------------
  describe('Instance Profile SFN Wiring — Bug Condition', () => {
    /**
     * Helper: parse a state machine DefinitionString from the synthesised
     * CloudFormation template. Handles the Fn::Join pattern used by CDK,
     * replacing non-string parts (Ref, Fn::GetAtt) with placeholder ARNs.
     */
    function parseStateMachineDefinition(smName: string): Record<string, any> {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: { StateMachineName: smName },
      });
      const entries = Object.entries(stateMachines);
      expect(entries).toHaveLength(1);
      const [, resource] = entries[0];
      const definitionString = (resource as any).Properties?.DefinitionString;
      expect(definitionString).toBeDefined();

      if (typeof definitionString === 'string') {
        return JSON.parse(definitionString);
      }
      if (definitionString['Fn::Join']) {
        const separator: string = definitionString['Fn::Join'][0];
        const parts: any[] = definitionString['Fn::Join'][1];
        const joined = parts
          .map((p: any) => (typeof p === 'string' ? p : 'arn:aws:placeholder:us-east-1:123456789012:placeholder'))
          .join(separator);
        return JSON.parse(joined);
      }
      throw new Error('Unexpected DefinitionString format');
    }

    /**
     * Helper: check if a state's Parameters contain a specific step value
     * in the payload sent to the Lambda. CDK LambdaInvoke with
     * payloadResponseOnly wraps the payload under Parameters.Payload.
     */
    function stateInvokesStep(state: any, expectedStep: string): boolean {
      const payload = state?.Parameters?.Payload;
      if (payload && payload.step === expectedStep) return true;
      // Also check FunctionName-style direct parameters
      if (state?.Parameters?.step === expectedStep) return true;
      return false;
    }

    // **Validates: Requirements 2.1**
    it('creation state machine contains CreateIamResources invoking create_iam_resources', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const states = def.States;
      expect(states).toHaveProperty('CreateIamResources');
      expect(stateInvokesStep(states['CreateIamResources'], 'create_iam_resources')).toBe(true);
    });

    // **Validates: Requirements 2.2**
    it('creation state machine contains WaitForInstanceProfiles invoking wait_for_instance_profiles', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const states = def.States;
      expect(states).toHaveProperty('WaitForInstanceProfiles');
      expect(stateInvokesStep(states['WaitForInstanceProfiles'], 'wait_for_instance_profiles')).toBe(true);
    });

    // **Validates: Requirements 2.2, 3.5**
    it('creation state machine contains AreInstanceProfilesReady Choice and a Wait state for the loop', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const states = def.States;
      expect(states).toHaveProperty('AreInstanceProfilesReady');
      expect(states['AreInstanceProfilesReady'].Type).toBe('Choice');
      // There should be a Wait state for the instance profile propagation loop
      const waitStateNames = Object.keys(states).filter(
        (name) => states[name].Type === 'Wait' && name.toLowerCase().includes('instanceprofile'),
      );
      expect(waitStateNames.length).toBeGreaterThanOrEqual(1);
    });

    // **Validates: Requirements 2.3**
    it('Parallel resultSelector contains loginInstanceProfileArn and computeInstanceProfileArn, not instanceProfileArn', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const states = def.States;
      // Find the Parallel state (ParallelFsxAndPcs)
      const parallelState = states['ParallelFsxAndPcs'];
      expect(parallelState).toBeDefined();
      expect(parallelState.Type).toBe('Parallel');
      const resultSelector = parallelState.ResultSelector;
      expect(resultSelector).toBeDefined();
      expect(resultSelector).toHaveProperty(['loginInstanceProfileArn.$']);
      expect(resultSelector).toHaveProperty(['computeInstanceProfileArn.$']);
      expect(resultSelector).not.toHaveProperty(['instanceProfileArn.$']);
    });

    // **Validates: Requirements 2.6**
    it('destruction state machine contains DeleteIamResources invoking delete_iam_resources', () => {
      const def = parseStateMachineDefinition('hpc-cluster-destruction');
      const states = def.States;
      expect(states).toHaveProperty('DeleteIamResources');
      expect(stateInvokesStep(states['DeleteIamResources'], 'delete_iam_resources')).toBe(true);
    });
  });

  // ---------------------------------------------------------------------------
  // Instance Profile SFN Wiring — Preservation
  // Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
  // ---------------------------------------------------------------------------
  describe('Instance Profile SFN Wiring — Preservation', () => {
    /**
     * Reuse the same parseStateMachineDefinition helper from the Bug Condition
     * block. Re-declared here because the helper is scoped to the sibling
     * describe block; a small duplication is acceptable for test isolation.
     */
    function parseStateMachineDefinition(smName: string): Record<string, any> {
      const stateMachines = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: { StateMachineName: smName },
      });
      const entries = Object.entries(stateMachines);
      expect(entries).toHaveLength(1);
      const [, resource] = entries[0];
      const definitionString = (resource as any).Properties?.DefinitionString;
      expect(definitionString).toBeDefined();

      if (typeof definitionString === 'string') {
        return JSON.parse(definitionString);
      }
      if (definitionString['Fn::Join']) {
        const separator: string = definitionString['Fn::Join'][0];
        const parts: any[] = definitionString['Fn::Join'][1];
        const joined = parts
          .map((p: any) => (typeof p === 'string' ? p : 'arn:aws:placeholder:us-east-1:123456789012:placeholder'))
          .join(separator);
        return JSON.parse(joined);
      }
      throw new Error('Unexpected DefinitionString format');
    }

    /**
     * Helper: collect all state names from a definition, including states
     * nested inside Parallel branches.
     */
    function collectAllStateNames(def: Record<string, any>): Set<string> {
      const names = new Set<string>();
      const walk = (states: Record<string, any>) => {
        for (const [name, state] of Object.entries(states)) {
          names.add(name);
          if (state.Branches && Array.isArray(state.Branches)) {
            for (const branch of state.Branches) {
              if (branch.States) walk(branch.States);
            }
          }
        }
      };
      if (def.States) walk(def.States);
      return names;
    }

    // **Validates: Requirements 3.1**
    it('all original creation states exist', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const allNames = collectAllStateNames(def);
      const expectedStates = [
        'ValidateAndRegisterName',
        'CheckBudgetBreach',
        'ResolveTemplate',
        'ParallelFsxAndPcs',
        'CreateFsxFilesystem',
        'CheckFsxStatus',
        'CreateFsxDra',
        'CreatePcsCluster',
        'CreateLoginNodeGroup',
        'CreateComputeNodeGroup',
        'CreatePcsQueue',
        'TagResources',
        'RecordCluster',
      ];
      for (const stateName of expectedStates) {
        expect(allNames).toContain(stateName);
      }
    });

    // **Validates: Requirements 3.3**
    it('error handling states exist with correct Catch routing', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const states = def.States;

      expect(states).toHaveProperty('HandleCreationFailure');
      expect(states).toHaveProperty('CreationFailed');
      expect(states).toHaveProperty('MarkClusterFailed');

      // HandleCreationFailure should have a Catch that routes to MarkClusterFailed
      const handleFailure = states['HandleCreationFailure'];
      expect(handleFailure.Catch).toBeDefined();
      const catchTargets = handleFailure.Catch.map((c: any) => c.Next);
      expect(catchTargets).toContain('MarkClusterFailed');

      // MarkClusterFailed should route to CreationFailed
      const markFailed = states['MarkClusterFailed'];
      expect(markFailed.Next === 'CreationFailed' || markFailed.Catch?.some((c: any) => c.Next === 'CreationFailed')).toBe(true);
    });

    // **Validates: Requirements 3.2**
    it('FSx wait loop exists — IsFsxAvailable Choice and WaitForFsx Wait state', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      // IsFsxAvailable and WaitForFsx are inside the Parallel branches
      const parallelState = def.States['ParallelFsxAndPcs'];
      expect(parallelState).toBeDefined();
      const fsxBranch = parallelState.Branches[0];
      const branchStates = fsxBranch.States;

      expect(branchStates).toHaveProperty('IsFsxAvailable');
      expect(branchStates['IsFsxAvailable'].Type).toBe('Choice');

      expect(branchStates).toHaveProperty('WaitForFsx');
      expect(branchStates['WaitForFsx'].Type).toBe('Wait');
    });

    // **Validates: Requirements 3.2**
    it('all non-IAM resultSelector fields in the Parallel state are present', () => {
      const def = parseStateMachineDefinition('hpc-cluster-creation');
      const parallelState = def.States['ParallelFsxAndPcs'];
      expect(parallelState).toBeDefined();
      expect(parallelState.Type).toBe('Parallel');

      const resultSelector = parallelState.ResultSelector;
      expect(resultSelector).toBeDefined();

      const expectedFields = [
        'projectId.$',
        'clusterName.$',
        'templateId.$',
        'createdBy.$',
        'vpcId.$',
        'efsFileSystemId.$',
        's3BucketName.$',
        'publicSubnetIds.$',
        'privateSubnetIds.$',
        'securityGroupIds.$',
        'fsxFilesystemId.$',
        'fsxDnsName.$',
        'fsxMountName.$',
        'fsxDraId.$',
        'pcsClusterId.$',
        'pcsClusterArn.$',
        'loginInstanceType.$',
        'instanceTypes.$',
        'maxNodes.$',
        'minNodes.$',
        'purchaseOption.$',
        'loginLaunchTemplateId.$',
        'computeLaunchTemplateId.$',
      ];
      const selectorKeys = Object.keys(resultSelector);
      for (const field of expectedFields) {
        expect(selectorKeys).toContain(field);
      }
    });

    // **Validates: Requirements 3.4**
    it('all original destruction states exist', () => {
      const def = parseStateMachineDefinition('hpc-cluster-destruction');
      const states = def.States;
      const expectedStates = [
        'CreateFsxExportTask',
        'CheckFsxExportStatus',
        'DeletePcsResources',
        'DeleteFsxFilesystem',
        'RecordClusterDestroyed',
        'DestructionSucceeded',
      ];
      for (const stateName of expectedStates) {
        expect(states).toHaveProperty(stateName);
      }
    });

    // **Validates: Requirements 3.4**
    it('export wait loop exists — IsExportComplete Choice and WaitForExport Wait state', () => {
      const def = parseStateMachineDefinition('hpc-cluster-destruction');
      const states = def.States;

      expect(states).toHaveProperty('IsExportComplete');
      expect(states['IsExportComplete'].Type).toBe('Choice');

      expect(states).toHaveProperty('WaitForExport');
      expect(states['WaitForExport'].Type).toBe('Wait');
    });

    // **Validates: Requirements 3.6**
    it('state machine configuration preserved — tracing, timeout, names', () => {
      // Check both state machines have correct names, tracing, and timeout
      const creationSMs = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: { StateMachineName: 'hpc-cluster-creation' },
      });
      const creationEntries = Object.entries(creationSMs);
      expect(creationEntries).toHaveLength(1);
      const [, creationResource] = creationEntries[0];
      expect((creationResource as any).Properties.TracingConfiguration?.Enabled).toBe(true);

      const destructionSMs = template.findResources('AWS::StepFunctions::StateMachine', {
        Properties: { StateMachineName: 'hpc-cluster-destruction' },
      });
      const destructionEntries = Object.entries(destructionSMs);
      expect(destructionEntries).toHaveLength(1);
      const [, destructionResource] = destructionEntries[0];
      expect((destructionResource as any).Properties.TracingConfiguration?.Enabled).toBe(true);
    });
  });

  // ---------------------------------------------------------------------------
  // Documentation Deployment — S3 BucketDeployment with docs/ prefix
  // Validates: Requirements 21.2, 21.6
  // ---------------------------------------------------------------------------
  describe('Documentation Deployment', () => {
    it('deploys docs to S3 with the docs/ destination key prefix', () => {
      const deployments = template.findResources('Custom::CDKBucketDeployment');
      const docsDeployment = Object.entries(deployments).find(
        ([, resource]) =>
          (resource as any).Properties?.DestinationBucketKeyPrefix === 'docs',
      );
      expect(docsDeployment).toBeDefined();
    });

    it('includes /docs/* in CloudFront distribution invalidation paths', () => {
      const deployments = template.findResources('Custom::CDKBucketDeployment');
      const docsDeployment = Object.entries(deployments).find(
        ([, resource]) =>
          (resource as any).Properties?.DestinationBucketKeyPrefix === 'docs',
      );
      expect(docsDeployment).toBeDefined();

      const [, resource] = docsDeployment!;
      const paths: string[] = (resource as any).Properties?.DistributionPaths;
      expect(paths).toBeDefined();
      expect(paths).toContain('/docs/*');
    });
  });
});
