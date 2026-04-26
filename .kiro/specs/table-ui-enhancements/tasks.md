# Tasks

## Task 1: Create TableModule core with sorting and filtering logic

Create `frontend/js/table-module.js` with the reusable TableModule that handles sorting, filtering, scroll containment, and state management.

- [x] 1.1 Create `frontend/js/table-module.js` with the `TableModule` global object exposing `render()`, `clearState()`, and `clearAllState()` methods
- [x] 1.2 Implement internal state management keyed by `tableId` storing `sortColumn`, `sortDirection`, and `filterText`
- [x] 1.3 Implement sort logic with case-insensitive text comparator and numeric comparator, selected based on column `type` config
- [x] 1.4 Implement filter logic that performs case-insensitive substring matching across all visible (non-Actions) column values
- [x] 1.5 Implement `render()` method that builds: filter input, scroll container div (with `overflow-y: auto` and calculated `max-height`), and HTML table with sticky header
- [x] 1.6 Add click and keyboard (Enter/Space) event handlers on sortable column headers that update sort state and re-render the table body
- [x] 1.7 Add input event handler on filter input that updates filter state and re-renders the table body
- [x] 1.8 Add `aria-sort` attributes on column headers (`ascending`, `descending`, or `none`) and `aria-label` on filter input
- [x] 1.9 Add sort direction indicator (▲/▼) to the currently sorted column header
- [x] 1.10 Display `emptyMessage` when data array is empty and `noMatchMessage` when filter produces zero visible rows
- [x] 1.11 Add debounced `window.resize` listener to recalculate scroll container `max-height`

## Task 2: Add CSS styles for scroll containment, sticky header, sort indicators, and filter input

- [x] 2.1 Add `.table-scroll-container` styles with `overflow-y: auto` and `position: relative`
- [x] 2.2 Add sticky header styles (`thead th { position: sticky; top: 0; z-index: 1; }`) scoped to scroll containers
- [x] 2.3 Add `.table-filter-input` styles for the search input above each table
- [x] 2.4 Add sort indicator styles (`.sort-indicator`) and cursor pointer on sortable headers
- [x] 2.5 Add `.table-no-match` styles for the "no matching results" message

## Task 3: Integrate TableModule into app.js for all five tables

- [x] 3.1 Add `<script src="js/table-module.js"></script>` to `frontend/index.html` before `app.js`
- [x] 3.2 Define `usersTableConfig` column configuration and refactor `loadUsers()` to call `TableModule.render()`
- [x] 3.3 Define `projectsTableConfig` column configuration and refactor `loadProjects()` to call `TableModule.render()`
- [x] 3.4 Define `templatesTableConfig` column configuration and refactor `loadTemplates()` to call `TableModule.render()`
- [x] 3.5 Define `clustersTableConfig` column configuration and refactor `loadClusters()` to call `TableModule.render()`
- [x] 3.6 Define `accountingTableConfig` column configuration and refactor the accounting query handler to call `TableModule.render()`
- [x] 3.7 Call `TableModule.clearAllState()` in `navigate()` to reset state when switching pages

## Task 4: Set up Jest frontend test infrastructure and write unit tests

- [x] 4.1 Add `fast-check` as a devDependency and extend `jest.config.js` with a second project for `test/frontend/**/*.test.js` using `jsdom` environment
- [x] 4.2 Write unit tests for sort comparators (text case-insensitive, numeric, null/undefined handling)
- [x] 4.3 Write unit tests for filter logic (substring match, case insensitivity, empty filter, Actions column exclusion)
- [x] 4.4 Write unit tests for DOM structure (scroll container, sticky header class, filter input with aria-label, aria-sort attributes)
- [x] 4.5 Write unit tests for state management (state initialisation, state preservation across render calls, clearState, clearAllState)
- [x] 4.6 Write unit tests for keyboard accessibility (Enter and Space trigger sort on column headers)
- [x] 4.7 Write unit tests for edge cases (empty data shows empty message, no filter matches shows no-match message, missing column values treated as empty string/zero)

## Task 5: Write property-based tests for correctness properties

- [x] 5.1 Write property test: sorting produces correctly ordered output for any data array, any sortable column, and both directions (Property 1) ~PBT
- [x] 5.2 Write property test: filtering returns exactly the matching rows for any data array and any filter string (Property 2) ~PBT
- [x] 5.3 Write property test: sort and filter state is preserved across data refresh for any state and new data (Property 3) ~PBT

## Task 6: Update documentation

- [x] 6.1 Update `docs/admin/user-management.md` to describe table sorting and filtering on the Users page
- [x] 6.2 Update `docs/admin/project-management.md` to describe table sorting and filtering on the Projects page
- [x] 6.3 Update `docs/project-admin/cluster-management.md` to describe table sorting and filtering on the Clusters page
- [x] 6.4 Add a general "Table Features" section to relevant docs describing scrolling, sorting by column header click, and filtering via search input
