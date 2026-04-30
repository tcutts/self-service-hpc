# PCS Controller Sizing

This guide explains how the platform automatically selects the AWS PCS controller size when creating a cluster. The controller size determines the maximum number of instances and jobs the cluster can manage.

## Overview

Every AWS PCS cluster has a **controller size** that sets its capacity limits. The platform supports three controller size tiers — SMALL, MEDIUM, and LARGE — each with increasing instance and job limits. When you create a cluster, the platform automatically selects the smallest tier that can handle your requested `maxNodes` value plus the login node.

The controller size **cannot be changed after cluster creation**. If your workload outgrows the selected tier, you must create a new cluster with a higher `maxNodes` value.

## Controller Size Tiers

| Tier | Max Managed Instances | Max Tracked Jobs |
|------|----------------------|-----------------|
| SMALL | 32 | 256 |
| MEDIUM | 512 | 8,192 |
| LARGE | 2,048 | 16,384 |

**Managed instances** includes all EC2 instances the PCS cluster controls — both compute nodes and the login node.

## How the Tier Is Selected

The platform calculates the total number of managed instances as `maxNodes + 1`, where the additional instance is the login node. It then selects the smallest tier whose capacity is greater than or equal to that total.

| `maxNodes` Range | Total Managed Instances | Selected Tier |
|------------------|------------------------|---------------|
| 1–31 | 2–32 | SMALL |
| 32–511 | 33–512 | MEDIUM |
| 512–2,047 | 513–2,048 | LARGE |

For example, a cluster with `maxNodes=100` has 101 total managed instances (100 compute nodes + 1 login node), which selects the MEDIUM tier.

If `maxNodes` is not specified in the cluster creation request, it defaults to **10**, which selects the SMALL tier.

## Limits

The maximum supported `maxNodes` value is **2,047**. This corresponds to 2,048 total managed instances (2,047 compute nodes + 1 login node), which is the capacity of the LARGE tier.

Requesting a `maxNodes` value greater than 2,047 returns a `ValidationError` (HTTP 400) before any AWS resources are created. The error message indicates that the total managed instance count exceeds the maximum PCS cluster capacity of 2,048 managed instances.

## Boundary Examples

The following table shows the tier selected at each boundary value:

| `maxNodes` | Total Managed | Selected Tier | Notes |
|------------|---------------|---------------|-------|
| 1 | 2 | SMALL | Minimum valid value |
| 31 | 32 | SMALL | Upper boundary of SMALL |
| 32 | 33 | MEDIUM | First value requiring MEDIUM |
| 511 | 512 | MEDIUM | Upper boundary of MEDIUM |
| 512 | 513 | LARGE | First value requiring LARGE |
| 2,047 | 2,048 | LARGE | Maximum supported value |
| 2,048 | 2,049 | Error | Exceeds PCS maximum capacity |
