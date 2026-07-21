/**
 * QP Generations Module — read-only browse of QpGeneration snapshots plus a
 * proposal-vs-actual diff view (TODO_B_NEW pairing infra). No entry form by
 * design: actuals are written server-side via PUT /api/quick_proposal/actuals/{run_id}.
 */

import * as Modals from './modalManager.js';

const API_BASE = window.location.origin;
let _open = false;
let _runsCache = [];

function _esc(str) {
  if (str === null || str === undefined) return '';
  const d = document.createElement('div');
  d.textContent = String(str);
  return d.innerHTML;
}

function _fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return '$' + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function _fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '';
  const sign = v > 0 ? '+' : '';
  return `(${sign}${v.toFixed(1)}%)`;
}

function _deltaClass(v) {
  if (v === null || v === undefined) return '';
  return v > 0 ? 'qpgen-delta-pos' : (v < 0 ? 'qpgen-delta-neg' : '');
}

async function _fetchRuns() {
  const res = await fetch(`${API_BASE}/api/quick_proposal/generations`);
  if (!res.ok) throw new Error(`Failed to load generations (${res.status})`);
  const data = await res.json();
  return data.runs || [];
}

async function _fetchRunDetail(runId) {
  const res = await fetch(`${API_BASE}/api/quick_proposal/generations/${encodeURIComponent(runId)}`);
  if (!res.ok) throw new Error(`Failed to load run detail (${res.status})`);
  return res.json();
}

async function _fetchReliability() {
  const res = await fetch(`${API_BASE}/api/quick_proposal/actuals-reliability`);
  if (!res.ok) throw new Error(`Failed to load reliability report (${res.status})`);
  const data = await res.json();
  return data.fields || [];
}

function _renderReliabilityTable(fields) {
  const container = document.getElementById('qpgen-reliability-table');
  if (!container) return;
  if (!fields.length) {
    container.innerHTML = '<div class="qpgen-empty">No mapped fields yet — pair more actuals to populate this.</div>';
    return;
  }
  const rows = fields.map((f) => `
    <tr>
      <td class="qpgen-diff-field">${_esc(f.field)}</td>
      <td>${f.sample_count}</td>
      <td class="${_deltaClass(f.mean_delta_pct)}">${_fmtPct(f.mean_delta_pct)}</td>
      <td class="${_deltaClass(f.median_delta_pct)}">${_fmtPct(f.median_delta_pct)}</td>
      <td>${f.over_count} over / ${f.under_count} under</td>
    </tr>`).join('');
  container.innerHTML = `
    <table class="qpgen-diff-table">
      <thead>
        <tr><th>Field</th><th>N</th><th>Mean Δ%</th><th>Median Δ%</th><th>Direction</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="qpgen-reliability-note">Informational only — not wired into any manager rule or gate. Small sample sizes (see N); treat as a first look, not a threshold.</div>`;
}

function _renderRunsTable(runs) {
  const container = document.getElementById('qpgen-runs-table');
  if (!container) return;
  if (!runs.length) {
    container.innerHTML = '<div class="qpgen-empty">No QP generations recorded yet.</div>';
    return;
  }
  const rows = runs.map((r) => {
    const deltaPct = (r.has_actual && r.actual_total)
      ? ((r.latest_grand_total - r.actual_total) / r.actual_total * 100.0)
      : null;
    const actualCell = r.has_actual
      ? `${_fmtMoney(r.actual_total)} <span class="${_deltaClass(deltaPct)}">${_fmtPct(deltaPct)}</span>`
      : '<span class="qpgen-no-actual">— no actual</span>';
    return `
      <tr class="qpgen-run-row" data-run-id="${_esc(r.run_id)}">
        <td class="qpgen-run-name">${_esc(r.run_name || '—')}</td>
        <td class="qpgen-run-id">${_esc(r.run_id)}</td>
        <td>${r.generation_count}</td>
        <td>${_fmtMoney(r.latest_grand_total)}</td>
        <td>${actualCell}</td>
        <td>${_esc(r.manager_model || '—')}</td>
        <td>${r.created_at ? new Date(r.created_at).toLocaleDateString() : '—'}</td>
      </tr>`;
  }).join('');
  container.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Name</th><th>Run</th><th>Gens</th><th>Latest total</th><th>Actual</th><th>Manager</th><th>Created</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  container.querySelectorAll('.qpgen-run-row').forEach((row) => {
    row.addEventListener('click', () => _openDetail(row.dataset.runId));
  });
}

