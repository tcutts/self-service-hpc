---
inclusion: always
---
Infrastructure as code:  Use CDK in typescript, prefer L2 constructs to L1 constructs
Lambda functions: use python
HPC environments: use Parallel Computing Service
Filesystems: for long lived storage, use S3 buckets.  Use FSx for Lustre as a high performance cache with a data repository association to S3 buckets, if performance is needed.
S3 bucket policies: Do not use VPC-scoped deny policies (aws:SourceVpc condition) on S3 buckets that FSx for Lustre needs to access.  FSx's service-linked role accesses S3 from the AWS service plane, not from within the VPC, so a VPC deny blocks FSx data repository associations and creates a self-locking policy that even CloudFormation cannot remove without root credentials.  Rely on BlockPublicAccess and IAM-based access control instead.
Use serverless technology where possible
For local python development, always use a virtual environment, not the system python
!------------------------------------------------------------------------------------
   Add rules to this file or a short description and have Kiro refine them for you.
   
   Learn about inclusion modes: https://kiro.dev/docs/steering/#inclusion-modes
-------------------------------------------------------------------------------------> 