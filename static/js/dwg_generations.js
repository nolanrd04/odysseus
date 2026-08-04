/**
 * DWG Generations Module — read-only browse of automatically-captured live
 * DWG extraction runs (src/dwg_pipeline/generations.py) plus their
 * predicted-vs-actual scoring against known corpus jobs. Mirrors
 * qp_generations.js's structure; no entry form — every row here was captured
 * automatically at the end of a completed live agent turn.
 */

import * as Modals from './modalManager.js';

const API_BASE = window.location.origin;
let _open = false;

function _esc(str) {
  if (str === null || str === undefined) return '';
  const d = document.createElement('div');
  d.textContent = String(str);
  return d.innerHTML;
}

function _fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return (v * 100).toFixed(1) + '%';
}

function _deltaClass(v) {
  if (v === null || v === undefined) return '';
  return v > 0.10 ? 'dwggen-delta-bad' : 'dwggen-delta-ok';
}

async function _fetchJobs() {
  const res = await fetch(`${API_BASE}/api/dwg/generations`);
  if (!res.ok) throw new Error(`Failed to load generations (${res.status})`);
  const data = await res.json();
  return data.jobs || [];
}

async function _fetchJobDetail(jobId) {
  const res = await fetch(`${API_BASE}/api/dwg/generations/${encodeURIComponent(jobId)}`);
  if (!res.ok) throw new Error(`Failed to load job detail (${res.status})`);
  return res.json();
}

async function _fetchReliability() {
  const res = await fetch(`${API_BASE}/api/dwg/reliability`);
  if (!res.ok) throw new Error(`Failed to load reliability report (${res.status})`);
  return res.json();
}

function _renderReliabilityTable(data) {
  const container = document.getElementById('dwggen-reliability-table');
  if (!container) return;
  const fields = data.fields || [];
  if (!fields.length) {
    container.innerHTML = '<div class="dwggen-empty">No scored generations yet — run a job that matches a corpus job to populate this.</div>';
    return;
  }
  const rows = fields.map((f) => `
    <tr>
      <td class="dwggen-diff-field">${_esc(f.work_type)}</td>
      <td>${f.sample_count}</td>
      <td class="${_deltaClass(f.mean_pct_error)}">${_fmtPct(f.mean_pct_error)}</td>
      <td class="${_deltaClass(f.median_pct_error)}">${_fmtPct(f.median_pct_error)}</td>
      <td>${f.missed_count}</td>
    </tr>`).join('');
  container.innerHTML = `
    <table class="dwggen-diff-table">
      <thead>
        <tr><th>Work type</th><th>N</th><th>Mean % err</th><th>Median % err</th><th>Times missed</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="dwggen-reliability-note">Informational only — built from the latest generation of each scored job (${data.jobs_scored || 0} job(s)). Small sample sizes; treat as a first look, not a threshold.</div>`;
}

function _renderJobsTable(jobs) {
  const container = document.getElementById('dwggen-jobs-table');
  if (!container) return;
  if (!jobs.length) {
    container.innerHTML = '<div class="dwggen-empty">No DWG generations captured yet. A run is only captured automatically when its DXF filenames match a known corpus job.</div>';
    return;
  }
  const rows = jobs.map((j) => {
    const hitRate = (j.lines_compared)
      ? `${j.lines_within_tolerance}/${j.lines_compared}`
      : '—';
    return `
      <tr class="dwggen-job-row" data-job-id="${_esc(j.job_id)}">
        <td class="dwggen-job-id">${_esc(j.job_id)}</td>
        <td>${_esc(j.corpus_job_folder || '—')}</td>
        <td>${j.generation_count}</td>
        <td>${hitRate}</td>
        <td class="${_deltaClass(j.mean_abs_pct_error)}">${_fmtPct(j.mean_abs_pct_error)}</td>
        <td>${j.misses} miss / ${j.extras} extra</td>
        <td>${_esc(j.model || '—')}</td>
        <td>${j.created_at ? new Date(j.created_at).toLocaleDateString() : '—'}</td>
      </tr>`;
  }).join('');
  container.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Job</th><th>Corpus match</th><th>Gens</th><th>Within tol.</th><th>Mean % err</th><th>Miss/extra</th><th>Model</th><th>Created</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  container.querySelectorAll('.dwggen-job-row').forEach((row) => {
    row.addEventListener('click', () => _openDetail(row.dataset.jobId));
  });
}

