const STATUS_COLUMNS = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'ready', label: 'Ready' },
  { id: 'in_progress', label: 'In progress' },
  { id: 'review', label: 'Review' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'done', label: 'Done' },
];
const state = { tasks: [], events: [], search: '', busy: false, draggedId: null, timer: null };
const board = document.querySelector('#board-columns');
const taskDialog = document.querySelector('#task-dialog');
const setupDialog = document.querySelector('#setup-dialog');
const detailDialog = document.querySelector('#detail-dialog');
const toast = document.querySelector('#toast');
let toastTimer;

const escapeHtml = (value = '') => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const timeLabel = value => {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date);
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    body: options.body && typeof options.body !== 'string' ? JSON.stringify(options.body) : options.body,
  });
  const data = response.status === 204 ? {} : await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function notify(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3200);
}

function getVisibleTasks() {
  const query = state.search.trim().toLowerCase();
  if (!query) return state.tasks;
  return state.tasks.filter(task => [task.id, task.title, task.brief, task.repo, task.agent_id, task.progress_note, ...(task.scope || [])].join(' ').toLowerCase().includes(query));
}

function taskCard(task) {
  const isClaimed = !!task.claim;
  const unmetDependencies = (task.dependencies || []).filter(id => findTask(id)?.status !== 'done');
  const scopes = (task.scope || []).slice(0, 4).map(path => `<span class="scope-chip" title="${escapeHtml(path)}">${escapeHtml(path)}</span>`).join('');
  const moreScopes = (task.scope || []).length > 4 ? `<span class="scope-chip">+${task.scope.length - 4}</span>` : '';
  const dependencies = (task.dependencies || []).map(id => {
    const dependency = findTask(id);
    const done = dependency?.status === 'done';
    return `<span class="dependency-chip ${done ? 'is-met' : 'is-waiting'}" title="${done ? 'Done' : 'Waiting for Done'}">${done ? '✓' : '↗'} ${escapeHtml(id)}</span>`;
  }).join('');
  const classes = `task-card${isClaimed ? ' is-claimed' : ''}${task.status === 'review' ? ' is-review' : ''}`;
  const card = document.createElement('article');
  card.className = classes;
  card.draggable = !isClaimed;
  card.dataset.id = task.id;
  card.setAttribute('aria-label', `${task.id}: ${task.title}`);
  let secondary = '';
  if (task.status === 'backlog') secondary = `<button class="card-action" data-action="ready" data-id="${task.id}">Move to Ready</button>`;
  else if (task.status === 'ready' && unmetDependencies.length) secondary = `<span class="waiting-state" title="Complete ${escapeHtml(unmetDependencies.join(', '))} first">Waiting on tasks</span>`;
  else if (task.status === 'ready') secondary = `<button class="card-action copy-action" data-action="prompt" data-id="${task.id}">Copy agent prompt</button>`;
  else if (task.status === 'review') secondary = `<button class="card-action approve-action" data-action="approve" data-id="${task.id}">Accept</button><button class="card-action" data-action="reopen" data-id="${task.id}">Reopen</button>`;
  else if (task.status === 'done') secondary = `<button class="card-action" data-action="reopen-done" data-id="${task.id}">Reopen</button>`;
  else if (!isClaimed && task.status === 'blocked') secondary = `<button class="card-action" data-action="ready" data-id="${task.id}">Move to Ready</button>`;
  const agentBlock = task.agent_id ? `<div class="agent-line"><span class="agent-avatar">${escapeHtml(task.agent_id.slice(0, 2).toUpperCase())}</span><span>${escapeHtml(task.agent_id)}</span>${isClaimed ? `<span class="lock-label">${task.claim.state === 'review' ? 'SCOPE LOCKED' : 'CLAIMED'}</span>` : ''}</div>` : '';
  const emptyScope = !task.scope?.length ? `<div class="claim-warning">Exclusive lock: this chat owns the whole repository while claimed.</div>` : '';
  card.innerHTML = `
    <div class="card-topline"><span class="task-id">${escapeHtml(task.id)}</span><span class="priority ${escapeHtml(task.priority)}">${escapeHtml(task.priority)}</span></div>
    <h3 class="task-title">${escapeHtml(task.title)}</h3>
    ${task.brief ? `<p class="task-brief">${escapeHtml(task.brief)}</p>` : ''}
    ${scopes || emptyScope ? `<div class="scope-list">${scopes}${moreScopes}</div>${emptyScope}` : ''}
    ${dependencies ? `<div class="dependency-list"><span class="dependency-label">PREREQUISITES</span>${dependencies}</div>` : ''}
    ${task.status === 'ready' && unmetDependencies.length ? `<div class="claim-warning">Claim locked until ${unmetDependencies.map(escapeHtml).join(', ')} is Done.</div>` : ''}
    <div class="repo-line"><span>⌘</span>${escapeHtml(task.repo || 'default')}</div>
    ${task.progress_note ? `<p class="progress-note">${escapeHtml(task.progress_note)}</p>` : ''}
    ${agentBlock}
    <div class="card-footer"><div class="card-actions-right">${secondary}</div><div class="card-actions-right"><button class="card-action" data-action="details" data-id="${task.id}">Details</button>${!isClaimed && task.status !== 'done' ? `<button class="card-action" data-action="edit" data-id="${task.id}">Edit</button>` : ''}</div></div>`;
  return card;
}

