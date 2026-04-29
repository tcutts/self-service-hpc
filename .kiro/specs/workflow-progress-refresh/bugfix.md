# Bugfix Requirements Document

## Introduction

After a user triggers a workflow action (deploying a project, destroying a project, creating a cluster, destroying a cluster, or recreating a cluster), the web page does not immediately show a progress badge or indicator. The user must manually refresh the page to see the progress. This is caused by a race condition: the frontend calls `loadProjects()` or `loadClusters()` immediately after the API action succeeds, but the backend has not yet updated the resource status to a transitional state (e.g., DEPLOYING, DESTROYING, CREATING). Since polling only starts when a transitional state is detected in the API response, polling never begins, and the UI remains stale until the user manually refreshes.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a user deploys a project and the immediate `loadProjects()` response still shows the project in CREATED status (backend has not yet transitioned to DEPLOYING) THEN the system does not start polling and no progress indicator is displayed

1.2 WHEN a user destroys a project and the immediate `loadProjects()` response still shows the project in ACTIVE status (backend has not yet transitioned to DESTROYING) THEN the system does not start polling and no progress indicator is displayed

1.3 WHEN a user creates a cluster and the immediate `loadClusters()` response does not yet include the new cluster in CREATING status THEN the system does not start polling and no progress indicator is displayed

1.4 WHEN a user destroys a cluster and the immediate `loadClusters()` response still shows the cluster in ACTIVE or FAILED status (backend has not yet transitioned to DESTROYING) THEN the system does not start polling and no progress indicator is displayed

1.5 WHEN a user recreates a cluster and the immediate `loadClusters()` response still shows the cluster in DESTROYED status (backend has not yet transitioned to CREATING) THEN the system does not start polling and no progress indicator is displayed

1.6 WHEN a user updates a project and the immediate `loadProjects()` response still shows the project in ACTIVE status (backend has not yet transitioned to UPDATING) THEN the system does not start polling and no progress indicator is displayed

1.7 WHEN a user performs a bulk action (bulk deploy, bulk update, bulk destroy) on projects and the immediate `loadProjects()` response does not yet reflect transitional states THEN the system does not start polling and no progress indicators are displayed

### Expected Behavior (Correct)

2.1 WHEN a user deploys a project THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows a transitional state, so the UI picks up the DEPLOYING status as soon as the backend processes it

2.2 WHEN a user destroys a project THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows a transitional state, so the UI picks up the DESTROYING status as soon as the backend processes it

2.3 WHEN a user creates a cluster THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows a transitional state, so the UI picks up the CREATING status as soon as the backend processes it

2.4 WHEN a user destroys a cluster THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows a transitional state, so the UI picks up the DESTROYING status as soon as the backend processes it

2.5 WHEN a user recreates a cluster THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows a transitional state, so the UI picks up the CREATING status as soon as the backend processes it

2.6 WHEN a user updates a project THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows a transitional state, so the UI picks up the UPDATING status as soon as the backend processes it

2.7 WHEN a user performs a bulk action (bulk deploy, bulk update, bulk destroy) on projects THEN the system SHALL force-start polling for a reasonable duration regardless of whether the immediate API response shows transitional states

2.8 WHEN force-started polling detects that all resources have settled into non-transitional states AND the force-polling duration has elapsed THEN the system SHALL stop polling to avoid unnecessary API calls

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a user navigates to the projects page and no workflow action has been triggered THEN the system SHALL CONTINUE TO only start polling when transitional states (DEPLOYING, DESTROYING, UPDATING) are detected in the API response

3.2 WHEN a user navigates to the clusters page and no workflow action has been triggered THEN the system SHALL CONTINUE TO only start polling when transitional states (CREATING, DESTROYING) are detected in the API response

3.3 WHEN a user navigates away from the projects or clusters page THEN the system SHALL CONTINUE TO stop all active polling timers for that page

3.4 WHEN polling detects a status transition (e.g., DEPLOYING → ACTIVE, CREATING → FAILED) THEN the system SHALL CONTINUE TO display the appropriate toast notification

3.5 WHEN the existing polling mechanism is running and detects that all resources have settled into non-transitional states THEN the system SHALL CONTINUE TO stop polling normally

3.6 WHEN the page is refreshed during an active workflow THEN the system SHALL CONTINUE TO detect transitional states from the API response and start polling as before
