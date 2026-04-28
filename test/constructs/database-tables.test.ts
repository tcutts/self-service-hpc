import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { DatabaseTables } from '../../lib/constructs/database-tables';

describe('DatabaseTables', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    new DatabaseTables(stack, 'DatabaseTables');
    template = Template.fromStack(stack);
  });

  it('creates exactly 5 DynamoDB tables', () => {
    template.resourceCountIs('AWS::DynamoDB::Table', 5);
  });

  it('creates PlatformUsers table with correct key schema', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'PlatformUsers',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
    });
  });

  it('creates Projects table with correct key schema', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'Projects',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
    });
  });

  it('creates ClusterTemplates table with correct key schema', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ClusterTemplates',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
    });
  });

  it('creates Clusters table with correct key schema', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'Clusters',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
    });
  });

  it('creates ClusterNameRegistry table with correct key schema', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ClusterNameRegistry',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
    });
  });

  it('configures StatusIndex GSI on PlatformUsers', () => {
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

  it('configures UserProjectsIndex GSI on Projects', () => {
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

  it('uses PAY_PER_REQUEST billing on all tables', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.BillingMode).toBe('PAY_PER_REQUEST');
    }
  });

  it('enables PITR on all tables', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.PointInTimeRecoverySpecification).toEqual({
        PointInTimeRecoveryEnabled: true,
      });
    }
  });

  it('sets RETAIN removal policy on all tables', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).DeletionPolicy).toBe('Retain');
      expect((resource as any).UpdateReplacePolicy).toBe('Retain');
    }
  });

  it('creates 3 seed custom resources', () => {
    template.resourceCountIs('Custom::AWS', 3);
  });

  it('creates PosixUidCounterSeed custom resource', () => {
    const resources = template.findResources('Custom::AWS');
    const seedKeys = Object.keys(resources).filter((k) => k.includes('PosixUidCounterSeed'));
    expect(seedKeys).toHaveLength(1);
    // The Create property is an Fn::Join containing the DynamoDB putItem call
    const createProp = (resources[seedKeys[0]] as any).Properties.Create;
    expect(createProp).toBeDefined();
    // Verify the Fn::Join contains the expected seed data
    const joinParts = createProp['Fn::Join'][1];
    const joinedStr = joinParts.filter((p: any) => typeof p === 'string').join('');
    expect(joinedStr).toContain('"service":"DynamoDB"');
    expect(joinedStr).toContain('"action":"putItem"');
    expect(joinedStr).toContain('"PK":{"S":"COUNTER"}');
    expect(joinedStr).toContain('"SK":{"S":"POSIX_UID"}');
    expect(joinedStr).toContain('"currentValue":{"N":"10000"}');
  });

  it('creates DefaultTemplateCpuGeneralSeed custom resource', () => {
    const resources = template.findResources('Custom::AWS');
    const seedKeys = Object.keys(resources).filter((k) => k.includes('DefaultTemplateCpuGeneralSeed'));
    expect(seedKeys).toHaveLength(1);
    const createProp = (resources[seedKeys[0]] as any).Properties.Create;
    expect(createProp).toBeDefined();
    const joinParts = createProp['Fn::Join'][1];
    const joinedStr = joinParts.filter((p: any) => typeof p === 'string').join('');
    expect(joinedStr).toContain('"service":"DynamoDB"');
    expect(joinedStr).toContain('"action":"putItem"');
    expect(joinedStr).toContain('"PK":{"S":"TEMPLATE#cpu-general"}');
    expect(joinedStr).toContain('"templateId":{"S":"cpu-general"}');
  });

  it('creates DefaultTemplateGpuBasicSeed custom resource', () => {
    const resources = template.findResources('Custom::AWS');
    const seedKeys = Object.keys(resources).filter((k) => k.includes('DefaultTemplateGpuBasicSeed'));
    expect(seedKeys).toHaveLength(1);
    const createProp = (resources[seedKeys[0]] as any).Properties.Create;
    expect(createProp).toBeDefined();
    const joinParts = createProp['Fn::Join'][1];
    const joinedStr = joinParts.filter((p: any) => typeof p === 'string').join('');
    expect(joinedStr).toContain('"service":"DynamoDB"');
    expect(joinedStr).toContain('"action":"putItem"');
    expect(joinedStr).toContain('"PK":{"S":"TEMPLATE#gpu-basic"}');
    expect(joinedStr).toContain('"templateId":{"S":"gpu-basic"}');
  });
});