function render() {
  const tasks = getVisibleTasks();
  board.replaceChildren();
  for (const columnInfo of STATUS_COLUMNS) {
    const items = tasks.filter(task => task.status === columnInfo.id);
    const column = document.createElement('section');
    column.className = 'column';
    column.dataset.status = columnInfo.id;
    column.innerHTML = `<header class="column-head"><span class="column-dot"></span><span class="column-title">${columnInfo.label}</span><span class="column-count">${items.length}</span>${columnInfo.id === 'backlog' || columnInfo.id === 'ready' ? `<button class="column-add" type="button" title="Add task to ${columnInfo.label}" data-add-status="${columnInfo.id}">＋</button>` : ''}</header><div class="column-cards" data-drop-status="${columnInfo.id}"></div>`;
    const container = column.querySelector('.column-cards');
    if (!items.length) {
      const message = columnInfo.id === 'ready' ? 'Tasks with completed prerequisites can be claimed.' : columnInfo.id === 'review' ? 'Finished work waits here for your approval.' : 'Drop tasks here';
      container.innerHTML = `<p class="empty-column">${message}</p>`;
    } else items.forEach(task => container.appendChild(taskCard(task)));
    board.appendChild(column);
  }
  const open = state.tasks.filter(task => task.status !== 'done').length;
  const ready = state.tasks.filter(task => task.status === 'ready' && !(task.dependencies || []).some(id => findTask(id)?.status !== 'done')).length;
  const working = state.tasks.filter(task => task.status === 'in_progress' || task.status === 'blocked').length;
  const review = state.tasks.filter(task => task.status === 'review').length;
  document.querySelector('#metric-open').textContent = open;
  document.querySelector('#metric-ready').textContent = ready;
  document.querySelector('#metric-working').textContent = working;
  document.querySelector('#metric-review').textContent = review;
  document.querySelector('#task-count').textContent = `${state.tasks.length} ${state.tasks.length === 1 ? 'task' : 'tasks'}`;
  document.querySelector('#sync-label').textContent = `Updated ${timeLabel(new Date().toISOString())}`;
  wireCardActions();
  wireDragDrop();
}

async function refreshBoard(quiet = false) {
  if (state.busy) return;
  state.busy = true;
  try {
    const data = await api('/api/board');
    state.tasks = data.tasks || [];
    state.events = data.events || [];
    render();
  } catch (error) {
    document.querySelector('#sync-label').textContent = 'Board unavailable';
    if (!quiet) notify(error.message, true);
  } finally { state.busy = false; }
}

