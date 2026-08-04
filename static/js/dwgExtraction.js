// static/js/dwgExtraction.js

/**
 * DWG extraction entry point (dwg_to_qty_sheet ledger, DQ-2).
 *
 * A small sidebar-launched drag-and-drop modal — deliberately NOT a
 * QP-style docked panel. Files dropped here are attached through the
 * normal chat attach mechanism; the send carries a `dwg_extraction=true`
 * flag so the backend treats extraction as the assumed intent (forced
 * mode). Dropping a .dwg straight into normal chat still works, but the
 * model confirms intent first instead of assuming (soft mode) — the
 * backend decides all of that; this modal only supplies files + the flag.
 */

import fileHandlerModule from './fileHandler.js';
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

let _modal = null;
let _pendingDwgFiles = [];
let _dragWired = false;

function _isDwg(f) {
  return /\.dwg$/i.test(f && f.name ? f.name : '');
}

function _injectStyles() {
  if (document.getElementById('dwg-extract-styles')) return;
  const s = document.createElement('style');
  s.id = 'dwg-extract-styles';
  s.textContent = `
    .dwg-drop-zone {
      border: 2px dashed var(--border);
      border-radius: 8px;
      padding: 28px 16px;
      text-align: center;
      cursor: pointer;
      transition: border-color .15s, background .15s;
      user-select: none;
    }
    .dwg-drop-zone:hover,
    .dwg-drop-zone.dragover {
      border-color: var(--red);
      background: color-mix(in srgb, var(--red) 7%, transparent);
    }
    .dwg-drop-zone .dwg-drop-sub { opacity: .6; font-size: 12px; margin-top: 6px; }
    .dwg-file-list { margin: 10px 0 0; padding: 0; list-style: none; font-size: 13px; }
    .dwg-file-list li { display: flex; align-items: center; gap: 6px; padding: 3px 0; }
    .dwg-file-list .dwg-file-remove { cursor: pointer; opacity: .55; background: none; border: none; color: inherit; font: inherit; }
    .dwg-file-list .dwg-file-remove:hover { opacity: 1; }
    .dwg-extract-note { width: 100%; margin-top: 12px; resize: vertical; min-height: 54px; }
    .dwg-extract-holdout { width: 100%; margin-top: 8px; height: 30px; }
    .dwg-extract-actions {
      display: flex; justify-content: flex-end; gap: 8px;
      margin-top: 10px; padding-top: 10px;
      border-top: 1px solid var(--border);
      flex-shrink: 0;
    }
  `;
  document.head.appendChild(s);
}

function _renderFileList() {
  const list = _modal.querySelector('.dwg-file-list');
  list.innerHTML = '';
  _pendingDwgFiles.forEach((f, i) => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'grow';
    name.textContent = `${f.name} (${(f.size / (1024 * 1024)).toFixed(1)} MB)`;
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'dwg-file-remove';
    rm.setAttribute('aria-label', `Remove ${f.name}`);
    rm.textContent = '✖';
    rm.addEventListener('click', () => {
      _pendingDwgFiles.splice(i, 1);
      _renderFileList();
    });
    li.appendChild(name);
    li.appendChild(rm);
    list.appendChild(li);
  });
  const startBtn = _modal.querySelector('#dwg-extract-start');
  startBtn.disabled = _pendingDwgFiles.length === 0;
}

function _addFiles(files) {
  const dwgs = Array.from(files || []).filter(_isDwg);
  const skipped = (files ? files.length : 0) - dwgs.length;
  if (skipped > 0 && uiModule && uiModule.showToast) {
    uiModule.showToast(`Skipped ${skipped} non-DWG file(s)`);
  }
  _pendingDwgFiles.push(...dwgs);
  _renderFileList();
}

