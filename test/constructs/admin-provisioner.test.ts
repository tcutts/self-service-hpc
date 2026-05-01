import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AdminProvisioner } from '../../lib/constructs/admin-provisioner';

describe('AdminProvisioner', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');

    const userPool = new cognito.UserPool(stack, 'TestUserPool');
    const platformUsersTable = new dynamodb.Table(stack, 'TestUsersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    });

    new AdminProvisioner(stack, 'AdminProvisioner', {
      platformUsersTable,
      userPool,
      adminEmail: 'test@example.com',
    });

    template = Template.fromStack(stack);
  });

  it('creates Lambda with Python 3.13 runtime', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'python3.13',
    });
  });

  it('configures Lambda with required environment variables', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          TABLE_NAME: Match.anyValue(),
          USER_POOL_ID: Match.anyValue(),
          ADMIN_EMAIL: 'test@example.com',
        }),
      },
    });
  });

  it('grants least-privilege IAM for DynamoDB and Cognito', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'dynamodb:Scan',
              'dynamodb:PutItem',
              'dynamodb:UpdateItem',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });

    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'cognito-idp:AdminCreateUser',
              'cognito-idp:AdminAddUserToGroup',
              'cognito-idp:AdminGetUser',
              'cognito-idp:AdminDeleteUser',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('creates AdminUserName and AdminUserPassword CfnOutputs', () => {
    const outputs = template.findOutputs('*');
    const outputKeys = Object.keys(outputs);

    const hasAdminUserName = outputKeys.some((key) => key.includes('AdminUserName'));
    const hasAdminUserPassword = outputKeys.some((key) => key.includes('AdminUserPassword'));

    expect(hasAdminUserName).toBe(true);
    expect(hasAdminUserPassword).toBe(true);
  });

  it('creates Custom::AdminProvisioner with ServiceToken referencing Lambda', () => {
    template.hasResourceProperties('Custom::AdminProvisioner', {
      ServiceToken: Match.anyValue(),
    });
  });

  it('throws when adminEmail is not provided', () => {
    expect(() => {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'FailStack');

      const userPool = new cognito.UserPool(stack, 'TestUserPool');
      const platformUsersTable = new dynamodb.Table(stack, 'TestUsersTable', {
        partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      });

      new AdminProvisioner(stack, 'AdminProvisioner', {
        platformUsersTable,
        userPool,
        adminEmail: '',
      });
    }).toThrow();
  });
});
