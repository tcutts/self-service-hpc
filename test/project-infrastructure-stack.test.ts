import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ProjectInfrastructureStack } from '../lib/project-infrastructure-stack';

describe('ProjectInfrastructureStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new ProjectInfrastructureStack(app, 'TestProjectInfraStack', {
      projectId: 'test-project-001',
      projectName: 'Test Project',
      costAllocationTag: 'test-project-001',
      trustedCidrRanges: ['10.0.0.0/8', '172.16.0.0/12'],
    });
    template = Template.fromStack(stack);
  });

  // ---------------------------------------------------------------------------
  // Property 16: No open security groups
  // Validates: Requirements 15.2
  // ---------------------------------------------------------------------------
  describe('Property 16: No open security groups', () => {
    it('no security group ingress rule has source CIDR 0.0.0.0/0 or ::/0', () => {
      const OPEN_CIDRS = ['0.0.0.0/0', '::/0'];

      // Check inline ingress rules on AWS::EC2::SecurityGroup resources
      const securityGroups = template.findResources('AWS::EC2::SecurityGroup');
      for (const [logicalId, resource] of Object.entries(securityGroups)) {
        const ingressRules = (resource as any).Properties?.SecurityGroupIngress ?? [];
        for (const rule of ingressRules) {
          if (rule.CidrIp) {
            expect(OPEN_CIDRS).not.toContain(rule.CidrIp);
          }
          if (rule.CidrIpv6) {
            expect(OPEN_CIDRS).not.toContain(rule.CidrIpv6);
          }
        }
      }

      // Check standalone AWS::EC2::SecurityGroupIngress resources
      const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
      for (const [logicalId, resource] of Object.entries(ingressResources)) {
        const props = (resource as any).Properties ?? {};
        if (props.CidrIp) {
          expect(OPEN_CIDRS).not.toContain(props.CidrIp);
        }
        if (props.CidrIpv6) {
          expect(OPEN_CIDRS).not.toContain(props.CidrIpv6);
        }
      }
    });
  });

  // ---------------------------------------------------------------------------
  // VPC Isolation — dedicated VPC per project
  // Validates: Requirement 9.1
  // ---------------------------------------------------------------------------
  describe('VPC Isolation', () => {
    it('creates a dedicated VPC with the project name', () => {
      template.hasResourceProperties('AWS::EC2::VPC', {
        Tags: Match.arrayWith([
          Match.objectLike({ Key: 'Name', Value: 'hpc-test-project-001-vpc' }),
        ]),
      });
    });

    it('creates public and private subnets', () => {
      const subnets = template.findResources('AWS::EC2::Subnet');
      const subnetLogicalIds = Object.keys(subnets);
      const publicSubnets = subnetLogicalIds.filter(id =>
        (subnets[id] as any).Properties?.Tags?.some(
          (t: any) => t.Key === 'aws-cdk:subnet-type' && t.Value === 'Public',
        ),
      );
      const privateSubnets = subnetLogicalIds.filter(id =>
        (subnets[id] as any).Properties?.Tags?.some(
          (t: any) => t.Key === 'aws-cdk:subnet-type' && t.Value === 'Private',
        ),
      );
      expect(publicSubnets.length).toBeGreaterThanOrEqual(1);
      expect(privateSubnets.length).toBeGreaterThanOrEqual(1);
    });

    it('creates a NAT Gateway for private subnet egress', () => {
      template.resourceCountIs('AWS::EC2::NatGateway', 1);
    });
  });

  // ---------------------------------------------------------------------------
  // Security Groups — existence and rules
  // Validates: Requirements 9.4, 15.1, 15.2, 15.3, 15.4
  // ---------------------------------------------------------------------------
  describe('Security Groups', () => {
    it('creates at least 4 security groups (head node, compute node, EFS, FSx)', () => {
      const sgs = template.findResources('AWS::EC2::SecurityGroup');
      // VPC also creates a default SG, so we expect at least 4 custom ones
      expect(Object.keys(sgs).length).toBeGreaterThanOrEqual(4);
    });

    describe('Head Node SG', () => {
      it('has SSH (22) ingress from trusted CIDRs', () => {
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
          GroupDescription: 'Head Node: SSH and DCV from trusted CIDR ranges',
          SecurityGroupIngress: Match.arrayWith([
            Match.objectLike({ IpProtocol: 'tcp', FromPort: 22, ToPort: 22, CidrIp: '10.0.0.0/8' }),
            Match.objectLike({ IpProtocol: 'tcp', FromPort: 22, ToPort: 22, CidrIp: '172.16.0.0/12' }),
          ]),
        });
      });

      it('has DCV (8443) ingress from trusted CIDRs', () => {
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
          GroupDescription: 'Head Node: SSH and DCV from trusted CIDR ranges',
          SecurityGroupIngress: Match.arrayWith([
            Match.objectLike({ IpProtocol: 'tcp', FromPort: 8443, ToPort: 8443, CidrIp: '10.0.0.0/8' }),
            Match.objectLike({ IpProtocol: 'tcp', FromPort: 8443, ToPort: 8443, CidrIp: '172.16.0.0/12' }),
          ]),
        });
      });

      it('does not have ingress from 0.0.0.0/0', () => {
        const sgs = template.findResources('AWS::EC2::SecurityGroup');
        for (const [, resource] of Object.entries(sgs)) {
          const desc = (resource as any).Properties?.GroupDescription ?? '';
          if (desc.includes('Head Node')) {
            const rules = (resource as any).Properties?.SecurityGroupIngress ?? [];
            for (const rule of rules) {
              expect(rule.CidrIp).not.toBe('0.0.0.0/0');
            }
          }
        }
      });

      it('has slurmd (6818) ingress from Compute Node SG', () => {
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const headFromCompute6818 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'slurmd from slurmctld (Compute Node SG)' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 6818 &&
            props.ToPort === 6818 &&
            props.SourceSecurityGroupId != null
          );
        });
        expect(headFromCompute6818).toBe(true);
      });

      it('has srun (60001-63000) ingress from Compute Node SG', () => {
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const headFromComputeSrun = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'srun from Compute Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 60001 &&
            props.ToPort === 63000 &&
            props.SourceSecurityGroupId != null
          );
        });
        expect(headFromComputeSrun).toBe(true);
      });
    });

    describe('Compute Node SG', () => {
      it('has ingress from Head Node SG', () => {
        // CDK creates standalone SecurityGroupIngress resources for SG-to-SG rules
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const computeFromHead = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'All traffic from Head Node SG' &&
            props.IpProtocol === '-1' &&
            props.SourceSecurityGroupId != null
          );
        });
        expect(computeFromHead).toBe(true);
      });

      it('has self-referencing ingress for inter-compute traffic', () => {
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const computeSelf = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'All traffic from other Compute Nodes (self)' &&
            props.IpProtocol === '-1'
          );
        });
        expect(computeSelf).toBe(true);
      });
    });

    describe('EFS SG', () => {
      it('has NFS (2049) ingress from Head Node and Compute Node SGs', () => {
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const efsFromHead = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'NFS from Head Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 2049 &&
            props.ToPort === 2049
          );
        });
        const efsFromCompute = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'NFS from Compute Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 2049 &&
            props.ToPort === 2049
          );
        });
        expect(efsFromHead).toBe(true);
        expect(efsFromCompute).toBe(true);
      });
    });

    describe('FSx SG', () => {
      it('has Lustre LNET (988) ingress from Head Node, Compute Node, and self', () => {
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const fsxFromHead988 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'Lustre LNET from Head Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 988 &&
            props.ToPort === 988
          );
        });
        const fsxFromCompute988 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'Lustre LNET from Compute Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 988 &&
            props.ToPort === 988
          );
        });
        const fsxFromSelf988 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'Lustre LNET from self (FSx inter-node)' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 988 &&
            props.ToPort === 988
          );
        });
        expect(fsxFromHead988).toBe(true);
        expect(fsxFromCompute988).toBe(true);
        expect(fsxFromSelf988).toBe(true);
      });

      it('has Lustre service ports (1018-1023) ingress from Head Node, Compute Node, and self', () => {
        const ingressResources = template.findResources('AWS::EC2::SecurityGroupIngress');
        const fsxFromHead1018 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'Lustre service ports from Head Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 1018 &&
            props.ToPort === 1023
          );
        });
        const fsxFromCompute1018 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'Lustre service ports from Compute Node SG' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 1018 &&
            props.ToPort === 1023
          );
        });
        const fsxFromSelf1018 = Object.values(ingressResources).some((resource: any) => {
          const props = resource.Properties ?? {};
          return (
            props.Description === 'Lustre service ports from self (FSx inter-node)' &&
            props.IpProtocol === 'tcp' &&
            props.FromPort === 1018 &&
            props.ToPort === 1023
          );
        });
        expect(fsxFromHead1018).toBe(true);
        expect(fsxFromCompute1018).toBe(true);
        expect(fsxFromSelf1018).toBe(true);
      });
    });
  });

  // ---------------------------------------------------------------------------
  // EFS Filesystem — encryption enabled
  // Validates: Requirement 9.3
  // ---------------------------------------------------------------------------
  describe('EFS Filesystem', () => {
    it('creates an EFS filesystem', () => {
      template.resourceCountIs('AWS::EFS::FileSystem', 1);
    });

    it('has encryption enabled', () => {
      template.hasResourceProperties('AWS::EFS::FileSystem', {
        Encrypted: true,
      });
    });
  });

  // ---------------------------------------------------------------------------
  // S3 Bucket — versioning, public access block, and VPC-restricted policy
  // Validates: Requirements 9.2
  // ---------------------------------------------------------------------------
  describe('S3 Bucket', () => {
    it('has versioning enabled', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        VersioningConfiguration: { Status: 'Enabled' },
      });
    });

    it('blocks all public access', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
    });

    it('does not have a VPC-scoped deny policy (would break FSx data repository associations)', () => {
      // The bucket must NOT have a Deny statement conditioned on aws:SourceVpc
      // because the FSx service-linked role accesses S3 from outside the VPC.
      // Security is enforced via BlockPublicAccess and IAM policies instead.
      const policies = template.findResources('AWS::S3::BucketPolicy');
      const policyLogicalIds = Object.keys(policies);

      let foundDenyWithVpcCondition = false;
      for (const logicalId of policyLogicalIds) {
        const statements = (policies[logicalId] as any).Properties?.PolicyDocument?.Statement ?? [];
        for (const stmt of statements) {
          if (
            stmt.Effect === 'Deny' &&
            stmt.Condition?.StringNotEquals?.['aws:SourceVpc'] != null
          ) {
            foundDenyWithVpcCondition = true;
          }
        }
      }
      expect(foundDenyWithVpcCondition).toBe(false);
    });
  });

  // ---------------------------------------------------------------------------
  // Cluster Access Logging — CloudWatch Log Group for SSH/DCV access logs
  // Validates: Requirements 13.1, 13.2, 13.4
  // ---------------------------------------------------------------------------
  describe('Cluster Access Logging', () => {
    it('creates a CloudWatch Log Group for cluster access logs', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/hpc-platform/clusters/test-project-001/access-logs',
      });
    });

    it('sets 365-day retention on the access log group', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/hpc-platform/clusters/test-project-001/access-logs',
        RetentionInDays: 365,
      });
    });

    it('retains the access log group on stack deletion', () => {
      const logGroups = template.findResources('AWS::Logs::LogGroup', {
        Properties: {
          LogGroupName: '/hpc-platform/clusters/test-project-001/access-logs',
        },
      });
      const logicalIds = Object.keys(logGroups);
      expect(logicalIds.length).toBe(1);
      const resource = logGroups[logicalIds[0]] as any;
      expect(resource.DeletionPolicy).toBe('Retain');
    });
  });

  // ---------------------------------------------------------------------------
  // Node Diagnostics Logging — CloudWatch Log Group for syslog/cloud-init
  // Validates: Requirements 8.3, 8.4
  // ---------------------------------------------------------------------------
  describe('Node Diagnostics Logging', () => {
    it('creates a CloudWatch Log Group for node diagnostics with correct name and 1-day retention', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/hpc-platform/clusters/test-project-001/node-diagnostics',
        RetentionInDays: 1,
      });
    });

    it('sets DESTROY removal policy on the node diagnostics log group', () => {
      const logGroups = template.findResources('AWS::Logs::LogGroup', {
        Properties: {
          LogGroupName: '/hpc-platform/clusters/test-project-001/node-diagnostics',
        },
      });
      const logicalIds = Object.keys(logGroups);
      expect(logicalIds.length).toBe(1);
      const resource = logGroups[logicalIds[0]] as any;
      expect(resource.DeletionPolicy).toBe('Delete');
    });
  });

  // ---------------------------------------------------------------------------
  // IAM — no project-level PCS instance profile or role
  // Validates: Requirement 3.1 (instance profiles created per-cluster at runtime)
  // ---------------------------------------------------------------------------
  describe('No project-level PCS IAM resources', () => {
    it('does not create an IAM role named AWSPCS-*-node', () => {
      const roles = template.findResources('AWS::IAM::Role');
      for (const [logicalId, resource] of Object.entries(roles)) {
        const roleName = (resource as any).Properties?.RoleName ?? '';
        expect(roleName).not.toMatch(/^AWSPCS-.*-node$/);
      }
    });

    it('does not create a CfnInstanceProfile for PCS nodes', () => {
      const profiles = template.findResources('AWS::IAM::InstanceProfile');
      for (const [logicalId, resource] of Object.entries(profiles)) {
        const profileName = (resource as any).Properties?.InstanceProfileName ?? '';
        expect(profileName).not.toMatch(/^AWSPCS-.*-node$/);
      }
    });

    it('does not output an InstanceProfileArn', () => {
      const outputs = template.findOutputs('*');
      for (const [outputId] of Object.entries(outputs)) {
        expect(outputId).not.toMatch(/InstanceProfileArn/i);
      }
    });
  });

  // ---------------------------------------------------------------------------
  // No project-level launch templates
  // Validates: Requirements 1.1, 1.2 (cluster-scoped-launch-templates)
  // ---------------------------------------------------------------------------
  describe('No project-level launch templates', () => {
    it('does not create any EC2 launch template resources', () => {
      const launchTemplates = template.findResources('AWS::EC2::LaunchTemplate');
      expect(Object.keys(launchTemplates)).toHaveLength(0);
    });

    it('does not output LoginLaunchTemplateId', () => {
      const outputs = template.findOutputs('*');
      for (const [outputId] of Object.entries(outputs)) {
        expect(outputId).not.toMatch(/LoginLaunchTemplate/i);
      }
    });

    it('does not output ComputeLaunchTemplateId', () => {
      const outputs = template.findOutputs('*');
      for (const [outputId] of Object.entries(outputs)) {
        expect(outputId).not.toMatch(/ComputeLaunchTemplate/i);
      }
    });

    it('still outputs HeadNodeSecurityGroupId and ComputeNodeSecurityGroupId', () => {
      const outputs = template.findOutputs('*');
      const outputIds = Object.keys(outputs);
      expect(outputIds.some(id => id.includes('HeadNodeSecurityGroupId'))).toBe(true);
      expect(outputIds.some(id => id.includes('ComputeNodeSecurityGroupId'))).toBe(true);
    });
  });

  // ---------------------------------------------------------------------------
  // Cost Allocation Tagging — all resources tagged with Project cost allocation tag
  // Validates: Requirement 14.1, 14.2
  // ---------------------------------------------------------------------------
  describe('Cost Allocation Tagging', () => {
    it('tags the VPC with the Project tag', () => {
      template.hasResourceProperties('AWS::EC2::VPC', {
        Tags: Match.arrayWith([
          Match.objectLike({ Key: 'Project', Value: 'test-project-001' }),
        ]),
      });
    });

    it('tags security groups with the Project tag', () => {
      const sgs = template.findResources('AWS::EC2::SecurityGroup');
      for (const [logicalId, resource] of Object.entries(sgs)) {
        const tags = (resource as any).Properties?.Tags ?? [];
        const hasProjectTag = tags.some(
          (t: any) => t.Key === 'Project' && t.Value === 'test-project-001',
        );
        expect(hasProjectTag).toBe(true);
      }
    });

    it('tags the EFS filesystem with the Project tag', () => {
      template.hasResourceProperties('AWS::EFS::FileSystem', {
        FileSystemTags: Match.arrayWith([
          Match.objectLike({ Key: 'Project', Value: 'test-project-001' }),
        ]),
      });
    });

    it('tags the S3 bucket with the Project tag', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        Tags: Match.arrayWith([
          Match.objectLike({ Key: 'Project', Value: 'test-project-001' }),
        ]),
      });
    });

    it('tags subnets with the Project tag', () => {
      const subnets = template.findResources('AWS::EC2::Subnet');
      for (const [logicalId, resource] of Object.entries(subnets)) {
        const tags = (resource as any).Properties?.Tags ?? [];
        const hasProjectTag = tags.some(
          (t: any) => t.Key === 'Project' && t.Value === 'test-project-001',
        );
        expect(hasProjectTag).toBe(true);
      }
    });
  });
});