function findTask(id) { return state.tasks.find(task => task.id === id); }
function renderDependencyOptions(currentId = '', selectedIds = []) {
  const target = document.querySelector('#dependency-options');
  const candidates = state.tasks.filter(task => task.id !== currentId && (task.status !== 'done' || selectedIds.includes(task.id)));
  if (!candidates.length) {
    target.innerHTML = '<span class="dependency-empty">No unfinished tasks to wait for.</span>';
    return;
  }
  target.innerHTML = candidates.map(task => `<label class="dependency-option"><input type="checkbox" value="${escapeHtml(task.id)}" ${selectedIds.includes(task.id) ? 'checked' : ''}><span><b>${escapeHtml(task.id)}</b><span>${escapeHtml(task.title)}</span><small>${escapeHtml(task.status.replace('_', ' '))}</small></span></label>`).join('');
}

function openNewTask(status = 'backlog') {
  document.querySelector('#task-form').reset();
  document.querySelector('#task-id').value = '';
  document.querySelector('#task-mode').textContent = 'New card';
  document.querySelector('#task-dialog-title').textContent = 'Create a task';
  document.querySelector('#task-ready').checked = status === 'ready';
  document.querySelector('#task-repo').value = 'main repo';
  renderDependencyOptions();
  document.querySelector('#delete-task-button').classList.add('hidden');
  document.querySelector('#save-task-button').textContent = 'Save task';
  taskDialog.showModal();
  document.querySelector('#task-title').focus();
}

function openEditTask(task) {
  document.querySelector('#task-id').value = task.id;
  document.querySelector('#task-mode').textContent = task.id;
  document.querySelector('#task-dialog-title').textContent = 'Edit task';
  document.querySelector('#task-title').value = task.title;
  document.querySelector('#task-brief').value = task.brief || '';
  document.querySelector('#task-repo').value = task.repo || 'default';
  document.querySelector('#task-priority').value = task.priority || 'normal';
  document.querySelector('#task-scope').value = (task.scope || []).join('\n');
  document.querySelector('#task-acceptance').value = (task.acceptance || []).join('\n');
  renderDependencyOptions(task.id, task.dependencies || []);
  document.querySelector('#task-ready').checked = task.status === 'ready';
  document.querySelector('#delete-task-button').classList.remove('hidden');
  document.querySelector('#save-task-button').textContent = 'Save changes';
  taskDialog.showModal();
  document.querySelector('#task-title').focus();
}

function taskFormPayload() {
  return {
    title: document.querySelector('#task-title').value,
    brief: document.querySelector('#task-brief').value,
    repo: document.querySelector('#task-repo').value.trim() || 'default',
    priority: document.querySelector('#task-priority').value,
    scope: document.querySelector('#task-scope').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
    dependencies: [...document.querySelectorAll('#dependency-options input:checked')].map(input => input.value),
    acceptance: document.querySelector('#task-acceptance').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
  };
}

function agentPrompt(task, serverPath) {
  const cli = `'${String(serverPath).replace(/'/g, "''")}'`;
  const scope = task.scope?.length ? task.scope.map(path => `- ${path}`).join('\n') : '- No paths are listed. This task has an exclusive lock on the whole repository.';
  return `Work on Agent Board task ${task.id}: ${task.title}

The board is shared through a local command-line program. Use these commands from PowerShell; they read and update the same board as the browser. A successful claim is atomic, and the board itself rejects overlapping scopes and unmet prerequisites.

Before editing:
1. Confirm this chat is in its own fresh Codex worktree for this task. Do not edit in a worktree another chat is using.
2. Inspect the ready queue with: python ${cli} list --status ready
3. Claim this task before touching files. Run:
   $worktree = (git rev-parse --show-toplevel).Trim()
   python ${cli} claim ${task.id} --agent 'choose a unique short chat label' --worktree $worktree
   Read the JSON output and save its lease_token. If the command fails or returns an error, do not edit files.
4. Stay within the declared scope below. Paths are relative to the repository; a folder scope ends with /.

Declared scope:
${scope}

During work, renew a long-running claim about once an hour:
   python ${cli} heartbeat ${task.id} --token '<lease_token>'

If blocked, post a note:
   python ${cli} progress ${task.id} --token '<lease_token>' --blocked --note 'brief reason'

When ready for review, submit a summary:
   python ${cli} complete ${task.id} --token '<lease_token>' --summary 'what changed and what you checked'

Do not merge or cherry-pick. The board owner reviews the work and accepts or reopens the task; its scope stays locked until then.`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    notify('Copied to clipboard');
  } catch {
    const input = document.createElement('textarea');
    input.value = text;
    input.style.position = 'fixed'; input.style.opacity = '0';
    document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove();
    notify('Copied to clipboard');
  }
}