function _buildModal() {
  _injectStyles();
  const modal = document.createElement('div');
  modal.id = 'dwg-extract-modal';
  modal.className = 'modal hidden';
  modal.innerHTML = `
    <div class="modal-content" role="dialog" aria-label="DWG extraction" style="background:var(--bg);max-width:480px">
      <div class="modal-header">
        <h4>DWG → Quantity Sheet</h4>
        <button class="close-btn" id="dwg-extract-close" aria-label="Close">✖</button>
      </div>
      <div class="modal-body">
        <div class="dwg-drop-zone" id="dwg-drop-zone" tabindex="0" role="button" aria-label="Drop DWG files or click to browse">
          <div>Drop .dwg file(s) here, or click to browse</div>
          <div class="dwg-drop-sub">Each file is converted and censused automatically; the extraction agent starts from that.</div>
        </div>
        <input type="file" id="dwg-extract-input" accept=".dwg" multiple style="display:none">
        <ul class="dwg-file-list"></ul>
        <textarea class="dwg-extract-note memory-add-input" id="dwg-extract-note" placeholder="Optional notes for the agent (known quirks, what to focus on...)"></textarea>
        <input type="text" class="dwg-extract-holdout memory-add-input" id="dwg-extract-holdout" placeholder="Testing an existing corpus job? Name it to hold out (e.g. WOODMAN)">
      </div>
      <div class="modal-footer dwg-extract-actions">
        <button type="button" class="confirm-btn confirm-btn-secondary" id="dwg-extract-cancel">Cancel</button>
        <button type="button" class="confirm-btn confirm-btn-primary" id="dwg-extract-start" disabled>Start extraction</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const zone = modal.querySelector('#dwg-drop-zone');
  const input = modal.querySelector('#dwg-extract-input');
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', () => { _addFiles(input.files); input.value = ''; });
  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
  }));
  zone.addEventListener('drop', (e) => _addFiles(e.dataTransfer && e.dataTransfer.files));

  modal.querySelector('#dwg-extract-close').addEventListener('click', close);
  modal.querySelector('#dwg-extract-cancel').addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  modal.querySelector('#dwg-extract-start').addEventListener('click', _start);
  _wireDrag(modal);
  return modal;
}

function _wireDrag(modal) {
  if (_dragWired) return;
  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  if (!content || !header) return;
  _dragWired = true;
  makeWindowDraggable(modal, {
    content,
    header,
    skipSelector: 'button, input, select, textarea',
    enableDock: true,
    enableLeftDock: true,
  });
}

async function _start() {
  if (!_pendingDwgFiles.length) return;
  try {
    await fileHandlerModule.addFiles(_pendingDwgFiles);
  } catch (e) {
    console.error('DWG extraction: attach failed', e);
    if (uiModule && uiModule.showToast) uiModule.showToast('Could not attach files');
    return;
  }
  const note = (_modal.querySelector('#dwg-extract-note').value || '').trim();
  const holdout = (_modal.querySelector('#dwg-extract-holdout').value || '').trim();
  const msgInput = uiModule.el('message');
  if (msgInput) {
    msgInput.value = note
      ? `Extract a quantity takeoff sheet from the attached DWG file(s). Notes: ${note}`
      : 'Extract a quantity takeoff sheet from the attached DWG file(s).';
  }
  // One-shot flags consumed by chat.js's send path → `dwg_extraction=true` /
  // `dwg_holdout_job=<name>` form fields → backend forced-extraction mode
  // and corpus self-exclusion, respectively.
  window.__dwgExtractionPending = true;
  if (holdout) window.__dwgHoldoutJob = holdout;
  close();
  const form = document.getElementById('chat-form');
  if (form) {
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
  }
}

export function open() {
  if (!_modal) _modal = _buildModal();
  _pendingDwgFiles = [];
  _renderFileList();
  _modal.querySelector('#dwg-extract-note').value = '';
  _modal.querySelector('#dwg-extract-holdout').value = '';
  _modal.classList.remove('hidden');
}

export function close() {
  if (!_modal || _modal.classList.contains('hidden')) return;
  const content = _modal.querySelector('.modal-content');
  if (content && !content.classList.contains('modal-closing')) {
    content.classList.add('modal-closing');
    const finish = () => {
      _modal.classList.add('hidden');
      content.classList.remove('modal-closing');
    };
    content.addEventListener('animationend', finish, { once: true });
    setTimeout(finish, 200);
  } else {
    _modal.classList.add('hidden');
  }
}

export function init() {
  const btn = document.getElementById('dwg-extract-btn');
  if (btn) btn.addEventListener('click', open);
}

export default { init, open, close };
