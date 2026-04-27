import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

/**
 * Props for the per-project infrastructure stack.
 */
export interface ProjectInfrastructureStackProps extends cdk.StackProps {
  /** Unique project identifier. */
  readonly projectId: string;
  /** Human-readable project name. */
  readonly projectName: string;
  /** Value for the Cost_Allocation_Tag. Defaults to projectId if not provided. */
  readonly costAllocationTag?: string;
  /** Trusted CIDR ranges for security group ingress (used by task 6.2). */
  readonly trustedCidrRanges: string[];
  /** Optional existing S3 bucket ARN. If provided, the stack imports it instead of creating a new one. */
  readonly existingBucketArn?: string;
}

/**
 * Per-project infrastructure stack providing network isolation, persistent
 * storage, and cost-allocation tagging.
 *
 * Resources created:
 *  - Dedicated VPC (2 AZs, public + private-with-egress subnets, NAT Gateway)
 *  - EFS filesystem for persistent home directories
 *  - S3 bucket for project storage (or imported from an existing ARN)
 *  - Cost_Allocation_Tag applied to all resources
 *
 * Security groups:
 *  - Head Node SG: SSH (22) and DCV (8443) from trusted CIDR ranges only
 *  - Compute Node SG: all traffic from Head Node SG and self (other compute nodes)
 *  - EFS SG: NFS (2049) from Head Node and Compute Node SGs
 *  - FSx for Lustre SG: Lustre (988, 1018-1023) from Head Node SG, Compute Node SG, and self
 */
export class ProjectInfrastructureStack extends cdk.Stack {
  /** Dedicated VPC for this project. */
  public readonly vpc: ec2.Vpc;
  /** EFS filesystem for persistent home directories. */
  public readonly fileSystem: efs.FileSystem;
  /** S3 bucket for project storage (may be imported or newly created). */
  public readonly projectBucket: s3.IBucket;
  /** Security group for Head (Login) Nodes — SSH and DCV from trusted CIDRs. */
  public readonly headNodeSecurityGroup: ec2.SecurityGroup;
  /** Security group for Compute Nodes — traffic from Head Node SG and self. */
  public readonly computeNodeSecurityGroup: ec2.SecurityGroup;
  /** Security group for EFS — NFS from Head Node and Compute Node SGs. */
  public readonly efsSecurityGroup: ec2.SecurityGroup;
  /** Security group for FSx for Lustre — Lustre traffic from Head Node and Compute Node SGs. */
  public readonly fsxSecurityGroup: ec2.SecurityGroup;
  /** CloudWatch Log Group for cluster SSH/DCV access logs (365-day retention). */
  public readonly clusterAccessLogGroup: logs.LogGroup;
  /** EC2 launch template for login (head) nodes. */
  public readonly loginLaunchTemplate: ec2.LaunchTemplate;
  /** EC2 launch template for compute nodes. */
  public readonly computeLaunchTemplate: ec2.LaunchTemplate;