function _renderDiffTable(diff) {
  if (!diff || !diff.length) {
    return '<div class="qpgen-no-diff">No actual entered for this run yet.</div>';
  }
  const rows = diff.map((d) => `
    <tr>
      <td class="qpgen-diff-field">${_esc(d.field)}</td>
      <td>${d.proposal_value === null || d.proposal_value === undefined ? '—' : _esc(d.proposal_value)}</td>
      <td>${d.actual_value === null || d.actual_value === undefined ? '—' : _esc(d.actual_value)}</td>
      <td class="${_deltaClass(d.delta)}">${d.delta === null || d.delta === undefined ? '—' : _esc(Math.round(d.delta * 100) / 100)}</td>
      <td class="${_deltaClass(d.delta)}">${_fmtPct(d.delta_pct)}</td>
    </tr>`).join('');
  return `
    <table class="qpgen-diff-table">
      <thead>
        <tr><th>Field</th><th>Proposal</th><th>Actual</th><th>Delta</th><th>Delta %</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function _openDetail(runId) {
  const listEl = document.getElementById('qpgen-list-view');
  const detailEl = document.getElementById('qpgen-detail-view');
  if (!listEl || !detailEl) return;
  detailEl.innerHTML = '<div class="qpgen-empty">Loading…</div>';
  listEl.style.display = 'none';
  detailEl.classList.add('open');
  try {
    const data = await _fetchRunDetail(runId);
    const cards = data.generations.map((g) => `
      <div class="qpgen-gen-card">
        <div class="qpgen-gen-card-header">
          <span>Generation ${g.generation_index}</span>
          <span>${_fmtMoney(g.grand_total)}${g.grand_total_delta !== null && g.grand_total_delta !== undefined
            ? ` <span class="${_deltaClass(g.grand_total_delta)}">(${g.grand_total_delta > 0 ? '+' : ''}${_fmtMoney(g.grand_total_delta)})</span>`
            : ''}</span>
        </div>
        <div class="qpgen-gen-card-meta">
          ${_esc(g.manager_model || '—')} / ${_esc(g.gemini_model || '—')} · ${g.created_at ? new Date(g.created_at).toLocaleString() : '—'}
        </div>
        ${_renderDiffTable(g.diff)}
      </div>`).join('');
    detailEl.innerHTML = `
      <button class="qpgen-back-btn" id="qpgen-back-btn">&larr; Back to runs</button>
      <h4 style="margin:4px 0 10px;font-size:13px;">
        ${data.run_name ? `${_esc(data.run_name)} — ` : ''}<span class="qpgen-run-id">${_esc(data.run_id)}</span>
        ${data.actual ? '' : ' — no actual entered yet'}
      </h4>
      ${cards}`;
    document.getElementById('qpgen-back-btn').addEventListener('click', () => {
      detailEl.classList.remove('open');
      detailEl.innerHTML = '';
      listEl.style.display = '';
    });
  } catch (e) {
    detailEl.innerHTML = `<div class="qpgen-empty">Failed to load run detail: ${_esc(e.message)}</div>`;
  }
}

async function _refresh() {
  const container = document.getElementById('qpgen-runs-table');
  if (container) container.innerHTML = '<div class="qpgen-empty">Loading…</div>';
  try {
    _runsCache = await _fetchRuns();
    _renderRunsTable(_runsCache);
  } catch (e) {
    if (container) container.innerHTML = `<div class="qpgen-empty">Failed to load: ${_esc(e.message)}</div>`;
  }

  const reliabilityContainer = document.getElementById('qpgen-reliability-table');
  if (reliabilityContainer) reliabilityContainer.innerHTML = '<div class="qpgen-empty">Loading…</div>';
  try {
    _renderReliabilityTable(await _fetchReliability());
  } catch (e) {
    if (reliabilityContainer) reliabilityContainer.innerHTML = `<div class="qpgen-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function _doClose() {
  _open = false;
  const modal = document.getElementById('qp-generations-modal');
  if (modal) modal.remove();
  const btn = document.getElementById('qp-generations-btn');
  if (btn) btn.classList.remove('active');
}

export function closeQpGenerations() {
  if (!_open && !Modals.isMinimized('qp-generations-modal')) return;
  if (Modals.isRegistered('qp-generations-modal')) {
    Modals.close('qp-generations-modal');
  } else {
    _doClose();
  }
}

export function isQpGenerationsOpen() {
  if (Modals.isMinimized('qp-generations-modal')) return false;
  return _open;
}

export function openQpGenerations() {
  if (Modals.isRegistered('qp-generations-modal') && Modals.isMinimized('qp-generations-modal')) {
    Modals.restore('qp-generations-modal');
    return;
  }
  if (_open) return;
  _open = true;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'qp-generations-modal';
  modal.innerHTML = `
    <div class="modal-content qpgen-modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>QP Generations</h4>
        <button class="modal-close" id="qpgen-close">&times;</button>
      </div>
      <div class="modal-body qpgen-body">
        <div id="qpgen-list-view">
          <button class="qpgen-reliability-toggle" id="qpgen-reliability-toggle" aria-expanded="false">
            Reliability report (TODO_B_NEW-2) <span class="qpgen-reliability-chevron">&rsaquo;</span>
          </button>
          <div class="qpgen-reliability-panel hidden" id="qpgen-reliability-panel">
            <div id="qpgen-reliability-table"></div>
          </div>
          <div class="qpgen-runs-table" id="qpgen-runs-table"></div>
        </div>
        <div class="qpgen-detail" id="qpgen-detail-view"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  document.getElementById('qpgen-reliability-toggle').addEventListener('click', () => {
    const toggle = document.getElementById('qpgen-reliability-toggle');
    const panel = document.getElementById('qpgen-reliability-panel');
    const expanded = toggle.getAttribute('aria-expanded') === 'true';
    toggle.setAttribute('aria-expanded', String(!expanded));
    panel.classList.toggle('hidden', expanded);
    toggle.classList.toggle('open', !expanded);
  });

  Modals.register('qp-generations-modal', {
    railBtnId: null,
    sidebarBtnId: 'qp-generations-btn',
    closeFn: () => _doClose(),
    restoreFn: () => {},
  });

  document.getElementById('qpgen-close').addEventListener('click', () => closeQpGenerations());

  const btn = document.getElementById('qp-generations-btn');
  if (btn) btn.classList.add('active');

  _refresh();
}

const qpGenerationsModule = {
  openQpGenerations,
  closeQpGenerations,
  isQpGenerationsOpen,
};

export default qpGenerationsModule;
