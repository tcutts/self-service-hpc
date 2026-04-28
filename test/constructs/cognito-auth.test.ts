import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { CognitoAuth } from '../../lib/constructs/cognito-auth';

describe('CognitoAuth', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    new CognitoAuth(stack, 'CognitoAuth');
    template = Template.fromStack(stack);
  });

  it('creates exactly 1 UserPool', () => {
    template.resourceCountIs('AWS::Cognito::UserPool', 1);
  });

  it('creates exactly 1 UserPoolClient', () => {
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
  });

  it('creates exactly 1 CfnUserPoolGroup', () => {
    template.resourceCountIs('AWS::Cognito::UserPoolGroup', 1);
  });

  it('configures email sign-in', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      UsernameAttributes: ['email'],
      AutoVerifiedAttributes: ['email'],
    });
  });

  it('sets correct password policy', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: {
        PasswordPolicy: {
          MinimumLength: 12,
          RequireLowercase: true,
          RequireUppercase: true,
          RequireNumbers: true,
          RequireSymbols: true,
        },
      },
    });
  });

  it('sets RETAIN removal policy on the UserPool', () => {
    template.hasResource('AWS::Cognito::UserPool', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });
});
