import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

/**
 * Encapsulates all DynamoDB tables, GSIs, and seed data for the HPC platform.
 */
export class DatabaseTables extends Construct {
  public readonly platformUsersTable: dynamodb.Table;
  public readonly projectsTable: dynamodb.Table;
  public readonly clusterTemplatesTable: dynamodb.Table;
  public readonly clustersTable: dynamodb.Table;
  public readonly clusterNameRegistryTable: dynamodb.Table;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // PlatformUsers table
    this.platformUsersTable = new dynamodb.Table(this, 'PlatformUsersTable', {
      tableName: 'PlatformUsers',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: StatusIndex — status (PK), userId (SK)
    this.platformUsersTable.addGlobalSecondaryIndex({
      indexName: 'StatusIndex',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Seed the POSIX UID atomic counter item (PK=COUNTER, SK=POSIX_UID, currentValue=10000)
    // Seed custom resources are created on `scope` (the parent stack) rather
    // than `this` so that their CloudFormation logical IDs are unchanged from
    // the original monolithic FoundationStack, avoiding resource replacement.
    new cr.AwsCustomResource(scope, 'PosixUidCounterSeed', {
      onCreate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.platformUsersTable.tableName,
          Item: {
            PK: { S: 'COUNTER' },
            SK: { S: 'POSIX_UID' },
            currentValue: { N: '10000' },
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        },
        physicalResourceId: cr.PhysicalResourceId.of('PosixUidCounterSeed'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.platformUsersTable.tableArn],
      }),
    });

    // Projects table
    this.projectsTable = new dynamodb.Table(this, 'ProjectsTable', {
      tableName: 'Projects',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: UserProjectsIndex — userId (PK), projectId (SK)
    this.projectsTable.addGlobalSecondaryIndex({
      indexName: 'UserProjectsIndex',
      partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'projectId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ClusterTemplates table
    this.clusterTemplatesTable = new dynamodb.Table(this, 'ClusterTemplatesTable', {
      tableName: 'ClusterTemplates',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Seed default cluster template: cpu-general
    const seedTimestamp = new Date().toISOString();
    new cr.AwsCustomResource(scope, 'DefaultTemplateCpuGeneralSeed', {
      onCreate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.clusterTemplatesTable.tableName,
          Item: {
            PK: { S: 'TEMPLATE#cpu-general' },
            SK: { S: 'METADATA' },
            templateId: { S: 'cpu-general' },
            templateName: { S: 'General CPU Workloads' },
            description: { S: 'Cost-effective CPU cluster template suitable for general HPC workloads. Uses Graviton-based c7g.medium instances.' },
            instanceTypes: { L: [{ S: 'c7g.medium' }] },
            loginInstanceType: { S: 'c7g.medium' },
            minNodes: { N: '1' },
            maxNodes: { N: '10' },
            amiId: { S: 'ami-placeholder-cpu' },
            softwareStack: { M: { scheduler: { S: 'slurm' }, schedulerVersion: { S: '24.11' } } },
            createdAt: { S: seedTimestamp },
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        },
        physicalResourceId: cr.PhysicalResourceId.of('DefaultTemplateCpuGeneralSeed'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.clusterTemplatesTable.tableArn],
      }),
    });

    // Seed default cluster template: gpu-basic
    new cr.AwsCustomResource(scope, 'DefaultTemplateGpuBasicSeed', {
      onCreate: {
        service: 'DynamoDB',
        action: 'putItem',
        parameters: {
          TableName: this.clusterTemplatesTable.tableName,
          Item: {
            PK: { S: 'TEMPLATE#gpu-basic' },
            SK: { S: 'METADATA' },
            templateId: { S: 'gpu-basic' },
            templateName: { S: 'Basic GPU Workloads' },
            description: { S: 'Basic GPU cluster template suitable for introductory GPU workloads. Uses NVIDIA T4-based g4dn.xlarge instances.' },
            instanceTypes: { L: [{ S: 'g4dn.xlarge' }] },
            loginInstanceType: { S: 'g4dn.xlarge' },
            minNodes: { N: '1' },
            maxNodes: { N: '4' },
            amiId: { S: 'ami-placeholder-gpu' },
            softwareStack: { M: { scheduler: { S: 'slurm' }, schedulerVersion: { S: '24.11' }, cudaVersion: { S: '12.4' } } },
            createdAt: { S: seedTimestamp },
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        },
        physicalResourceId: cr.PhysicalResourceId.of('DefaultTemplateGpuBasicSeed'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [this.clusterTemplatesTable.tableArn],
      }),
    });

    // Clusters table
    this.clustersTable = new dynamodb.Table(this, 'ClustersTable', {
      tableName: 'Clusters',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ClusterNameRegistry table
    this.clusterNameRegistryTable = new dynamodb.Table(this, 'ClusterNameRegistryTable', {
      tableName: 'ClusterNameRegistry',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
  }
}