function wireCardActions() {
  board.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', async event => {
      event.stopPropagation();
      const action = button.dataset.action;
      const task = findTask(button.dataset.id);
      if (!task) return;
      try {
        if (action === 'details') return openDetails(task);
        if (action === 'edit') return openEditTask(task);
        if (action === 'prompt') {
          const info = await api('/api/info');
          return copyText(agentPrompt(task, info.server_path));
        }
        if (action === 'ready') await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'PATCH', body: { status: 'ready' } });
        if (action === 'approve') await api(`/api/tasks/${encodeURIComponent(task.id)}/review`, { method: 'POST', body: { action: 'approve' } });
        if (action === 'reopen') await api(`/api/tasks/${encodeURIComponent(task.id)}/review`, { method: 'POST', body: { action: 'reopen' } });
        if (action === 'reopen-done') await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'PATCH', body: { status: 'ready' } });
        await refreshBoard(true);
        if (action === 'approve') notify(`${task.id} accepted. Its file scope is unlocked.`);
        else if (action === 'reopen' || action === 'reopen-done') notify(`${task.id} returned to Ready.`);
        else notify(`${task.id} moved to Ready.`);
      } catch (error) { notify(error.message, true); }
    });
  });
  board.querySelectorAll('[data-add-status]').forEach(button => button.addEventListener('click', () => openNewTask(button.dataset.addStatus)));
}