  constructor(scope: Construct, id: string, props: ProjectInfrastructureStackProps) {
    super(scope, id, props);

    const tagValue = props.costAllocationTag ?? props.projectId;

    // -----------------------------------------------------------------
    // VPC — dedicated per project for isolation
    // -----------------------------------------------------------------
    this.vpc = new ec2.Vpc(this, 'ProjectVpc', {
      vpcName: `hpc-${props.projectId}-vpc`,
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
    });

    // -----------------------------------------------------------------
    // Security Groups — least-privilege, no 0.0.0.0/0
    // -----------------------------------------------------------------

    // Head Node (Login Node) SG: SSH (22) and DCV (8443) from trusted CIDRs
    this.headNodeSecurityGroup = new ec2.SecurityGroup(this, 'HeadNodeSG', {
      vpc: this.vpc,
      description: 'Head Node: SSH and DCV from trusted CIDR ranges',
      allowAllOutbound: true,
    });

    for (const cidr of props.trustedCidrRanges) {
      this.headNodeSecurityGroup.addIngressRule(
        ec2.Peer.ipv4(cidr),
        ec2.Port.tcp(22),
        `SSH from ${cidr}`,
      );
      this.headNodeSecurityGroup.addIngressRule(
        ec2.Peer.ipv4(cidr),
        ec2.Port.tcp(8443),
        `DCV from ${cidr}`,
      );
    }

    // Compute Node SG: all traffic from Head Node SG and self
    this.computeNodeSecurityGroup = new ec2.SecurityGroup(this, 'ComputeNodeSG', {
      vpc: this.vpc,
      description: 'Compute Node: traffic from Head Node and other Compute Nodes',
      allowAllOutbound: true,
    });

    this.computeNodeSecurityGroup.addIngressRule(
      this.headNodeSecurityGroup,
      ec2.Port.allTraffic(),
      'All traffic from Head Node SG',
    );
    this.computeNodeSecurityGroup.addIngressRule(
      this.computeNodeSecurityGroup,
      ec2.Port.allTraffic(),
      'All traffic from other Compute Nodes (self)',
    );

    // EFS SG: NFS (2049) from Head Node and Compute Node SGs
    this.efsSecurityGroup = new ec2.SecurityGroup(this, 'EfsSecurityGroup', {
      vpc: this.vpc,
      description: 'EFS: NFS from Head Node and Compute Node SGs',
      allowAllOutbound: false,
    });

    this.efsSecurityGroup.addIngressRule(
      this.headNodeSecurityGroup,
      ec2.Port.tcp(2049),
      'NFS from Head Node SG',
    );
    this.efsSecurityGroup.addIngressRule(
      this.computeNodeSecurityGroup,
      ec2.Port.tcp(2049),
      'NFS from Compute Node SG',
    );

    // FSx for Lustre SG: Lustre LNET (988) and service ports (1018-1023)
    // from Head Node SG, Compute Node SG, and self (FSx inter-node traffic).
    this.fsxSecurityGroup = new ec2.SecurityGroup(this, 'FsxSecurityGroup', {
      vpc: this.vpc,
      description: 'FSx for Lustre: Lustre traffic from Head Node, Compute Node, and self',
      allowAllOutbound: false,
    });

    this.fsxSecurityGroup.addIngressRule(
      this.headNodeSecurityGroup,
      ec2.Port.tcp(988),
      'Lustre LNET from Head Node SG',
    );
    this.fsxSecurityGroup.addIngressRule(
      this.computeNodeSecurityGroup,
      ec2.Port.tcp(988),
      'Lustre LNET from Compute Node SG',
    );
    this.fsxSecurityGroup.addIngressRule(
      this.fsxSecurityGroup,
      ec2.Port.tcp(988),
      'Lustre LNET from self (FSx inter-node)',
    );

    this.fsxSecurityGroup.addIngressRule(
      this.headNodeSecurityGroup,
      ec2.Port.tcpRange(1018, 1023),
      'Lustre service ports from Head Node SG',
    );
    this.fsxSecurityGroup.addIngressRule(
      this.computeNodeSecurityGroup,
      ec2.Port.tcpRange(1018, 1023),
      'Lustre service ports from Compute Node SG',
    );
    this.fsxSecurityGroup.addIngressRule(
      this.fsxSecurityGroup,
      ec2.Port.tcpRange(1018, 1023),
      'Lustre service ports from self (FSx inter-node)',
    );

    // -----------------------------------------------------------------
    // EFS — persistent home directories
    // -----------------------------------------------------------------
    this.fileSystem = new efs.FileSystem(this, 'HomeDirectories', {
      fileSystemName: `hpc-${props.projectId}-home`,
      vpc: this.vpc,
      performanceMode: efs.PerformanceMode.GENERAL_PURPOSE,
      encrypted: true,
      lifecyclePolicy: efs.LifecyclePolicy.AFTER_30_DAYS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      securityGroup: this.efsSecurityGroup,
    });

    // -----------------------------------------------------------------
    // S3 — project storage
    // -----------------------------------------------------------------
    if (props.existingBucketArn) {
      this.projectBucket = s3.Bucket.fromBucketArn(this, 'ImportedBucket', props.existingBucketArn);
    } else {
      const bucket = new s3.Bucket(this, 'ProjectBucket', {
        bucketName: `hpc-${props.projectId}-storage-${cdk.Aws.ACCOUNT_ID}`,
        versioned: true,
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      // The bucket is protected by BlockPublicAccess.BLOCK_ALL and
      // IAM-based access control.  We intentionally do NOT add a
      // VPC-scoped deny policy here because the FSx for Lustre
      // service-linked role must access the bucket from outside the
      // VPC when creating data repository associations.  A blanket
      // VPC deny would cause FSx CreateFileSystem to fail with
      // "unable to validate access to the S3 bucket".

      this.projectBucket = bucket;
    }

    // -----------------------------------------------------------------
    // CloudWatch Log Group — cluster SSH/DCV access logs (365 days)
    // -----------------------------------------------------------------
    this.clusterAccessLogGroup = new logs.LogGroup(this, 'ClusterAccessLogGroup', {
      logGroupName: `/hpc-platform/clusters/${props.projectId}/access-logs`,
      retention: logs.RetentionDays.ONE_YEAR,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -----------------------------------------------------------------
    // EC2 Launch Templates — login and compute nodes
    // -----------------------------------------------------------------
    // Login nodes: head node SG, SSH/DCV access from trusted CIDRs
    this.loginLaunchTemplate = new ec2.LaunchTemplate(this, 'LoginLaunchTemplate', {
      launchTemplateName: `hpc-${props.projectId}-login`,
      securityGroup: this.headNodeSecurityGroup,
    });

    // Compute nodes: compute node SG, private subnet only
    this.computeLaunchTemplate = new ec2.LaunchTemplate(this, 'ComputeLaunchTemplate', {
      launchTemplateName: `hpc-${props.projectId}-compute`,
      securityGroup: this.computeNodeSecurityGroup,
    });

    // -----------------------------------------------------------------
    // Tags — Cost_Allocation_Tag on all resources
    // -----------------------------------------------------------------
    cdk.Tags.of(this).add('Project', tagValue);

    // -----------------------------------------------------------------
    // Stack Outputs
    // -----------------------------------------------------------------
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: `VPC ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'EfsFileSystemId', {
      value: this.fileSystem.fileSystemId,
      description: `EFS filesystem ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'S3BucketName', {
      value: this.projectBucket.bucketName,
      description: `S3 bucket name for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'PublicSubnetIds', {
      value: this.vpc.publicSubnets.map(s => s.subnetId).join(','),
      description: `Public subnet IDs for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'PrivateSubnetIds', {
      value: this.vpc.privateSubnets.map(s => s.subnetId).join(','),
      description: `Private subnet IDs for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'HeadNodeSecurityGroupId', {
      value: this.headNodeSecurityGroup.securityGroupId,
      description: `Head Node security group ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'ComputeNodeSecurityGroupId', {
      value: this.computeNodeSecurityGroup.securityGroupId,
      description: `Compute Node security group ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'EfsSecurityGroupId', {
      value: this.efsSecurityGroup.securityGroupId,
      description: `EFS security group ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'FsxSecurityGroupId', {
      value: this.fsxSecurityGroup.securityGroupId,
      description: `FSx for Lustre security group ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'ClusterAccessLogGroupName', {
      value: this.clusterAccessLogGroup.logGroupName,
      description: `CloudWatch Log Group for cluster access logs for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'LoginLaunchTemplateId', {
      value: this.loginLaunchTemplate.launchTemplateId!,
      description: `Login node launch template ID for project ${props.projectId}`,
    });

    new cdk.CfnOutput(this, 'ComputeLaunchTemplateId', {
      value: this.computeLaunchTemplate.launchTemplateId!,
      description: `Compute node launch template ID for project ${props.projectId}`,
    });
  }
}
