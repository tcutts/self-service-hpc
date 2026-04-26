# Requirements Document

## Introduction

This feature enhances all data tables in the HPC Self-Service Portal (Users, Projects, Templates, Clusters, and Accounting/Jobs) with three capabilities: viewport-constrained scrolling so tables never exceed the visible screen area, column-header sorting to reorder rows by any column, and text filtering to quickly locate specific items. The implementation must remain vanilla JavaScript with no build system or framework dependencies, and sort/filter state must survive automatic data refreshes caused by polling.

## Glossary

- **Table_Container**: A wrapper `<div>` element (e.g., `#users-list`, `#projects-list`, `#templates-list`, `#clusters-list`, `#accounting-results`) that holds a rendered HTML table
- **Table_Module**: The reusable vanilla JavaScript module responsible for rendering tables with scroll containment, sorting, and filtering capabilities
- **Column_Header**: A `<th>` element in the table header row that acts as a clickable sort control
- **Sort_Indicator**: A visual arrow or symbol displayed alongside a Column_Header to communicate the current sort direction (ascending or descending)
- **Filter_Input**: A text input field rendered above a table that accepts user-typed search terms for filtering visible rows
- **Sort_State**: An object recording the currently sorted column key and direction (ascending or descending) for a given table
- **Filter_State**: A string representing the current filter text for a given table
- **Viewport**: The visible browser window area available to the main content region
- **Scroll_Container**: A DOM element with constrained height and `overflow-y: auto` that enables vertical scrolling of table content within the Viewport
- **Data_Refresh**: An automatic re-render of table content triggered by polling (clusters, projects) or explicit user action (accounting query)
- **Sticky_Header**: A table header row that remains visible at the top of the Scroll_Container while the table body scrolls

## Requirements

### Requirement 1: Viewport-Constrained Table Scrolling

**User Story:** As a portal user, I want tables to never extend beyond the visible screen area, so that I can always see the page header and navigation without needing to scroll the entire page.

#### Acceptance Criteria

1. THE Table_Module SHALL render each table inside a Scroll_Container that constrains its maximum height to fit within the available Viewport space below the table's position
2. WHEN table content exceeds the Scroll_Container height, THE Scroll_Container SHALL display a vertical scrollbar allowing the user to scroll through all table rows
3. WHILE the user scrolls within a Scroll_Container, THE Sticky_Header SHALL remain fixed at the top of the Scroll_Container so column headings are always visible
4. WHEN the browser window is resized, THE Scroll_Container SHALL recalculate its maximum height to continue fitting within the Viewport
5. THE Table_Module SHALL apply scroll containment to all five tables: Users, Projects, Templates, Clusters, and Accounting/Jobs

### Requirement 2: Column-Header Sorting

**User Story:** As a portal user, I want to sort table rows by clicking on a column header, so that I can organize data in a meaningful order to find what I need.

#### Acceptance Criteria

1. WHEN the user clicks a Column_Header, THE Table_Module SHALL sort the table rows by that column's values in ascending order
2. WHEN the user clicks the same Column_Header a second time, THE Table_Module SHALL reverse the sort direction to descending order
3. WHEN the user clicks a different Column_Header, THE Table_Module SHALL sort by the new column in ascending order and clear the previous sort
4. THE Table_Module SHALL display a Sort_Indicator on the currently sorted Column_Header showing the active sort direction
5. THE Table_Module SHALL sort text values using case-insensitive lexicographic comparison
6. THE Table_Module SHALL sort numeric values (e.g., POSIX UID, Budget, Node counts) using numeric comparison
7. WHEN a Data_Refresh occurs, THE Table_Module SHALL re-apply the current Sort_State so the user's chosen sort order is preserved
8. THE Table_Module SHALL support sorting on all columns except the Actions column across all five tables

### Requirement 3: Text Filtering

**User Story:** As a portal user, I want to type a search term to filter table rows, so that I can quickly find a specific user, project, template, cluster, or job without scrolling through the entire list.

#### Acceptance Criteria

1. THE Table_Module SHALL render a Filter_Input above each table
2. WHEN the user types text into the Filter_Input, THE Table_Module SHALL hide all table rows whose visible cell text does not contain the filter term (case-insensitive substring match)
3. WHEN the Filter_Input is cleared, THE Table_Module SHALL display all table rows
4. WHEN a Data_Refresh occurs, THE Table_Module SHALL re-apply the current Filter_State so the user's filter is preserved across polling updates
5. THE Table_Module SHALL filter against all visible text columns (excluding the Actions column) for each row
6. WHEN no rows match the filter term, THE Table_Module SHALL display a message indicating no matching results were found

### Requirement 4: State Preservation Across Data Refreshes

**User Story:** As a portal user, I want my sort and filter selections to persist when the table data refreshes automatically, so that I am not disrupted while monitoring cluster or project progress.

#### Acceptance Criteria

1. WHILE a Sort_State is active for a table, THE Table_Module SHALL re-apply that Sort_State after each Data_Refresh
2. WHILE a Filter_State is active for a table, THE Table_Module SHALL re-apply that Filter_State after each Data_Refresh
3. WHEN the user navigates away from a page and returns, THE Table_Module SHALL reset Sort_State and Filter_State to their defaults (no sort, no filter)
4. THE Table_Module SHALL store Sort_State and Filter_State in memory only (not in localStorage or URL parameters)

### Requirement 5: Accessible Interaction

**User Story:** As a portal user who relies on assistive technology, I want the sort and filter controls to be keyboard-accessible and properly labelled, so that I can use the enhanced tables without a mouse.

#### Acceptance Criteria

1. THE Column_Header SHALL be activatable via keyboard (Enter or Space key) to trigger sorting
2. THE Column_Header SHALL include an `aria-sort` attribute reflecting the current sort state (ascending, descending, or none)
3. THE Filter_Input SHALL include a visible label or `aria-label` attribute describing its purpose (e.g., "Filter users")
4. THE Sort_Indicator SHALL be conveyed to screen readers through the `aria-sort` attribute rather than relying solely on visual icons
5. WHEN the user activates a Column_Header via keyboard, THE Table_Module SHALL provide the same sorting behaviour as a mouse click

### Requirement 6: Documentation Update

**User Story:** As a portal administrator, I want the documentation to describe the new table sorting and filtering capabilities, so that users know how to use these features.

#### Acceptance Criteria

1. WHEN the table enhancements are implemented, THE Documentation SHALL be updated to describe the scrolling, sorting, and filtering capabilities available on each table view
2. THE Documentation SHALL describe how to sort by clicking column headers and how to filter using the search input
