/**
 * Quick Run — sidebar section for manual-trigger tasks.
 * Fetches tasks with trigger_type='manual' and renders them as one-click
 * run buttons in the Manual Workflows sidebar section.
 */

const API_BASE = window.location.origin;

let _tasks = [];
let _running = new Set();

async function _fetchManualTasks() {
  try {
    const res = await fetch(`${API_BASE}/api/tasks/manual`, { credentials: 'same-origin' });
    if (!res.ok) return [];
    const data = await res.json();
    return data.tasks || [];
  } catch (e) {
    return [];
  }
}

function _relativeTime(isoStr) {
  if (!isoStr) return '';
  try {
    const diff = Date.now() - new Date(isoStr).getTime();
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  } catch (e) {
    return '';
  }
}

function _render() {
  const list = document.getElementById('manual-workflows-list');
  if (!list) return;

  if (!_tasks.length) {
    list.innerHTML = '';
    return;
  }

  const items = _tasks.map(t => {
    const rel = _relativeTime(t.last_run);
    const isRunning = _running.has(t.id);
    return `<div class="list-item quick-run-item${isRunning ? ' quick-run-running' : ''}" data-qr-id="${t.id}" title="Run: ${t.name}" style="cursor:pointer;">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;opacity:0.45;margin-right:1px;">
        ${isRunning
          ? '<circle cx="12" cy="12" r="9" stroke-dasharray="4 2" class="qr-spin"/>'
          : '<polygon points="5 3 19 12 5 21 5 3"/>'
        }
      </svg>
      <span class="grow">${t.name}</span>
      ${rel ? `<span class="quick-run-last-run">${rel}</span>` : ''}
    </div>`;
  }).join('');

  list.innerHTML = items;

  list.querySelectorAll('.quick-run-item').forEach(el => {
    el.addEventListener('click', () => _runTask(el.dataset.qrId));
  });
}

async function _runTask(id) {
  if (_running.has(id)) return;
  _running.add(id);
  _render();

  let result = null;
  let error = null;
  try {
    const res = await fetch(`${API_BASE}/api/tasks/${id}/run`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    const data = await res.json();
    if (!res.ok) {
      error = data.detail || 'Failed to start task';
    }
  } catch (e) {
    error = String(e);
  }

  // Poll for completion (max ~90s, every 2s)
  if (!error) {
    for (let i = 0; i < 45; i++) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const r = await fetch(`${API_BASE}/api/tasks/${id}/runs?limit=1`, { credentials: 'same-origin' });
        if (r.ok) {
          const d = await r.json();
          const latest = (d.runs || [])[0];
          if (latest && latest.status !== 'queued' && latest.status !== 'running') {
            result = latest.result || '';
            if (latest.status === 'error') error = latest.error || result;
            break;
          }
        }
      } catch (_) {}
    }
  }

  _running.delete(id);

  // Refresh last_run time
  const refreshed = await _fetchManualTasks();
  if (refreshed.length) _tasks = refreshed;
  _render();

  // Show result in a simple toast/modal
  _showResult(id, result, error);
}

function _showResult(id, result, error) {
  const task = _tasks.find(t => t.id === id);
  const title = task ? task.name : 'Task';

  // Remove any previous result modal
  document.getElementById('qr-result-modal')?.remove();

  const modal = document.createElement('div');
  modal.id = 'qr-result-modal';
  modal.className = 'qr-result-modal';
  modal.innerHTML = `
    <div class="qr-result-inner">
      <div class="qr-result-header">
        <span class="qr-result-title">${title}</span>
        <button class="qr-result-close" id="qr-result-close" aria-label="Close">&#x2715;</button>
      </div>
      <div class="qr-result-body">${error
        ? `<span style="color:var(--red,#e06c75);">Error: ${error}</span>`
        : (result || '(no output)')
      }</div>
    </div>
  `;
  document.body.appendChild(modal);
  document.getElementById('qr-result-close')?.addEventListener('click', () => modal.remove());
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

async function init() {
  _tasks = await _fetchManualTasks();
  _render();

  // Wire the "+" button in the section header to open Tasks modal pre-set to Manual
  const addBtn = document.getElementById('manual-workflow-add-btn');
  if (addBtn && !addBtn._qrWired) {
    addBtn._qrWired = true;
    addBtn.addEventListener('click', () => {
      const ev = new CustomEvent('open-tasks-modal', { detail: { triggerType: 'manual' } });
      document.dispatchEvent(ev);
    });
  }
}

// Auto-init: wait for DOM then run.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

// Re-render on visibility change (user returns to tab) to freshen last_run labels
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && _tasks.length) _render();
});

// Refresh when tasks panel closes so newly-created manual tasks appear immediately
document.addEventListener('tasks-panel-closed', init);

export default { init, refresh: init };
