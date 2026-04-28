/**
 * HPC Self-Service Portal — Main Application
 *
 * Lightweight single-page application using vanilla JS.
 * Authenticates via Amazon Cognito (USER_PASSWORD_AUTH flow)
 * and calls the API Gateway REST endpoints.
 */

/* ============================================================
   State
   ============================================================ */

const state = {
  idToken: null,
  accessToken: null,
  refreshToken: null,
  user: null,        // { username, email, groups }
  currentPage: null,
  pollTimers: {},    // clusterName → intervalId
  clusterStatusCache: {}, // clusterName → last known status (for transition detection)
  projectStatusCache: {}, // projectId → last known status (for transition detection)
  projectContext: localStorage.getItem('hpc_project_context') || null,
};

/* Foundation stack timestamp and projects data for staleness detection */
let foundationStackTimestamp = null;
let projectsData = [];

/* ============================================================
   Table Column Configurations
   ============================================================ */

const usersTableConfig = {
  selectable: true,
  rowId: 'userId',
  columns: [
    { key: 'userId', label: 'User ID', type: 'text', sortable: true },
    { key: 'displayName', label: 'Display Name', type: 'text', sortable: true },
    { key: 'role', label: 'Role', type: 'text', sortable: true },
    {
      key: 'posixUid', label: 'POSIX UID', type: 'numeric', sortable: true,
      value: (row) => row.posixUid || 0,
      render: (row) => row.posixUid || '—',
    },
    {
      key: 'status', label: 'Status', type: 'text', sortable: true,
      render: (row) => `<span class="badge badge-${(row.status || '').toLowerCase()}">${esc(row.status || 'UNKNOWN')}</span>`,
    },
    {
      key: '_actions', label: 'Actions', type: 'custom', sortable: false,
      render: (row) => {
        if (row.status === 'ACTIVE') return `<button class="btn btn-danger btn-sm" onclick="deleteUser('${esc(row.userId)}')">Deactivate</button>`;
        if (row.status === 'INACTIVE') return `<button class="btn btn-primary btn-sm" onclick="reactivateUser('${esc(row.userId)}')">Reactivate</button>`;
        return '';
      },
    },
  ],
  filterLabel: 'Filter users',
  emptyMessage: 'No users found.',
  noMatchMessage: 'No matching users found.',
};

const projectsTableConfig = {
  selectable: true,
  rowId: 'projectId',
  columns: [
    {
      key: 'projectId', label: 'Project ID', type: 'text', sortable: true,
      render: (row) => `<a href="#" onclick="setProjectContext('${esc(row.projectId)}');navigate('clusters',{projectId:'${esc(row.projectId)}'});return false">${esc(row.projectId)}</a>`,
    },
    { key: 'projectName', label: 'Name', type: 'text', sortable: true },
    {
      key: 'budgetLimit', label: 'Budget', type: 'numeric', sortable: true,
      value: (row) => Number(row.budgetLimit) || 0,
      render: (row) => {
        const budgetDisplay = row.budgetLimit ? '$' + Number(row.budgetLimit).toLocaleString() : 'None';
        const budgetBreach = row.budgetBreached ? ' <span class="badge badge-failed">BREACHED</span>' : '';
        return budgetDisplay + budgetBreach;
      },
    },
    {
      key: 'status', label: 'Status', type: 'text', sortable: true,
      render: (row) => {
        const status = row.status || 'ACTIVE';
        return `<span class="badge badge-${status.toLowerCase()}">${esc(status)}</span>`;
      },
    },
    {
      key: '_actions', label: 'Actions', type: 'custom', sortable: false,
      render: (row) => {
        const status = row.status || 'ACTIVE';
        if (status === 'CREATED') {
          return `<button class="btn btn-primary btn-sm" onclick="deployProject('${esc(row.projectId)}')">Deploy</button>`;
        } else if (status === 'DEPLOYING') {
          const cur = row.currentStep || 0;
          const total = row.totalSteps || 5;
          const pct = total > 0 ? Math.round((cur / total) * 100) : 0;
          const desc = row.stepDescription || 'Deploying…';
          return `<div class="progress-container compact">
            <div class="progress-label">${esc(desc)} (${cur}/${total})</div>
            <div class="progress-bar-track"><div class="progress-bar-fill" style="width:${pct}%">${pct}%</div></div>
          </div>`;
        } else if (status === 'UPDATING') {
          const cur = row.currentStep || 0;
          const total = row.totalSteps || 5;
          const pct = total > 0 ? Math.round((cur / total) * 100) : 0;
          const desc = row.stepDescription || 'Updating…';
          return `<div class="progress-container compact">
            <div class="progress-label">${esc(desc)} (${cur}/${total})</div>
            <div class="progress-bar-track"><div class="progress-bar-fill" style="width:${pct}%">${pct}%</div></div>
          </div>`;
        } else if (status === 'ACTIVE') {
          const isUpToDate = foundationStackTimestamp && row.statusChangedAt && row.statusChangedAt >= foundationStackTimestamp;
          const updateDisabled = isUpToDate ? ' disabled title="Project is up to date"' : '';
          const updateClass = isUpToDate ? 'btn btn-primary btn-sm disabled-btn' : 'btn btn-primary btn-sm';
          return `<button class="btn btn-primary btn-sm" onclick="editProject('${esc(row.projectId)}')">Edit</button>
            <button class="${updateClass}" style="margin-left:0.25rem" onclick="updateProject('${esc(row.projectId)}')"${updateDisabled}>Update</button>
            <button class="btn btn-danger btn-sm" style="margin-left:0.25rem" onclick="showDestroyConfirmation('${esc(row.projectId)}')">Destroy</button>`;
        } else if (status === 'DESTROYING') {
          const cur = row.currentStep || 0;
          const total = row.totalSteps || 5;
          const pct = total > 0 ? Math.round((cur / total) * 100) : 0;
          const desc = row.stepDescription || 'Destroying…';
          return `<div class="progress-container compact">
            <div class="progress-label">${esc(desc)} (${cur}/${total})</div>
            <div class="progress-bar-track"><div class="progress-bar-fill" style="width:${pct}%">${pct}%</div></div>
          </div>`;
        }
        return '';
      },
    },
  ],
  filterLabel: 'Filter projects',
  emptyMessage: 'No projects found.',
  noMatchMessage: 'No matching projects found.',
};

const templatesTableConfig = {
  selectable: true,
  rowId: 'templateId',
  columns: [
    { key: 'templateId', label: 'Template ID', type: 'text', sortable: true },
    { key: 'templateName', label: 'Name', type: 'text', sortable: true },
    {
      key: 'instanceTypes', label: 'Instance Types', type: 'text', sortable: true,
      value: (row) => (row.instanceTypes || []).join(', '),
      render: (row) => esc((row.instanceTypes || []).join(', ')),
    },
    {
      key: 'loginInstanceType', label: 'Login Instance', type: 'text', sortable: true,
      value: (row) => row.loginInstanceType || '—',
      render: (row) => esc(row.loginInstanceType || '—'),
    },
    {
      key: 'nodes', label: 'Nodes (min–max)', type: 'text', sortable: true,
      value: (row) => (row.minNodes || 0) + ' – ' + (row.maxNodes || '∞'),
      render: (row) => esc((row.minNodes || 0) + ' – ' + (row.maxNodes || '∞')),
    },
    {
      key: '_actions', label: 'Actions', type: 'custom', sortable: false,
      render: (row) => `<button class="btn btn-primary btn-sm" onclick="editTemplate('${esc(row.templateId)}')">Edit</button> <button class="btn btn-danger btn-sm" onclick="deleteTemplate('${esc(row.templateId)}')">Delete</button>`,
    },
  ],
  filterLabel: 'Filter templates',
  emptyMessage: 'No templates found.',
  noMatchMessage: 'No matching templates found.',
};

const accountingTableConfig = {
  columns: [
    { key: 'jobId', label: 'Job ID', type: 'text', sortable: true, value: (row) => row.jobId || row.JobID || '—' },
    { key: 'user', label: 'User', type: 'text', sortable: true, value: (row) => row.user || row.User || '—' },
    { key: 'cluster', label: 'Cluster', type: 'text', sortable: true, value: (row) => row.cluster || row.Cluster || '—' },
    { key: 'partition', label: 'Partition', type: 'text', sortable: true, value: (row) => row.partition || row.Partition || '—' },
    { key: 'state', label: 'State', type: 'text', sortable: true, value: (row) => row.state || row.State || '—' },
    { key: 'start', label: 'Start', type: 'text', sortable: true, value: (row) => row.start || row.Start || '—' },
    { key: 'end', label: 'End', type: 'text', sortable: true, value: (row) => row.end || row.End || '—' },
  ],
  filterLabel: 'Filter jobs',
  emptyMessage: 'No job records found.',
  noMatchMessage: 'No matching job records found.',
};

/* ============================================================
   Cognito Authentication (USER_PASSWORD_AUTH via InitiateAuth)
   ============================================================ */

async function cognitoInitiateAuth(username, password) {
  const url = `https://cognito-idp.${CONFIG.cognitoRegion}.amazonaws.com/`;
  const body = {
    AuthFlow: 'USER_PASSWORD_AUTH',
    ClientId: CONFIG.cognitoClientId,
    AuthParameters: {
      USERNAME: username,
      PASSWORD: password,
    },
  };
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-amz-json-1.1',
      'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json();
    throw new Error(err.message || 'Authentication failed');
  }
  const data = await resp.json();

  // Handle NEW_PASSWORD_REQUIRED challenge
  if (data.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
    return { challenge: 'NEW_PASSWORD_REQUIRED', session: data.Session, username };
  }

  return data.AuthenticationResult;
}

async function cognitoRespondNewPassword(username, newPassword, session) {
  const url = `https://cognito-idp.${CONFIG.cognitoRegion}.amazonaws.com/`;
  const body = {
    ChallengeName: 'NEW_PASSWORD_REQUIRED',
    ClientId: CONFIG.cognitoClientId,
    Session: session,
    ChallengeResponses: {
      USERNAME: username,
      NEW_PASSWORD: newPassword,
    },
  };
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-amz-json-1.1',
      'X-Amz-Target': 'AWSCognitoIdentityProviderService.RespondToAuthChallenge',
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json();
    throw new Error(err.message || 'Password change failed');
  }
  const data = await resp.json();
  return data.AuthenticationResult;
}

function parseJwt(token) {
  const base64Url = token.split('.')[1];
  const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
  const json = decodeURIComponent(
    atob(base64).split('').map(c => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)).join('')
  );
  return JSON.parse(json);
}

function setSession(authResult) {
  state.idToken = authResult.IdToken;
  state.accessToken = authResult.AccessToken;
  state.refreshToken = authResult.RefreshToken || state.refreshToken;

  const claims = parseJwt(state.idToken);
  state.user = {
    username: claims['cognito:username'] || claims.email || claims.sub,
    email: claims.email || '',
    groups: claims['cognito:groups'] || [],
  };

  localStorage.setItem('hpc_id_token', state.idToken);
  localStorage.setItem('hpc_access_token', state.accessToken);
  if (state.refreshToken) localStorage.setItem('hpc_refresh_token', state.refreshToken);
}

function clearSession() {
  state.idToken = null;
  state.accessToken = null;
  state.refreshToken = null;
  state.user = null;
  state.projectContext = null;
  Object.values(state.pollTimers).forEach(clearInterval);
  state.pollTimers = {};
  state.clusterStatusCache = {};
  state.projectStatusCache = {};
  localStorage.removeItem('hpc_id_token');
  localStorage.removeItem('hpc_access_token');
  localStorage.removeItem('hpc_refresh_token');
  localStorage.removeItem('hpc_project_context');
}

function tryRestoreSession() {
  const idToken = localStorage.getItem('hpc_id_token');
  if (!idToken) return false;
  try {
    const claims = parseJwt(idToken);
    if (claims.exp * 1000 < Date.now()) {
      clearSession();
      return false;
    }
    state.idToken = idToken;
    state.accessToken = localStorage.getItem('hpc_access_token');
    state.refreshToken = localStorage.getItem('hpc_refresh_token');
    state.user = {
      username: claims['cognito:username'] || claims.email || claims.sub,
      email: claims.email || '',
      groups: claims['cognito:groups'] || [],
    };
    return true;
  } catch {
    clearSession();
    return false;
  }
}