function _renderPerLineTable(perLine) {
  if (!perLine || !perLine.length) {
    return '<div class="dwggen-no-diff">No comparable lines.</div>';
  }
  const rows = perLine.map((l) => `
    <tr>
      <td class="dwggen-diff-field">${_esc(l.work_type)}</td>
      <td>${l.actual}</td>
      <td>${l.predicted}</td>
      <td class="${_deltaClass(l.pct_error)}">${_fmtPct(l.pct_error)}</td>
      <td>${l.within_tolerance ? '✓' : '✗'}</td>
    </tr>`).join('');
  return `
    <table class="dwggen-diff-table">
      <thead>
        <tr><th>Work type</th><th>Actual</th><th>Predicted</th><th>% err</th><th>OK?</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function _openDetail(jobId) {
  const listEl = document.getElementById('dwggen-list-view');
  const detailEl = document.getElementById('dwggen-detail-view');
  if (!listEl || !detailEl) return;
  detailEl.innerHTML = '<div class="dwggen-empty">Loading…</div>';
  listEl.style.display = 'none';
  detailEl.classList.add('open');
  try {
    const data = await _fetchJobDetail(jobId);
    const cards = data.generations.map((g) => {
      const m = g.metrics || {};
      return `
      <div class="dwggen-gen-card">
        <div class="dwggen-gen-card-header">
          <span>Generation ${g.generation_index}</span>
          <span>${m.lines_within_tolerance ?? '—'}/${m.lines_compared ?? '—'} within tolerance</span>
        </div>
        <div class="dwggen-gen-card-meta">
          ${_esc(g.model || '—')} · ${g.created_at ? new Date(g.created_at).toLocaleString() : '—'}
          ${m.misses && m.misses.length ? ` · ${m.misses.length} miss(es)` : ''}
          ${m.extras && m.extras.length ? ` · ${m.extras.length} extra(s)` : ''}
        </div>
        ${_renderPerLineTable(m.per_line)}
      </div>`;
    }).join('');
    detailEl.innerHTML = `
      <button class="dwggen-back-btn" id="dwggen-back-btn">&larr; Back to jobs</button>
      <h4 style="margin:4px 0 10px;font-size:13px;">
        <span class="dwggen-job-id">${_esc(data.job_id)}</span>
        ${data.corpus_job_folder ? ` — matched to ${_esc(data.corpus_job_folder)}` : ''}
      </h4>
      ${cards}`;
    document.getElementById('dwggen-back-btn').addEventListener('click', () => {
      detailEl.classList.remove('open');
      detailEl.innerHTML = '';
      listEl.style.display = '';
    });
  } catch (e) {
    detailEl.innerHTML = `<div class="dwggen-empty">Failed to load job detail: ${_esc(e.message)}</div>`;
  }
}

async function _refresh() {
  const container = document.getElementById('dwggen-jobs-table');
  if (container) container.innerHTML = '<div class="dwggen-empty">Loading…</div>';
  try {
    _renderJobsTable(await _fetchJobs());
  } catch (e) {
    if (container) container.innerHTML = `<div class="dwggen-empty">Failed to load: ${_esc(e.message)}</div>`;
  }

  const reliabilityContainer = document.getElementById('dwggen-reliability-table');
  if (reliabilityContainer) reliabilityContainer.innerHTML = '<div class="dwggen-empty">Loading…</div>';
  try {
    _renderReliabilityTable(await _fetchReliability());
  } catch (e) {
    if (reliabilityContainer) reliabilityContainer.innerHTML = `<div class="dwggen-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function _doClose() {
  _open = false;
  const modal = document.getElementById('dwg-generations-modal');
  if (modal) modal.remove();
  const btn = document.getElementById('dwg-generations-btn');
  if (btn) btn.classList.remove('active');
}

export function closeDwgGenerations() {
  if (!_open && !Modals.isMinimized('dwg-generations-modal')) return;
  if (Modals.isRegistered('dwg-generations-modal')) {
    Modals.close('dwg-generations-modal');
  } else {
    _doClose();
  }
}

export function isDwgGenerationsOpen() {
  if (Modals.isMinimized('dwg-generations-modal')) return false;
  return _open;
}

export function openDwgGenerations() {
  if (Modals.isRegistered('dwg-generations-modal') && Modals.isMinimized('dwg-generations-modal')) {
    Modals.restore('dwg-generations-modal');
    return;
  }
  if (_open) return;
  _open = true;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'dwg-generations-modal';
  modal.innerHTML = `
    <div class="modal-content dwggen-modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>DWG Generations</h4>
        <button class="modal-close" id="dwggen-close">&times;</button>
      </div>
      <div class="modal-body dwggen-body">
        <div id="dwggen-list-view">
          <button class="dwggen-reliability-toggle" id="dwggen-reliability-toggle" aria-expanded="false">
            Reliability report <span class="dwggen-reliability-chevron">&rsaquo;</span>
          </button>
          <div class="dwggen-reliability-panel hidden" id="dwggen-reliability-panel">
            <div id="dwggen-reliability-table"></div>
          </div>
          <div class="dwggen-jobs-table" id="dwggen-jobs-table"></div>
        </div>
        <div class="dwggen-detail" id="dwggen-detail-view"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  document.getElementById('dwggen-reliability-toggle').addEventListener('click', () => {
    const toggle = document.getElementById('dwggen-reliability-toggle');
    const panel = document.getElementById('dwggen-reliability-panel');
    const expanded = toggle.getAttribute('aria-expanded') === 'true';
    toggle.setAttribute('aria-expanded', String(!expanded));
    panel.classList.toggle('hidden', expanded);
    toggle.classList.toggle('open', !expanded);
  });

  Modals.register('dwg-generations-modal', {
    railBtnId: null,
    sidebarBtnId: 'dwg-generations-btn',
    closeFn: () => _doClose(),
    restoreFn: () => {},
  });

  document.getElementById('dwggen-close').addEventListener('click', () => closeDwgGenerations());

  const btn = document.getElementById('dwg-generations-btn');
  if (btn) btn.classList.add('active');

  _refresh();
}

const dwgGenerationsModule = {
  openDwgGenerations,
  closeDwgGenerations,
  isDwgGenerationsOpen,
};

export default dwgGenerationsModule;
