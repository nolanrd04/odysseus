// qp_jobs.js — Jobs tab in the brain (Memories) modal. TODO_WW part 1.
// CRUD over the Quick Proposal case library (/api/quick_proposal/case_library).
// Lazy-loaded by memory.js when the Jobs tab is clicked; styles in css/qp_jobs.css.

import { renderJobForm, collectJobForm } from './qp_job_form.js';

let _wired = false;
let _jobs = [];
let _editOriginal = null;   // full record being edited, for form→JSON merge

const API = '/api/quick_proposal/case_library';

function _esc(str) {
  return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _fmtTotal(v) {
  if (typeof v !== 'number') return null;
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

const _el = (id) => document.getElementById(id);

export async function loadJobsTab() {
  _wireOnce();
  _closeEditor();
  const list = _el('qp-jobs-tab-list');
  if (!list) return;
  list.innerHTML = '<span style="opacity:0.5;font-size:12px">Loading…</span>';
  try {
    const r = await fetch(API, { credentials: 'same-origin', cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _jobs = await r.json();
  } catch (e) {
    list.innerHTML = `<span class="qp-jobs-tab-card-error">Could not load case library — ${_esc(e.message)}</span>`;
    return;
  }
  _renderList();
}

function _wireOnce() {
  if (_wired) return;
  _wired = true;
  _el('qp-jobs-tab-search')?.addEventListener('input', _renderList);
  _el('qp-jobs-tab-add')?.addEventListener('click', () => _openEditor(null));
}

function _renderList() {
  const list = _el('qp-jobs-tab-list');
  if (!list) return;
  const q = (_el('qp-jobs-tab-search')?.value || '').trim().toLowerCase();
  const visible = _jobs.filter(j =>
    !q || (j.job_name || '').toLowerCase().includes(q) || (j.client || '').toLowerCase().includes(q));

  const badge = _el('qp-jobs-tab-count-badge');
  if (badge) badge.textContent = String(_jobs.length);
  const count = _el('qp-jobs-tab-count');
  if (count) count.textContent = q ? `${visible.length}/${_jobs.length}` : `${_jobs.length} jobs`;

  list.innerHTML = '';
  if (!visible.length) {
    list.innerHTML = '<span style="opacity:0.5;font-size:12px">No jobs match</span>';
    return;
  }
  visible.forEach(job => {
    const card = document.createElement('div');
    card.className = 'qp-jobs-tab-card';
    if (job.parse_error) {
      card.innerHTML = `
        <div class="qp-jobs-tab-card-main">
          <div class="qp-jobs-tab-card-name">${_esc(job.slug)}.json</div>
          <div class="qp-jobs-tab-card-error">Invalid JSON on disk: ${_esc(job.parse_error)}</div>
        </div>`;
    } else {
      const meta = [
        job.client,
        job.job_type,
        job.line_item_count ? `${job.line_item_count} line items` : null,
        _fmtTotal(job.grand_total),
      ].filter(Boolean).join(' · ');
      card.innerHTML = `
        <div class="qp-jobs-tab-card-main">
          <div class="qp-jobs-tab-card-name">${_esc(job.job_name)}</div>
          <div class="qp-jobs-tab-card-meta">${_esc(meta) || '<span style="opacity:0.5">no proposal data</span>'}</div>
        </div>
        <button data-act="edit">Edit</button>
        <button data-act="delete" class="qp-jobs-danger">Delete</button>`;
      card.querySelector('[data-act="edit"]').addEventListener('click', () => _openEditor(job.slug));
      card.querySelector('[data-act="delete"]').addEventListener('click', () => _deleteJob(job));
    }
    list.appendChild(card);
  });
}

async function _deleteJob(job) {
  if (!confirm(`Delete "${job.job_name}" from the case library?\n\nFuture knowledge packs will no longer include it. Past runs are unaffected (they snapshot their own KP).`)) return;
  try {
    const r = await fetch(`${API}/${encodeURIComponent(job.slug)}`, { method: 'DELETE', credentials: 'same-origin' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail || `HTTP ${r.status}`);
  } catch (e) {
    alert(`Delete failed — ${e.message}`);
    return;
  }
  _jobs = _jobs.filter(j => j.slug !== job.slug);
  _renderList();
}

function _newJobTemplate() {
  return {
    schema_version: '1.0',
    job_name: '',
    built_at: new Date().toISOString().slice(0, 10),
    identity: { job_name: '', client: '', client_location: '' },
    classification: { job_type: 'subdivision_road', job_type_source: 'manual' },
    primary_proposal_index: 0,
    proposals: [
      { revision_label: 'base revision', proposal_date: '', grand_total: null, line_items: [] },
    ],
  };
}

// slug=null → create mode; otherwise fetch and edit the existing record
async function _openEditor(slug) {
  const editor = _el('qp-jobs-tab-editor');
  const list = _el('qp-jobs-tab-list');
  if (!editor || !list) return;

  let content = _newJobTemplate();
  if (slug) {
    try {
      const r = await fetch(`${API}/${encodeURIComponent(slug)}`, { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail || `HTTP ${r.status}`);
      content = (await r.json()).content;
    } catch (e) {
      alert(`Could not load job — ${e.message}`);
      return;
    }
  }

  _editOriginal = content;
  list.classList.add('hidden');
  document.querySelector('.qp-jobs-tab-toolbar')?.classList.add('hidden');
  editor.classList.remove('hidden');
  editor.innerHTML = `
    <div class="qp-jobs-editor-head">
      <div class="qp-jobs-editor-title">${slug ? 'Edit Job' : 'New Job'}</div>
      <div class="qp-jobs-editor-row">
        <label for="qp-jobs-editor-slug">File</label>
        <input type="text" id="qp-jobs-editor-slug" value="${_esc(slug || '')}"
               placeholder="(auto from job name)" ${slug ? 'readonly' : ''} spellcheck="false">
        <span style="font-size:11px;opacity:0.6">.json</span>
      </div>
    </div>
    <div id="qp-jobs-form-host" class="qp-jobs-form-host"></div>
    <div id="qp-jobs-editor-status" class="qp-jobs-editor-status"></div>
    <div class="qp-jobs-editor-actions">
      <button type="button" class="primary" id="qp-jobs-editor-save">${slug ? 'Save' : 'Create'}</button>
      <button type="button" id="qp-jobs-editor-cancel">Cancel</button>
    </div>`;

  // Seed the Job Type dropdown with every value already used across the library.
  const jobTypes = [...new Set(_jobs.map(j => j.job_type).filter(Boolean))];
  renderJobForm(_el('qp-jobs-form-host'), content, { jobTypes });
  _el('qp-jobs-editor-cancel').addEventListener('click', () => { _closeEditor(); _renderList(); });
  _el('qp-jobs-editor-save').addEventListener('click', () => _save(slug));
}

function _setStatus(msg, isError) {
  const s = _el('qp-jobs-editor-status');
  if (!s) return;
  s.textContent = msg;
  s.classList.toggle('error', !!isError);
}

async function _save(slug) {
  let content;
  try { content = collectJobForm(_el('qp-jobs-form-host'), _editOriginal); }
  catch (e) { _setStatus(e.message, true); return; }

  const btn = _el('qp-jobs-editor-save');
  btn.disabled = true;
  _setStatus('Saving…');
  try {
    let r;
    if (slug) {
      r = await fetch(`${API}/${encodeURIComponent(slug)}`, {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
    } else {
      const wantSlug = _el('qp-jobs-editor-slug')?.value.trim() || '';
      r = await fetch(API, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, slug: wantSlug }),
      });
    }
    if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail || `HTTP ${r.status}`);
  } catch (e) {
    _setStatus(`Save failed — ${e.message}`, true);
    btn.disabled = false;
    return;
  }
  _closeEditor();
  await loadJobsTab();
}

function _closeEditor() {
  _editOriginal = null;
  const editor = _el('qp-jobs-tab-editor');
  if (editor) { editor.classList.add('hidden'); editor.innerHTML = ''; }
  _el('qp-jobs-tab-list')?.classList.remove('hidden');
  document.querySelector('.qp-jobs-tab-toolbar')?.classList.remove('hidden');
}

// Deep-link entry point (QP session "Jobs" button): open the brain modal on this tab.
export function openJobsTab() {
  const modal = document.getElementById('memory-modal');
  if (modal?.classList.contains('hidden')) {
    // go through the sidebar button so modalManager bookkeeping stays correct
    document.getElementById('tool-memory-btn')?.click();
  }
  document.querySelector('.memory-tab[data-memory-tab="jobs"]')?.click();
}