function wireDragDrop() {
  board.querySelectorAll('.task-card[draggable="true"]').forEach(card => {
    card.addEventListener('dragstart', event => {
      state.draggedId = card.dataset.id;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', state.draggedId);
      requestAnimationFrame(() => card.style.opacity = '.45');
    });
    card.addEventListener('dragend', () => { card.style.opacity = ''; state.draggedId = null; board.querySelectorAll('.column').forEach(column => column.classList.remove('drag-over')); });
  });
  board.querySelectorAll('.column').forEach(column => {
    column.addEventListener('dragover', event => { if (!state.draggedId) return; event.preventDefault(); column.classList.add('drag-over'); });
    column.addEventListener('dragleave', event => { if (!column.contains(event.relatedTarget)) column.classList.remove('drag-over'); });
    column.addEventListener('drop', async event => {
      event.preventDefault();
      column.classList.remove('drag-over');
      const id = state.draggedId || event.dataTransfer.getData('text/plain');
      const task = findTask(id);
      if (!task || task.status === column.dataset.status) return;
      try {
        if (task.status === 'review') return notify('Accept or reopen this card to release its file lock.', true);
        await api(`/api/tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: { status: column.dataset.status } });
        await refreshBoard(true);
      } catch (error) { notify(error.message, true); }
      state.draggedId = null;
    });
  });
}

async function openDetails(task) {
  document.querySelector('#detail-id').textContent = `${task.id} · ${task.repo || 'default'}`;
  document.querySelector('#detail-title').textContent = task.title;
  const fields = [];
  fields.push(`<section class="detail-field"><h3>Brief</h3><p>${escapeHtml(task.brief || 'No brief added.')}</p></section>`);
  fields.push(`<section class="detail-field"><h3>Edit scope</h3><div class="detail-scope">${task.scope?.length ? task.scope.map(path => `<span class="scope-chip">${escapeHtml(path)}</span>`).join('') : '<p>Whole repository (exclusive lock)</p>'}</div></section>`);
  if (task.dependencies?.length) fields.push(`<section class="detail-field"><h3>Prerequisites</h3><p>${task.dependencies.map(id => { const prerequisite = findTask(id); return `${escapeHtml(id)} · ${escapeHtml(prerequisite?.status || 'missing')}`; }).join('<br>')}</p></section>`);
  if (task.acceptance?.length) fields.push(`<section class="detail-field"><h3>Done when</h3><p>${task.acceptance.map(item => `• ${escapeHtml(item)}`).join('<br>')}</p></section>`);
  if (task.agent_id) fields.push(`<section class="detail-field"><h3>Current chat</h3><p>${escapeHtml(task.agent_id)}${task.worktree ? ` · ${escapeHtml(task.worktree)}` : ''}${task.claim?.state === 'review' ? ' · awaiting review, scope remains locked' : ''}</p></section>`);
  if (task.progress_note) fields.push(`<section class="detail-field"><h3>Latest update</h3><p>${escapeHtml(task.progress_note)}</p></section>`);
  document.querySelector('#detail-content').innerHTML = fields.join('');
  const eventBox = document.querySelector('#event-list');
  eventBox.innerHTML = '<div class="event-item">Loading task activity…</div>';
  detailDialog.showModal();
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/events`);
    eventBox.innerHTML = data.events.length ? data.events.map(item => `<div class="event-item"><div><strong>${escapeHtml(item.actor)}</strong> ${escapeHtml(item.message)}<span class="event-time">${escapeHtml(timeLabel(item.created_at))}</span></div></div>`).join('') : '<div class="event-item">No activity yet.</div>';
  } catch { eventBox.innerHTML = ''; }
}

async function showAgentInstructions() {
  setupDialog.showModal();
  try {
    const info = await api('/api/info');
    document.querySelector('#agent-cli-path').textContent = info.server_path;
  } catch (error) { document.querySelector('#agent-cli-path').textContent = error.message; }
}

document.querySelector('#new-task-button').addEventListener('click', () => openNewTask('backlog'));
document.querySelector('#instructions-button').addEventListener('click', showAgentInstructions);
document.querySelector('#instructions-link').addEventListener('click', showAgentInstructions);
document.querySelector('#task-close').addEventListener('click', () => taskDialog.close());
document.querySelector('#task-cancel').addEventListener('click', () => taskDialog.close());
document.querySelector('#setup-close').addEventListener('click', () => setupDialog.close());
document.querySelector('#detail-close').addEventListener('click', () => detailDialog.close());
document.querySelector('#refresh-button').addEventListener('click', () => refreshBoard());
document.querySelector('#search-input').addEventListener('input', event => { state.search = event.target.value; render(); });
document.querySelector('#copy-cli-path').addEventListener('click', () => copyText(document.querySelector('#agent-cli-path').textContent));

document.querySelector('#task-form').addEventListener('submit', async event => {
  if (event.submitter?.value === 'cancel') return;
  event.preventDefault();
  const id = document.querySelector('#task-id').value;
  const payload = taskFormPayload();
  try {
    if (id) {
      payload.status = document.querySelector('#task-ready').checked ? 'ready' : 'backlog';
      await api(`/api/tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: payload });
      notify(`${id} updated`);
    } else {
      payload.status = document.querySelector('#task-ready').checked ? 'ready' : 'backlog';
      await api('/api/tasks', { method: 'POST', body: payload });
      notify('Task added to the board');
    }
    taskDialog.close();
    await refreshBoard(true);
  } catch (error) { notify(error.message, true); }
});

document.querySelector('#delete-task-button').addEventListener('click', async () => {
  const id = document.querySelector('#task-id').value;
  if (!id || !confirm(`Delete ${id}? This also removes its activity history.`)) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
    taskDialog.close(); await refreshBoard(true); notify(`${id} deleted`);
  } catch (error) { notify(error.message, true); }
});

refreshBoard();
state.timer = setInterval(() => refreshBoard(true), 5000);
