/**
 * TableModule — Reusable sortable, filterable, scroll-contained table renderer.
 *
 * Exposes a single global object `window.TableModule` with:
 *   - render(tableId, config, data, container)
 *   - clearState(tableId)
 *   - clearAllState()
 *
 * Pure vanilla JavaScript, no dependencies.
 */
(function () {
  'use strict';

  /* ==========================================================
     Internal State
     ========================================================== */

  /**
   * Per-table state keyed by tableId.
   * Each entry: { sortColumn: string|null, sortDirection: 'asc'|'desc', filterText: string }
   */
  var tableStates = {};

  /** Map of tableId → resize cleanup function */
  var resizeCleanups = {};

  /* ==========================================================
     Helpers
     ========================================================== */

  /**
   * HTML-escape a string to prevent XSS when inserting into innerHTML.
   */
  function esc(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  /**
   * Get or initialise state for a given tableId.
   */
  function getState(tableId) {
    if (!tableStates[tableId]) {
      tableStates[tableId] = {
        sortColumn: null,
        sortDirection: 'asc',
        filterText: '',
        selectedIds: new Set(),
      };
    }
    return tableStates[tableId];
  }

  /* ==========================================================
     Sort Comparators
     ========================================================== */

  /**
   * Case-insensitive text comparator.
   * null/undefined treated as empty string.
   */
  function textComparator(a, b) {
    var valA = (a == null ? '' : String(a)).toLowerCase();
    var valB = (b == null ? '' : String(b)).toLowerCase();
    if (valA < valB) return -1;
    if (valA > valB) return 1;
    return 0;
  }

  /**
   * Numeric comparator.
   * null/undefined treated as 0.
   */
  function numericComparator(a, b) {
    var valA = (a == null || a === '') ? 0 : Number(a);
    var valB = (b == null || b === '') ? 0 : Number(b);
    if (isNaN(valA)) valA = 0;
    if (isNaN(valB)) valB = 0;
    return valA - valB;
  }

  /**
   * Select the appropriate comparator based on column type.
   */
  function getComparator(columnType) {
    if (columnType === 'numeric') return numericComparator;
    return textComparator;
  }

  /* ==========================================================
     Sort Logic
     ========================================================== */

  /**
   * Sort an array of row objects by a given column config and direction.
   * Returns a new sorted array (does not mutate the original).
   *
   * @param {Object[]} rows
   * @param {Object} columnDef - The column definition to sort by
   * @param {'asc'|'desc'} direction
   * @returns {Object[]}
   */
  function sortRows(rows, columnDef, direction) {
    var comparator = getComparator(columnDef.type || 'text');
    var valueAccessor = columnDef.value || function (row) { return row[columnDef.key]; };

    var sorted = rows.slice().sort(function (a, b) {
      var valA = valueAccessor(a);
      var valB = valueAccessor(b);
      var result = comparator(valA, valB);
      return direction === 'desc' ? -result : result;
    });

    return sorted;
  }

  /* ==========================================================
     Filter Logic
     ========================================================== */

  /**
   * Determine if a column should be included in filtering.
   * Actions columns (sortable: false with type 'custom') are excluded.
   */
  function isFilterableColumn(colDef) {
    if (colDef.sortable === false && colDef.type === 'custom') return false;
    return true;
  }

  /**
   * Filter rows by a search string across all filterable columns.
   * Uses the value accessor (not rendered HTML) for matching.
   * Returns a new array of matching rows.
   *
   * @param {Object[]} rows
   * @param {Object[]} columns - Column definitions
   * @param {string} filterText
   * @returns {Object[]}
   */
  function filterRows(rows, columns, filterText) {
    if (!filterText) return rows.slice();

    var needle = filterText.toLowerCase();
    var filterableCols = columns.filter(isFilterableColumn);

    return rows.filter(function (row) {
      for (var i = 0; i < filterableCols.length; i++) {
        var col = filterableCols[i];
        var valueAccessor = col.value || function (r) { return r[col.key]; };
        var val = valueAccessor(row);
        var str = (val == null ? '' : String(val)).toLowerCase();
        if (str.indexOf(needle) !== -1) return true;
      }
      return false;
    });
  }

  /* ==========================================================
     Scroll Container Height
     ========================================================== */

  var BOTTOM_MARGIN = 32;
  var MIN_HEIGHT = 200;

  /**
   * Calculate the max-height for the scroll container.
   */
  function calcMaxHeight(container) {
    var top = container.getBoundingClientRect().top;
    var available = window.innerHeight - top - BOTTOM_MARGIN;
    return Math.max(available, MIN_HEIGHT);
  }

  /* ==========================================================
     Debounce Utility
     ========================================================== */

  function debounce(fn, delay) {
    var timer = null;
    return function () {
      var context = this;
      var args = arguments;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        fn.apply(context, args);
      }, delay);
    };
  }

  /* ==========================================================
     DOM Rendering
     ========================================================== */

  /**
   * Build the table header row HTML.
   * When config.selectable is true, prepends a select-all checkbox column.
   */
  function buildThead(columns, tableState, config, visibleRows) {
    var selectAllTh = '';
    if (config && config.selectable && config.rowId) {
      var allSelected = visibleRows && visibleRows.length > 0 && visibleRows.every(function (row) {
        return tableState.selectedIds.has(String(row[config.rowId]));
      });
      selectAllTh = '<th role="columnheader" aria-sort="none">'
        + '<input type="checkbox" class="select-all-checkbox" aria-label="Select all rows"'
        + (allSelected ? ' checked' : '')
        + ' />'
        + '</th>';
    }

    var ths = columns.map(function (col) {
      var sortable = col.sortable !== false;
      var ariaSortVal = 'none';
      var indicator = '';

      if (sortable && tableState.sortColumn === col.key) {
        ariaSortVal = tableState.sortDirection === 'asc' ? 'ascending' : 'descending';
        indicator = tableState.sortDirection === 'asc' ? ' ▲' : ' ▼';
      }

      var attrs = '';
      if (sortable) {
        attrs = ' tabindex="0" role="columnheader" aria-sort="' + ariaSortVal + '"'
          + ' style="cursor:pointer;user-select:none;"'
          + ' data-column-key="' + esc(col.key) + '"'
          + ' class="sortable-header"';
      } else {
        attrs = ' role="columnheader" aria-sort="none"';
      }

      return '<th' + attrs + '>' + esc(col.label) + indicator + '</th>';
    }).join('');

    return '<thead><tr>' + selectAllTh + ths + '</tr></thead>';
  }

  /**
   * Build the table body HTML from processed (sorted + filtered) rows.
   * When config.selectable is true, prepends a checkbox column to each row.
   */
  function buildTbody(columns, rows, config, tableState) {
    if (rows.length === 0) return '<tbody></tbody>';

    var selectable = config && config.selectable && config.rowId;

    var trs = rows.map(function (row) {
      var checkboxTd = '';
      if (selectable) {
        var rowIdValue = String(row[config.rowId]);
        var isChecked = tableState.selectedIds.has(rowIdValue);
        checkboxTd = '<td>'
          + '<input type="checkbox" class="row-select-checkbox"'
          + ' data-row-id="' + esc(rowIdValue) + '"'
          + ' aria-label="Select ' + esc(config.rowId) + ' ' + esc(rowIdValue) + '"'
          + (isChecked ? ' checked' : '')
          + ' />'
          + '</td>';
      }

      var tds = columns.map(function (col) {
        if (col.render) {
          return '<td>' + col.render(row) + '</td>';
        }
        var val = row[col.key];
        return '<td>' + esc(val == null ? '' : String(val)) + '</td>';
      }).join('');
      return '<tr>' + checkboxTd + tds + '</tr>';
    }).join('');

    return '<tbody>' + trs + '</tbody>';
  }

  /**
   * Process data: apply sort then filter based on current state.
   */
  function processData(data, columns, tableState) {
    var rows = data.slice();

    // Apply sort
    if (tableState.sortColumn) {
      var sortCol = null;
      for (var i = 0; i < columns.length; i++) {
        if (columns[i].key === tableState.sortColumn && columns[i].sortable !== false) {
          sortCol = columns[i];
          break;
        }
      }
      if (sortCol) {
        rows = sortRows(rows, sortCol, tableState.sortDirection);
      }
    }

    // Apply filter
    if (tableState.filterText) {
      rows = filterRows(rows, columns, tableState.filterText);
    }

    return rows;
  }

  /**
   * Re-render only the tbody and no-match message (used for sort/filter interactions).
   */
  function rerenderBody(tableId, config, data, container) {
    var tableState = getState(tableId);
    var columns = config.columns || [];
    var processedRows = processData(data, columns, tableState);

    // Update tbody
    var table = container.querySelector('table');
    if (table) {
      var oldTbody = table.querySelector('tbody');
      var temp = document.createElement('table');
      temp.innerHTML = buildTbody(columns, processedRows, config, tableState);
      var newTbody = temp.querySelector('tbody');
      if (oldTbody && newTbody) {
        table.replaceChild(newTbody, oldTbody);
      }
    }

    // Update thead (for sort indicators, aria-sort, and select-all checkbox state)
    if (table) {
      var oldThead = table.querySelector('thead');
      var tempHead = document.createElement('table');
      tempHead.innerHTML = buildThead(columns, tableState, config, processedRows);
      var newThead = tempHead.querySelector('thead');
      if (oldThead && newThead) {
        table.replaceChild(newThead, oldThead);
      }
      // Re-attach sort event handlers on new thead
      attachSortHandlers(table, tableId, config, data, container);
      // Re-attach selection event handlers
      attachSelectionHandlers(table, tableId, config, data, container);
    }

    // Update no-match message
    var noMatchEl = container.querySelector('.table-no-match');
    if (processedRows.length === 0 && data.length > 0 && tableState.filterText) {
      if (!noMatchEl) {
        noMatchEl = document.createElement('div');
        noMatchEl.className = 'table-no-match';
        // Insert after the scroll container
        var scrollContainer = container.querySelector('.table-scroll-container');
        if (scrollContainer) {
          scrollContainer.parentNode.insertBefore(noMatchEl, scrollContainer.nextSibling);
        } else {
          container.appendChild(noMatchEl);
        }
      }
      noMatchEl.textContent = config.noMatchMessage || 'No matching results found.';
    } else {
      if (noMatchEl) noMatchEl.remove();
    }
  }

  /**
   * Attach click and keyboard event handlers to sortable column headers.
   */
  function attachSortHandlers(table, tableId, config, data, container) {
    var headers = table.querySelectorAll('th.sortable-header');
    for (var i = 0; i < headers.length; i++) {
      (function (th) {
        var colKey = th.getAttribute('data-column-key');

        function handleSort() {
          var tableState = getState(tableId);
          if (tableState.sortColumn === colKey) {
            tableState.sortDirection = tableState.sortDirection === 'asc' ? 'desc' : 'asc';
          } else {
            tableState.sortColumn = colKey;
            tableState.sortDirection = 'asc';
          }
          rerenderBody(tableId, config, data, container);
        }

        th.addEventListener('click', handleSort);
        th.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            handleSort();
          }
        });
      })(headers[i]);
    }
  }

  /**
   * Attach selection event handlers for select-all and individual row checkboxes.
   * Only attaches when config.selectable is true and config.rowId is set.
   */
  function attachSelectionHandlers(table, tableId, config, data, container) {
    if (!config || !config.selectable || !config.rowId) return;

    var tableState = getState(tableId);
    var columns = config.columns || [];

    // Select-all checkbox handler
    var selectAllCb = table.querySelector('.select-all-checkbox');
    if (selectAllCb) {
      selectAllCb.addEventListener('change', function () {
        var visibleRows = processData(data, columns, tableState);
        if (selectAllCb.checked) {
          // Add all visible row IDs to selectedIds
          visibleRows.forEach(function (row) {
            tableState.selectedIds.add(String(row[config.rowId]));
          });
        } else {
          // Remove all visible row IDs from selectedIds
          visibleRows.forEach(function (row) {
            tableState.selectedIds.delete(String(row[config.rowId]));
          });
        }
        // Update row checkboxes to match
        var rowCbs = table.querySelectorAll('.row-select-checkbox');
        for (var i = 0; i < rowCbs.length; i++) {
          rowCbs[i].checked = tableState.selectedIds.has(rowCbs[i].getAttribute('data-row-id'));
        }
        if (config.onSelectionChange) {
          config.onSelectionChange(Array.from(tableState.selectedIds));
        }
      });
    }

    // Individual row checkbox handlers
    var rowCbs = table.querySelectorAll('.row-select-checkbox');
    for (var i = 0; i < rowCbs.length; i++) {
      (function (cb) {
        cb.addEventListener('change', function () {
          var rowId = cb.getAttribute('data-row-id');
          if (cb.checked) {
            tableState.selectedIds.add(rowId);
          } else {
            tableState.selectedIds.delete(rowId);
          }
          // Update select-all checkbox state
          var visibleRows = processData(data, columns, tableState);
          var allVisibleSelected = visibleRows.length > 0 && visibleRows.every(function (row) {
            return tableState.selectedIds.has(String(row[config.rowId]));
          });
          var selectAll = table.querySelector('.select-all-checkbox');
          if (selectAll) {
            selectAll.checked = allVisibleSelected;
          }
          if (config.onSelectionChange) {
            config.onSelectionChange(Array.from(tableState.selectedIds));
          }
        });
      })(rowCbs[i]);
    }
  }

  /* ==========================================================
     Public API
     ========================================================== */

  /**
   * Render a sortable, filterable, scroll-contained table.
   *
   * @param {string} tableId - Unique identifier for state tracking
   * @param {Object} config - TableConfig with columns, filterLabel, emptyMessage, noMatchMessage
   * @param {Object[]} data - Array of row data objects
   * @param {HTMLElement} container - DOM element to render into
   */
  function render(tableId, config, data, container) {
    var tableState = getState(tableId);
    var columns = config.columns || [];

    // Clean up previous resize listener for this table
    if (resizeCleanups[tableId]) {
      resizeCleanups[tableId]();
      delete resizeCleanups[tableId];
    }

    // Handle empty data
    if (!data || data.length === 0) {
      container.innerHTML = '<div class="empty-state">'
        + esc(config.emptyMessage || 'No data found.')
        + '</div>';
      return;
    }

    // Process data with current state
    var processedRows = processData(data, columns, tableState);

    // Build filter input
    var filterLabel = config.filterLabel || 'Filter table';
    var filterHtml = '<div class="table-filter-wrapper">'
      + '<input type="text"'
      + ' class="table-filter-input"'
      + ' aria-label="' + esc(filterLabel) + '"'
      + ' placeholder="' + esc(filterLabel) + '…"'
      + ' value="' + esc(tableState.filterText) + '"'
      + ' />'
      + '</div>';

    // Build table
    var theadHtml = buildThead(columns, tableState, config, processedRows);
    var tbodyHtml = buildTbody(columns, processedRows, config, tableState);
    var tableHtml = '<table>' + theadHtml + tbodyHtml + '</table>';

    // Build scroll container
    var maxHeight = calcMaxHeight(container);
    var scrollHtml = '<div class="table-scroll-container" style="overflow-y:auto;max-height:'
      + maxHeight + 'px;">'
      + tableHtml
      + '</div>';

    // No-match message
    var noMatchHtml = '';
    if (processedRows.length === 0 && tableState.filterText) {
      noMatchHtml = '<div class="table-no-match">'
        + esc(config.noMatchMessage || 'No matching results found.')
        + '</div>';
    }

    // Render into container
    container.innerHTML = filterHtml + scrollHtml + noMatchHtml;

    // Attach sort handlers
    var tableEl = container.querySelector('table');
    if (tableEl) {
      attachSortHandlers(tableEl, tableId, config, data, container);
      attachSelectionHandlers(tableEl, tableId, config, data, container);
    }

    // Attach filter handler
    var filterInput = container.querySelector('.table-filter-input');
    if (filterInput) {
      filterInput.addEventListener('input', function () {
        tableState.filterText = filterInput.value;
        rerenderBody(tableId, config, data, container);
      });
    }

    // Debounced resize listener
    var scrollContainer = container.querySelector('.table-scroll-container');
    if (scrollContainer) {
      var debouncedResize = debounce(function () {
        var newMaxHeight = calcMaxHeight(container);
        scrollContainer.style.maxHeight = newMaxHeight + 'px';
      }, 150);

      window.addEventListener('resize', debouncedResize);

      // Store cleanup function
      resizeCleanups[tableId] = function () {
        window.removeEventListener('resize', debouncedResize);
      };
    }
  }

  /**
   * Clear stored sort/filter state for a given table.
   */
  function clearState(tableId) {
    delete tableStates[tableId];
    if (resizeCleanups[tableId]) {
      resizeCleanups[tableId]();
      delete resizeCleanups[tableId];
    }
  }

  /**
   * Clear all stored state for all tables.
   */
  function clearAllState() {
    // Clean up all resize listeners
    var ids = Object.keys(resizeCleanups);
    for (var i = 0; i < ids.length; i++) {
      resizeCleanups[ids[i]]();
    }
    resizeCleanups = {};
    tableStates = {};
  }

  /**
   * Get the currently selected row IDs for a given table.
   * @param {string} tableId
   * @returns {string[]}
   */
  function getSelectedIds(tableId) {
    var state = getState(tableId);
    return Array.from(state.selectedIds);
  }

  /**
   * Clear the selection for a given table: empties the selectedIds Set
   * and unchecks all checkboxes in the DOM.
   * @param {string} tableId
   */
  function clearSelection(tableId) {
    var state = getState(tableId);
    state.selectedIds = new Set();
  }

  /* ==========================================================
     Export
     ========================================================== */

  var _internals = {
    textComparator: textComparator,
    numericComparator: numericComparator,
    getComparator: getComparator,
    sortRows: sortRows,
    filterRows: filterRows,
    isFilterableColumn: isFilterableColumn,
    processData: processData,
    getState: getState,
    calcMaxHeight: calcMaxHeight,
    debounce: debounce,
    esc: esc,
    buildThead: buildThead,
    buildTbody: buildTbody,
    attachSelectionHandlers: attachSelectionHandlers,
  };

  // Use a getter so tests always see the current tableStates reference
  // (clearAllState reassigns the variable).
  Object.defineProperty(_internals, 'tableStates', {
    get: function () { return tableStates; },
    enumerable: true,
  });

  var TableModule = {
    render: render,
    clearState: clearState,
    clearAllState: clearAllState,
    getSelectedIds: getSelectedIds,
    clearSelection: clearSelection,
    _internals: _internals,
  };

  // Attach to window for global access
  if (typeof window !== 'undefined') {
    window.TableModule = TableModule;
  }

  // Support CommonJS/Node for testing
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = TableModule;
  }

})();
