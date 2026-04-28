# Template Management

This guide covers managing cluster templates, including bulk deletion. Template creation, editing, and deletion require the **Administrator** role.

For the full API reference for template endpoints, see the [API Reference](../api/reference.md).

## Overview

Cluster templates define the configuration used when creating new clusters. Each template specifies instance types, node counts, AMI, and scheduler configuration. Templates are shared across all projects — any project member can create a cluster from any template.

## Scheduler Version

Each template includes a **Scheduler Version** field that determines the Slurm version used when creating PCS clusters and the OS prefix used when resolving AMIs for node groups.

The field is presented as a dropdown in both the create and edit template forms. The supported versions are:

| Slurm Version | AMI OS Prefix |
|---------------|---------------|
| 24.11         | amzn2         |
| 25.05         | amzn2         |
| 25.11         | al2023        |

The default version is **25.11**.

The selected version controls two things during cluster creation:

1. The Slurm scheduler version passed to the PCS `CreateCluster` API call.
2. The AMI name pattern used to resolve the correct PCS sample AMI for compute and login node groups.

Both values are derived from the same template setting, ensuring the cluster and its node group AMIs always use a matching Slurm version.

The supported versions and their OS mappings are maintained in a single shared configuration constant (`SUPPORTED_SLURM_VERSIONS` in `lambda/shared/pcs_versions.py`). When AWS PCS adds a new Slurm version or deprecates an existing one, update this constant and the frontend dropdown options to keep them in sync.

## Bulk Template Actions

Administrators can select multiple templates and delete them all at once, rather than deleting each template individually.

### Selecting Templates

The Templates table includes a checkbox column. Use the checkboxes to select individual templates, or click the "Select all" checkbox in the column header to select all visible templates (respecting any active filter). Selections are preserved when you change the filter text — templates that become hidden remain selected.

When one or more templates are selected, a bulk action toolbar appears above the table showing the number of selected items and the available actions.

### Available Bulk Actions

| Button | Action | Eligible Templates |
|--------|--------|--------------------|
| Delete All | Deletes all selected templates | Templates that exist in the database |
| Clear Selection | Deselects all templates and hides the toolbar | — |

### Confirmation Dialog

Delete All displays a confirmation dialog listing the template IDs that will be deleted. Review the list before confirming to avoid accidentally removing templates that are still in use.

### Result Summary

After a bulk delete completes, a toast notification displays a summary: "X of Y succeeded, Z failed". If any items failed (e.g., a template was already deleted by another administrator), the toast uses an error style. On network errors, the selection is preserved so you can retry.

### Batch Size Limit

Each bulk action can process up to 25 templates at a time. If you need to delete more than 25 templates, perform the operation in multiple batches.

### Important Notes

- Deleting a template does not affect clusters that were already created from it. Existing clusters retain their original configuration.
- Deleted templates cannot be recovered. If you need the same configuration again, you must create a new template.
