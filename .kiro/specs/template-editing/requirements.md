# Requirements Document

## Introduction

This feature adds the ability for administrators to edit cluster templates after they have been created. The key constraint is that updating a template has no effect on clusters already created with that template — cluster configuration values are already copied into the cluster record at creation time, so template edits only affect future cluster creations.

The feature spans the backend (new PUT endpoint, Lambda handler routing, business logic, validation), the frontend (edit UI in the templates page), the CDK infrastructure (new API Gateway route), and documentation updates.

## Glossary

- **Template_Management_API**: The Lambda-backed REST API that handles cluster template CRUD operations, served through API Gateway.
- **Template_Record**: A DynamoDB item representing a cluster template, keyed by `PK=TEMPLATE#{templateId}` and `SK=METADATA`.
- **Administrator**: A platform user belonging to the Cognito `Administrators` group, authorised to perform write operations on templates.
- **Authenticated_User**: Any user with a valid Cognito JWT token, authorised for read-only template operations.
- **Cluster_Record**: A DynamoDB item representing a cluster instance. Configuration values from the template are copied into the Cluster_Record at creation time and are independent of subsequent template changes.
- **Editable_Fields**: The subset of Template_Record fields that can be modified after creation: `templateName`, `description`, `instanceTypes`, `loginInstanceType`, `minNodes`, `maxNodes`, `amiId`, `softwareStack`.
- **Immutable_Fields**: Template_Record fields that cannot be changed after creation: `templateId`, `createdAt`.

## Requirements

### Requirement 1: Update Template API Endpoint

**User Story:** As an Administrator, I want to update an existing cluster template's configuration fields via a PUT endpoint, so that future clusters created from the template use the updated settings.

#### Acceptance Criteria

1. WHEN a PUT request is received at `/templates/{templateId}` with a valid request body, THE Template_Management_API SHALL update the corresponding Template_Record in DynamoDB with the provided Editable_Fields and return the updated Template_Record with HTTP status 200.
2. WHEN a PUT request is received at `/templates/{templateId}`, THE Template_Management_API SHALL add an `updatedAt` field to the Template_Record containing the current UTC timestamp in ISO 8601 format.
3. WHEN a PUT request is received at `/templates/{templateId}`, THE Template_Management_API SHALL preserve the original `createdAt` value in the Template_Record.
4. WHEN a PUT request is received at `/templates/{templateId}` and the template does not exist, THE Template_Management_API SHALL return a NOT_FOUND error with HTTP status 404.
5. IF a PUT request to `/templates/{templateId}` contains a `templateId` field in the body that differs from the path parameter, THEN THE Template_Management_API SHALL return a VALIDATION_ERROR with HTTP status 400.

### Requirement 2: Authorisation for Template Updates

**User Story:** As a platform operator, I want template updates restricted to administrators, so that only authorised users can modify cluster configurations.

#### Acceptance Criteria

1. WHEN a non-Administrator user sends a PUT request to `/templates/{templateId}`, THE Template_Management_API SHALL return an AUTHORISATION_ERROR with HTTP status 403.
2. WHEN an Administrator user sends a PUT request to `/templates/{templateId}`, THE Template_Management_API SHALL proceed with the update operation.

### Requirement 3: Validation of Updated Template Fields

**User Story:** As an Administrator, I want the same validation rules applied to template updates as to template creation, so that templates always contain valid configuration.

#### Acceptance Criteria

1. WHEN a PUT request is received with Editable_Fields, THE Template_Management_API SHALL validate all provided fields using the same rules as template creation (non-empty templateName, non-empty instanceTypes list of strings, valid loginInstanceType string, non-negative integer minNodes, positive integer maxNodes where maxNodes >= minNodes, non-empty amiId string).
2. IF any Editable_Field in a PUT request fails validation, THEN THE Template_Management_API SHALL return a VALIDATION_ERROR with HTTP status 400 and a details object identifying the invalid field.
3. WHEN a PUT request is received with an empty request body, THE Template_Management_API SHALL return a VALIDATION_ERROR with HTTP status 400.

### Requirement 4: Cluster Isolation from Template Updates

**User Story:** As a platform operator, I want template updates to have no effect on clusters already created with that template, so that running workloads are not disrupted by configuration changes.

#### Acceptance Criteria

1. WHEN a Template_Record is updated, THE Cluster_Record for any cluster previously created from that template SHALL retain the configuration values that were copied at cluster creation time.
2. WHEN a cluster is created after a template has been updated, THE cluster creation process SHALL use the current (updated) values from the Template_Record.

### Requirement 5: Template Edit User Interface

**User Story:** As an Administrator, I want to edit a template from the web portal's templates page, so that I can update template settings without using the API directly.

#### Acceptance Criteria

1. THE templates list table SHALL display an Edit button for each template row.
2. WHEN the Edit button is clicked, THE portal SHALL display a modal dialog pre-populated with the template's current field values.
3. WHEN the user submits the edit form with valid changes, THE portal SHALL send a PUT request to `/templates/{templateId}` and display a success notification.
4. IF the PUT request returns an error, THEN THE portal SHALL display the error message in a notification.
5. WHEN the edit modal is submitted successfully, THE portal SHALL refresh the templates list to reflect the updated values.
6. THE edit modal SHALL prevent modification of the templateId field by displaying it as a read-only input.

### Requirement 6: API Gateway Route for Template Updates

**User Story:** As a platform operator, I want the PUT /templates/{templateId} route configured in the CDK stack, so that the API Gateway forwards update requests to the Template Management Lambda.

#### Acceptance Criteria

1. THE CDK stack SHALL define a PUT method on the `/templates/{templateId}` API Gateway resource, integrated with the Template Management Lambda function and protected by the Cognito authoriser.

### Requirement 7: API Documentation for Template Updates

**User Story:** As a developer integrating with the platform API, I want the PUT /templates/{templateId} endpoint documented in the API reference, so that I can understand the request format, response format, and error conditions.

#### Acceptance Criteria

1. THE API reference document SHALL include a section for `PUT /templates/{templateId}` describing the required role, request body schema, response format, and error codes.
2. THE API reference document SHALL document the `updatedAt` field in the response for updated templates.

### Requirement 8: Update Template Storage Round-Trip

**User Story:** As a developer, I want confidence that updating a template and then retrieving it returns the updated values, so that the persistence layer is correct.

#### Acceptance Criteria

1. FOR ALL valid Template_Records and valid sets of Editable_Fields, updating the template and then retrieving it SHALL return a Template_Record with all Editable_Fields equal to the updated values (round-trip property).
2. FOR ALL valid Template_Records and valid sets of Editable_Fields, updating the template SHALL preserve the original `createdAt` value and set an `updatedAt` value (metamorphic property).
