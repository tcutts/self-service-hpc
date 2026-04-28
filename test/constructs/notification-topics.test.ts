import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { NotificationTopics } from '../../lib/constructs/notification-topics';

describe('NotificationTopics', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    new NotificationTopics(stack, 'NotificationTopics');
    template = Template.fromStack(stack);
  });

  it('creates exactly 2 SNS topics', () => {
    template.resourceCountIs('AWS::SNS::Topic', 2);
  });

  it('creates the budget notification topic with correct name and display name', () => {
    template.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: 'hpc-budget-notifications',
      DisplayName: 'HPC Platform Budget Notifications',
    });
  });

  it('creates the cluster lifecycle notification topic with correct name and display name', () => {
    template.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: 'hpc-cluster-lifecycle-notifications',
      DisplayName: 'HPC Cluster Lifecycle Notifications',
    });
  });
});
