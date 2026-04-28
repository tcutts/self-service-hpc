import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

/**
 * NotificationTopics — SNS topics for budget and cluster lifecycle notifications.
 */
export class NotificationTopics extends Construct {
  /** SNS topic for budget notifications. */
  public readonly budgetNotificationTopic: sns.Topic;
  /** SNS topic for cluster lifecycle notifications. */
  public readonly clusterLifecycleNotificationTopic: sns.Topic;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.budgetNotificationTopic = new sns.Topic(this, 'BudgetNotificationTopic', {
      topicName: 'hpc-budget-notifications',
      displayName: 'HPC Platform Budget Notifications',
    });

    this.clusterLifecycleNotificationTopic = new sns.Topic(this, 'ClusterLifecycleNotificationTopic', {
      topicName: 'hpc-cluster-lifecycle-notifications',
      displayName: 'HPC Cluster Lifecycle Notifications',
    });
  }
}