/* ============================================================
   Token Refresh
   ============================================================ */

/**
 * Check whether the current ID token is expired or about to expire
 * (within 5 minutes).  Returns true when a refresh is needed.
 */
function isTokenExpiringSoon() {
  if (!state.idToken) return true;
  try {
    const claims = parseJwt(state.idToken);
    const bufferMs = 5 * 60 * 1000; // refresh 5 min before expiry
    return claims.exp * 1000 - Date.now() < bufferMs;
  } catch {
    return true;
  }
}

/**
 * Use the stored Cognito refresh token to obtain a fresh ID / access
 * token pair.  Updates session state and localStorage on success.
 * Returns true if the refresh succeeded, false otherwise.
 */
async function refreshSession() {
  if (!state.refreshToken) return false;
  try {
    const url = `https://cognito-idp.${CONFIG.cognitoRegion}.amazonaws.com/`;
    const body = {
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: CONFIG.cognitoClientId,
      AuthParameters: {
        REFRESH_TOKEN: state.refreshToken,
      },
    };
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    if (data.AuthenticationResult) {
      setSession(data.AuthenticationResult);
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

/**
 * Ensure we have a valid (non-expired) ID token before making an API
 * call.  Attempts a silent refresh when the token is close to expiry.
 * Clears the session and redirects to login if refresh fails.
 */
async function ensureValidToken() {
  if (!isTokenExpiringSoon()) return;
  const ok = await refreshSession();
  if (!ok) {
    clearSession();
    showToast('Session expired — please sign in again.', 'error');
    renderLoginPage();
    throw new Error('Session expired');
  }
}

/* ============================================================
   API Client
   ============================================================ */

async function apiCall(method, path, body) {
  // Silently refresh the token if it is expired or about to expire
  await ensureValidToken();

  const base = CONFIG.apiBaseUrl.replace(/\/+$/, '');
  const url = `${base}${path}`;
  const headers = {
    'Content-Type': 'application/json',
    Authorization: state.idToken,
  };
  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);

  const resp = await fetch(url, opts);
  const data = await resp.json();
  if (!resp.ok) {
    const msg = data?.error?.message || data?.message || `Request failed (${resp.status})`;
    throw new Error(msg);
  }
  return data;
}

/* ============================================================
   Toast Notifications
   ============================================================ */

function showToast(message, type = 'success') {
  let container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    container.setAttribute('role', 'status');
    container.setAttribute('aria-live', 'polite');
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

/* ============================================================
   Project Context
   ============================================================ */

function setProjectContext(projectId) {
  state.projectContext = projectId || null;
  if (projectId) {
    localStorage.setItem('hpc_project_context', projectId);
  } else {
    localStorage.removeItem('hpc_project_context');
  }
  updateProjectContextIndicator();
  updateMembersTabVisibility();
}

function updateProjectContextIndicator() {
  const el = document.getElementById('project-context-indicator');
  if (!el) return;
  el.textContent = state.projectContext
    ? `Project: ${state.projectContext}`
    : 'No project selected';
  el.classList.toggle('no-project', !state.projectContext);
}

/**
 * Show or hide the Members nav link based on the current user's role
 * and the active project context. Called whenever the project context changes.
 */
function updateMembersTabVisibility() {
  const nav = document.querySelector('nav[aria-label="Main navigation"]');
  if (!nav) return;
  const existing = nav.querySelector('a[data-page="members"]');
  if (canSeeMembers()) {
    if (!existing) {
      const link = document.createElement('a');
      link.href = '#';
      link.dataset.page = 'members';
      link.textContent = 'Members';
      link.addEventListener('click', (e) => {
        e.preventDefault();
        navigate('members');
      });
      // Insert before the Accounting link
      const accountingLink = nav.querySelector('a[data-page="accounting"]');
      if (accountingLink) {
        nav.insertBefore(link, accountingLink);
      } else {
        nav.appendChild(link);
      }
    }
  } else {
    if (existing) {
      existing.remove();
      // If user was on the members page, redirect to clusters
      if (state.currentPage === 'members') {
        navigate('clusters');
      }
    }
  }
}

/* ============================================================
   Role Helpers
   ============================================================ */

/** Returns true when the current user belongs to the Administrators group. */
function isPlatformAdmin() {
  return (state.user?.groups || []).includes('Administrators');
}

/** Returns true when the current user belongs to ProjectAdmin-{projectId}. */
function isProjectAdmin(projectId) {
  if (!projectId) return false;
  return (state.user?.groups || []).includes(`ProjectAdmin-${projectId}`);
}

/** Returns true when the current user should see the Members tab for the active project context. */
function canSeeMembers() {
  if (isPlatformAdmin()) return true;
  if (state.projectContext && isProjectAdmin(state.projectContext)) return true;
  return false;
}

/* ============================================================
   Router
   ============================================================ */

function navigate(page, params = {}) {
  // Stop any active polling when navigating away
  Object.values(state.pollTimers).forEach(clearInterval);
  state.pollTimers = {};

  // Reset table sort/filter state when switching pages
  TableModule.clearAllState();

  state.currentPage = page;
  document.querySelectorAll('nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.page === page);
  });
  renderPage(page, params);
}


/* ============================================================
   Page Renderer
   ============================================================ */

function renderPage(page, params = {}) {
  const main = document.getElementById('main-content');
  if (!main) return;

  switch (page) {
    case 'users':      renderUsersPage(main); break;
    case 'projects':   renderProjectsPage(main); break;
    case 'templates':  renderTemplatesPage(main); break;
    case 'clusters':   renderClustersPage(main, params); break;
    case 'cluster-detail': renderClusterDetailPage(main, params); break;
    case 'accounting': renderAccountingPage(main); break;
    case 'members':    renderMembersPage(main); break;
    default:           renderClustersPage(main);
  }
}

/* ============================================================
   Users Page
   ============================================================ */

function renderUsersPage(container) {
  usersTableConfig.onSelectionChange = function (selectedIds) {
    const toolbar = document.getElementById('users-bulk-toolbar');
    if (!toolbar) return;
    if (selectedIds.length > 0) {
      toolbar.style.display = '';
      toolbar.querySelector('.bulk-selection-count').textContent = selectedIds.length + ' selected';
    } else {
      toolbar.style.display = 'none';
    }
  };

  container.innerHTML = `
    <div class="page-header">
      <h2>User Management</h2>
      <button class="btn btn-primary" id="btn-add-user">Add User</button>
    </div>
    <div id="users-bulk-toolbar" class="bulk-action-toolbar" role="toolbar" aria-label="Bulk actions" aria-live="polite" style="display:none">
      <span class="bulk-selection-count"></span>
      <button class="btn btn-danger btn-sm" id="btn-bulk-deactivate-users">Deactivate All</button>
      <button class="btn btn-primary btn-sm" id="btn-bulk-reactivate-users">Reactivate All</button>
      <button class="btn btn-sm" id="btn-bulk-clear-users">Clear Selection</button>
    </div>
    <div id="users-list"><div class="empty-state"><span class="spinner"></span> Loading users…</div></div>
    <div id="add-user-form" style="display:none" class="detail-panel">
      <h3>Add New User</h3>
      <div class="form-group">
        <label for="new-user-id">User ID</label>
        <input type="text" id="new-user-id" placeholder="jsmith" />
      </div>
      <div class="form-group">
        <label for="new-user-name">Display Name</label>
        <input type="text" id="new-user-name" placeholder="Jane Doe" />
      </div>
      <div class="form-group">
        <label for="new-user-email">Email</label>
        <input type="email" id="new-user-email" placeholder="jane.smith@example.com" />
      </div>
      <div class="form-group">
        <label for="new-user-role">Role</label>
        <select id="new-user-role">
          <option value="User">User</option>
          <option value="Administrator">Administrator</option>
        </select>
      </div>
      <button class="btn btn-primary" id="btn-submit-user">Create User</button>
      <button class="btn" id="btn-cancel-user" style="margin-left:0.5rem">Cancel</button>
    </div>
  `;

  document.getElementById('btn-add-user').addEventListener('click', () => {
    document.getElementById('add-user-form').style.display = 'block';
  });
  document.getElementById('btn-cancel-user').addEventListener('click', () => {
    document.getElementById('add-user-form').style.display = 'none';
  });
  document.getElementById('btn-submit-user').addEventListener('click', async () => {
    const userId = document.getElementById('new-user-id').value.trim();
    const displayName = document.getElementById('new-user-name').value.trim();
    const email = document.getElementById('new-user-email').value.trim();
    const role = document.getElementById('new-user-role').value;
    if (!userId) return showToast('User ID is required', 'error');
    if (!email) return showToast('Email is required', 'error');
    try {
      await apiCall('POST', '/users', { userId, displayName, email, role });
      showToast(`User '${userId}' created`);
      document.getElementById('add-user-form').style.display = 'none';
      loadUsers();
    } catch (e) { showToast(e.message, 'error'); }
  });

  document.getElementById('btn-bulk-clear-users').addEventListener('click', () => {
    TableModule.clearSelection('users');
    const toolbar = document.getElementById('users-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
  });

  document.getElementById('btn-bulk-deactivate-users').addEventListener('click', () => {
    const ids = TableModule.getSelectedIds('users');
    if (ids.length > 0) bulkDeactivateUsers(ids);
  });

  document.getElementById('btn-bulk-reactivate-users').addEventListener('click', () => {
    const ids = TableModule.getSelectedIds('users');
    if (ids.length > 0) bulkReactivateUsers(ids);
  });

  loadUsers();
}

async function loadUsers() {
  try {
    const data = await apiCall('GET', '/users');
    const users = data.users || [];
    const el = document.getElementById('users-list');
    TableModule.render('users', usersTableConfig, users, el);
  } catch (e) {
    document.getElementById('users-list').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}

async function deleteUser(userId) {
  if (!confirm(`Deactivate user '${userId}'?`)) return;
  try {
    await apiCall('DELETE', `/users/${encodeURIComponent(userId)}`);
    showToast(`User '${userId}' deactivated`);
    loadUsers();
  } catch (e) { showToast(e.message, 'error'); }
}

async function reactivateUser(userId) {
  if (!confirm(`Reactivate user '${userId}'?`)) return;
  try {
    await apiCall('POST', `/users/${encodeURIComponent(userId)}/reactivate`);
    showToast(`User '${userId}' reactivated`);
    loadUsers();
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Bulk User Actions
   ============================================================ */

async function bulkDeactivateUsers(ids) {
  try {
    const data = await apiCall('POST', '/users/batch/deactivate', { userIds: ids });
    const s = data.summary || {};
    const toastType = s.failed > 0 ? 'error' : 'success';
    showToast(`${s.succeeded} of ${s.total} succeeded, ${s.failed} failed`, toastType);
    TableModule.clearSelection('users');
    const toolbar = document.getElementById('users-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
    loadUsers();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function bulkReactivateUsers(ids) {
  try {
    const data = await apiCall('POST', '/users/batch/reactivate', { userIds: ids });
    const s = data.summary || {};
    const toastType = s.failed > 0 ? 'error' : 'success';
    showToast(`${s.succeeded} of ${s.total} succeeded, ${s.failed} failed`, toastType);
    TableModule.clearSelection('users');
    const toolbar = document.getElementById('users-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
    loadUsers();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

/* ============================================================
   Projects Page
   ============================================================ */

function renderProjectsPage(container) {
  projectsTableConfig.onSelectionChange = function (selectedIds) {
    const toolbar = document.getElementById('projects-bulk-toolbar');
    if (!toolbar) return;
    if (selectedIds.length > 0) {
      toolbar.style.display = '';
      toolbar.querySelector('.bulk-selection-count').textContent = selectedIds.length + ' selected';

      // Disable "Update All" if all selected ACTIVE projects are up to date
      const updateAllBtn = document.getElementById('btn-bulk-update-projects');
      if (updateAllBtn) {
        const hasStale = selectedIds.some(id => {
          const project = projectsData.find(p => p.projectId === id);
          if (!project || project.status !== 'ACTIVE') return false;
          if (!foundationStackTimestamp || !project.statusChangedAt) return true;
          return project.statusChangedAt < foundationStackTimestamp;
        });
        const hasActiveSelected = selectedIds.some(id => {
          const project = projectsData.find(p => p.projectId === id);
          return project && project.status === 'ACTIVE';
        });
        // Disable if there are ACTIVE projects selected but none are stale
        updateAllBtn.disabled = hasActiveSelected && !hasStale;
      }
    } else {
      toolbar.style.display = 'none';
    }
  };

  container.innerHTML = `
    <div class="page-header">
      <h2>Project Management</h2>
      <button class="btn btn-primary" id="btn-add-project">Create Project</button>
    </div>
    <div id="projects-bulk-toolbar" class="bulk-action-toolbar" role="toolbar" aria-label="Bulk actions" aria-live="polite" style="display:none">
      <span class="bulk-selection-count"></span>
      <button class="btn btn-primary btn-sm" id="btn-bulk-deploy-projects">Deploy All</button>
      <button class="btn btn-primary btn-sm" id="btn-bulk-update-projects">Update All</button>
      <button class="btn btn-danger btn-sm" id="btn-bulk-destroy-projects">Destroy All</button>
      <button class="btn btn-sm" id="btn-bulk-clear-projects">Clear Selection</button>
    </div>
    <div id="projects-list"><div class="empty-state"><span class="spinner"></span> Loading projects…</div></div>
    <div id="add-project-form" style="display:none" class="detail-panel">
      <h3>Create New Project</h3>
      <div class="form-group">
        <label for="new-project-id">Project ID</label>
        <input type="text" id="new-project-id" placeholder="my-research-project" />
      </div>
      <div class="form-group">
        <label for="new-project-name">Project Name</label>
        <input type="text" id="new-project-name" placeholder="My Research Project" />
      </div>
      <button class="btn btn-primary" id="btn-submit-project">Create</button>
      <button class="btn" id="btn-cancel-project" style="margin-left:0.5rem">Cancel</button>
    </div>
  `;

  document.getElementById('btn-add-project').addEventListener('click', () => {
    document.getElementById('add-project-form').style.display = 'block';
  });
  document.getElementById('btn-cancel-project').addEventListener('click', () => {
    document.getElementById('add-project-form').style.display = 'none';
  });
  document.getElementById('btn-submit-project').addEventListener('click', async () => {
    const projectId = document.getElementById('new-project-id').value.trim();
    const projectName = document.getElementById('new-project-name').value.trim();
    if (!projectId) return showToast('Project ID is required', 'error');
    try {
      await apiCall('POST', '/projects', { projectId, projectName });
      showToast(`Project '${projectId}' created`);
      document.getElementById('add-project-form').style.display = 'none';
      loadProjects();
    } catch (e) { showToast(e.message, 'error'); }
  });

  document.getElementById('btn-bulk-clear-projects').addEventListener('click', () => {
    TableModule.clearSelection('projects');
    const toolbar = document.getElementById('projects-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
  });

  document.getElementById('btn-bulk-deploy-projects').addEventListener('click', () => {
    const ids = TableModule.getSelectedIds('projects');
    if (ids.length > 0) bulkDeployProjects(ids);
  });

  document.getElementById('btn-bulk-update-projects').addEventListener('click', () => {
    const ids = TableModule.getSelectedIds('projects');
    if (ids.length > 0) bulkUpdateProjects(ids);
  });

  document.getElementById('btn-bulk-destroy-projects').addEventListener('click', () => {
    const ids = TableModule.getSelectedIds('projects');
    if (ids.length > 0) bulkDestroyProjects(ids);
  });

  loadProjects();
}

async function loadProjects() {
  try {
    const data = await apiCall('GET', '/projects');
    const projects = data.projects || [];
    foundationStackTimestamp = data.foundationStackTimestamp || null;
    projectsData = projects;
    const el = document.getElementById('projects-list');
    if (!el) return;

    TableModule.render('projects', projectsTableConfig, projects, el);

    // Start polling if any projects are in transitional states
    const transitional = projects.filter(p => ['DEPLOYING', 'DESTROYING', 'UPDATING'].includes(p.status));
    if (transitional.length > 0) {
      startProjectListPolling();
    } else {
      stopProjectListPolling();
    }

    // Detect status transitions for toast notifications
    projects.forEach(p => {
      const prev = state.projectStatusCache[p.projectId];
      const cur = p.status || 'ACTIVE';
      if (prev === 'DEPLOYING' && cur === 'ACTIVE') {
        showToast(`Project '${p.projectId}' is now ACTIVE`, 'success');
      } else if (prev === 'DEPLOYING' && cur === 'CREATED') {
        showToast(`Project '${p.projectId}' deployment failed`, 'error');
      } else if (prev === 'DESTROYING' && cur === 'ARCHIVED') {
        showToast(`Project '${p.projectId}' has been archived`, 'success');
      } else if (prev === 'UPDATING' && cur === 'ACTIVE') {
        if (p.errorMessage) {
          showToast(`Project '${p.projectId}' update failed: ${p.errorMessage}`, 'error');
        } else {
          showToast(`Project '${p.projectId}' update completed`, 'success');
        }
      }
      state.projectStatusCache[p.projectId] = cur;
    });

  } catch (e) {
    // During background polling, don't replace the visible project table
    // with an error message — that causes the UI to flicker between the
    // table and an error box on every transient failure.  Only show the
    // error when there is no existing content (i.e. the initial load).
    const el = document.getElementById('projects-list');
    if (el && !el.querySelector('table')) {
      el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }
  }
}

async function deployProject(projectId) {
  try {
    await apiCall('POST', `/projects/${encodeURIComponent(projectId)}/deploy`);
    showToast(`Project '${projectId}' deployment started`);
    loadProjects();
  } catch (e) { showToast(e.message, 'error'); }
}

async function updateProject(projectId) {
  try {
    await apiCall('POST', `/projects/${encodeURIComponent(projectId)}/update`);
    showToast(`Project '${projectId}' update started`);
    loadProjects();
  } catch (e) { showToast(e.message, 'error'); }
}

async function editProject(projectId) {
  try {
    const project = await apiCall('GET', `/projects/${encodeURIComponent(projectId)}`);
    showEditProjectDialog(project);
  } catch (e) { showToast(e.message, 'error'); }
}

function showDestroyConfirmation(projectId) {
  // Remove any existing modal
  const existing = document.getElementById('destroy-confirm-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'destroy-confirm-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-content">
      <h3>Destroy Project</h3>
      <p>Are you sure you want to destroy project <strong>${esc(projectId)}</strong>?</p>
      <p style="font-size:0.85rem;color:var(--color-text-muted)">This will delete all infrastructure (VPC, EFS, S3 bucket, security groups). Type the project ID to confirm.</p>
      <div class="form-group">
        <label for="destroy-confirm-input">Project ID</label>
        <input type="text" id="destroy-confirm-input" placeholder="Type project ID to confirm" autocomplete="off" />
      </div>
      <div style="display:flex;gap:0.5rem;justify-content:flex-end">
        <button class="btn" id="destroy-cancel-btn">Cancel</button>
        <button class="btn btn-danger" id="destroy-confirm-btn" disabled>Destroy</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const input = document.getElementById('destroy-confirm-input');
  const confirmBtn = document.getElementById('destroy-confirm-btn');

  input.addEventListener('input', () => {
    confirmBtn.disabled = input.value.trim() !== projectId;
  });

  document.getElementById('destroy-cancel-btn').addEventListener('click', () => {
    modal.remove();
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  confirmBtn.addEventListener('click', async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Destroying…';
    try {
      await apiCall('POST', `/projects/${encodeURIComponent(projectId)}/destroy`);
      showToast(`Project '${projectId}' destruction started`);
      modal.remove();
      loadProjects();
    } catch (e) {
      showToast(e.message, 'error');
      modal.remove();
    }
  });

  input.focus();
}

function showEditProjectDialog(project) {
  // Remove any existing modal
  const existing = document.getElementById('edit-project-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'edit-project-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-content">
      <h3>Edit Project</h3>
      <div class="form-group">
        <label for="edit-project-id">Project ID</label>
        <input type="text" id="edit-project-id" value="${esc(project.projectId || '')}" disabled class="input-disabled" />
      </div>
      <div class="form-group">
        <label for="edit-project-name">Project Name</label>
        <input type="text" id="edit-project-name" value="${esc(project.projectName || '')}" disabled class="input-disabled" />
      </div>
      <div class="form-group">
        <label for="edit-cost-tag">Cost Allocation Tag</label>
        <input type="text" id="edit-cost-tag" value="${esc(project.costAllocationTag || '')}" disabled class="input-disabled" />
      </div>
      <div class="form-group">
        <label for="edit-budget-limit">Budget Limit ($)</label>
        <input type="number" id="edit-budget-limit" value="${project.budgetLimit || 50}" min="1" step="1" />
      </div>
      <div class="form-group">
        <label for="edit-budget-type">Budget Type</label>
        <select id="edit-budget-type">
          <option value="MONTHLY" ${(project.budgetType || 'MONTHLY') === 'MONTHLY' ? 'selected' : ''}>Monthly</option>
          <option value="TOTAL" ${project.budgetType === 'TOTAL' ? 'selected' : ''}>Total (Project Lifetime)</option>
        </select>
      </div>
      <div style="display:flex;gap:0.5rem;justify-content:flex-end">
        <button class="btn" id="edit-cancel-btn">Cancel</button>
        <button class="btn btn-primary" id="edit-save-btn">Save</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  document.getElementById('edit-cancel-btn').addEventListener('click', () => {
    modal.remove();
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  document.getElementById('edit-save-btn').addEventListener('click', async () => {
    const budgetLimit = parseFloat(document.getElementById('edit-budget-limit').value);
    const budgetType = document.getElementById('edit-budget-type').value;

    if (!budgetLimit || budgetLimit <= 0) {
      showToast('Budget limit must be a positive number', 'error');
      return;
    }

    const saveBtn = document.getElementById('edit-save-btn');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';

    try {
      await apiCall('PUT', `/projects/${encodeURIComponent(project.projectId)}`, {
        budgetLimit,
        budgetType,
      });
      showToast(`Project '${project.projectId}' updated`);
      modal.remove();
      loadProjects();
    } catch (e) {
      showToast(e.message, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
}

/* ============================================================
   Bulk Project Actions
   ============================================================ */

async function bulkUpdateProjects(ids) {
  // Filter to only stale projects (statusChangedAt < foundationStackTimestamp)
  const staleIds = ids.filter(id => {
    const project = projectsData.find(p => p.projectId === id);
    if (!project || project.status !== 'ACTIVE') return true; // let backend handle non-ACTIVE
    if (!foundationStackTimestamp || !project.statusChangedAt) return true; // no timestamp info, include
    return project.statusChangedAt < foundationStackTimestamp;
  });

  if (staleIds.length === 0) {
    showToast('All selected projects are already up to date', 'success');
    return;
  }

  try {
    const data = await apiCall('POST', '/projects/batch/update', { projectIds: staleIds });
    const s = data.summary || {};
    const toastType = s.failed > 0 ? 'error' : 'success';
    showToast(`${s.succeeded} of ${s.total} succeeded, ${s.failed} failed`, toastType);
    TableModule.clearSelection('projects');
    const toolbar = document.getElementById('projects-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
    loadProjects();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function bulkDeployProjects(ids) {
  try {
    const data = await apiCall('POST', '/projects/batch/deploy', { projectIds: ids });
    const s = data.summary || {};
    const toastType = s.failed > 0 ? 'error' : 'success';
    showToast(`${s.succeeded} of ${s.total} succeeded, ${s.failed} failed`, toastType);
    TableModule.clearSelection('projects');
    const toolbar = document.getElementById('projects-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
    loadProjects();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function bulkDestroyProjects(ids) {
  // Remove any existing modal
  const existing = document.getElementById('bulk-destroy-confirm-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'bulk-destroy-confirm-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-content">
      <h3>Bulk Destroy Projects</h3>
      <p>Are you sure you want to destroy <strong>${ids.length}</strong> project(s)?</p>
      <p style="font-size:0.85rem;color:var(--color-text-muted)">This will delete all infrastructure for the selected projects. Type <strong>CONFIRM</strong> to proceed.</p>
      <div class="form-group">
        <label for="bulk-destroy-confirm-input">Type CONFIRM</label>
        <input type="text" id="bulk-destroy-confirm-input" placeholder="Type CONFIRM to proceed" autocomplete="off" />
      </div>
      <div style="display:flex;gap:0.5rem;justify-content:flex-end">
        <button class="btn" id="bulk-destroy-cancel-btn">Cancel</button>
        <button class="btn btn-danger" id="bulk-destroy-confirm-btn" disabled>Destroy All</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const input = document.getElementById('bulk-destroy-confirm-input');
  const confirmBtn = document.getElementById('bulk-destroy-confirm-btn');

  input.addEventListener('input', () => {
    confirmBtn.disabled = input.value.trim() !== 'CONFIRM';
  });

  document.getElementById('bulk-destroy-cancel-btn').addEventListener('click', () => {
    modal.remove();
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  confirmBtn.addEventListener('click', async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Destroying…';
    try {
      const data = await apiCall('POST', '/projects/batch/destroy', { projectIds: ids });
      const s = data.summary || {};
      const toastType = s.failed > 0 ? 'error' : 'success';
      showToast(`${s.succeeded} of ${s.total} succeeded, ${s.failed} failed`, toastType);
      modal.remove();
      TableModule.clearSelection('projects');
      const toolbar = document.getElementById('projects-bulk-toolbar');
      if (toolbar) toolbar.style.display = 'none';
      loadProjects();
    } catch (e) {
      showToast(e.message, 'error');
      modal.remove();
    }
  });

  input.focus();
}

/* ============================================================
   Project List Polling
   ============================================================ */

function startProjectListPolling() {
  const key = 'project-list-poll';
  if (state.pollTimers[key]) return; // already polling
  state.pollTimers[key] = setInterval(() => {
    if (state.currentPage !== 'projects') {
      stopProjectListPolling();
      return;
    }
    loadProjects();
  }, CONFIG.projectPollIntervalMs);
}

function stopProjectListPolling() {
  const key = 'project-list-poll';
  if (state.pollTimers[key]) {
    clearInterval(state.pollTimers[key]);
    delete state.pollTimers[key];
  }
}


/* ============================================================
   Templates Page
   ============================================================ */

/**
 * Instance type family prefixes supported by AWS PCS.
 * Must stay in sync with VALID_INSTANCE_TYPE_PREFIXES in templates.py.
 */
const PCS_VALID_INSTANCE_PREFIXES = [
  'c5', 'c5n', 'c6g', 'c6i', 'c6gn', 'c7g', 'c7gn', 'c7i',
  'm5', 'm6g', 'm6i', 'm7g', 'm7i',
  'r5', 'r6g', 'r6i', 'r7g', 'r7i',
  'g4dn', 'g5', 'g6', 'p3', 'p4d', 'p5',
  'hpc6a', 'hpc6id', 'hpc7a', 'hpc7g',
  't3', 't3a', 't4g',
  'x2idn', 'x2iedn',
  'trn1', 'inf1', 'inf2',
  'dl1',
];

/** Graviton (arm64) instance family prefixes. All others are x86_64. */
const ARM64_INSTANCE_PREFIXES = [
  'c6g', 'c6gn', 'c7g', 'c7gn',
  'm6g', 'm7g',
  'r6g', 'r7g',
  'hpc7g',
  't4g',
];

/**
 * Validate that an instance type string matches a PCS-supported family.
 * Returns the prefix that matched, or null if invalid.
 */
function validateInstanceType(instanceType) {
  const lower = instanceType.toLowerCase().trim();
  // Sort longest-first so e.g. "c5n" matches before "c5"
  const sorted = [...PCS_VALID_INSTANCE_PREFIXES].sort((a, b) => b.length - a.length);
  for (const prefix of sorted) {
    if (lower.startsWith(prefix + '.')) return prefix;
  }
  return null;
}

/**
 * Infer CPU architecture from a list of instance types.
 * Returns "arm64" if all types are Graviton, "x86_64" otherwise.
 * Returns null if the list is empty.
 */
function inferArchitecture(instanceTypes) {
  if (!instanceTypes.length) return null;
  const sortedArm = [...ARM64_INSTANCE_PREFIXES].sort((a, b) => b.length - a.length);
  const allArm = instanceTypes.every(it => {
    const lower = it.toLowerCase().trim();
    return sortedArm.some(prefix => lower.startsWith(prefix + '.'));
  });
  return allArm ? 'arm64' : 'x86_64';
}

/**
 * Fetch the latest PCS sample AMI for the given architecture.
 * Returns { amiId, name, architecture, creationDate } or null on failure.
 */
async function fetchDefaultAmi(arch) {
  try {
    return await apiCall('GET', `/templates/default-ami?arch=${encodeURIComponent(arch)}`);
  } catch {
    return null;
  }
}

function renderTemplatesPage(container) {
  templatesTableConfig.onSelectionChange = function (selectedIds) {
    const toolbar = document.getElementById('templates-bulk-toolbar');
    if (!toolbar) return;
    if (selectedIds.length > 0) {
      toolbar.style.display = '';
      toolbar.querySelector('.bulk-selection-count').textContent = selectedIds.length + ' selected';
    } else {
      toolbar.style.display = 'none';
    }
  };

  container.innerHTML = `
    <div class="page-header">
      <h2>Cluster Templates</h2>
      <button class="btn btn-primary" id="btn-add-template">Create Template</button>
    </div>
    <div id="templates-bulk-toolbar" class="bulk-action-toolbar" role="toolbar" aria-label="Bulk actions" aria-live="polite" style="display:none">
      <span class="bulk-selection-count"></span>
      <button class="btn btn-danger btn-sm" id="btn-bulk-delete-templates">Delete All</button>
      <button class="btn btn-sm" id="btn-bulk-clear-templates">Clear Selection</button>
    </div>
    <div id="templates-list"><div class="empty-state"><span class="spinner"></span> Loading templates…</div></div>
    <div id="add-template-form" style="display:none" class="detail-panel">
      <h3>Create New Template</h3>
      <div class="form-group">
        <label for="new-tpl-id">Template ID</label>
        <input type="text" id="new-tpl-id" placeholder="cpu-general" />
      </div>
      <div class="form-group">
        <label for="new-tpl-name">Template Name</label>
        <input type="text" id="new-tpl-name" placeholder="General CPU Workloads" />
      </div>
      <div class="form-group">
        <label for="new-tpl-desc">Description</label>
        <input type="text" id="new-tpl-desc" placeholder="Cost-effective CPU cluster for general HPC workloads" />
      </div>
      <div class="form-group">
        <label for="new-tpl-instance">Compute Instance Types (comma-separated)</label>
        <input type="text" id="new-tpl-instance" placeholder="c7g.medium, c7g.xlarge" />
        <small class="form-hint">PCS-supported families: c5–c7i, m5–m7i, r5–r7i, g4dn–g6, p3–p5, hpc6a–hpc7g, t3–t4g, trn1, inf1–inf2, etc.</small>
      </div>
      <div class="form-group">
        <label for="new-tpl-login-instance">Login Node Instance Type</label>
        <input type="text" id="new-tpl-login-instance" value="t3.medium" placeholder="t3.medium" />
        <small class="form-hint">Instance type for the login/head node. Typically a small general-purpose instance.</small>
      </div>
      <div class="form-group">
        <label for="new-tpl-min">Min Nodes</label>
        <input type="number" id="new-tpl-min" value="0" min="0" />
      </div>
      <div class="form-group">
        <label for="new-tpl-max">Max Nodes</label>
        <input type="number" id="new-tpl-max" value="4" min="1" />
      </div>
      <div class="form-group">
        <label for="new-tpl-ami">Compute AMI ID</label>
        <div style="display:flex;gap:0.5rem">
          <input type="text" id="new-tpl-ami" placeholder="Auto-detected from instance types" style="flex:1" />
          <button class="btn" type="button" id="btn-detect-ami">Detect</button>
        </div>
        <small class="form-hint" id="ami-hint">Enter compute instance types above, then click Detect to find the latest PCS sample AMI.</small>
      </div>
      <div class="form-group">
        <label for="new-tpl-login-ami">Login Node AMI ID (optional)</label>
        <input type="text" id="new-tpl-login-ami" placeholder="Same as compute AMI if blank" />
        <small class="form-hint">Only needed when the login node uses a different architecture than compute nodes.</small>
      </div>
      <fieldset class="form-fieldset">
        <legend>Software Stack</legend>
        <div class="form-group">
          <label for="new-tpl-scheduler">Scheduler</label>
          <select id="new-tpl-scheduler">
            <option value="slurm" selected>Slurm</option>
          </select>
        </div>
        <div class="form-group">
          <label for="new-tpl-scheduler-ver">Scheduler Version</label>
          <select id="new-tpl-scheduler-ver">
            <option value="24.11">24.11</option>
            <option value="25.05">25.05</option>
            <option value="25.11" selected>25.11</option>
          </select>
        </div>
        <div class="form-group">
          <label for="new-tpl-cuda">CUDA Version (optional, for GPU templates)</label>
          <input type="text" id="new-tpl-cuda" placeholder="12.4" />
        </div>
      </fieldset>
      <button class="btn btn-primary" id="btn-submit-template">Create</button>
      <button class="btn" id="btn-cancel-template" style="margin-left:0.5rem">Cancel</button>
    </div>
  `;

  document.getElementById('btn-add-template').addEventListener('click', () => {
    document.getElementById('add-template-form').style.display = 'block';
  });
  document.getElementById('btn-cancel-template').addEventListener('click', () => {
    document.getElementById('add-template-form').style.display = 'none';
  });

  // Auto-detect AMI when instance types field loses focus
  async function detectAndPopulateAmi() {
    const instanceInput = document.getElementById('new-tpl-instance').value;
    const types = instanceInput.split(',').map(s => s.trim()).filter(Boolean);
    if (!types.length) return;
    const arch = inferArchitecture(types);
    if (!arch) return;
    const hint = document.getElementById('ami-hint');
    const amiInput = document.getElementById('new-tpl-ami');
    hint.textContent = `Looking up latest PCS sample AMI for ${arch}…`;
    const result = await fetchDefaultAmi(arch);
    if (result && result.amiId) {
      amiInput.value = result.amiId;
      hint.textContent = `${result.name || result.amiId} (${arch})`;
    } else {
      hint.textContent = `Could not find a PCS sample AMI for ${arch}. Enter an AMI ID manually.`;
    }
  }

  document.getElementById('new-tpl-instance').addEventListener('blur', () => {
    // Only auto-detect if the AMI field is empty
    if (!document.getElementById('new-tpl-ami').value.trim()) {
      detectAndPopulateAmi();
    }
  });
  document.getElementById('btn-detect-ami').addEventListener('click', detectAndPopulateAmi);
  document.getElementById('btn-submit-template').addEventListener('click', async () => {
    const templateId = document.getElementById('new-tpl-id').value.trim();
    const templateName = document.getElementById('new-tpl-name').value.trim();
    const description = document.getElementById('new-tpl-desc').value.trim();
    const instanceTypes = document.getElementById('new-tpl-instance').value.split(',').map(s => s.trim()).filter(Boolean);
    const loginInstanceType = document.getElementById('new-tpl-login-instance').value.trim();
    const minNodes = parseInt(document.getElementById('new-tpl-min').value, 10) || 0;
    const maxNodes = parseInt(document.getElementById('new-tpl-max').value, 10) || 4;
    const amiId = document.getElementById('new-tpl-ami').value.trim();
    const loginAmiId = document.getElementById('new-tpl-login-ami').value.trim();
    const scheduler = document.getElementById('new-tpl-scheduler').value;
    const schedulerVersion = document.getElementById('new-tpl-scheduler-ver').value.trim();
    const cudaVersion = document.getElementById('new-tpl-cuda').value.trim();

    if (!templateId) return showToast('Template ID is required', 'error');
    if (!templateName) return showToast('Template Name is required', 'error');
    if (!instanceTypes.length) return showToast('At least one compute instance type is required', 'error');
    if (!loginInstanceType) return showToast('Login node instance type is required', 'error');
    if (!amiId) return showToast('AMI ID is required', 'error');
    if (minNodes > maxNodes) return showToast('Min nodes cannot exceed max nodes', 'error');

    // Validate all instance types against PCS-supported families
    const allTypes = [...instanceTypes, loginInstanceType];
    for (const it of allTypes) {
      if (!validateInstanceType(it)) {
        return showToast(`Instance type '${it}' is not a PCS-supported family. Supported: c5–c7i, m5–m7i, r5–r7i, g4dn–g6, p3–p5, hpc6a–hpc7g, t3–t4g, trn1, inf1–inf2, dl1, x2idn, x2iedn.`, 'error');
      }
    }

    const softwareStack = { scheduler, schedulerVersion };
    if (cudaVersion) softwareStack.cudaVersion = cudaVersion;

    try {
      const body = {
        templateId, templateName, description, instanceTypes,
        loginInstanceType, minNodes, maxNodes, amiId, softwareStack,
      };
      if (loginAmiId) body.loginAmiId = loginAmiId;
      await apiCall('POST', '/templates', body);
      showToast(`Template '${templateId}' created`);
      document.getElementById('add-template-form').style.display = 'none';
      loadTemplates();
    } catch (e) { showToast(e.message, 'error'); }
  });

  document.getElementById('btn-bulk-clear-templates').addEventListener('click', () => {
    TableModule.clearSelection('templates');
    const toolbar = document.getElementById('templates-bulk-toolbar');
    if (toolbar) toolbar.style.display = 'none';
  });

  document.getElementById('btn-bulk-delete-templates').addEventListener('click', () => {
    const ids = TableModule.getSelectedIds('templates');
    if (ids.length > 0) bulkDeleteTemplates(ids);
  });

  loadTemplates();
}

async function loadTemplates() {
  try {
    const data = await apiCall('GET', '/templates');
    const templates = data.templates || [];
    const el = document.getElementById('templates-list');
    TableModule.render('templates', templatesTableConfig, templates, el);
  } catch (e) {
    document.getElementById('templates-list').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}

async function editTemplate(templateId) {
  try {
    const template = await apiCall('GET', `/templates/${encodeURIComponent(templateId)}`);
    showEditTemplateDialog(template);
  } catch (e) { showToast(e.message, 'error'); }
}

function showEditTemplateDialog(template) {
  // Remove any existing modal
  const existing = document.getElementById('edit-template-modal');
  if (existing) existing.remove();

  const sw = template.softwareStack || {};

  const modal = document.createElement('div');
  modal.id = 'edit-template-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-content" style="max-width:520px">
      <h3>Edit Template</h3>
      <div class="form-group">
        <label for="edit-tpl-id">Template ID</label>
        <input type="text" id="edit-tpl-id" value="${esc(template.templateId || '')}" disabled class="input-disabled" />
      </div>
      <div class="form-group">
        <label for="edit-tpl-name">Template Name</label>
        <input type="text" id="edit-tpl-name" value="${esc(template.templateName || '')}" />
      </div>
      <div class="form-group">
        <label for="edit-tpl-desc">Description</label>
        <input type="text" id="edit-tpl-desc" value="${esc(template.description || '')}" />
      </div>
      <div class="form-group">
        <label for="edit-tpl-instance">Compute Instance Types (comma-separated)</label>
        <input type="text" id="edit-tpl-instance" value="${esc((template.instanceTypes || []).join(', '))}" />
        <small class="form-hint">PCS-supported families: c5–c7i, m5–m7i, r5–r7i, g4dn–g6, p3–p5, hpc6a–hpc7g, t3–t4g, trn1, inf1–inf2, etc.</small>
      </div>
      <div class="form-group">
        <label for="edit-tpl-login-instance">Login Node Instance Type</label>
        <input type="text" id="edit-tpl-login-instance" value="${esc(template.loginInstanceType || '')}" />
        <small class="form-hint">Instance type for the login/head node. Typically a small general-purpose instance.</small>
      </div>
      <div class="form-group">
        <label for="edit-tpl-min">Min Nodes</label>
        <input type="number" id="edit-tpl-min" value="${template.minNodes != null ? template.minNodes : 0}" min="0" />
      </div>
      <div class="form-group">
        <label for="edit-tpl-max">Max Nodes</label>
        <input type="number" id="edit-tpl-max" value="${template.maxNodes != null ? template.maxNodes : 4}" min="1" />
      </div>
      <div class="form-group">
        <label for="edit-tpl-ami">Compute AMI ID</label>
        <div style="display:flex;gap:0.5rem">
          <input type="text" id="edit-tpl-ami" value="${esc(template.amiId || '')}" style="flex:1" />
          <button class="btn" type="button" id="btn-edit-detect-ami">Detect</button>
        </div>
        <small class="form-hint" id="edit-ami-hint">Click Detect to find the latest PCS sample AMI for the current instance types.</small>
      </div>
      <div class="form-group">
        <label for="edit-tpl-login-ami">Login Node AMI ID (optional)</label>
        <input type="text" id="edit-tpl-login-ami" value="${esc(template.loginAmiId || '')}" />
        <small class="form-hint">Only needed when the login node uses a different architecture than compute nodes.</small>
      </div>
      <fieldset class="form-fieldset">
        <legend>Software Stack</legend>
        <div class="form-group">
          <label for="edit-tpl-scheduler">Scheduler</label>
          <select id="edit-tpl-scheduler">
            <option value="slurm" ${(sw.scheduler || 'slurm') === 'slurm' ? 'selected' : ''}>Slurm</option>
          </select>
        </div>
        <div class="form-group">
          <label for="edit-tpl-scheduler-ver">Scheduler Version</label>
          <select id="edit-tpl-scheduler-ver">
            <option value="24.11"${sw.schedulerVersion === '24.11' ? ' selected' : ''}>24.11</option>
            <option value="25.05"${sw.schedulerVersion === '25.05' ? ' selected' : ''}>25.05</option>
            <option value="25.11"${!sw.schedulerVersion || sw.schedulerVersion === '25.11' ? ' selected' : ''}>25.11</option>
          </select>
        </div>
        <div class="form-group">
          <label for="edit-tpl-cuda">CUDA Version (optional, for GPU templates)</label>
          <input type="text" id="edit-tpl-cuda" value="${esc(sw.cudaVersion || '')}" />
        </div>
      </fieldset>
      <div style="display:flex;gap:0.5rem;justify-content:flex-end">
        <button class="btn" id="edit-tpl-cancel-btn">Cancel</button>
        <button class="btn btn-primary" id="edit-tpl-save-btn">Save</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  document.getElementById('edit-tpl-cancel-btn').addEventListener('click', () => {
    modal.remove();
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  // Handle Escape key to close modal
  function onKeyDown(e) {
    if (e.key === 'Escape') {
      modal.remove();
      document.removeEventListener('keydown', onKeyDown);
    }
  }
  document.addEventListener('keydown', onKeyDown);

  document.getElementById('btn-edit-detect-ami').addEventListener('click', async () => {
    const instanceInput = document.getElementById('edit-tpl-instance').value;
    const types = instanceInput.split(',').map(s => s.trim()).filter(Boolean);
    if (!types.length) return;
    const arch = inferArchitecture(types);
    if (!arch) return;
    const hint = document.getElementById('edit-ami-hint');
    const amiInput = document.getElementById('edit-tpl-ami');
    hint.textContent = `Looking up latest PCS sample AMI for ${arch}…`;
    const result = await fetchDefaultAmi(arch);
    if (result && result.amiId) {
      amiInput.value = result.amiId;
      hint.textContent = `${result.name || result.amiId} (${arch})`;
    } else {
      hint.textContent = `Could not find a PCS sample AMI for ${arch}. Enter an AMI ID manually.`;
    }
  });

  document.getElementById('edit-tpl-save-btn').addEventListener('click', async () => {
    const templateName = document.getElementById('edit-tpl-name').value.trim();
    const description = document.getElementById('edit-tpl-desc').value.trim();
    const instanceTypes = document.getElementById('edit-tpl-instance').value.split(',').map(s => s.trim()).filter(Boolean);
    const loginInstanceType = document.getElementById('edit-tpl-login-instance').value.trim();
    const minNodes = parseInt(document.getElementById('edit-tpl-min').value, 10) || 0;
    const maxNodes = parseInt(document.getElementById('edit-tpl-max').value, 10) || 4;
    const amiId = document.getElementById('edit-tpl-ami').value.trim();
    const loginAmiId = document.getElementById('edit-tpl-login-ami').value.trim();
    const scheduler = document.getElementById('edit-tpl-scheduler').value;
    const schedulerVersion = document.getElementById('edit-tpl-scheduler-ver').value.trim();
    const cudaVersion = document.getElementById('edit-tpl-cuda').value.trim();

    // Client-side validation
    if (!templateName) return showToast('Template Name is required', 'error');
    if (!instanceTypes.length) return showToast('At least one compute instance type is required', 'error');
    if (!loginInstanceType) return showToast('Login node instance type is required', 'error');
    if (!amiId) return showToast('AMI ID is required', 'error');
    if (minNodes > maxNodes) return showToast('Min nodes cannot exceed max nodes', 'error');

    // Validate all instance types against PCS-supported families
    const allTypes = [...instanceTypes, loginInstanceType];
    for (const it of allTypes) {
      if (!validateInstanceType(it)) {
        return showToast(`Instance type '${it}' is not a PCS-supported family. Supported: c5–c7i, m5–m7i, r5–r7i, g4dn–g6, p3–p5, hpc6a–hpc7g, t3–t4g, trn1, inf1–inf2, dl1, x2idn, x2iedn.`, 'error');
      }
    }

    const softwareStack = { scheduler, schedulerVersion };
    if (cudaVersion) softwareStack.cudaVersion = cudaVersion;

    const saveBtn = document.getElementById('edit-tpl-save-btn');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';

    try {
      const body = {
        templateName, description, instanceTypes,
        loginInstanceType, minNodes, maxNodes, amiId, softwareStack,
      };
      if (loginAmiId) body.loginAmiId = loginAmiId;
      await apiCall('PUT', `/templates/${encodeURIComponent(template.templateId)}`, body);
      showToast(`Template '${template.templateId}' updated`);
      document.removeEventListener('keydown', onKeyDown);
      modal.remove();
      loadTemplates();
    } catch (e) {
      showToast(e.message, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });

  // Focus the first editable field
  document.getElementById('edit-tpl-name').focus();
}

async function deleteTemplate(templateId) {
  if (!confirm(`Delete template '${templateId}'?`)) return;
  try {
    await apiCall('DELETE', `/templates/${encodeURIComponent(templateId)}`);
    showToast(`Template '${templateId}' deleted`);
    loadTemplates();
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Bulk Template Actions
   ============================================================ */

function bulkDeleteTemplates(ids) {
  // Remove any existing modal
  const existing = document.getElementById('bulk-delete-confirm-modal');
  if (existing) existing.remove();

  const listHtml = ids.map(id => `<li>${esc(id)}</li>`).join('');

  const modal = document.createElement('div');
  modal.id = 'bulk-delete-confirm-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-content">
      <h3>Bulk Delete Templates</h3>
      <p>Are you sure you want to delete the following <strong>${ids.length}</strong> template(s)?</p>
      <ul style="font-size:0.85rem;max-height:150px;overflow-y:auto;margin:0.5rem 0">${listHtml}</ul>
      <div style="display:flex;gap:0.5rem;justify-content:flex-end">
        <button class="btn" id="bulk-delete-cancel-btn">Cancel</button>
        <button class="btn btn-danger" id="bulk-delete-confirm-btn">Delete All</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  document.getElementById('bulk-delete-cancel-btn').addEventListener('click', () => {
    modal.remove();
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  document.getElementById('bulk-delete-confirm-btn').addEventListener('click', async () => {
    const confirmBtn = document.getElementById('bulk-delete-confirm-btn');
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Deleting…';
    try {
      const data = await apiCall('POST', '/templates/batch/delete', { templateIds: ids });
      const s = data.summary || {};
      const toastType = s.failed > 0 ? 'error' : 'success';
      showToast(`${s.succeeded} of ${s.total} succeeded, ${s.failed} failed`, toastType);
      modal.remove();
      TableModule.clearSelection('templates');
      const toolbar = document.getElementById('templates-bulk-toolbar');
      if (toolbar) toolbar.style.display = 'none';
      loadTemplates();
    } catch (e) {
      showToast(e.message, 'error');
      modal.remove();
    }
  });
}

/* ============================================================
   Clusters Page
   ============================================================ */

function renderClustersPage(container, params) {
  // Allow passing projectId from navigation params, fall back to project context
  const projectId = (params && params.projectId) || state.projectContext || '';

  container.innerHTML = `
    <div class="page-header">
      <h2>Cluster Operations</h2>
    </div>
    <div class="detail-panel" style="margin-bottom:1rem">
      <div style="display:flex;gap:0.75rem;align-items:flex-end">
        <div class="form-group" style="flex:1;margin-bottom:0">
          <label for="cluster-project-id">Project ID</label>
          <input type="text" id="cluster-project-id" placeholder="Enter project ID" value="${esc(projectId)}" autocomplete="off" />
        </div>
        <button class="btn btn-primary" id="btn-load-clusters">Load Clusters</button>
        <button class="btn btn-primary" id="btn-create-cluster">Create Cluster</button>
      </div>
    </div>
    <div id="clusters-list"></div>
    <div id="create-cluster-form" style="display:none" class="detail-panel">
      <h3>Create New Cluster</h3>
      <div class="form-group">
        <label for="new-cluster-name">Cluster Name</label>
        <input type="text" id="new-cluster-name" placeholder="my-cluster-01" />
        <small class="form-hint">Alphanumeric characters, hyphens, and underscores only. Must be globally unique.</small>
      </div>
      <div class="form-group">
        <label for="new-cluster-template">Template</label>
        <select id="new-cluster-template">
          <option value="">Loading templates…</option>
        </select>
      </div>
      <div id="template-preview" class="template-preview" style="display:none"></div>
      <div class="form-group">
        <label for="new-cluster-storage-mode">Storage Mode</label>
        <select id="new-cluster-storage-mode">
          <option value="mountpoint">Mountpoint for Amazon S3</option>
          <option value="lustre">FSx for Lustre</option>
        </select>
      </div>
      <div id="lustre-capacity-group" class="form-group" style="display:none">
        <label for="new-cluster-lustre-capacity">Lustre Capacity (GiB)</label>
        <input type="number" id="new-cluster-lustre-capacity" min="1200" step="1200" value="1200" />
        <small class="form-hint">Must be a multiple of 1200 GiB. Minimum 1200 GiB.</small>
      </div>
      <fieldset class="form-fieldset">
        <legend>Node Scaling</legend>
        <div class="form-group">
          <label for="new-cluster-min-nodes">Min Nodes</label>
          <input type="number" id="new-cluster-min-nodes" min="0" value="" placeholder="From template" />
        </div>
        <div class="form-group" style="margin-bottom:0">
          <label for="new-cluster-max-nodes">Max Nodes</label>
          <input type="number" id="new-cluster-max-nodes" min="1" value="" placeholder="From template" />
        </div>
      </fieldset>
      <button class="btn btn-primary" id="btn-submit-cluster">Create</button>
      <button class="btn" id="btn-cancel-cluster" style="margin-left:0.5rem">Cancel</button>
    </div>
  `;

  // Cache fetched templates for the dropdown
  let cachedTemplates = [];

  document.getElementById('btn-load-clusters').addEventListener('click', () => {
    const pid = document.getElementById('cluster-project-id').value.trim();
    if (!pid) return showToast('Enter a project ID', 'error');
    setProjectContext(pid);
    loadClusters(pid);
  });

  document.getElementById('btn-create-cluster').addEventListener('click', async () => {
    const pid = document.getElementById('cluster-project-id').value.trim();
    if (!pid) return showToast('Enter a project ID first', 'error');
    document.getElementById('create-cluster-form').style.display = 'block';
    // Load templates into the dropdown
    try {
      const data = await apiCall('GET', '/templates');
      cachedTemplates = data.templates || [];
      const select = document.getElementById('new-cluster-template');
      if (!cachedTemplates.length) {
        select.innerHTML = '<option value="">No templates available</option>';
      } else {
        select.innerHTML = '<option value="">— Select a template —</option>'
          + cachedTemplates.map(t =>
            `<option value="${esc(t.templateId)}">${esc(t.templateId)} — ${esc(t.templateName || '')}</option>`
          ).join('');
      }
    } catch (e) {
      document.getElementById('new-cluster-template').innerHTML =
        '<option value="">Failed to load templates</option>';
    }
  });

  // Show template details when selection changes and pre-populate node scaling
  document.getElementById('new-cluster-template').addEventListener('change', () => {
    const templateId = document.getElementById('new-cluster-template').value;
    const preview = document.getElementById('template-preview');
    if (!templateId) {
      preview.style.display = 'none';
      document.getElementById('new-cluster-min-nodes').value = '';
      document.getElementById('new-cluster-max-nodes').value = '';
      return;
    }
    const tpl = cachedTemplates.find(t => t.templateId === templateId);
    if (!tpl) {
      preview.style.display = 'none';
      document.getElementById('new-cluster-min-nodes').value = '';
      document.getElementById('new-cluster-max-nodes').value = '';
      return;
    }
    const sw = tpl.softwareStack || {};
    const cudaLine = sw.cudaVersion ? `<div class="detail-row"><span class="label">CUDA</span><span>${esc(sw.cudaVersion)}</span></div>` : '';
    preview.innerHTML = `
      <div class="detail-row"><span class="label">Description</span><span>${esc(tpl.description || '—')}</span></div>
      <div class="detail-row"><span class="label">Compute Instances</span><span>${esc((tpl.instanceTypes || []).join(', '))}</span></div>
      <div class="detail-row"><span class="label">Login Instance</span><span>${esc(tpl.loginInstanceType || '—')}</span></div>
      <div class="detail-row"><span class="label">Nodes</span><span>${tpl.minNodes || 0} – ${tpl.maxNodes || '∞'}</span></div>
      <div class="detail-row"><span class="label">Scheduler</span><span>${esc(sw.scheduler || '—')} ${esc(sw.schedulerVersion || '')}</span></div>
      ${cudaLine}
    `;
    preview.style.display = 'block';

    // Pre-populate node scaling from template defaults
    document.getElementById('new-cluster-min-nodes').value = tpl.minNodes != null ? tpl.minNodes : 0;
    document.getElementById('new-cluster-max-nodes').value = tpl.maxNodes != null ? tpl.maxNodes : 10;
  });

  // Toggle lustre capacity visibility based on storage mode selection
  document.getElementById('new-cluster-storage-mode').addEventListener('change', (e) => {
    const lustreGroup = document.getElementById('lustre-capacity-group');
    lustreGroup.style.display = e.target.value === 'lustre' ? 'block' : 'none';
  });

  document.getElementById('btn-cancel-cluster').addEventListener('click', () => {
    document.getElementById('create-cluster-form').style.display = 'none';
    document.getElementById('template-preview').style.display = 'none';
    document.getElementById('lustre-capacity-group').style.display = 'none';
    // Reset storage mode to default
    document.getElementById('new-cluster-storage-mode').value = 'mountpoint';
  });
  document.getElementById('btn-submit-cluster').addEventListener('click', async () => {
    const pid = document.getElementById('cluster-project-id').value.trim();
    const clusterName = document.getElementById('new-cluster-name').value.trim();
    const templateId = document.getElementById('new-cluster-template').value;
    if (!clusterName) return showToast('Cluster name is required', 'error');
    if (!templateId) return showToast('Please select a template', 'error');

    const storageMode = document.getElementById('new-cluster-storage-mode').value;
    const body = { clusterName, templateId, storageMode };

    if (storageMode === 'lustre') {
      body.lustreCapacityGiB = parseInt(document.getElementById('new-cluster-lustre-capacity').value, 10) || 1200;
    }

    const minNodesVal = document.getElementById('new-cluster-min-nodes').value;
    const maxNodesVal = document.getElementById('new-cluster-max-nodes').value;
    if (minNodesVal !== '') body.minNodes = parseInt(minNodesVal, 10);
    if (maxNodesVal !== '') body.maxNodes = parseInt(maxNodesVal, 10);

    try {
      await apiCall('POST', `/projects/${encodeURIComponent(pid)}/clusters`, body);
      showToast(`Cluster '${clusterName}' creation started`);
      document.getElementById('create-cluster-form').style.display = 'none';
      document.getElementById('template-preview').style.display = 'none';
      document.getElementById('lustre-capacity-group').style.display = 'none';
      loadClusters(pid);
    } catch (e) { showToast(e.message, 'error'); }
  });

  // Auto-load if projectId was provided
  if (projectId) loadClusters(projectId);

  // Lazy autocomplete for project ID
  attachAutocomplete('cluster-project-id', async () => {
    const data = await apiCall('GET', '/projects');
    return (data.projects || []).map(p => ({ value: p.projectId, label: p.projectName || p.projectId }));
  });
}

async function loadClusters(projectId) {
  const el = document.getElementById('clusters-list');
  // Only show the loading spinner on the initial load, not during polling
  // refreshes — this prevents the table from flickering.
  if (!el.querySelector('table')) {
    el.innerHTML = '<div class="empty-state"><span class="spinner"></span> Loading clusters…</div>';
  }

  try {
    // Fetch project data and clusters in parallel
    const [projectData, data] = await Promise.all([
      apiCall('GET', `/projects/${encodeURIComponent(projectId)}`),
      apiCall('GET', `/projects/${encodeURIComponent(projectId)}/clusters`),
    ]);
    const budgetBreached = !!projectData.budgetBreached;
    const clusters = data.clusters || [];

    const clustersTableConfig = {
      columns: [
        {
          key: 'clusterName', label: 'Cluster Name', type: 'text', sortable: true,
          render: (row) => `<a href="#" onclick="navigate('cluster-detail',{projectId:'${esc(projectId)}',clusterName:'${esc(row.clusterName)}'});return false">${esc(row.clusterName)}</a>`,
        },
        { key: 'templateId', label: 'Template', type: 'text', sortable: true },
        {
          key: 'status', label: 'Status', type: 'text', sortable: true,
          render: (row) => `<span class="badge badge-${(row.status || '').toLowerCase()}">${esc(row.status)}</span>`,
        },
        {
          key: '_progress', label: 'Progress', type: 'custom', sortable: false,
          render: (row) => {
            if (row.status === 'CREATING') {
              const cur = row.currentStep || 0;
              const total = row.totalSteps || 10;
              const pct = total > 0 ? Math.round((cur / total) * 100) : 0;
              const desc = row.stepDescription || 'Initialising…';
              const isStale = row.createdAt && (Date.now() - new Date(row.createdAt).getTime()) > CONFIG.clusterCreationTimeoutMs;
              let html = `<div class="progress-container compact">
                <div class="progress-label">${esc(desc)} (${cur}/${total})</div>
                <div class="progress-bar-track"><div class="progress-bar-fill" style="width:${pct}%">${pct}%</div></div>
              </div>`;
              if (isStale) {
                html += `<span class="badge badge-stale-warning" role="alert">Creation may have failed</span>`;
              }
              return html;
            } else if (row.status === 'FAILED') {
              return `<span style="color:var(--color-danger);font-size:0.8rem">${esc(row.errorMessage || 'Unknown error')}</span>`;
            }
            return '—';
          },
        },
        {
          key: '_actions', label: 'Actions', type: 'custom', sortable: false,
          render: (row) => {
            if (['ACTIVE', 'FAILED'].includes(row.status)) {
              return `<button class="btn btn-danger btn-sm" onclick="destroyCluster('${esc(projectId)}','${esc(row.clusterName)}')">Destroy</button>`;
            } else if (row.status === 'DESTROYED' && !budgetBreached) {
              return `<button class="btn btn-primary btn-sm" onclick="recreateCluster('${esc(projectId)}','${esc(row.clusterName)}')">Recreate</button>`;
            } else if (row.status === 'CREATING' && row.createdAt && (Date.now() - new Date(row.createdAt).getTime()) > CONFIG.clusterCreationTimeoutMs) {
              return `<button class="btn btn-warning btn-sm" onclick="forceFailCluster('${esc(projectId)}','${esc(row.clusterName)}')">Mark as Failed</button>`;
            }
            return '';
          },
        },
      ],
      filterLabel: 'Filter clusters',
      emptyMessage: 'No clusters found for this project.',
      noMatchMessage: 'No matching clusters found.',
    };

    TableModule.render('clusters', clustersTableConfig, clusters, el);

    // Start or stop polling based on transitional cluster states
    const transitionalClusters = clusters.filter(c => ['CREATING', 'DESTROYING'].includes(c.status));
    if (transitionalClusters.length > 0) {
      startClusterListPolling(projectId);
    } else {
      stopClusterListPolling(projectId);
    }

    // Detect status transitions for in-app notifications
    clusters.forEach(c => {
      const prev = state.clusterStatusCache[c.clusterName];
      if (prev === 'CREATING' && c.status === 'ACTIVE') {
        showToast(`Cluster '${c.clusterName}' is now ACTIVE`, 'success');
      } else if (prev === 'CREATING' && c.status === 'FAILED') {
        showToast(`Cluster '${c.clusterName}' creation FAILED`, 'error');
      } else if (prev === 'DESTROYING' && c.status === 'DESTROYED') {
        showToast(`Cluster '${c.clusterName}' has been destroyed`, 'success');
      }
      state.clusterStatusCache[c.clusterName] = c.status;
    });

  } catch (e) {
    if (!el.querySelector('table')) {
      el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }
  }
}

function startClusterListPolling(projectId) {
  const key = `list-${projectId}`;
  if (state.pollTimers[key]) return; // already polling
  state.pollTimers[key] = setInterval(() => {
    if (state.currentPage !== 'clusters') {
      stopClusterListPolling(projectId);
      return;
    }
    loadClusters(projectId);
  }, CONFIG.clusterPollIntervalMs);
}

function stopClusterListPolling(projectId) {
  const key = `list-${projectId}`;
  if (state.pollTimers[key]) {
    clearInterval(state.pollTimers[key]);
    delete state.pollTimers[key];
  }
}

async function destroyCluster(projectId, clusterName) {
  if (!confirm(`Destroy cluster '${clusterName}'? This action cannot be undone.`)) return;
  try {
    await apiCall('DELETE', `/projects/${encodeURIComponent(projectId)}/clusters/${encodeURIComponent(clusterName)}`);
    showToast(`Cluster '${clusterName}' destruction started`);
    loadClusters(projectId);
  } catch (e) { showToast(e.message, 'error'); }
}

async function recreateCluster(projectId, clusterName) {
  if (!confirm(`Recreate cluster '${clusterName}'? This will provision new resources using the original template.`)) return;
  try {
    await apiCall('POST',
      `/projects/${encodeURIComponent(projectId)}/clusters/${encodeURIComponent(clusterName)}/recreate`
    );
    showToast(`Cluster '${clusterName}' recreation started`);
    loadClusters(projectId);
  } catch (e) { showToast(e.message, 'error'); }
}

async function forceFailCluster(projectId, clusterName) {
  if (!confirm(`Mark cluster '${clusterName}' as failed? This indicates the creation workflow has stopped and the cluster cannot recover.`)) return;
  try {
    await apiCall('POST',
      `/projects/${encodeURIComponent(projectId)}/clusters/${encodeURIComponent(clusterName)}/fail`
    );
    showToast(`Cluster '${clusterName}' marked as FAILED`);
    // Refresh whichever view is active
    if (state.currentPage === 'cluster-detail') {
      loadClusterDetail(projectId, clusterName);
    } else {
      loadClusters(projectId);
    }
  } catch (e) { showToast(e.message, 'error'); }
}


/* ============================================================
   Cluster Detail Page
   ============================================================ */

function renderClusterDetailPage(container, params) {
  const { projectId, clusterName } = params;
  if (!projectId || !clusterName) {
    container.innerHTML = '<div class="error-box">Missing project ID or cluster name.</div>';
    return;
  }

  container.innerHTML = `
    <div class="page-header">
      <h2>Cluster: ${esc(clusterName)}</h2>
      <button class="btn" onclick="navigate('clusters',{projectId:'${esc(projectId)}'})">← Back to Clusters</button>
    </div>
    <div id="cluster-detail"><div class="empty-state"><span class="spinner"></span> Loading cluster details…</div></div>
  `;

  loadClusterDetail(projectId, clusterName);
}

async function loadClusterDetail(projectId, clusterName) {
  const el = document.getElementById('cluster-detail');
  if (!el) return;

  try {
    const cluster = await apiCall('GET', `/projects/${encodeURIComponent(projectId)}/clusters/${encodeURIComponent(clusterName)}`);

    // Detect status transitions
    const prev = state.clusterStatusCache[clusterName];
    if (prev === 'CREATING' && cluster.status === 'ACTIVE') {
      showToast(`Cluster '${clusterName}' is now ACTIVE`, 'success');
    } else if (prev === 'CREATING' && cluster.status === 'FAILED') {
      showToast(`Cluster '${clusterName}' creation FAILED`, 'error');
    }
    state.clusterStatusCache[clusterName] = cluster.status;

    const statusClass = (cluster.status || '').toLowerCase();

    let html = `<div class="detail-panel">
      <div class="detail-row"><span class="label">Cluster Name</span><span>${esc(cluster.clusterName)}</span></div>
      <div class="detail-row"><span class="label">Project</span><span>${esc(cluster.projectId)}</span></div>
      <div class="detail-row"><span class="label">Template</span><span>${esc(cluster.templateId || '—')}</span></div>
      <div class="detail-row"><span class="label">Status</span><span class="badge badge-${statusClass}">${esc(cluster.status)}</span></div>
      <div class="detail-row"><span class="label">Created By</span><span>${esc(cluster.createdBy || '—')}</span></div>
      <div class="detail-row"><span class="label">Created At</span><span>${esc(cluster.createdAt || '—')}</span></div>
      ${cluster.destroyedAt ? `<div class="detail-row"><span class="label">Destroyed At</span><span>${esc(cluster.destroyedAt)}</span></div>` : ''}
    </div>`;

    // ACTIVE: show connection info
    if (cluster.status === 'ACTIVE' && cluster.connectionInfo) {
      html += `<div class="connection-info">
        <h4>Connection Information</h4>
        ${cluster.connectionInfo.ssh ? `<div><strong>SSH:</strong><code>${esc(cluster.connectionInfo.ssh)}</code></div>` : ''}
        ${cluster.connectionInfo.dcv ? `<div><strong>DCV:</strong><code>${esc(cluster.connectionInfo.dcv)}</code></div>` : ''}
      </div>`;
    }

    // CREATING: show progress
    if (cluster.status === 'CREATING') {
      const progress = cluster.progress || {};
      const cur = progress.currentStep || 0;
      const total = progress.totalSteps || 10;
      const pct = total > 0 ? Math.round((cur / total) * 100) : 0;
      const desc = progress.stepDescription || 'Initialising…';
      const isStale = cluster.createdAt && (Date.now() - new Date(cluster.createdAt).getTime()) > CONFIG.clusterCreationTimeoutMs;

      html += `<div class="info-box">
        <h4>Deployment Progress</h4>
        <div class="progress-container">
          <div class="progress-label">Step ${cur} of ${total}: ${esc(desc)}</div>
          <div class="progress-bar-track"><div class="progress-bar-fill" style="width:${pct}%">${pct}%</div></div>
        </div>
        <p style="font-size:0.8rem;margin:0.5rem 0 0;color:var(--color-text-muted)">
          This page refreshes automatically. You can navigate away and return to check progress.
        </p>
      </div>`;

      if (isStale) {
        html += `<div class="warning-box" role="alert">
          <h4>⚠ Creation may have failed</h4>
          <p>This cluster has been in CREATING status for longer than expected. The backend workflow may have stopped.</p>
          <button class="btn btn-warning" onclick="forceFailCluster('${esc(projectId)}','${esc(clusterName)}')">Mark as Failed</button>
        </div>`;
      }

      // Start polling for this cluster
      startClusterDetailPolling(projectId, clusterName);
    }

    // FAILED: show error
    if (cluster.status === 'FAILED') {
      html += `<div class="error-box">
        <h4>Deployment Failed</h4>
        <p>${esc(cluster.errorMessage || 'An unknown error occurred during cluster creation. Partially created resources have been cleaned up.')}</p>
      </div>`;
    }

    // Non-ACTIVE info message
    if (['CREATING', 'DESTROYING', 'DESTROYED'].includes(cluster.status)) {
      const msgs = {
        CREATING: 'Cluster is being provisioned. SSH/DCV access will be available once the cluster is ACTIVE.',
        DESTROYING: 'Cluster is being destroyed. Data is being exported to S3.',
        DESTROYED: 'This cluster has been destroyed. Home directories and project storage have been retained.',
      };
      if (cluster.status !== 'CREATING') { // CREATING already has progress box
        html += `<div class="info-box">${msgs[cluster.status]}</div>`;
      }
    }

    // Destroy button for ACTIVE/FAILED
    if (['ACTIVE', 'FAILED'].includes(cluster.status)) {
      html += `<div style="margin-top:1rem">
        <button class="btn btn-danger" onclick="destroyCluster('${esc(projectId)}','${esc(clusterName)}')">Destroy Cluster</button>
      </div>`;
    }

    // Recreate button for DESTROYED
    if (cluster.status === 'DESTROYED') {
      html += `<div style="margin-top:1rem">
        <button class="btn btn-primary" onclick="recreateCluster('${esc(projectId)}','${esc(clusterName)}')">Recreate Cluster</button>
      </div>`;
    }

    el.innerHTML = html;

  } catch (e) {
    el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}

function startClusterDetailPolling(projectId, clusterName) {
  const key = `detail-${projectId}-${clusterName}`;
  if (state.pollTimers[key]) return;
  state.pollTimers[key] = setInterval(() => {
    if (state.currentPage !== 'cluster-detail') {
      clearInterval(state.pollTimers[key]);
      delete state.pollTimers[key];
      return;
    }
    loadClusterDetail(projectId, clusterName);
  }, CONFIG.clusterPollIntervalMs);
}

/* ============================================================
   Members Page
   ============================================================ */

function renderMembersPage(container) {
  const projectId = state.projectContext;
  if (!projectId) {
    container.innerHTML = `
      <div class="page-header"><h2>Members</h2></div>
      <div class="info-box">Select a project first. Go to <a href="#" onclick="navigate('clusters');return false">Clusters</a> and enter a project ID.</div>
    `;
    return;
  }

  if (!canSeeMembers()) {
    container.innerHTML = `
      <div class="page-header"><h2>Members</h2></div>
      <div class="error-box">You do not have permission to manage members for this project.</div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="page-header">
      <h2>Members — ${esc(projectId)}</h2>
      <button class="btn btn-primary" id="btn-add-member">Add Member</button>
    </div>
    <div id="members-list"><div class="empty-state"><span class="spinner"></span> Loading members…</div></div>
    <div id="add-member-form" style="display:none" class="detail-panel">
      <h3>Add Member</h3>
      <div class="form-group">
        <label for="new-member-user-id">User ID</label>
        <input type="text" id="new-member-user-id" placeholder="jsmith" autocomplete="off" />
      </div>
      <div class="form-group">
        <label for="new-member-role">Role</label>
        <select id="new-member-role">
          <option value="PROJECT_USER">End User</option>
          <option value="PROJECT_ADMIN">Project Admin</option>
        </select>
      </div>
      <button class="btn btn-primary" id="btn-submit-member">Add</button>
      <button class="btn" id="btn-cancel-member" style="margin-left:0.5rem">Cancel</button>
    </div>
  `;

  document.getElementById('btn-add-member').addEventListener('click', () => {
    document.getElementById('add-member-form').style.display = 'block';
  });
  document.getElementById('btn-cancel-member').addEventListener('click', () => {
    document.getElementById('add-member-form').style.display = 'none';
  });
  document.getElementById('btn-submit-member').addEventListener('click', async () => {
    const userId = document.getElementById('new-member-user-id').value.trim();
    const role = document.getElementById('new-member-role').value;
    if (!userId) return showToast('User ID is required', 'error');
    try {
      await apiCall('POST', `/projects/${encodeURIComponent(projectId)}/members`, { userId, role });
      showToast(`Member '${userId}' added`);
      document.getElementById('add-member-form').style.display = 'none';
      loadMembers(projectId);
    } catch (e) { showToast(e.message, 'error'); }
  });

  loadMembers(projectId);

  // Lazy autocomplete for user ID (active users only)
  attachAutocomplete('new-member-user-id', async () => {
    const data = await apiCall('GET', '/users');
    return (data.users || [])
      .filter(u => u.status === 'ACTIVE')
      .map(u => ({ value: u.userId, label: u.displayName || u.userId }));
  });
}

async function loadMembers(projectId) {
  try {
    const data = await apiCall('GET', `/projects/${encodeURIComponent(projectId)}/members`);
    const members = data.members || [];
    const el = document.getElementById('members-list');
    if (!el) return;

    const membersTableConfig = {
      columns: [
        { key: 'userId', label: 'User ID', type: 'text', sortable: true },
        { key: 'displayName', label: 'Display Name', type: 'text', sortable: true },
        {
          key: 'role', label: 'Role', type: 'text', sortable: true,
          render: (row) => {
            const roleLabel = row.role === 'PROJECT_ADMIN' ? 'Project Admin' : 'End User';
            return `<select class="member-role-select" data-user-id="${esc(row.userId)}" aria-label="Change role for ${esc(row.userId)}">
              <option value="PROJECT_USER"${row.role === 'PROJECT_USER' ? ' selected' : ''}>End User</option>
              <option value="PROJECT_ADMIN"${row.role === 'PROJECT_ADMIN' ? ' selected' : ''}>Project Admin</option>
            </select>`;
          },
        },
        {
          key: 'addedAt', label: 'Added', type: 'text', sortable: true,
          render: (row) => esc(row.addedAt || '—'),
        },
        {
          key: '_actions', label: 'Actions', type: 'custom', sortable: false,
          render: (row) => `<button class="btn btn-danger btn-sm" onclick="removeMember('${esc(projectId)}','${esc(row.userId)}')">Remove</button>`,
        },
      ],
      filterLabel: 'Filter members',
      emptyMessage: 'No members found for this project.',
      noMatchMessage: 'No matching members found.',
    };

    TableModule.render('members', membersTableConfig, members, el);

    // Attach role-change handlers to the dropdowns
    el.querySelectorAll('.member-role-select').forEach(select => {
      select.addEventListener('change', async () => {
        const userId = select.dataset.userId;
        const newRole = select.value;
        try {
          await apiCall('PUT', `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`, { role: newRole });
          showToast(`Role updated for '${userId}'`);
          loadMembers(projectId);
        } catch (e) {
          showToast(e.message, 'error');
          loadMembers(projectId); // reload to reset the dropdown
        }
      });
    });
  } catch (e) {
    const el = document.getElementById('members-list');
    if (el) el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}

async function removeMember(projectId, userId) {
  if (!confirm(`Remove member '${userId}' from project '${projectId}'?`)) return;
  try {
    await apiCall('DELETE', `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`);
    showToast(`Member '${userId}' removed`);
    loadMembers(projectId);
  } catch (e) { showToast(e.message, 'error'); }
}

/* ============================================================
   Accounting Page
   ============================================================ */

function renderAccountingPage(container) {
  container.innerHTML = `
    <div class="page-header">
      <h2>Accounting Queries</h2>
    </div>
    <div class="detail-panel">
      <div style="display:flex;gap:0.75rem;align-items:flex-end">
        <div class="form-group" style="flex:1;margin-bottom:0">
          <label for="acct-project-id">Project ID (optional — leave blank for all)</label>
          <input type="text" id="acct-project-id" placeholder="Filter by project ID" autocomplete="off" />
        </div>
        <button class="btn btn-primary" id="btn-query-accounting">Query Jobs</button>
      </div>
    </div>
    <div id="accounting-results" style="margin-top:1rem"></div>
  `;

  document.getElementById('btn-query-accounting').addEventListener('click', async () => {
    const projectId = document.getElementById('acct-project-id').value.trim();
    const el = document.getElementById('accounting-results');
    el.innerHTML = '<div class="empty-state"><span class="spinner"></span> Querying accounting data…</div>';
    try {
      const path = projectId ? `/accounting/jobs?projectId=${encodeURIComponent(projectId)}` : '/accounting/jobs';
      const data = await apiCall('GET', path);
      const jobs = data.jobs || [];
      TableModule.render('accounting', accountingTableConfig, jobs, el);
    } catch (e) {
      el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }
  });

  // Lazy autocomplete for project ID
  attachAutocomplete('acct-project-id', async () => {
    const data = await apiCall('GET', '/projects');
    return (data.projects || []).map(p => ({ value: p.projectId, label: p.projectName || p.projectId }));
  });
}

/* ============================================================
   Utility
   ============================================================ */

/**
 * Attach a lazy, debounced autocomplete dropdown to a text input.
 *
 * On the first keystroke the fetchFn is called to retrieve the full
 * option list.  The result is cached so subsequent keystrokes filter
 * locally without additional API calls.  The cache is per-input so
 * navigating away and back triggers a fresh fetch.
 *
 * @param {string}   inputId   - DOM id of the text input
 * @param {Function} fetchFn   - async () => Array<{value, label}>
 * @param {number}   [delay=200] - debounce delay in ms
 */
function attachAutocomplete(inputId, fetchFn, delay = 200) {
  const input = document.getElementById(inputId);
  if (!input) return;

  let cache = null;      // null = not yet fetched
  let loading = false;
  let timer = null;
  let dropdown = null;
  let selectedIdx = -1;

  // Ensure the parent can anchor the absolute dropdown
  const wrapper = input.parentElement;
  if (wrapper) wrapper.style.position = 'relative';

  function createDropdown() {
    if (dropdown) return dropdown;
    dropdown = document.createElement('ul');
    dropdown.className = 'autocomplete-list';
    dropdown.setAttribute('role', 'listbox');
    dropdown.id = inputId + '-autocomplete-list';
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', dropdown.id);
    input.parentElement.appendChild(dropdown);
    return dropdown;
  }

  function hideDropdown() {
    if (dropdown) {
      dropdown.style.display = 'none';
      input.removeAttribute('aria-activedescendant');
    }
    selectedIdx = -1;
  }

  function showFiltered(query) {
    if (!cache) { hideDropdown(); return; }
    const q = query.toLowerCase();
    const matches = q
      ? cache.filter(item =>
          item.value.toLowerCase().includes(q) ||
          item.label.toLowerCase().includes(q)
        )
      : [];
    if (!matches.length) { hideDropdown(); return; }

    const dl = createDropdown();
    dl.innerHTML = matches.slice(0, 20).map((item, i) => {
      const id = inputId + '-opt-' + i;
      const primary = esc(item.value);
      const secondary = item.label !== item.value ? ` <span class="autocomplete-label">${esc(item.label)}</span>` : '';
      return `<li role="option" id="${id}" class="autocomplete-item" data-value="${esc(item.value)}">${primary}${secondary}</li>`;
    }).join('');
    dl.style.display = 'block';
    selectedIdx = -1;

    dl.querySelectorAll('.autocomplete-item').forEach(li => {
      li.addEventListener('mousedown', (e) => {
        e.preventDefault();          // keep focus on input
        input.value = li.dataset.value;
        hideDropdown();
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
    });
  }

  function updateHighlight() {
    if (!dropdown) return;
    const items = dropdown.querySelectorAll('.autocomplete-item');
    items.forEach((li, i) => {
      li.classList.toggle('autocomplete-item-active', i === selectedIdx);
    });
    if (selectedIdx >= 0 && items[selectedIdx]) {
      input.setAttribute('aria-activedescendant', items[selectedIdx].id);
      items[selectedIdx].scrollIntoView({ block: 'nearest' });
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!cache && !loading) {
        loading = true;
        try { cache = await fetchFn(); } catch (_) { cache = []; }
        loading = false;
      }
      showFiltered(input.value.trim());
    }, delay);
  });

  input.addEventListener('keydown', (e) => {
    if (!dropdown || dropdown.style.display === 'none') return;
    const items = dropdown.querySelectorAll('.autocomplete-item');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIdx = Math.min(selectedIdx + 1, items.length - 1);
      updateHighlight();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIdx = Math.max(selectedIdx - 1, 0);
      updateHighlight();
    } else if (e.key === 'Enter' && selectedIdx >= 0) {
      e.preventDefault();
      input.value = items[selectedIdx].dataset.value;
      hideDropdown();
      input.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (e.key === 'Escape') {
      hideDropdown();
    }
  });

  input.addEventListener('blur', () => {
    // Small delay so mousedown on dropdown items fires first
    setTimeout(hideDropdown, 150);
  });
}

function esc(str) {
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

/* ============================================================
   Login Page
   ============================================================ */

function renderLoginPage() {
  const app = document.getElementById('app');
  app.innerHTML = `
    <div class="login-container">
      <h2>HPC Platform</h2>
      <p style="text-align:center;color:var(--color-text-muted);font-size:0.85rem">Sign in to the Self-Service HPC Portal</p>
      <div id="login-error" class="error-box" style="display:none"></div>
      <div id="login-form">
        <div class="form-group">
          <label for="login-username">Email</label>
          <input type="email" id="login-username" placeholder="user@example.com" autocomplete="username" />
        </div>
        <div class="form-group">
          <label for="login-password">Password</label>
          <input type="password" id="login-password" placeholder="Password" autocomplete="current-password" />
        </div>
        <button class="btn btn-primary" style="width:100%" id="btn-login">Sign In</button>
      </div>
      <div id="new-password-form" style="display:none">
        <p style="font-size:0.85rem;color:var(--color-text-muted)">You must set a new password.</p>
        <div class="form-group">
          <label for="new-password">New Password</label>
          <input type="password" id="new-password" placeholder="New password" autocomplete="new-password" />
        </div>
        <button class="btn btn-primary" style="width:100%" id="btn-set-password">Set Password & Sign In</button>
      </div>
    </div>
  `;

  let challengeSession = null;
  let challengeUsername = null;

  document.getElementById('btn-login').addEventListener('click', async () => {
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    const errEl = document.getElementById('login-error');
    errEl.style.display = 'none';

    if (!username || !password) {
      errEl.textContent = 'Email and password are required.';
      errEl.style.display = 'block';
      return;
    }

    try {
      const result = await cognitoInitiateAuth(username, password);
      if (result.challenge === 'NEW_PASSWORD_REQUIRED') {
        challengeSession = result.session;
        challengeUsername = result.username;
        document.getElementById('login-form').style.display = 'none';
        document.getElementById('new-password-form').style.display = 'block';
        return;
      }
      setSession(result);
      renderApp();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.style.display = 'block';
    }
  });

  // Allow Enter key to submit
  document.getElementById('login-password').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('btn-login').click();
  });

  document.getElementById('btn-set-password').addEventListener('click', async () => {
    const newPw = document.getElementById('new-password').value;
    const errEl = document.getElementById('login-error');
    errEl.style.display = 'none';
    if (!newPw) {
      errEl.textContent = 'New password is required.';
      errEl.style.display = 'block';
      return;
    }
    try {
      const result = await cognitoRespondNewPassword(challengeUsername, newPw, challengeSession);
      setSession(result);
      renderApp();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.style.display = 'block';
    }
  });
}

/* ============================================================
   Main App Shell
   ============================================================ */

function renderApp() {
  const app = document.getElementById('app');
  const ctxLabel = state.projectContext
    ? `Project: ${esc(state.projectContext)}`
    : 'No project selected';
  const ctxClass = state.projectContext ? '' : ' no-project';
  const membersTab = canSeeMembers()
    ? '<a href="#" data-page="members">Project Members</a>'
    : '';
  app.innerHTML = `
    <header>
      <h1>HPC Self-Service Portal</h1>
      <div class="user-info">
        <span id="project-context-indicator" class="project-context-indicator${ctxClass}">${ctxLabel}</span>
        <span>${esc(state.user.email || state.user.username)}</span>
        <button id="btn-logout">Sign Out</button>
      </div>
    </header>
    <div class="layout">
      <nav aria-label="Main navigation">
        <a href="#" data-page="projects">Projects</a>
        <a href="#" data-page="clusters">Clusters</a>
        <hr/>
        <a href="#" data-page="templates">Cluster Templates</a>
        <a href="#" data-page="users">User Management</a>
        ${membersTab}
        <a href="#" data-page="accounting">Accounting</a>
      </nav>
      <main id="main-content"></main>
    </div>
  `;

  document.getElementById('btn-logout').addEventListener('click', () => {
    clearSession();
    renderLoginPage();
  });

  document.querySelectorAll('nav a').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      navigate(a.dataset.page);
    });
  });

  // Default page
  navigate('clusters');
}

/* ============================================================
   Bootstrap
   ============================================================ */

document.addEventListener('DOMContentLoaded', () => {
  if (tryRestoreSession()) {
    renderApp();
  } else {
    renderLoginPage();
  }
});
