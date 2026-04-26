---
inclusion: always
---
Infrastructure as code:  Use CDK in typescript, prefer L2 constructs to L1 constructs
Lambda functions: use python
HPC environments: use Parallel Computing Service
Filesystems: for long lived storage, use S3 buckets.  Use FSx for Lustre as a high performance cache with a data repository association to S3 buckets, if performance is needed.
Use serverless technology where possible
For local python development, always use a virtual environment, not the system python
!------------------------------------------------------------------------------------
   Add rules to this file or a short description and have Kiro refine them for you.
   
   Learn about inclusion modes: https://kiro.dev/docs/steering/#inclusion-modes
-------------------------------------------------------------------------------------> 