import { startStream } from './stream.js';
import markdownModule from '../markdown.js';

const IMPORTANCE_COLOR = { high: '#22c55e', medium: '#f59e0b', low: '#6b7280' };
const SHEET_TYPES = ['cover','typical_section','plan_view','profile','utility_plan','plat','grading','detail','spec','erosion','other'];
const pageClassifications = {}; // page_idx → {sheet_type, importance, description, regions}

const STYLES = `
.qp-overlay {
    width: 100%; height: 100%;
    background: var(--bg);
    display: flex; flex-direction: column;
    font-family: inherit; color: var(--fg);
    overflow: hidden;
}
.qp-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}
.qp-title { font-size: 14px; font-weight: 600; letter-spacing: 0.3px; }
.qp-run-meta {
    flex: 1; display: flex; align-items: center; gap: 8px;
    font-size: 11px; color: color-mix(in srgb, var(--fg) 55%, transparent);
    overflow: hidden;
}
.qp-run-meta-item {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    background: color-mix(in srgb, var(--fg) 7%, transparent);
    border-radius: 4px; padding: 2px 7px;
}
.qp-close-btn {
    background: none; border: none; cursor: pointer;
    color: color-mix(in srgb, var(--fg) 50%, transparent);
    font-size: 16px; padding: 2px 6px; border-radius: 4px;
}
.qp-close-btn:hover {
    background: color-mix(in srgb, var(--fg) 8%, transparent);
    color: var(--fg);
}
.qp-cancel-btn {
    background: none; border: 1px solid color-mix(in srgb, #ef4444 60%, transparent);
    color: #ef4444; cursor: pointer; font-size: 12px; font-weight: 500;
    padding: 3px 10px; border-radius: 4px; transition: background 0.15s;
}
.qp-cancel-btn:hover { background: color-mix(in srgb, #ef4444 12%, transparent); }
.qp-bbox-svg {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    pointer-events: none; overflow: visible;
}
.qp-phase1-index {
    flex: 1; min-height: 0; overflow-y: auto; padding: 12px 16px;
    display: flex; flex-direction: column; gap: 10px;
}
.qp-p1-page {
    border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
}
.qp-p1-page-header {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 10px; font-size: 12px;
    background: color-mix(in srgb, var(--fg) 4%, var(--bg));
    cursor: pointer;
}
.qp-p1-page-header:hover { background: color-mix(in srgb, var(--fg) 7%, var(--bg)); }
.qp-p1-imp {
    font-size: 10px; font-weight: 600; padding: 1px 5px;
    border-radius: 3px; color: #fff;
}
.qp-p1-type { font-weight: 600; }
.qp-p1-desc { color: color-mix(in srgb, var(--fg) 55%, transparent); flex: 1; }
.qp-p1-regions {
    padding: 4px 10px 6px 10px;
    display: flex; flex-direction: column; gap: 2px;
    max-height: 110px; overflow-y: auto;
}
.qp-p1-region {
    font-size: 11px; font-family: monospace;
    color: color-mix(in srgb, var(--fg) 65%, transparent);
    padding: 1px 0;
}
.qp-p1-region-id { color: var(--accent, #0af); margin-right: 6px; }
.qp-p1-region-coords { color: color-mix(in srgb, var(--fg) 38%, transparent); margin-left: 5px; font-size: 10px; }
.qp-body { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.qp-status {
    display: flex; align-items: center; gap: 7px;
    padding: 8px 16px; font-size: 12px;
    color: color-mix(in srgb, var(--fg) 55%, transparent);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}
.qp-form-area {
    display: flex; flex-direction: column; align-items: center;
    flex: 1; min-height: 0; overflow-y: auto; gap: 16px; padding: 32px;
}
.qp-upload-zone {
    width: 100%; max-width: 480px;
    border: 2px dashed var(--border);
    border-radius: 10px; padding: 32px 24px;
    text-align: center; cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
}
.qp-upload-zone:hover, .qp-upload-zone.drag-over {
    border-color: var(--accent, #0af);
    background: color-mix(in srgb, var(--accent, #0af) 8%, transparent);
}
.qp-upload-zone svg { display: block; margin: 0 auto 10px; opacity: 0.5; }
.qp-upload-zone .qp-upload-hint {
    font-size: 13px;
    color: color-mix(in srgb, var(--fg) 55%, transparent);
}
.qp-file-chosen { font-size: 13px; color: var(--accent, #0af); margin-top: 6px; font-weight: 500; }
.qp-run-name-input {
    width: 100%; max-width: 480px; box-sizing: border-box;
    background: color-mix(in srgb, var(--fg) 5%, var(--bg));
    border: 1px solid var(--border);
    border-radius: 6px; color: var(--fg); padding: 8px 10px;
    font-size: 13px; font-family: inherit;
}
.qp-run-name-input:focus { outline: none; border-color: var(--accent, #0af); }
.qp-notes {
    width: 100%; max-width: 480px; box-sizing: border-box;
    background: color-mix(in srgb, var(--fg) 5%, var(--bg));
    border: 1px solid var(--border);
    border-radius: 6px; color: var(--fg); padding: 8px 10px;
    font-size: 13px; resize: vertical; min-height: 64px; font-family: inherit;
}
.qp-notes:focus { outline: none; border-color: var(--accent, #0af); }
.qp-run-btn {
    padding: 9px 28px; background: var(--accent, #0af);
    color: #fff; border: none; border-radius: 6px;
    font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity 0.15s;
}
.qp-run-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.qp-run-btn:not(:disabled):hover { opacity: 0.85; }
.qp-resume-btn {
    background: color-mix(in srgb, var(--accent, #0af) 20%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent, #0af) 55%, transparent);
    color: var(--fg);
}
.qp-resume-btn:not(:disabled):hover {
    background: color-mix(in srgb, var(--accent, #0af) 32%, transparent);
    opacity: 1;
}
.qp-completeness-btn {
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--accent, #0af) 45%, transparent);
    color: color-mix(in srgb, var(--accent, #0af) 90%, var(--fg));
}
.qp-completeness-btn:not(:disabled):hover {
    background: color-mix(in srgb, var(--accent, #0af) 15%, transparent);
    opacity: 1;
}
.qp-phase5-bar {
    display: flex; flex-direction: column; gap: 10px;
    padding: 12px 16px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.qp-phase5-row {
    display: flex; align-items: center; gap: 10px;
}
.qp-phase5-label {
    font-size: 12px; color: color-mix(in srgb, var(--fg) 65%, transparent);
    white-space: nowrap; min-width: 120px;
}
.qp-phase5-btns { display: flex; gap: 8px; }
.qp-content { display: flex; flex: 1; min-height: 0; }
.qp-sidebar {
    width: 270px; flex-shrink: 0;
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden;
}
.qp-sidebar-header {
    padding: 8px 12px; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
    color: color-mix(in srgb, var(--fg) 50%, transparent);
    border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.qp-sidebar-pages {
    flex: 1; overflow-y: auto; padding: 8px;
    display: flex; flex-direction: column; gap: 6px;
}
.qp-page-thumb {
    position: relative; width: 100%; cursor: pointer;
    border-radius: 4px; overflow: hidden;
    border: 2px solid var(--border);
    transition: border-color 0.15s;
    flex-shrink: 0;
}
.qp-page-thumb:hover { border-color: var(--accent, #0af); }
.qp-page-thumb.active { border-color: var(--accent, #0af); }
.qp-thumb-img-wrap { position: relative; overflow: hidden; }
.qp-thumb-img-wrap img { width: 100%; display: block; }
.qp-page-label {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(0,0,0,0.6); color: #fff;
    font-size: 10px; padding: 2px 4px; text-align: center;
}
.qp-classification-badge {
    position: absolute; top: 3px; right: 3px;
    font-size: 9px; font-weight: 600; padding: 1px 4px;
    border-radius: 3px; color: #fff; opacity: 0.92;
    max-width: 90%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.qp-thumb-info {
    padding: 4px 8px 6px; display: flex; flex-direction: column; gap: 3px;
    border-top: 1px solid var(--border);
}
.qp-thumb-desc {
    font-size: 10px; line-height: 1.3;
    color: color-mix(in srgb, var(--fg) 65%, transparent);
}
.qp-thumb-regions {
    display: flex; flex-direction: column; gap: 1px;
    max-height: 90px; overflow-y: auto; flex-shrink: 0;
}
.qp-thumb-region-row {
    display: flex; align-items: flex-start; gap: 4px;
    font-size: 10px; font-family: monospace;
    color: color-mix(in srgb, var(--fg) 50%, transparent);
    flex-shrink: 0; cursor: pointer;
    flex-wrap: wrap;
}
.qp-thumb-region-row:hover { color: color-mix(in srgb, var(--fg) 75%, transparent); }
.qp-thumb-region-coords {
    color: color-mix(in srgb, var(--fg) 38%, transparent);
    font-size: 9px; width: 100%; padding-left: 10px;
}
.qp-thumb-region-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.qp-thumb-region-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qp-main-output {
    flex: 1; min-height: 0; overflow: hidden; padding: 0;
    display: flex; flex-direction: column;
}
.qp-main-placeholder {
    flex: 1; display: flex; align-items: center; justify-content: center;
    gap: 8px;
    color: color-mix(in srgb, var(--fg) 40%, transparent); font-size: 13px;
}
@keyframes qp-spin { to { transform: rotate(360deg); } }
.qp-spinner {
    width: 14px; height: 14px; flex-shrink: 0;
    border: 2px solid color-mix(in srgb, var(--fg) 20%, transparent);
    border-top-color: color-mix(in srgb, var(--fg) 55%, transparent);
    border-radius: 50%;
    animation: qp-spin 0.7s linear infinite;
    display: inline-block;
}
.qp-preview-area {
    flex: 1; min-height: 0; position: relative;
    display: flex; flex-direction: column;
    overflow: hidden;
}
.qp-preview-close {
    position: absolute; top: 8px; right: 10px; z-index: 1;
    background: rgba(0,0,0,0.45); color: #fff; border: none;
    border-radius: 4px; padding: 4px 10px; font-size: 12px;
    cursor: pointer; transition: background 0.15s;
}
.qp-preview-close:hover { background: rgba(0,0,0,0.72); }
.qp-preview-redo {
    position: absolute; top: 8px; right: 110px; z-index: 1;
    background: rgba(0,0,0,0.45); color: #fff; border: none;
    border-radius: 4px; padding: 4px 10px; font-size: 12px;
    cursor: pointer; transition: background 0.15s;
}
.qp-preview-redo:hover { background: rgba(0,80,200,0.72); }
.qp-preview-redo:disabled { opacity: 0.5; cursor: default; }
.qp-preview-img {
    flex: 1; min-height: 0;
    width: 100%; object-fit: contain; display: block;
    transform-origin: center center;
    user-select: none;
    will-change: transform;
}
.qp-jobs-grid {
    display: flex; flex-wrap: wrap; gap: 8px;
}
.qp-job-chip {
    display: flex; align-items: center; gap: 5px;
    padding: 4px 10px; border-radius: 20px;
    border: 1px solid var(--border);
    font-size: 12px; cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
    user-select: none;
}
.qp-job-chip input[type=checkbox] { accent-color: var(--accent, #0af); margin: 0; }
.qp-job-chip:hover { border-color: var(--accent, #0af); }
.qp-model-row {
    width: 100%; max-width: 480px;
    display: flex; align-items: center; gap: 10px;
}
.qp-model-label {
    font-size: 13px; white-space: nowrap; flex-shrink: 0;
    width: 130px;
    color: color-mix(in srgb, var(--fg) 65%, transparent);
}
.qp-model-select {
    flex: 1; min-width: 0; width: 100%;
    background: color-mix(in srgb, var(--fg) 5%, var(--bg));
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--fg); padding: 6px 8px; font-size: 13px;
    font-family: inherit; cursor: pointer;
}
.qp-model-select:focus { outline: none; border-color: var(--accent, #0af); }
.qp-jobs-section {
    width: 100%; max-width: 480px;
    display: flex; flex-direction: column; gap: 6px;
}
.qp-jobs-section .qp-model-label { width: auto; }
.qp-retry-row {
    width: 100%; max-width: 480px;
    display: flex; align-items: center; gap: 10px;
}
.qp-retry-input {
    width: 72px; flex-shrink: 0;
    background: color-mix(in srgb, var(--fg) 5%, var(--bg));
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--fg); padding: 6px 8px; font-size: 13px;
    font-family: inherit;
}
.qp-retry-input:focus { outline: none; border-color: var(--accent, #0af); }
.qp-holdout-input {
    background: color-mix(in srgb, var(--fg) 5%, var(--bg));
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--fg); padding: 6px 8px; font-size: 12px;
    font-family: monospace; min-width: 0;
}
.qp-holdout-input:focus { outline: none; border-color: var(--accent, #0af); }
.qp-fallback-section {
    width: 100%; max-width: 480px;
    display: flex; flex-direction: column; gap: 6px;
}
.qp-fallback-row { display: flex; gap: 6px; align-items: center; }
.qp-fallback-add-btn {
    flex-shrink: 0; background: none;
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--fg); cursor: pointer;
    font-size: 17px; padding: 3px 10px; line-height: 1;
    transition: background 0.15s;
}
.qp-fallback-add-btn:hover { background: color-mix(in srgb, var(--fg) 8%, transparent); }
.qp-fallback-list { display: flex; flex-direction: column; gap: 4px; }
.qp-fallback-chip {
    display: flex; align-items: center; justify-content: space-between;
    background: color-mix(in srgb, var(--fg) 5%, var(--bg));
    border: 1px solid var(--border); border-radius: 5px;
    padding: 4px 8px; font-size: 12px;
}
.qp-fallback-remove {
    background: none; border: none; cursor: pointer;
    color: color-mix(in srgb, var(--fg) 35%, transparent);
    font-size: 13px; padding: 0 2px; line-height: 1; transition: color 0.1s;
}
.qp-fallback-remove:hover { color: #ef4444; }
.qp-auto-mode-row {
    width: 100%; max-width: 480px;
    display: flex; align-items: center;
}
.qp-auto-mode-label {
    display: flex; align-items: center; gap: 7px;
    font-size: 12px; color: color-mix(in srgb, var(--fg) 70%, transparent);
    cursor: pointer; user-select: none;
}
.qp-auto-mode-label input[type=checkbox] { cursor: pointer; accent-color: var(--accent, #0af); }
.qp-header-auto-mode { margin-right: 8px; }
.qp-run-delete-btn {
    background: none; border: 1px solid transparent; border-radius: 5px;
    padding: 4px 8px; cursor: pointer; font-size: 12px;
    color: color-mix(in srgb, #ef4444 70%, transparent);
}
.qp-run-delete-btn:hover { border-color: #ef4444; color: #ef4444; background: color-mix(in srgb, #ef4444 8%, transparent); }
.qp-gate-btn {
    display: block; margin: 8px auto 0;
    padding: 7px 18px; border-radius: 7px;
    background: var(--accent, #0af); color: #fff;
    border: none; cursor: pointer; font-size: 13px; font-weight: 600;
    transition: opacity 0.15s;
}
.qp-gate-btn:hover { opacity: 0.85; }
.qp-gate-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.qp-phase-bar {
    display: flex; align-items: center; gap: 6px;
    padding: 7px 16px; border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}
.qp-phase-step {
    background: none; border: 1px solid transparent;
    border-radius: 20px; padding: 3px 14px;
    font-size: 12px; font-weight: 500; cursor: default;
    color: color-mix(in srgb, var(--fg) 30%, transparent);
    transition: all 0.15s; white-space: nowrap;
}
.qp-phase-step.done {
    cursor: pointer;
    color: color-mix(in srgb, var(--fg) 60%, transparent);
    border-color: color-mix(in srgb, var(--fg) 18%, transparent);
}
.qp-phase-step.done:hover {
    border-color: var(--accent, #0af);
    color: var(--fg);
}
.qp-phase-step.active {
    border-color: var(--accent, #0af);
    color: var(--fg);
    background: color-mix(in srgb, var(--accent, #0af) 10%, transparent);
}
.qp-phase-step.running {
    cursor: pointer;
    border-color: var(--accent, #0af);
    color: var(--fg);
}
.qp-phase-sep { font-size: 12px; opacity: 0.3; flex-shrink: 0; }
.qp-run-controls {
    padding: 10px 16px; border-bottom: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 8px; flex-shrink: 0;
}
.qp-run-controls-toggle {
    background: none; border: none; cursor: pointer; padding: 0;
    font-size: 11px; color: color-mix(in srgb, var(--fg) 45%, transparent);
    text-align: left; display: flex; align-items: center; gap: 4px;
}
.qp-run-controls-toggle:hover { color: var(--fg); }
.qp-run-controls-settings { display: none; flex-direction: column; gap: 8px; }
.qp-run-controls-settings.open { display: flex; }
.qp-advanced-models {
    display: flex; flex-direction: column; gap: 8px;
    padding-left: 10px; margin-left: 2px;
    border-left: 2px solid var(--border);
}
`;

function injectStyles() {
    if (document.getElementById('qp-styles')) return;
    const style = document.createElement('style');
    style.id = 'qp-styles';
    style.textContent = STYLES;
    document.head.appendChild(style);
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/css/quick_proposal.css';
    document.head.appendChild(link);
}

export function buildPanel({ onClose, prefillUploadId = '', prefillFilename = '' }) {
    injectStyles();

    const overlay = document.createElement('div');
    overlay.className = 'qp-overlay';
    overlay.id = 'qp-overlay';

    overlay.innerHTML = `
        <div class="qp-header">
            <span class="qp-title">Quick Proposal</span>
            <div class="qp-run-meta" id="qp-run-meta"></div>
            <button class="qp-jobs-btn qp-header-jobs-btn" title="Manage the case library of reference jobs" type="button">Jobs</button>
            <label class="qp-auto-mode-label qp-header-auto-mode" id="qp-header-auto-mode" style="display:none">
                <input type="checkbox" id="qp-auto-mode-header" checked> Auto
            </label>
            <button class="qp-cancel-btn" id="qp-cancel-btn" style="display:none">Cancel</button>
            <button class="qp-close-btn" id="qp-close-btn" title="Close">✕</button>
        </div>
        <div class="qp-body" id="qp-body">
            <div class="qp-form-area" id="qp-form-area">
                <div class="qp-upload-zone" id="qp-upload-zone" tabindex="0" role="button" aria-label="Upload plan PDF">
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                        <polyline points="17 8 12 3 7 8"/>
                        <line x1="12" y1="3" x2="12" y2="15"/>
                    </svg>
                    <div class="qp-upload-hint">Click or drag a plan PDF</div>
                    <div class="qp-file-chosen" id="qp-file-chosen" style="display:none"></div>
                </div>
                <input type="file" id="qp-file-input" accept=".pdf,.jpg,.jpeg,.png" style="display:none">
                <input type="text" class="qp-run-name-input" id="qp-run-name" placeholder="Run name (optional)…">
                <textarea class="qp-notes" id="qp-notes" placeholder="Optional notes for this job…"></textarea>
                <div class="qp-model-row">
                    <label class="qp-model-label" for="qp-model-select">Classifying Model</label>
                    <select class="qp-model-select" id="qp-model-select">
                        <option value="">Loading models…</option>
                    </select>
                </div>
                <div class="qp-model-row">
                    <label class="qp-model-label" for="qp-manager-model-select">Manager Model</label>
                    <select class="qp-model-select" id="qp-manager-model-select">
                        <option value="">Loading models…</option>
                    </select>
                </div>
                <div class="qp-auto-mode-row">
                    <label class="qp-auto-mode-label">
                        <input type="checkbox" id="qp-advanced-mode">
                        Advanced: per-phase models
                        <span class="qp-import-hint">Override the model used for individual phases instead of one model for everything</span>
                    </label>
                </div>
                <div class="qp-advanced-models" id="qp-advanced-models" style="display:none">
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-phase1">Job Type Detection</label>
                        <select class="qp-model-select" id="qp-pm-phase1"><option value="">Use Classifying Model</option></select>
                    </div>
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-phase2">Page Classification <span style="opacity:0.5;font-weight:400;font-size:11px">(Gemini-family only)</span></label>
                        <select class="qp-model-select" id="qp-pm-phase2"><option value="">Use Classifying Model</option></select>
                    </div>
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-phase3">Completeness Scoring</label>
                        <select class="qp-model-select" id="qp-pm-phase3"><option value="">Use Classifying Model</option></select>
                    </div>
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-notes">Notes Transcription</label>
                        <select class="qp-model-select" id="qp-pm-notes"><option value="">Use Classifying Model</option></select>
                    </div>
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-scope">Scope Analysis</label>
                        <select class="qp-model-select" id="qp-pm-scope"><option value="">Use Classifying Model</option></select>
                    </div>
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-phase5_gemini">Extraction — Vision Model</label>
                        <select class="qp-model-select" id="qp-pm-phase5_gemini"><option value="">Use Classifying Model</option></select>
                    </div>
                    <div class="qp-model-row">
                        <label class="qp-model-label" for="qp-pm-phase5_manager">Extraction — Manager Model</label>
                        <select class="qp-model-select" id="qp-pm-phase5_manager"><option value="">Use Manager Model</option></select>
                    </div>
                </div>
                <div class="qp-retry-row">
                    <label class="qp-model-label" for="qp-retry-attempts">Gemini Retries</label>
                    <input type="number" class="qp-retry-input" id="qp-retry-attempts" value="3" min="1" max="10">
                </div>
                <div class="qp-retry-row">
                    <label class="qp-model-label" for="qp-memory-recall-count">Memories Recalled</label>
                    <input type="number" class="qp-retry-input" id="qp-memory-recall-count" value="12" min="0" max="50">
                </div>
                <div class="qp-fallback-section">
                    <div class="qp-model-label">Gemini Fallbacks <span style="opacity:0.5;font-weight:400;font-size:11px">(tried in order)</span></div>
                    <div class="qp-fallback-row">
                        <select class="qp-model-select" id="qp-fallback-select">
                            <option value="">Loading models…</option>
                        </select>
                        <button class="qp-fallback-add-btn" id="qp-fallback-add-btn" type="button" title="Add fallback model">+</button>
                    </div>
                    <div class="qp-fallback-list" id="qp-fallback-list"></div>
                </div>
                <div class="qp-jobs-section" id="qp-jobs-section">
                    <div class="qp-model-label">Reference Jobs <span id="qp-jobs-count" class="qp-jobs-count"></span></div>
                    <div class="qp-jobs-toolbar">
                        <input type="text" id="qp-jobs-search" class="qp-jobs-search" placeholder="Search jobs…" autocomplete="off">
                        <button type="button" class="qp-jobs-bulk-btn" id="qp-jobs-all">All</button>
                        <button type="button" class="qp-jobs-bulk-btn" id="qp-jobs-none">None</button>
                    </div>
                    <div class="qp-jobs-list" id="qp-jobs-grid"><span style="opacity:0.5;font-size:11px">Loading…</span></div>
                    <div class="qp-holdout-notice" id="qp-holdout-notice" style="display:none"></div>
                </div>
                <div class="qp-model-row">
                    <label class="qp-model-label" for="qp-project-type-select">Project Type</label>
                    <select class="qp-model-select" id="qp-project-type-select">
                        <option value="">Auto-detect (Gemini)</option>
                        <option value="residential_subdivision">Residential Subdivision</option>
                        <option value="commercial_development">Commercial Development</option>
                        <option value="rural_access">Rural / Ranch Access</option>
                        <option value="mixed">Mixed</option>
                    </select>
                </div>
                <div class="qp-auto-mode-row">
                    <label class="qp-auto-mode-label">
                        <input type="checkbox" id="qp-auto-mode" checked>
                        Auto-advance phases
                    </label>
                </div>
                <div class="qp-auto-mode-row">
                    <label class="qp-auto-mode-label">
                        <input type="checkbox" id="qp-auto-memory">
                        Save memory snapshot
                        <span class="qp-import-hint">Write a provisional memory of this proposal (job, total, key items) when the run completes</span>
                    </label>
                </div>
                <div class="qp-import-section" id="qp-import-section">
                    <label class="qp-import-toggle">
                        <input type="checkbox" id="qp-import-toggle"> Import Classifications
                        <span class="qp-import-hint">Skip Phase 1 — reuse bboxes from a previous run</span>
                    </label>
                    <div class="qp-import-picker" id="qp-import-picker" style="display:none">
                        <select class="qp-model-select" id="qp-import-run-select">
                            <option value="">Loading runs…</option>
                        </select>
                        <div class="qp-import-run-label" id="qp-import-run-label"></div>
                    </div>
                </div>
                <div class="qp-import-section" id="qp-import-notes-section">
                    <label class="qp-import-toggle">
                        <input type="checkbox" id="qp-import-notes-toggle"> Import Notes
                        <span class="qp-import-hint">Skip notes extraction — reuse transcribed notes from a previous run</span>
                    </label>
                    <div class="qp-import-picker" id="qp-import-notes-picker" style="display:none">
                        <select class="qp-model-select" id="qp-import-notes-run-select">
                            <option value="">Loading runs…</option>
                        </select>
                        <div class="qp-import-run-label" id="qp-import-notes-run-label"></div>
                    </div>
                </div>
                <div class="qp-import-section" id="qp-import-scope-section">
                    <label class="qp-import-toggle">
                        <input type="checkbox" id="qp-import-scope-toggle"> Import Scope Analysis
                        <span class="qp-import-hint">Skip scope analysis — reuse a previous run's scope determination</span>
                    </label>
                    <div class="qp-import-picker" id="qp-import-scope-picker" style="display:none">
                        <select class="qp-model-select" id="qp-import-scope-run-select">
                            <option value="">Loading runs…</option>
                        </select>
                        <div class="qp-import-run-label" id="qp-import-scope-run-label"></div>
                    </div>
                </div>
                <button class="qp-run-btn" id="qp-run-btn" disabled>Start</button>
            </div>
            <div class="qp-status" id="qp-status" style="display:none"></div>
            <div class="qp-phase-bar" id="qp-phase-bar" style="display:none">
                <button class="qp-phase-step" data-phase="classification">Classification</button>
                <span class="qp-phase-sep">→</span>
                <button class="qp-phase-step" data-phase="completeness">Completeness</button>
                <span class="qp-phase-sep">→</span>
                <button class="qp-phase-step" data-phase="extraction">Extraction</button>
            </div>
            <div id="qp-content" style="display:none;flex:1;min-height:0;flex-direction:column;">
                <div class="qp-phase-pane" data-phase="classification" style="display:flex;flex:1;min-height:0;flex-direction:column;">
                    <div id="qp-index-main-output" style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">
                        <div class="qp-preview-area" style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">
                            <div style="flex:1;min-height:0;overflow-y:auto;">
                                <div class="qp-sidebar-pages" id="qp-sidebar-pages" style="padding:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;align-content:start;"></div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="qp-phase-pane" data-phase="extraction" style="display:none;flex:1;min-height:0;">
                    <div class="qp-main-output" id="qp-main-output" style="flex:1;min-height:0;overflow:hidden;">
                        <div class="qp-preview-area" id="qp-preview-area">
                            <div class="qp-main-placeholder" id="qp-main-placeholder">
                                <span class="qp-spinner"></span>Rendering pages…
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    overlay.querySelector('#qp-close-btn').addEventListener('click', onClose);

    const fileInput  = overlay.querySelector('#qp-file-input');
    const uploadZone = overlay.querySelector('#qp-upload-zone');
    const fileChosen = overlay.querySelector('#qp-file-chosen');
    const modelSelect       = overlay.querySelector('#qp-model-select');
    const managerSelect     = overlay.querySelector('#qp-manager-model-select');
    const projectTypeSelect = overlay.querySelector('#qp-project-type-select');
    const autoModeToggle    = overlay.querySelector('#qp-auto-mode');
    const runBtn            = overlay.querySelector('#qp-run-btn');
    let chosenFile = null;

    // Persist auto-mode preference in localStorage
    const AUTO_MODE_KEY = 'qp_auto_mode';
    autoModeToggle.checked = localStorage.getItem(AUTO_MODE_KEY) !== 'false';
    autoModeToggle.addEventListener('change', () => {
        localStorage.setItem(AUTO_MODE_KEY, autoModeToggle.checked ? 'true' : 'false');
    });

    // Advanced mode: per-phase model overrides. Off by default — regular mode is
    // unaffected (phase_models stays empty, every phase uses modelSelect/managerSelect).
    const advancedToggle = overlay.querySelector('#qp-advanced-mode');
    const advancedPanel  = overlay.querySelector('#qp-advanced-models');
    const ADVANCED_MODE_KEY = 'qp_advanced_mode';
    const PHASE_MODEL_KEYS = ['phase1', 'phase2', 'phase3', 'notes', 'scope', 'phase5_gemini', 'phase5_manager'];
    const phaseModelSelects = {};
    PHASE_MODEL_KEYS.forEach(key => {
        const sel = overlay.querySelector(`#qp-pm-${key}`);
        phaseModelSelects[key] = sel;
        loadModels(sel, { preferClaude: key === 'phase5_manager', includeBlank: true,
                           blankLabel: key === 'phase5_manager' ? 'Use Manager Model' : 'Use Classifying Model',
                           excludeAnthropic: key === 'phase2' });
    });
    advancedToggle.checked = localStorage.getItem(ADVANCED_MODE_KEY) === 'true';
    advancedPanel.style.display = advancedToggle.checked ? 'flex' : 'none';
    advancedToggle.addEventListener('change', () => {
        localStorage.setItem(ADVANCED_MODE_KEY, advancedToggle.checked ? 'true' : 'false');
        advancedPanel.style.display = advancedToggle.checked ? 'flex' : 'none';
    });

    loadModels(modelSelect, { preferClaude: false });
    loadModels(managerSelect, { preferClaude: true });

    const retryInput     = overlay.querySelector('#qp-retry-attempts');
    const memoryRecallInput = overlay.querySelector('#qp-memory-recall-count');
    const fallbackSelect = overlay.querySelector('#qp-fallback-select');
    const fallbackAddBtn = overlay.querySelector('#qp-fallback-add-btn');
    const fallbackList   = overlay.querySelector('#qp-fallback-list');
    let fallbackModels   = [];

    loadModels(fallbackSelect, { preferClaude: false });

    fallbackAddBtn.addEventListener('click', () => {
        const mid = fallbackSelect.value;
        if (!mid || fallbackModels.includes(mid)) return;
        const label = fallbackSelect.options[fallbackSelect.selectedIndex]?.textContent || mid;
        fallbackModels.push(mid);
        const chip = document.createElement('div');
        chip.className = 'qp-fallback-chip';
        chip.dataset.mid = mid;
        chip.innerHTML = `<span>${_esc(label)}</span><button class="qp-fallback-remove" type="button" title="Remove">✕</button>`;
        chip.querySelector('.qp-fallback-remove').addEventListener('click', () => {
            fallbackModels = fallbackModels.filter(m => m !== mid);
            chip.remove();
        });
        fallbackList.appendChild(chip);
    });

    // Fetch and render the reference jobs list up front so the selection is fixed before the run starts.
    // Unchecking any subset now makes the server derive a run-scoped knowledge pack from the
    // checked jobs (dynamic KP) — the old prebuilt-holdout activation is gone from this form.
    const jobsGrid   = overlay.querySelector('#qp-jobs-grid');
    const jobsSearch = overlay.querySelector('#qp-jobs-search');
    const jobsCount  = overlay.querySelector('#qp-jobs-count');
    fetch('/api/quick_proposal/jobs', { credentials: 'same-origin' })
        .then(r => r.ok ? r.json() : [])
        .then(jobs => {
            jobsGrid.innerHTML = '';
            if (!jobs.length) {
                jobsGrid.innerHTML = '<span style="opacity:0.5;font-size:11px">No reference jobs found</span>';
                return;
            }
            const holdoutNotice = overlay.querySelector('#qp-holdout-notice');
            const updateSelectionNotice = () => {
                const boxes   = [...jobsGrid.querySelectorAll('input[type=checkbox]')];
                const checked = boxes.filter(cb => cb.checked);
                if (jobsCount) jobsCount.textContent = `${checked.length}/${boxes.length}`;
                if (checked.length === boxes.length) {
                    holdoutNotice.style.display = 'none';
                } else if (checked.length === 0) {
                    holdoutNotice.textContent = 'No jobs selected — check at least one reference job';
                    holdoutNotice.style.display = 'block';
                } else {
                    holdoutNotice.textContent = `A knowledge pack will be built from the ${checked.length} selected job${checked.length === 1 ? '' : 's'} for this run`;
                    holdoutNotice.style.display = 'block';
                }
            };
            jobs.forEach(job => {
                const row = document.createElement('label');
                row.className = 'qp-job-row';
                row.dataset.name = (job.name || '').toLowerCase();
                row.innerHTML = `<input type="checkbox" value="${_esc(job.id)}" checked> <span class="qp-job-row-name">${_esc(job.name)}</span>`;
                row.querySelector('input').addEventListener('change', updateSelectionNotice);
                jobsGrid.appendChild(row);
            });
            updateSelectionNotice();
            if (jobsSearch) jobsSearch.addEventListener('input', () => {
                const q = jobsSearch.value.trim().toLowerCase();
                jobsGrid.querySelectorAll('.qp-job-row').forEach(row => {
                    row.style.display = !q || row.dataset.name.includes(q) ? '' : 'none';
                });
            });
            const setAllVisible = (state) => {
                // bulk buttons act on the rows currently visible under the search filter
                jobsGrid.querySelectorAll('.qp-job-row').forEach(row => {
                    if (row.style.display === 'none') return;
                    row.querySelector('input').checked = state;
                });
                updateSelectionNotice();
            };
            overlay.querySelector('#qp-jobs-all')?.addEventListener('click', () => setAllVisible(true));
            overlay.querySelector('#qp-jobs-none')?.addEventListener('click', () => setAllVisible(false));
        })
        .catch(() => { jobsGrid.innerHTML = '<span style="opacity:0.5;font-size:11px">Could not load jobs</span>'; });

    // Import classifications toggle
    const importToggle = overlay.querySelector('#qp-import-toggle');
    const importPicker = overlay.querySelector('#qp-import-picker');
    const importSelect = overlay.querySelector('#qp-import-run-select');
    const importLabel  = overlay.querySelector('#qp-import-run-label');

    importToggle.addEventListener('change', () => {
        importPicker.style.display = importToggle.checked ? 'block' : 'none';
        if (importToggle.checked && importSelect.options.length <= 1) {
            fetch('/api/quick_proposal/runs', { credentials: 'same-origin' })
                .then(r => r.ok ? r.json() : [])
                .then(runs => {
                    importSelect.innerHTML = '<option value="">— select a previous run —</option>';
                    runs.forEach(run => {
                        const opt = document.createElement('option');
                        opt.value = run.id;
                        const ts = run.timestamp ? new Date(run.timestamp * 1000).toLocaleDateString() : '';
                        opt.textContent = `${run.run_name || run.filename || run.id}${ts ? '  (' + ts + ')' : ''}`;
                        importSelect.appendChild(opt);
                    });
                })
                .catch(() => { importSelect.innerHTML = '<option value="">Could not load runs</option>'; });
        }
    });

    importSelect.addEventListener('change', () => {
        const opt = importSelect.options[importSelect.selectedIndex];
        importLabel.textContent = opt.value ? `Will import from: ${opt.textContent.trim()}` : '';
    });

    // Import notes toggle
    const importNotesToggle = overlay.querySelector('#qp-import-notes-toggle');
    const importNotesPicker = overlay.querySelector('#qp-import-notes-picker');
    const importNotesSelect = overlay.querySelector('#qp-import-notes-run-select');
    const importNotesLabel  = overlay.querySelector('#qp-import-notes-run-label');

    importNotesToggle.addEventListener('change', () => {
        importNotesPicker.style.display = importNotesToggle.checked ? 'block' : 'none';
        if (importNotesToggle.checked && importNotesSelect.options.length <= 1) {
            fetch('/api/quick_proposal/runs', { credentials: 'same-origin' })
                .then(r => r.ok ? r.json() : [])
                .then(runs => {
                    importNotesSelect.innerHTML = '<option value="">— select a previous run —</option>';
                    runs.forEach(run => {
                        const opt = document.createElement('option');
                        opt.value = run.id;
                        const ts = run.timestamp ? new Date(run.timestamp * 1000).toLocaleDateString() : '';
                        opt.textContent = `${run.run_name || run.filename || run.id}${ts ? '  (' + ts + ')' : ''}`;
                        importNotesSelect.appendChild(opt);
                    });
                })
                .catch(() => { importNotesSelect.innerHTML = '<option value="">Could not load runs</option>'; });
        }
    });

    importNotesSelect.addEventListener('change', () => {
        const opt = importNotesSelect.options[importNotesSelect.selectedIndex];
        importNotesLabel.textContent = opt.value ? `Will import from: ${opt.textContent.trim()}` : '';
    });

    // Import scope analysis toggle
    const importScopeToggle = overlay.querySelector('#qp-import-scope-toggle');
    const importScopePicker = overlay.querySelector('#qp-import-scope-picker');
    const importScopeSelect = overlay.querySelector('#qp-import-scope-run-select');
    const importScopeLabel  = overlay.querySelector('#qp-import-scope-run-label');

    importScopeToggle.addEventListener('change', () => {
        importScopePicker.style.display = importScopeToggle.checked ? 'block' : 'none';
        if (importScopeToggle.checked && importScopeSelect.options.length <= 1) {
            fetch('/api/quick_proposal/runs', { credentials: 'same-origin' })
                .then(r => r.ok ? r.json() : [])
                .then(runs => {
                    importScopeSelect.innerHTML = '<option value="">— select a previous run —</option>';
                    runs.forEach(run => {
                        const opt = document.createElement('option');
                        opt.value = run.id;
                        const ts = run.timestamp ? new Date(run.timestamp * 1000).toLocaleDateString() : '';
                        opt.textContent = `${run.run_name || run.filename || run.id}${ts ? '  (' + ts + ')' : ''}`;
                        importScopeSelect.appendChild(opt);
                    });
                })
                .catch(() => { importScopeSelect.innerHTML = '<option value="">Could not load runs</option>'; });
        }
    });

    importScopeSelect.addEventListener('change', () => {
        const opt = importScopeSelect.options[importScopeSelect.selectedIndex];
        importScopeLabel.textContent = opt.value ? `Will import from: ${opt.textContent.trim()}` : '';
    });

    // Pre-fill from an existing upload (Proposal Runs → Re-run flow)
    if (prefillUploadId) {
        fileChosen.textContent = `↩ ${prefillFilename || prefillUploadId}`;
        fileChosen.style.display = 'block';
        uploadZone.style.opacity = '0.4';
        uploadZone.style.pointerEvents = 'none';
        runBtn.disabled = false;
    }

    function onFileSelected(file) {
        if (!file) return;
        chosenFile = file;
        fileChosen.textContent = file.name;
        fileChosen.style.display = 'block';
        runBtn.disabled = false;
    }

    uploadZone.addEventListener('click', () => fileInput.click());
    uploadZone.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') fileInput.click(); });
    fileInput.addEventListener('change', () => onFileSelected(fileInput.files[0]));
    uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('drag-over'); });
    uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
    uploadZone.addEventListener('drop', e => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
        onFileSelected(e.dataTransfer.files[0]);
    });

    runBtn.addEventListener('click', () => {
        const phaseModels = {};
        if (advancedToggle.checked) {
            PHASE_MODEL_KEYS.forEach(key => {
                const v = phaseModelSelects[key]?.value || '';
                if (v) phaseModels[key] = v;
            });
        }
        handleRun(overlay, chosenFile, modelSelect.value, managerSelect.value, prefillUploadId, prefillFilename, parseInt(retryInput.value, 10) || 3, [...fallbackModels], importToggle.checked ? importSelect.value : '', projectTypeSelect.value, autoModeToggle.checked, importNotesToggle.checked ? importNotesSelect.value : '', importScopeToggle.checked ? importScopeSelect.value : '', _intOrDefault(memoryRecallInput.value, 12), phaseModels);
    });

    return overlay;
}

export async function loadModels(select, { preferClaude = false, includeBlank = false, blankLabel = '', excludeAnthropic = false } = {}) {
    try {
        // Same fetch pattern as the main model picker: cached (no refresh), with credentials.
        const res = await fetch('/api/models', { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const items = Array.isArray(data) ? data : (data.items ?? []);
        const models = [];
        for (const item of items) {
            // Page classification (phase2) has no Anthropic-native request path (it uses
            // response_format:"json_object", which Claude's API doesn't support) — keep
            // Claude out of that picker's options rather than offer a choice that errors.
            if (excludeAnthropic && (item.url || '').includes('anthropic.com')) continue;
            const displayNames = item.models_display || item.models || [];
            const extraDisplayNames = item.models_extra_display || item.models_extra || [];
            (item.models || []).forEach((mid, i) => {
                models.push({ mid, label: displayNames[i] || mid });
            });
            (item.models_extra || []).forEach((mid, i) => {
                models.push({ mid, label: extraDisplayNames[i] || mid });
            });
        }
        select.innerHTML = '';
        if (!models.length) {
            select.innerHTML = '<option value="">No models found</option>';
            return;
        }
        if (includeBlank) {
            const blankOpt = document.createElement('option');
            blankOpt.value = '';
            blankOpt.textContent = blankLabel || '(default)';
            blankOpt.selected = true;
            select.appendChild(blankOpt);
        }
        const defaultMid = preferClaude
            ? (models.find(m => m.mid === 'claude-sonnet-4-6')?.mid
                || models.find(m => m.mid.toLowerCase().includes('claude'))?.mid
                || models[0].mid)
            : (models.find(m => m.mid.toLowerCase().includes('gemini'))?.mid || models[0].mid);
        models.forEach(({ mid, label }) => {
            const opt = document.createElement('option');
            opt.value = mid;
            opt.textContent = label.split('/').pop();
            opt.selected = !includeBlank && mid === defaultMid;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error('[quick_proposal] loadModels error:', e);
        select.innerHTML = `<option value="">Error: ${e.message}</option>`;
    }
}

async function handleRun(overlay, file, geminiModel = '', managerModel = '', existingUploadId = '', existingFilename = '', geminiRetryAttempts = 3, geminiFallbackModels = [], importFromRunId = '', projectType = '', autoMode = true, importNotesFromRunId = '', importScopeFromRunId = '', memoryRecallCount = 12, phaseModels = {}) {
    const runMetaState = {};
    const runBtn       = overlay.querySelector('#qp-run-btn');
    const cancelBtn    = overlay.querySelector('#qp-cancel-btn');
    const statusEl     = overlay.querySelector('#qp-status');
    const formArea     = overlay.querySelector('#qp-form-area');
    const contentArea  = overlay.querySelector('#qp-content');
    const sidebarPages = overlay.querySelector('#qp-sidebar-pages');
    const mainOutput   = overlay.querySelector('#qp-main-output');
    const runName      = overlay.querySelector('#qp-run-name')?.value.trim() || '';
    const notes        = overlay.querySelector('#qp-notes').value;
    const autoMemory   = !!overlay.querySelector('#qp-auto-memory')?.checked;
    const selectedJobs = [...overlay.querySelectorAll('#qp-jobs-grid input[type=checkbox]:checked')].map(cb => cb.value);
    // Empty on purpose: a subset selection makes the server build a run-scoped
    // dynamic KP; an explicit holdout path is only settable from the phase-3 modal.
    const holdoutKpPath = '';

    runBtn.disabled = true;
    runBtn.textContent = 'Start';
    statusEl.style.display = 'block';

    let uploadId;
    let filename;

    if (existingUploadId) {
        uploadId = existingUploadId;
        filename = existingFilename || existingUploadId;
        setStatus(statusEl, 'Starting pipeline…', true);
    } else {
        setStatus(statusEl, 'Uploading file…', true);
        try {
            const fd = new FormData();
            fd.append('files', file);
            const res = await fetch('/api/upload/qp', { method: 'POST', body: fd });
            if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
            uploadId = (await res.json()).files[0].id;
            filename = file.name;
        } catch (err) {
            setStatus(statusEl, `Error: ${err.message}`);
            runBtn.disabled = false;
            return;
        }
    }

    setStatus(statusEl, 'Starting pipeline…', true);

    let runId, sessionId;
    try {
        const res = await fetch('/api/quick_proposal/start-proposal-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ upload_id: uploadId, run_name: runName, notes, gemini_model: geminiModel, manager_model: managerModel, phase_models: phaseModels, filename, selected_jobs: selectedJobs, gemini_retry_attempts: geminiRetryAttempts, gemini_fallback_models: geminiFallbackModels, holdout_kp_path: holdoutKpPath, import_from_run_id: importFromRunId, import_notes_from_run_id: importNotesFromRunId, import_scope_from_run_id: importScopeFromRunId, project_type: projectType, auto_memory: autoMemory, memory_recall_count: memoryRecallCount, auto_mode: autoMode }),
            credentials: 'same-origin',
        });
        if (!res.ok) throw new Error(`Run failed: ${res.status}`);
        ({ run_id: runId, session_id: sessionId } = await res.json());
    } catch (err) {
        setStatus(statusEl, `Error: ${err.message}`);
        runBtn.disabled = false;
        return;
    }

    // Close the overlay, reload the session list, then navigate to the new
    // proposal session. selectSession dispatches proposal-session-loaded which
    // enterProposalMode in chat.js uses to wire up proposal-mode behaviour.
    window.quickProposalModule?.close();
    if (window.sessionModule) {
        await window.sessionModule.loadSessions();
        await window.sessionModule.selectSession(sessionId);
    } else {
        window.location.hash = sessionId;
    }
    return;

    formArea.style.display = 'none';
    contentArea.style.display = 'flex';

    const phaseBar = overlay.querySelector('#qp-phase-bar');
    if (phaseBar) phaseBar.style.display = '';
    const _donePhases = new Set();
    const _reachablePhases = new Set();
    let _runningPhase = null;

    // completeness phase shares the 'extraction' pane (same extraction log panel)
    const _paneFor = phaseKey => phaseKey === 'classification' ? 'classification' : 'extraction';

    const _setActivePhase = (phaseKey) => {
        _reachablePhases.add(phaseKey);
        overlay.querySelectorAll('.qp-phase-step').forEach(s => {
            const k = s.dataset.phase;
            if (k === phaseKey) { s.classList.add('active'); s.classList.remove('done', 'running'); }
            else if (_donePhases.has(k)) { s.classList.add('done'); s.classList.remove('active', 'running'); }
            else if (k === _runningPhase) { s.classList.add('running'); s.classList.remove('active', 'done'); }
            else { s.classList.remove('active', 'done', 'running'); }
        });
        const pane = _paneFor(phaseKey);
        overlay.querySelectorAll('.qp-phase-pane').forEach(p => {
            p.style.display = p.dataset.phase === pane ? 'flex' : 'none';
        });
    };

    phaseBar?.addEventListener('click', e => {
        const step = e.target.closest('.qp-phase-step');
        if (step && (_donePhases.has(step.dataset.phase) || _reachablePhases.has(step.dataset.phase))) {
            _setActivePhase(step.dataset.phase);
        }
    });

    _setActivePhase('classification');

    const idxMainOutput = overlay.querySelector('#qp-index-main-output');
    if (idxMainOutput) {
        idxMainOutput._qpRunId        = runId;
        idxMainOutput._qpSidebarPages = sidebarPages;
        idxMainOutput._qpGeminiModel  = geminiModel;
    }

    const headerAutoMode  = overlay.querySelector('#qp-header-auto-mode');
    const headerAutoCheck = overlay.querySelector('#qp-auto-mode-header');
    if (headerAutoMode) {
        headerAutoCheck.checked = autoMode;
        headerAutoMode.style.display = '';
        headerAutoCheck.addEventListener('change', () => {
            localStorage.setItem('qp_auto_mode', headerAutoCheck.checked ? 'true' : 'false');
            // Push the new state to the server — the only network call auto-mode
            // needs. If a gate is currently open and this turns auto-mode on, the
            // server advances it immediately (see set_auto_mode).
            fetch(`/api/quick_proposal/runs/${runId}/auto-mode`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ auto_mode: headerAutoCheck.checked }),
                credentials: 'same-origin',
            }).catch(err => console.warn('[quick_proposal] auto-mode push error:', err));
        });
    }

    if (cancelBtn) {
        cancelBtn.style.display = '';
        cancelBtn.onclick = () => {
            cancelBtn.disabled = true;
            fetch(`/api/quick_proposal/runs/${runId}/cancel`, { method: 'POST', credentials: 'same-origin' })
                .catch(() => {});
        };
    }

    let statusPoll = null;
    overlay.querySelector('#qp-close-btn').addEventListener('click', () => {
        if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
    });

    startStream(runId, {
        onPhaseStart(data) {
            const label = data.cached
                ? `${data.label || data.phase} (prompt cached)`
                : (data.label || `Phase: ${data.phase}`);
            setStatus(statusEl, label, true);
            if (data.phase === 'phase3') {
                _runningPhase = 'completeness';
                _donePhases.add('classification');
                _setActivePhase('completeness');
                if (!mainOutput.querySelector('#qp-extraction-log')) renderExtractionPanel(mainOutput);
            } else if (data.phase === 'phase5') {
                _runningPhase = 'extraction';
                _donePhases.add('classification');
                _donePhases.add('completeness');
                _setActivePhase('extraction');
                renderExtractionPanel(mainOutput);
            }
        },

        onPhaseComplete(data) {
            const msgs = {
                load:   'Pages rendered.',
                index:  'Knowledge base loaded.',
                phase1: 'Job type detected.',
                phase2: 'Classification complete — regions annotated.',
                phase3: 'Completeness scoring done.',
                phase4: 'Extraction index built.',
                phase5: 'Extraction complete.',
            };
            setStatus(statusEl, msgs[data.phase] || `${data.phase} complete.`);
            if (data.phase === 'load') {
                const ph = mainOutput.querySelector('#qp-main-placeholder');
                if (ph) { ph.innerHTML = 'Click a page to preview'; }
            }
            if (data.phase === 'phase3') _donePhases.add('completeness');
            if (data.phase === 'phase5') _donePhases.add('extraction');
        },

        onPhaseGate(data) {
            const nextLabel = data.next_phase_label || 'Next Phase';
            if (overlay.querySelector('#qp-auto-mode-header')?.checked) {
                // Auto-mode: nothing to do here. The server already self-advances
                // gates on its own (_wait_for_gate / _get_auto_mode) based on the
                // auto_mode value it was seeded with at run start and any later
                // push from the header toggle — no client call needed, and none
                // would help anyway if the browser were closed when this fires.
            } else {
                // Manual mode: show gate button in status area
                setStatus(statusEl, `Ready for ${nextLabel}`);
                const gateBtn = document.createElement('button');
                gateBtn.className = 'qp-gate-btn';
                gateBtn.textContent = `▶ Start ${nextLabel}`;
                gateBtn.onclick = () => {
                    gateBtn.disabled = true;
                    gateBtn.textContent = 'Starting…';
                    fetch('/api/quick_proposal/advance-phase', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ run_id: runId, phase: data.phase }),
                        credentials: 'same-origin',
                    }).then(r => {
                        if (r.ok) {
                            gateBtn.remove();
                        } else {
                            gateBtn.disabled = false;
                            gateBtn.textContent = `▶ Start ${nextLabel}`;
                        }
                    }).catch(err => {
                        console.warn('[quick_proposal] advance-phase error:', err);
                        gateBtn.disabled = false;
                        gateBtn.textContent = `▶ Start ${nextLabel}`;
                    });
                };
                const statusParent = statusEl.parentElement || mainOutput;
                statusParent.appendChild(gateBtn);
            }
        },

        onPageReady(data) {
            addThumbnail(sidebarPages, idxMainOutput || mainOutput, data.page_idx, data.url);
        },

        onIndexLoaded(data) {
            const kp = data.kp_path || '';
            const isHoldout = kp.includes('holdout');
            console.info(`[quick_proposal] knowledge pack loaded: ${kp}${isHoldout ? ' (HOLDOUT)' : ''}`);
            if (isHoldout) {
                // Patch the phase-complete message so the user sees "holdout" in status
                const label = kp.split('/').slice(-2, -1)[0] || 'holdout';
                setStatus(statusEl, `Knowledge base loaded — holdout KP: ${label}`);
            }
        },

        onPageClassified(data) {
            updateThumbnailClassification(sidebarPages, data);
            if (data.error) {
                setStatus(statusEl, `Classification error (p.${data.page_idx + 1}): ${data.error}`);
            }
        },

        onExtractionMessage(data) {
            appendExtractionMessage(mainOutput, data);
        },

        onIndexUpdate(data) {
            updateLiveIndex(mainOutput, data);
            if (data.key === 'project_type' || data.key === 'plan_completeness') {
                runMetaState[data.key] = data.value;
                updateRunMeta(overlay, runMetaState);
            }
        },

        onRegionPreview(data) { addRegionPreview(mainOutput, data); },

        onContextUsage(data) { updateContextMeter(mainOutput, data); },

        onStreamDrop() {
            setStatus(statusEl, 'Connection lost — pipeline still running in background…', true);
            statusPoll = setInterval(async () => {
                try {
                    const res = await fetch(`/api/quick_proposal/runs/${runId}/status`, { credentials: 'same-origin' });
                    if (!res.ok) return;
                    const { status } = await res.json();
                    if (status === 'complete') {
                        clearInterval(statusPoll); statusPoll = null;
                        setStatus(statusEl, 'Pipeline complete.');
                        if (cancelBtn) cancelBtn.style.display = 'none';
                        if (headerAutoMode) headerAutoMode.style.display = 'none';
                    } else if (status === 'error' || status === 'cancelled') {
                        clearInterval(statusPoll); statusPoll = null;
                        setStatus(statusEl, `Pipeline ${status}.`);
                        if (cancelBtn) cancelBtn.style.display = 'none';
                        if (headerAutoMode) headerAutoMode.style.display = 'none';
                    }
                } catch { /* ignore network errors during poll */ }
            }, 3000);
        },

        onError(data) {
            setStatus(statusEl, `Error: ${data.message}`);
            if (cancelBtn) cancelBtn.style.display = 'none';
            if (headerAutoMode) headerAutoMode.style.display = 'none';
        },

        onDone() {
            setStatus(statusEl, 'Pipeline complete.');
            if (cancelBtn) cancelBtn.style.display = 'none';
            if (headerAutoMode) headerAutoMode.style.display = 'none';
            const pages = Object.entries(pageClassifications).map(([idx, cls]) => ({
                idx:         parseInt(idx),
                sheet_type:  cls.sheet_type,
                importance:  cls.importance,
                description: cls.description || '',
            }));
            if (pages.length && runId) {
                fetch(`/api/quick_proposal/runs/${runId}/classifications`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pages }),
                    credentials: 'same-origin',
                }).catch(e => console.warn('[quick_proposal] classification save failed:', e));
            }
        },
    });
}

function setStatus(el, text, loading = false) {
    el.style.display = 'block';
    el.innerHTML = '';
    if (loading) {
        const spinner = document.createElement('span');
        spinner.className = 'qp-spinner';
        el.appendChild(spinner);
    }
    el.appendChild(document.createTextNode(text));
}

function addThumbnail(container, mainOutput, pageIdx, url) {
    const thumb = document.createElement('div');
    thumb.className = 'qp-page-thumb';
    thumb.dataset.pageIdx = pageIdx;

    const imgWrap = document.createElement('div');
    imgWrap.className = 'qp-thumb-img-wrap';

    const img = document.createElement('img');
    img.src = url;
    img.alt = `Page ${pageIdx + 1}`;
    img.loading = 'lazy';

    const label = document.createElement('div');
    label.className = 'qp-page-label';
    label.textContent = `p.${pageIdx + 1}`;

    imgWrap.appendChild(img);
    imgWrap.appendChild(label);
    thumb.appendChild(imgWrap);
    container.appendChild(thumb);

    // Click → full preview in main area
    thumb.addEventListener('click', () => {
        container.querySelectorAll('.qp-page-thumb').forEach(t => t.classList.remove('active'));
        thumb.classList.add('active');
        showPreview(mainOutput, url, pageIdx);
    });
}

function showPreview(mainOutput, url, pageIdx) {
    const previewArea = mainOutput.querySelector('.qp-preview-area');
    if (!previewArea) return;

    // Clean up any existing pan/zoom listeners before replacing content
    previewArea._panCtrl?.abort();

    // Preserve the current contents (extraction panel or placeholder) so back restores them.
    const savedFragment = document.createDocumentFragment();
    while (previewArea.firstChild) savedFragment.appendChild(previewArea.firstChild);

    previewArea.innerHTML = '';

    const panCtrl = new AbortController();
    previewArea._panCtrl = panCtrl;
    const { signal } = panCtrl;

    const closeBtn = document.createElement('button');
    closeBtn.className = 'qp-preview-close';
    closeBtn.textContent = '← Back';
    closeBtn.addEventListener('click', () => {
        panCtrl.abort();
        (mainOutput.closest('.qp-content') || mainOutput.closest('[data-phase]') || mainOutput.closest('[data-tab]'))
            ?.querySelectorAll('.qp-page-thumb')
            .forEach(t => t.classList.remove('active'));
        previewArea._panCtrl = null;
        previewArea.innerHTML = '';
        if (savedFragment.childNodes.length) {
            previewArea.appendChild(savedFragment);
        } else {
            const ph = document.createElement('div');
            ph.className = 'qp-main-placeholder';
            ph.id = 'qp-main-placeholder';
            ph.textContent = 'Click a page to preview';
            previewArea.appendChild(ph);
        }
    });

    const img = document.createElement('img');
    img.className = 'qp-preview-img';
    img.src = url;
    img.alt = `Page ${pageIdx + 1} full preview`;
    img.draggable = false;

    // Zoom + pan state
    let scale = 1, px = 0, py = 0;
    let dragging = false, dragStartX = 0, dragStartY = 0, dragPx = 0, dragPy = 0;
    let previewSvg = null;

    function applyTransform() {
        const t = `translate(${px}px, ${py}px) scale(${scale})`;
        img.style.transform = t;
        if (previewSvg) previewSvg.style.transform = t;
        previewArea.style.cursor = scale > 1 ? (dragging ? 'grabbing' : 'grab') : 'default';
    }

    // Scroll to zoom, centered on cursor
    previewArea.addEventListener('wheel', e => {
        e.preventDefault();
        const rect = previewArea.getBoundingClientRect();
        const mx = e.clientX - rect.left - rect.width / 2;
        const my = e.clientY - rect.top - rect.height / 2;
        const factor = e.deltaY < 0 ? 1.05 : 1 / 1.05;
        const newScale = Math.min(8, Math.max(1, scale * factor));
        if (newScale === scale) return;
        px = mx + (px - mx) * (newScale / scale);
        py = my + (py - my) * (newScale / scale);
        scale = newScale;
        if (scale <= 1) { scale = 1; px = 0; py = 0; }
        applyTransform();
    }, { passive: false, signal });

    // Drag to pan when zoomed
    previewArea.addEventListener('mousedown', e => {
        if (scale <= 1 || e.button !== 0) return;
        dragging = true;
        dragStartX = e.clientX; dragStartY = e.clientY;
        dragPx = px; dragPy = py;
        e.preventDefault();
    }, { signal });

    window.addEventListener('mousemove', e => {
        if (!dragging) return;
        px = dragPx + (e.clientX - dragStartX);
        py = dragPy + (e.clientY - dragStartY);
        applyTransform();
    }, { signal });

    window.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        applyTransform();
    }, { signal });

    // Double-click to reset zoom
    previewArea.addEventListener('dblclick', () => {
        scale = 1; px = 0; py = 0;
        applyTransform();
    }, { signal });

    previewArea.appendChild(closeBtn);

    const qpRunId = mainOutput._qpRunId;
    if (qpRunId) {
        const redoBtn = document.createElement('button');
        redoBtn.className = 'qp-preview-redo';
        redoBtn.textContent = '↻ Redo Page';
        redoBtn.addEventListener('click', async () => {
            redoBtn.disabled = true;
            redoBtn.textContent = '↻ Classifying…';
            try {
                const geminiModel = mainOutput._qpGeminiModel || '';
                const res = await fetch(
                    `/api/quick_proposal/runs/${qpRunId}/reclassify/${pageIdx}`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ gemini_model: geminiModel }) }
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                pageClassifications[pageIdx] = {
                    sheet_type:  data.sheet_type,
                    importance:  data.importance,
                    description: data.description,
                    regions:     data.regions || [],
                };
                const sp = mainOutput._qpSidebarPages;
                if (sp) updateThumbnailClassification(sp, { page_idx: pageIdx, ...data });
                showPreview(mainOutput, url, pageIdx);
            } catch (e) {
                redoBtn.textContent = `↻ Error: ${e.message}`;
                redoBtn.disabled = false;
            }
        });
        previewArea.appendChild(redoBtn);
    }

    previewArea.appendChild(img);

    // Bbox overlay — aligned with the image using the same xMidYMid meet strategy as object-fit:contain
    const regions = (pageClassifications[pageIdx] || {}).regions || [];
    if (regions.length) {
        const NS = 'http://www.w3.org/2000/svg';
        previewSvg = document.createElementNS(NS, 'svg');
        previewSvg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        previewSvg.style.cssText = 'position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none; will-change:transform;';
        img.addEventListener('load', () => {
            const W = img.naturalWidth || 1000;
            const H = img.naturalHeight || 1000;
            previewSvg.setAttribute('viewBox', `0 0 ${W} ${H}`);
            regions.forEach(r => {
                const [x1p, y1p, x2p, y2p] = r.bbox || [0, 0, 0, 0];
                if (x2p <= x1p || y2p <= y1p) return;
                const color = IMPORTANCE_COLOR[r.importance] || '#0af';
                const rect = document.createElementNS(NS, 'rect');
                rect.setAttribute('x',      String(x1p / 100 * W));
                rect.setAttribute('y',      String(y1p / 100 * H));
                rect.setAttribute('width',  String((x2p - x1p) / 100 * W));
                rect.setAttribute('height', String((y2p - y1p) / 100 * H));
                rect.setAttribute('fill', 'none');
                rect.setAttribute('stroke', color);
                rect.setAttribute('stroke-width', '2');
                rect.setAttribute('vector-effect', 'non-scaling-stroke');
                previewSvg.appendChild(rect);
            });
        });
        previewArea.appendChild(previewSvg);
    }
}

function renderBboxOverlays(thumb, regions) {
    const target = thumb.querySelector('.qp-thumb-img-wrap') || thumb;
    target.querySelectorAll('.qp-bbox-svg').forEach(el => el.remove());
    if (!regions || !regions.length) return;

    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 100 100');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.classList.add('qp-bbox-svg');

    regions.forEach(r => {
        const [x1, y1, x2, y2] = r.bbox || [0, 0, 0, 0];
        if (x2 <= x1 || y2 <= y1) return;
        const color = IMPORTANCE_COLOR[r.importance] || '#0af';
        const rect = document.createElementNS(NS, 'rect');
        rect.setAttribute('x', x1);
        rect.setAttribute('y', y1);
        rect.setAttribute('width', x2 - x1);
        rect.setAttribute('height', y2 - y1);
        rect.setAttribute('fill', 'none');
        rect.setAttribute('stroke', color);
        rect.setAttribute('stroke-width', '1.2');
        rect.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.appendChild(rect);
    });

    target.appendChild(svg);
}

function renderPhase1IndexPanel(mainOutput) {
    const previewArea = mainOutput.querySelector('.qp-preview-area');
    if (!previewArea) return;

    previewArea._panCtrl?.abort();
    previewArea.innerHTML = '';

    const container = document.createElement('div');
    container.className = 'qp-phase1-index';
    container.id = 'qp-phase1-index';

    const importanceOrder = { high: 0, medium: 1, low: 2 };
    const sorted = Object.entries(pageClassifications)
        .sort(([ai, a], [bi, b]) =>
            (importanceOrder[a.importance] ?? 3) - (importanceOrder[b.importance] ?? 3)
            || parseInt(ai) - parseInt(bi)
        );

    sorted.forEach(([idxStr, cls]) => {
        const idx = parseInt(idxStr);
        const card = document.createElement('div');
        card.className = 'qp-p1-page';

        const color = IMPORTANCE_COLOR[cls.importance] || '#6b7280';
        const header = document.createElement('div');
        header.className = 'qp-p1-page-header';
        header.innerHTML = `
            <span class="qp-p1-imp" style="background:${color}">${cls.importance || 'low'}</span>
            <span class="qp-p1-type">p.${idx + 1} — ${(cls.sheet_type || 'other').replace(/_/g, ' ')}</span>
            <span class="qp-p1-desc">${cls.description || ''}</span>
        `;

        header.addEventListener('click', () => {
            const url = mainOutput.closest('.qp-content')
                ?.querySelector(`[data-page-idx="${idx}"] img`)?.src;
            if (url) showPreview(mainOutput, url, idx);
        });

        card.appendChild(header);

        if (cls.regions && cls.regions.length) {
            const regList = document.createElement('div');
            regList.className = 'qp-p1-regions';
            cls.regions.forEach(r => {
                const row = document.createElement('div');
                row.className = 'qp-p1-region';
                const coords = r.bbox ? r.bbox.map(v => Math.round(v)).join(', ') : '';
                row.innerHTML = `<span class="qp-p1-region-id">${r.id}</span>${r.label || r.extraction_hint || ''}<span class="qp-p1-region-coords" style="display:none"> [${coords}]</span>`;
                row.style.cursor = coords ? 'pointer' : '';
                if (coords) {
                    row.addEventListener('click', () => {
                        const el = row.querySelector('.qp-p1-region-coords');
                        el.style.display = el.style.display === 'none' ? '' : 'none';
                    });
                }
                regList.appendChild(row);
            });
            card.appendChild(regList);
        }

        container.appendChild(card);
    });

    previewArea.appendChild(container);
}

function updateThumbnailClassification(container, data, onSave) {
    const thumb = container.querySelector(`[data-page-idx="${data.page_idx}"]`);
    if (!thumb) return;

    pageClassifications[data.page_idx] = {
        sheet_type:  data.sheet_type  || 'other',
        importance:  data.importance  || 'low',
        description: data.description || '',
        regions:     data.regions     || [],
    };

    const imgWrap = thumb.querySelector('.qp-thumb-img-wrap') || thumb;

    const label = imgWrap.querySelector('.qp-page-label');
    if (label) label.textContent = `p.${data.page_idx + 1} · ${data.sheet_type || '?'}`;

    renderBboxOverlays(thumb, data.regions || []);

    imgWrap.querySelector('.qp-classification-badge')?.remove();
    const badge = document.createElement('div');
    badge.className = 'qp-classification-badge';
    if (data.error) {
        badge.textContent = 'error';
        badge.style.background = '#ef4444';
    } else {
        badge.textContent = (data.sheet_type || 'other').replace(/_/g, ' ');
        badge.style.background = IMPORTANCE_COLOR[data.importance] || '#6b7280';
    }
    badge.addEventListener('click', e => {
        e.stopPropagation();
        openClassifyEditor(badge, data.page_idx, container, onSave);
    });
    imgWrap.appendChild(badge);

    // Info panel: description + region list, always below the image
    thumb.querySelector('.qp-thumb-info')?.remove();
    if (!data.error && (data.description || (data.regions || []).length)) {
        const info = document.createElement('div');
        info.className = 'qp-thumb-info';

        if (data.description) {
            const desc = document.createElement('div');
            desc.className = 'qp-thumb-desc';
            desc.textContent = data.description;
            info.appendChild(desc);
        }

        if ((data.regions || []).length) {
            const regList = document.createElement('div');
            regList.className = 'qp-thumb-regions';
            (data.regions || []).forEach(r => {
                const row = document.createElement('div');
                row.className = 'qp-thumb-region-row';
                const dot = document.createElement('span');
                dot.className = 'qp-thumb-region-dot';
                dot.style.background = IMPORTANCE_COLOR[r.importance] || '#6b7280';
                const lbl = document.createElement('span');
                lbl.className = 'qp-thumb-region-label';
                lbl.textContent = r.label || r.extraction_hint || r.id;
                row.appendChild(dot);
                row.appendChild(lbl);
                if (r.bbox) {
                    const coords = document.createElement('span');
                    coords.className = 'qp-thumb-region-coords';
                    coords.textContent = `[${r.bbox.map(v => Math.round(v)).join(', ')}]`;
                    coords.style.display = 'none';
                    row.appendChild(coords);
                    row.addEventListener('click', e => {
                        e.stopPropagation();
                        coords.style.display = coords.style.display === 'none' ? '' : 'none';
                    });
                }
                regList.appendChild(row);
            });
            info.appendChild(regList);
        }

        thumb.appendChild(info);
    }
}

function openClassifyEditor(anchor, pageIdx, container, onSave) {
    let editor = document.getElementById('qp-classify-editor');
    if (!editor) {
        editor = document.createElement('div');
        editor.id = 'qp-classify-editor';
        editor.className = 'qp-classify-editor';
        editor.innerHTML = `
            <div class="qp-classify-editor-row">
                <div class="qp-classify-editor-label">Sheet Type</div>
                <select id="qp-ce-type"></select>
            </div>
            <div class="qp-classify-editor-row">
                <div class="qp-classify-editor-label">Importance</div>
                <select id="qp-ce-importance">
                    <option value="high">high</option>
                    <option value="medium">medium</option>
                    <option value="low">low</option>
                </select>
            </div>
            <div class="qp-classify-editor-actions">
                <button class="qp-classify-editor-cancel" id="qp-ce-cancel">Cancel</button>
                <button class="qp-classify-editor-save"   id="qp-ce-save">Save</button>
            </div>`;
        const typeSelect = editor.querySelector('#qp-ce-type');
        SHEET_TYPES.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t.replace(/_/g, ' ');
            typeSelect.appendChild(opt);
        });
        document.body.appendChild(editor);
        document.addEventListener('click', e => {
            if (!editor.contains(e.target)) closeClassifyEditor();
        });
    }

    // Store active page and save callback on the editor element
    editor._pageIdx   = pageIdx;
    editor._container = container;
    editor._onSave    = onSave ?? null;

    // Pre-populate with current values
    const cls = pageClassifications[pageIdx] || {};
    editor.querySelector('#qp-ce-type').value = cls.sheet_type || 'other';
    editor.querySelector('#qp-ce-importance').value = cls.importance || 'low';

    // Position near anchor
    const rect = anchor.getBoundingClientRect();
    const top  = Math.min(rect.bottom + 4, window.innerHeight - 180);
    const left = Math.min(rect.left, window.innerWidth - 200);
    editor.style.top  = `${top}px`;
    editor.style.left = `${left}px`;
    editor.style.display = 'flex';

    editor.querySelector('#qp-ce-cancel').onclick = () => closeClassifyEditor();
    editor.querySelector('#qp-ce-save').onclick = () => {
        const newType = editor.querySelector('#qp-ce-type').value;
        const newImp  = editor.querySelector('#qp-ce-importance').value;
        const current = pageClassifications[pageIdx] || {};
        const updated = { ...current, sheet_type: newType, importance: newImp };
        updateThumbnailClassification(editor._container, {
            page_idx:    pageIdx,
            sheet_type:  newType,
            importance:  newImp,
            description: updated.description,
            regions:     updated.regions,
        }, editor._onSave);
        editor._onSave?.(pageIdx, newType, newImp);
        closeClassifyEditor();
    };
}

function closeClassifyEditor() {
    const editor = document.getElementById('qp-classify-editor');
    if (editor) editor.style.display = 'none';
}

function _exportLogText(container) {
    const parts = [];
    for (const child of container.children) {
        if (child.classList.contains('qp-exmsg-manager')) {
            const avatar = child.querySelector('.qp-msg-avatar')?.textContent?.trim() || 'Manager';
            const thinking = child.querySelector('.thinking-content-inner')?.innerText?.trim();
            const text = child.querySelector('.qp-msg-text')?.innerText?.trim() || '';
            if (thinking) parts.push(`[${avatar} – thinking]\n${thinking}`);
            if (text) parts.push(`[${avatar}]\n${text}`);
        } else if (child.classList.contains('qp-exmsg-gemini')) {
            const thinking = child.querySelector('.thinking-content-inner')?.innerText?.trim();
            const text = child.querySelector('.qp-msg-text')?.innerText?.trim() || '';
            if (thinking) parts.push(`[Gemini – thinking]\n${thinking}`);
            if (text) parts.push(`[Gemini]\n${text}`);
        } else if (child.classList.contains('qp-exmsg-instruction')) {
            const text = child.querySelector('.thinking-content-inner')?.innerText?.trim() || '';
            if (text) parts.push(`[→ Gemini instruction]\n${text}`);
        } else if (child.classList.contains('agent-thread')) {
            for (const node of child.querySelectorAll('.agent-thread-node')) {
                const tool = node.querySelector('.agent-thread-tool')?.textContent?.trim() || 'tool';
                const outputs = node.querySelectorAll('.agent-tool-output pre');
                const input  = outputs[0]?.textContent?.trim() || '';
                const output = outputs[1]?.textContent?.trim() || '';
                let entry = `[tool: ${tool}]`;
                if (input)  entry += `\nInput:  ${input}`;
                if (output) entry += `\nOutput: ${output}`;
                parts.push(entry);
            }
        } else {
            const text = child.innerText?.trim();
            if (text) parts.push(text);
        }
    }
    return parts.join('\n\n---\n\n');
}

function renderExtractionPanel(mainOutput) {
    const previewArea = mainOutput.querySelector('.qp-preview-area');
    if (!previewArea) return;
    // Abort any active pan/zoom listeners
    previewArea._panCtrl?.abort();
    previewArea.innerHTML = `
        <div class="qp-extraction-log" id="qp-extraction-log">
            <div class="qp-extraction-header">
                Extraction Log
                <button class="qp-export-log-btn" id="qp-export-log-btn" title="Copy log as plain text">Export log</button>
            </div>
            <div class="qp-context-meter" id="qp-context-meter">
                <div class="qp-context-row" id="qp-ctx-claude">
                    <span class="qp-ctx-label">Claude</span>
                    <div class="qp-ctx-bar-wrap"><div class="qp-ctx-bar"></div></div>
                    <span class="qp-ctx-tokens">—</span>
                </div>
                <div class="qp-context-row" id="qp-ctx-gemini">
                    <span class="qp-ctx-label">Extractor</span>
                    <div class="qp-ctx-bar-wrap"><div class="qp-ctx-bar"></div></div>
                    <span class="qp-ctx-tokens">—</span>
                </div>
                <div class="qp-context-row qp-ctx-cost-row" id="qp-ctx-cost-claude">
                    <span class="qp-ctx-label">Claude</span>
                    <span class="qp-ctx-cost-text">—</span>
                </div>
                <div class="qp-context-row qp-ctx-cost-row" id="qp-ctx-cost-gemini">
                    <span class="qp-ctx-label">Extractor</span>
                    <span class="qp-ctx-cost-text">—</span>
                </div>
            </div>
            <div class="qp-extraction-messages" id="qp-extraction-messages"></div>
        </div>
        <div class="qp-index-panel" id="qp-index-panel">
            <div class="qp-index-header">Extracted Values</div>
            <div class="qp-index-values" id="qp-index-values"></div>
        </div>
    `;
    previewArea.querySelector('#qp-export-log-btn').addEventListener('click', () => {
        const msgs = previewArea.querySelector('#qp-extraction-messages');
        let text = _exportLogText(msgs);
        // Append the running session-cost summary (TODO_CCC) from the context meter above
        // the log — read from its rendered text (one line per role) rather than a stored
        // JS property, so this still works after a history reload with no live events.
        const claudeCost = previewArea.querySelector('#qp-ctx-cost-claude .qp-ctx-cost-text')?.textContent?.trim();
        const geminiCost = previewArea.querySelector('#qp-ctx-cost-gemini .qp-ctx-cost-text')?.textContent?.trim();
        const costLines = [];
        if (claudeCost && claudeCost !== '—') costLines.push(`Claude: ${claudeCost}`);
        if (geminiCost && geminiCost !== '—') costLines.push(`Extractor: ${geminiCost}`);
        if (costLines.length) {
            text += `\n\n---\n\n[Cost]\n${costLines.join('\n')}`;
        }
        navigator.clipboard.writeText(text).then(() => {
            const btn = previewArea.querySelector('#qp-export-log-btn');
            const orig = btn.textContent;
            btn.textContent = 'Copied!';
            setTimeout(() => { btn.textContent = orig; }, 1500);
        });
    });
}

function _esc(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _intOrDefault(value, fallback) {
    const n = parseInt(value, 10);
    return Number.isNaN(n) ? fallback : n;
}

function updateContextMeter(mainOutput, data) {
    const row = mainOutput.querySelector(`#qp-ctx-${data.role}`);
    if (row) {
        // No warn/caution color threshold here — `data.context_window` is only a
        // best-effort default (e.g. hardcoded to 200k for Claude in several places
        // server-side) that doesn't reflect actual model context windows (already
        // 1M for some), and is expected to keep changing — a color threshold tied to
        // a stale/guessed number is actively misleading, so the bar is neutral.
        const pct = Math.min(100, (data.input_tokens / data.context_window) * 100);
        const bar = row.querySelector('.qp-ctx-bar');
        bar.style.width = pct.toFixed(1) + '%';
        bar.className = 'qp-ctx-bar';
        const fmt = t => t >= 1000000 ? (t / 1000000).toFixed(2) + 'M' : t >= 1000 ? (t / 1000).toFixed(1) + 'k' : String(t);
        row.querySelector('.qp-ctx-tokens').textContent = fmt(data.input_tokens);
    }

    // Running session cost (TODO_CCC) — its own line per role, below both context rows.
    const costRow = mainOutput.querySelector(`#qp-ctx-cost-${data.role}`);
    if (costRow && data.session_cost_usd != null) {
        const costText = costRow.querySelector('.qp-ctx-cost-text');
        if (costText) costText.textContent = `$${data.session_cost_usd.toFixed(2)}`;
    }
}


export function appendExtractionMessage(mainOutput, data) {
    const container = mainOutput.querySelector('#qp-extraction-messages');
    if (!container) return;
    if (!container._toolNodes) container._toolNodes = new Map();

    if (data.role === 'tool_call') {
        // Group consecutive tool calls in one agent-thread wrapper
        let thread = container.lastElementChild?.classList.contains('agent-thread')
            ? container.lastElementChild : null;
        if (!thread) {
            thread = document.createElement('div');
            thread.className = 'agent-thread qp-gemini-thread';
            container.appendChild(thread);
        }
        const toolId = data.tool_id || '';
        const node = document.createElement('div');
        node.className = 'agent-thread-node running';
        node.dataset.toolId = toolId;
        if (data.model) node.dataset.model = data.model;
        const modelBadge = data.model
            ? `<span class="qp-tool-model-badge">${_esc(data.model)}</span>`
            : '';
        node.innerHTML = `<div class="agent-thread-dot"></div>
            <div class="agent-thread-header">
                <span class="agent-thread-icon">⚙</span>
                ${modelBadge}<span class="agent-thread-tool">${_esc(data.tool)}</span>
                <span class="agent-thread-wave">▱▲△</span>
            </div>
            <div class="agent-thread-content">
                <details class="agent-tool-output"><summary>Input</summary><pre>${_esc(data.args || '{}')}</pre></details>
            </div>`;
        thread.appendChild(node);
        if (toolId) container._toolNodes.set(toolId, node);

    } else if (data.role === 'tool_result') {
        const node = data.tool_id ? container._toolNodes.get(data.tool_id) : null;
        if (node) {
            const inputHtml = node.querySelector('.agent-tool-output')?.outerHTML || '';
            node.className = 'agent-thread-node';
            const model = data.model || node.dataset.model || '';
            const modelBadge = model ? `<span class="qp-tool-model-badge">${_esc(model)}</span>` : '';
            const imgHtml = data.image_url
                ? `<img src="${_esc(data.image_url)}" style="max-width:100%;border-radius:4px;margin-top:6px;" loading="lazy">`
                : '';
            node.innerHTML = `<div class="agent-thread-dot"></div>
                <div class="agent-thread-header">
                    <span class="agent-thread-icon">✓</span>
                    ${modelBadge}<span class="agent-thread-tool">${_esc(data.tool)}</span>
                    <span class="agent-thread-status">done</span>
                    <span class="agent-thread-chevron">▶</span>
                </div>
                <div class="agent-thread-content">
                    ${inputHtml}
                    <details class="agent-tool-output"><summary>Output</summary><pre>${_esc(data.result || '(no output)')}</pre>${imgHtml}</details>
                </div>`;
        }

    } else if (data.role === 'claude_thinking_start') {
        // First thinking token arrived — create a live block that delta events fill in.
        const id = 'qp-think-' + data.thinking_id;
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-thinking';
        wrap.innerHTML = `<div class="thinking-section">
            <div class="thinking-header" data-thinking-id="${id}">
                <div class="thinking-header-left"><span data-label="Manager thinking">Manager thinking</span></div>
                <div style="display:flex;align-items:center;gap:6px;"><span class="thinking-toggle" id="${id}-toggle"></span></div>
            </div>
            <div class="thinking-content" id="${id}"><pre class="thinking-content-inner" id="${id}-live" style="white-space:pre-wrap;margin:0;font-family:inherit;font-size:inherit"></pre></div>
        </div>`;
        container.appendChild(wrap);

    } else if (data.role === 'claude_thinking_delta') {
        const liveEl = document.getElementById('qp-think-' + data.thinking_id + '-live');
        if (liveEl) {
            liveEl.textContent += data.text || '';
            container.scrollTop = container.scrollHeight;
        }

    } else if (data.role === 'claude_thinking') {
        // Fallback: inline <think> tag thinking, emitted as a single complete event.
        const id = 'qp-thinking-' + Date.now() + '-' + Math.floor(Math.random() * 1e6);
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-thinking';
        wrap.innerHTML = `<div class="thinking-section">
            <div class="thinking-header" data-thinking-id="${id}">
                <div class="thinking-header-left"><span data-label="Manager thinking">Manager thinking</span></div>
                <div style="display:flex;align-items:center;gap:6px;"><span class="thinking-toggle" id="${id}-toggle"></span></div>
            </div>
            <div class="thinking-content" id="${id}"><div class="thinking-content-inner">${markdownModule.mdToHtml(data.text || '')}</div></div>
        </div>`;
        container.appendChild(wrap);

    } else if (data.role === 'gemini_thinking') {
        // Gemini's reasoning text that precedes tool calls — collapsed by default.
        const id = 'qp-gthink-' + Date.now() + '-' + Math.floor(Math.random() * 1e6);
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-gemini-thinking';
        wrap.innerHTML = `<div class="thinking-section qp-instr-section">
            <div class="thinking-header" data-thinking-id="${id}">
                <div class="thinking-header-left"><span data-label="Gemini reasoning">Gemini reasoning</span></div>
                <div style="display:flex;align-items:center;gap:6px;"><span class="thinking-toggle" id="${id}-toggle"></span></div>
            </div>
            <div class="thinking-content" id="${id}"><div class="thinking-content-inner">${markdownModule.mdToHtml(data.text || '')}</div></div>
        </div>`;
        container.appendChild(wrap);

    } else if (data.role === 'claude_text_start') {
        const label = data.model || 'Manager';
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-manager qp-claude-live';
        wrap.innerHTML = `<div class="qp-msg-bubble qp-msg-manager">
                <span class="qp-msg-avatar qp-avatar-manager">${_esc(label)}</span>
                <div class="qp-msg-text"><pre class="qp-live-pre" style="white-space:pre-wrap;margin:0;font-family:inherit;font-size:inherit"></pre></div>
            </div>`;
        container.appendChild(wrap);
        container.scrollTop = container.scrollHeight;

    } else if (data.role === 'claude_text_delta') {
        const live = container.querySelector('.qp-claude-live .qp-live-pre');
        if (live) {
            live.textContent += data.text || '';
            container.scrollTop = container.scrollHeight;
        }

    } else if (data.role === 'claude_text_end') {
        // Turn produced no visible content — remove the live bubble to prevent it going stale
        // and capturing the next turn's content via querySelector('.qp-claude-live').
        const liveWrap = container.querySelector('.qp-claude-live');
        if (liveWrap) liveWrap.remove();

    } else if (data.role === 'claude') {
        const label = data.model || 'Manager';
        // If a live streaming bubble exists, upgrade it to rendered markdown.
        const liveWrap = container.querySelector('.qp-claude-live');
        if (liveWrap) {
            liveWrap.classList.remove('qp-claude-live');
            const textDiv = liveWrap.querySelector('.qp-msg-text');
            if (textDiv) textDiv.innerHTML = markdownModule.processWithThinking(data.text || '');
        } else {
            const wrap = document.createElement('div');
            wrap.className = 'qp-exmsg qp-exmsg-manager';
            wrap.innerHTML = `<div class="qp-msg-bubble qp-msg-manager">
                    <span class="qp-msg-avatar qp-avatar-manager">${_esc(label)}</span>
                    <div class="qp-msg-text">${markdownModule.processWithThinking(data.text || '')}</div>
                </div>`;
            container.appendChild(wrap);
        }

    } else if (data.role === 'claude_to_gemini') {
        const id = 'qp-instr-' + Date.now() + '-' + Math.floor(Math.random() * 1e6);
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-instruction';
        wrap.innerHTML = `<div class="thinking-section qp-instr-section">
            <div class="thinking-header" data-thinking-id="${id}">
                <div class="thinking-header-left"><span data-label="Gemini instruction">→ Gemini instruction</span></div>
                <div style="display:flex;align-items:center;gap:6px;"><span class="thinking-toggle" id="${id}-toggle"></span></div>
            </div>
            <div class="thinking-content" id="${id}"><div class="thinking-content-inner">${markdownModule.mdToHtml(data.text || '')}</div></div>
        </div>`;
        container.appendChild(wrap);

    } else if (data.role === 'gemini') {
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-gemini';
        wrap.innerHTML = `<div class="qp-msg-bubble qp-msg-gemini">
            <span class="qp-msg-avatar qp-avatar-gemini">Gemini</span>
            <div class="qp-msg-text">${markdownModule.processWithThinking(data.text || '')}</div>
        </div>`;
        container.appendChild(wrap);

    } else if (data.role === 'retry_notice') {
        const wrap = document.createElement('div');
        wrap.className = 'qp-exmsg qp-exmsg-retry-notice';
        wrap.innerHTML = `<span style="margin-right:5px;opacity:0.7">↻</span>${_esc(data.text || '')}`;
        container.appendChild(wrap);

    } else {
        const row = document.createElement('div');
        row.className = 'qp-exmsg';
        row.textContent = data.text || JSON.stringify(data);
        container.appendChild(row);
    }

    container.scrollTop = container.scrollHeight;
}

function updateRunMeta(overlay, state) {
    const el = overlay.querySelector('#qp-run-meta');
    if (!el) return;
    const fmt = s => s ? String(s).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : null;
    const projectType  = fmt(state.project_type);
    const cScore = state.plan_completeness?.score;
    const completeness = cScore != null ? `${cScore}%` : null;
    el.innerHTML = [
        projectType  ? `<span class="qp-run-meta-item">${_esc(projectType)}</span>`  : '',
        completeness ? `<span class="qp-run-meta-item">Completeness: ${_esc(completeness)}</span>` : '',
    ].join('');
}

function updateLiveIndex(mainOutput, data) {
    const container = mainOutput.querySelector('#qp-index-values');
    if (!container) return;

    const key = data.key;
    let item = container.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (!item) {
        item = document.createElement('div');
        item.className = 'qp-index-item';
        item.dataset.key = key;
        container.appendChild(item);
    }
    const confClass = `qp-index-conf-${data.confidence || 'medium'}`;
    const valText   = data.value === null || data.value === undefined
        ? 'null'
        : (data.value !== null && typeof data.value === 'object' ? JSON.stringify(data.value) : String(data.value));
    item.innerHTML = `
        <span class="qp-index-key">${key}</span>
        <span class="qp-index-val">${valText}</span>
        <span class="qp-index-conf ${confClass}">${data.confidence || ''}</span>
    `;
}

function addRegionPreview(mainOutput, data) {
    const sidebar = mainOutput.closest('.qp-content')?.querySelector('#qp-sidebar-pages');
    if (!sidebar) return;
    let section = sidebar.querySelector('#qp-viewed-regions');
    if (!section) {
        const hdr = document.createElement('div');
        hdr.className = 'qp-sidebar-header';
        hdr.style.cssText = 'margin-top:8px;';
        hdr.textContent = 'Viewed Regions';
        section = document.createElement('div');
        section.id = 'qp-viewed-regions';
        section.className = 'qp-region-preview-grid';
        sidebar.appendChild(hdr);
        sidebar.appendChild(section);
    }
    const tile = document.createElement('div');
    tile.className = 'qp-region-tile';
    tile.title = data.bbox_id || '';
    if (data.image_url) {
        const img = document.createElement('img');
        img.src = data.image_url;
        tile.appendChild(img);
        // Click → full preview, same as page thumbnails
        const pageIdx = parseInt((data.bbox_id || '').split('_')[0], 10);
        tile.style.cursor = 'pointer';
        tile.addEventListener('click', () => {
            section.querySelectorAll('.qp-region-tile').forEach(t => t.classList.remove('active'));
            tile.classList.add('active');
            showPreview(mainOutput, data.image_url, isNaN(pageIdx) ? 0 : pageIdx);
        });
    }
    const lbl = document.createElement('div');
    lbl.className = 'qp-region-tile-lbl';
    lbl.textContent = data.bbox_id || '';
    tile.appendChild(lbl);
    section.appendChild(tile);
    section.scrollTop = section.scrollHeight;
}

function _escHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

const PROMPT_DISPLAY_NAMES = {
    'gemini_phase0_5':      'Detect Job Type',
    'gemini_phase1':        'Classification',
    'gemini_completeness':  'Completeness',
    'gemini_notes':         'Extract Notes',
    'gemini_scope':         'Detect Scope',
    'gemini_phase3':        'Extraction',
    'manager_system':       'Manager',
    'system_prompt':        'System',
};

// Pipeline order (matches phase execution order), not alphabetical filename order.
// Anything returned by the API but not listed here is appended at the end, in the
// order the API returned it, so a newly added prompt file never silently disappears.
const PROMPT_ORDER = [
    'gemini_phase0_5',
    'gemini_phase1',
    'gemini_completeness',
    'gemini_notes',
    'gemini_scope',
    'gemini_phase3',
    'manager_system',
    'system_prompt',
];

function sortPromptsForDisplay(prompts) {
    return [...prompts].sort((a, b) => {
        const ia = PROMPT_ORDER.indexOf(a.name);
        const ib = PROMPT_ORDER.indexOf(b.name);
        if (ia === -1 && ib === -1) return 0;
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        return ia - ib;
    });
}

export async function buildPromptsPanel({ onClose }) {
    injectStyles();

    const overlay = document.createElement('div');
    overlay.className = 'qp-overlay';
    overlay.id = 'qp-overlay';

    overlay.innerHTML = `
        <div class="qp-header">
            <span class="qp-title">Quick Proposal — Prompts</span>
            <button class="qp-close-btn" id="qp-close-btn" title="Close">✕</button>
        </div>
        <div class="qp-prompts-body" id="qp-prompts-body">
            <div class="qp-main-placeholder"><span class="qp-spinner"></span>Loading prompts…</div>
        </div>
    `;

    overlay.querySelector('#qp-close-btn').addEventListener('click', onClose);
    const body = overlay.querySelector('#qp-prompts-body');

    try {
        const res = await fetch('/api/quick_proposal/prompts', { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const prompts = sortPromptsForDisplay(await res.json());

        if (!prompts.length) {
            body.innerHTML = '<div style="padding:32px;text-align:center;opacity:0.5;">No prompts found.</div>';
            return overlay;
        }

        // Tab bar
        const tabBar = document.createElement('div');
        tabBar.className = 'qp-prompt-tabs';
        const contentArea = document.createElement('div');
        contentArea.className = 'qp-prompt-content';

        body.innerHTML = '';
        body.appendChild(tabBar);
        body.appendChild(contentArea);

        let activeTab = null;

        prompts.forEach((p, i) => {
            const label = PROMPT_DISPLAY_NAMES[p.name] || p.name;

            const tab = document.createElement('button');
            tab.className = 'qp-prompt-tab';
            tab.textContent = label;
            tab.dataset.name = p.name;
            tabBar.appendChild(tab);

            const pane = document.createElement('div');
            pane.className = 'qp-prompt-pane';
            pane.dataset.name = p.name;
            pane.dataset.mode = 'view';
            pane.style.display = 'none';
            pane.innerHTML = `
                <div class="qp-prompt-toolbar">
                    <button class="qp-prompt-edit-btn">Edit</button>
                    <span class="qp-prompt-status"></span>
                    <button class="qp-prompt-save-btn" disabled>Save</button>
                </div>
                <div class="qp-prompt-markdown"></div>
                <textarea class="qp-prompt-textarea" spellcheck="false">${_escHtml(p.content)}</textarea>
            `;
            contentArea.appendChild(pane);

            const markdownDiv = pane.querySelector('.qp-prompt-markdown');
            const textarea    = pane.querySelector('.qp-prompt-textarea');
            const editBtn      = pane.querySelector('.qp-prompt-edit-btn');
            const saveBtn      = pane.querySelector('.qp-prompt-save-btn');
            const status       = pane.querySelector('.qp-prompt-status');
            let savedContent = p.content;

            markdownDiv.innerHTML = markdownModule.mdToHtml(savedContent);

            editBtn.addEventListener('click', () => {
                if (pane.dataset.mode === 'view') {
                    pane.dataset.mode = 'edit';
                    editBtn.textContent = 'Preview';
                    textarea.focus();
                } else {
                    markdownDiv.innerHTML = markdownModule.mdToHtml(textarea.value);
                    pane.dataset.mode = 'view';
                    editBtn.textContent = 'Edit';
                }
            });

            textarea.addEventListener('input', () => {
                const dirty = textarea.value !== savedContent;
                saveBtn.disabled = !dirty;
                status.textContent = dirty ? 'Unsaved changes' : '';
            });

            saveBtn.addEventListener('click', async () => {
                saveBtn.disabled = true;
                status.textContent = 'Saving…';
                try {
                    const res = await fetch(`/api/quick_proposal/prompts/${encodeURIComponent(p.name)}`, {
                        method: 'PUT',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ content: textarea.value }),
                    });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    savedContent = textarea.value;
                    markdownDiv.innerHTML = markdownModule.mdToHtml(savedContent);
                    status.textContent = 'Saved';
                    setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2000);
                } catch (e) {
                    status.textContent = `Error: ${e.message}`;
                    saveBtn.disabled = false;
                }
            });

            tab.addEventListener('click', () => {
                tabBar.querySelectorAll('.qp-prompt-tab').forEach(t => t.classList.remove('active'));
                contentArea.querySelectorAll('.qp-prompt-pane').forEach(p => p.style.display = 'none');
                tab.classList.add('active');
                pane.style.display = 'flex';
                activeTab = p.name;
            });

            if (i === 0) tab.click();
        });
    } catch (e) {
        body.innerHTML = `<div style="padding:32px;text-align:center;color:#ef4444;">Error loading prompts: ${_escHtml(e.message)}</div>`;
    }

    return overlay;
}

export async function buildViewPanel({ runId, onClose, onRerun }) {
    injectStyles();

    const overlay = document.createElement('div');
    overlay.className = 'qp-overlay';
    overlay.id = 'qp-overlay';

    overlay.innerHTML = `
        <div class="qp-header">
            <span class="qp-title">Quick Proposal</span>
            <div class="qp-run-meta" id="qp-run-meta"></div>
            <button class="qp-jobs-btn qp-header-jobs-btn" title="Manage the case library of reference jobs" type="button">Jobs</button>
            <button class="qp-view-rerun-btn qp-run-rerun-btn" title="Re-run full pipeline">Re-run</button>
            <label class="qp-auto-mode-label qp-header-auto-mode" id="qp-header-auto-mode">
                <input type="checkbox" id="qp-auto-mode-header" checked> Auto
            </label>
            <button class="qp-cancel-btn" id="qp-cancel-btn" style="display:none">Cancel</button>
            <button class="qp-close-btn" id="qp-close-btn" title="Close">✕</button>
        </div>
        <div class="qp-phase-bar" id="qp-phase-bar">
            <button class="qp-phase-step" data-phase="classification">Classification</button>
            <span class="qp-phase-sep">→</span>
            <button class="qp-phase-step" data-phase="completeness">Completeness</button>
            <span class="qp-phase-sep">→</span>
            <button class="qp-phase-step" data-phase="extraction">Extraction</button>
        </div>
        <div class="qp-run-controls" id="qp-run-controls" style="display:none">
            <div class="qp-phase5-btns" style="margin:0">
                <button class="qp-run-btn qp-completeness-btn" id="qp-p3-completeness-btn" style="display:none;margin:0;padding:6px 18px;">Run Completeness Score</button>
                <button class="qp-run-btn" id="qp-p3-run-btn" style="margin:0;padding:6px 18px;">Run Extraction</button>
                <button class="qp-run-btn qp-resume-btn" id="qp-p3-resume-btn" style="display:none;margin:0;padding:6px 18px;">Resume</button>
            </div>
            <button class="qp-run-controls-toggle" id="qp-run-controls-toggle">⚙ Settings ▾</button>
            <div class="qp-run-controls-settings" id="qp-run-controls-settings">
                <div class="qp-phase5-row">
                    <span class="qp-phase5-label">Extraction Model</span>
                    <select class="qp-model-select" id="qp-p3-gemini-select" style="flex:1;min-width:0;">
                        <option value="">Loading…</option>
                    </select>
                </div>
                <div class="qp-phase5-row">
                    <span class="qp-phase5-label">Manager Model</span>
                    <select class="qp-model-select" id="qp-p3-manager-select" style="flex:1;min-width:0;">
                        <option value="">Loading…</option>
                    </select>
                </div>
                <div class="qp-phase5-row">
                    <span class="qp-phase5-label">Retries</span>
                    <input type="number" class="qp-retry-input" id="qp-p3-retry-attempts" value="3" min="1" max="10">
                </div>
                <div class="qp-phase5-row">
                    <span class="qp-phase5-label">Memories Recalled</span>
                    <input type="number" class="qp-retry-input" id="qp-p3-memory-recall-count" value="12" min="0" max="50">
                </div>
                <div class="qp-phase5-row">
                    <span class="qp-phase5-label" title="Leave blank to use the full knowledge pack">Holdout KP</span>
                    <input type="text" class="qp-holdout-input" id="qp-p3-holdout-kp" placeholder="(none — full KP)" style="flex:1;min-width:0;">
                </div>
            </div>
        </div>
        <div class="qp-status" id="qp-status" style="display:none"></div>
        <div class="qp-body" id="qp-body">
            <div class="qp-phase-pane" data-phase="classification" style="display:flex;flex:1;min-height:0;flex-direction:column;">
                <div id="qp-index-meta" style="display:none;flex-shrink:0;padding:6px 12px;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--border);align-items:center;font-size:11px;"></div>
                <div id="qp-index-main-output" style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">
                    <div class="qp-preview-area" style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">
                        <div style="flex:1;min-height:0;overflow-y:auto;">
                            <div class="qp-sidebar-pages" id="qp-sidebar-pages" style="padding:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;align-content:start;"></div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="qp-phase-pane" data-phase="extraction" style="display:none;flex:1;min-height:0;">
                <div class="qp-main-output" id="qp-main-output" style="flex:1;min-height:0;overflow-y:auto;">
                    <div class="qp-preview-area" id="qp-preview-area">
                        <div class="qp-main-placeholder" id="qp-main-placeholder">
                            <span class="qp-spinner"></span>Loading run…
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    overlay.querySelector('#qp-close-btn').addEventListener('click', onClose);

    const _donePhases = new Set();
    const _reachablePhases = new Set();
    let _runningPhase = null;
    const _paneFor = phaseKey => phaseKey === 'classification' ? 'classification' : 'extraction';

    const _setActivePhase = (phaseKey) => {
        _reachablePhases.add(phaseKey);
        overlay.querySelectorAll('.qp-phase-step').forEach(s => {
            const k = s.dataset.phase;
            if (k === phaseKey) { s.classList.add('active'); s.classList.remove('done', 'running'); }
            else if (_donePhases.has(k)) { s.classList.add('done'); s.classList.remove('active', 'running'); }
            else if (k === _runningPhase) { s.classList.add('running'); s.classList.remove('active', 'done'); }
            else { s.classList.remove('active', 'done', 'running'); }
        });
        const pane = _paneFor(phaseKey);
        overlay.querySelectorAll('.qp-phase-pane').forEach(p => {
            p.style.display = p.dataset.phase === pane ? 'flex' : 'none';
        });
    };

    overlay.querySelector('#qp-phase-bar').addEventListener('click', e => {
        const step = e.target.closest('.qp-phase-step');
        if (step && (_donePhases.has(step.dataset.phase) || _reachablePhases.has(step.dataset.phase))) {
            _setActivePhase(step.dataset.phase);
        }
    });

    const runControlsEl = overlay.querySelector('#qp-run-controls');
    overlay.querySelector('#qp-run-controls-toggle').addEventListener('click', () => {
        const s = overlay.querySelector('#qp-run-controls-settings');
        s.classList.toggle('open');
        overlay.querySelector('#qp-run-controls-toggle').textContent = s.classList.contains('open')
            ? '⚙ Settings ▴' : '⚙ Settings ▾';
    });

    const sidebarPages  = overlay.querySelector('#qp-sidebar-pages');
    const mainOutput    = overlay.querySelector('#qp-main-output');
    const placeholder   = overlay.querySelector('#qp-main-placeholder');
    const statusEl      = overlay.querySelector('#qp-status');

    mainOutput._qpRunId        = runId;
    mainOutput._qpSidebarPages = sidebarPages;
    mainOutput._qpGeminiModel  = '';
    const idxMainOutput = overlay.querySelector('#qp-index-main-output');
    if (idxMainOutput) {
        idxMainOutput._qpRunId        = runId;
        idxMainOutput._qpSidebarPages = sidebarPages;
        idxMainOutput._qpGeminiModel  = '';
    }
    const geminiSelect   = overlay.querySelector('#qp-p3-gemini-select');
    const managerSelect  = overlay.querySelector('#qp-p3-manager-select');
    const p3RetryInput   = overlay.querySelector('#qp-p3-retry-attempts');
    const p3MemoryRecallInput = overlay.querySelector('#qp-p3-memory-recall-count');
    const phase5RunBtn   = overlay.querySelector('#qp-p3-run-btn');
    const resumeBtn      = overlay.querySelector('#qp-p3-resume-btn');
    const holdoutInput      = overlay.querySelector('#qp-p3-holdout-kp');
    const completenessBtn   = overlay.querySelector('#qp-p3-completeness-btn');
    const cancelBtn         = overlay.querySelector('#qp-cancel-btn');

    loadModels(geminiSelect, { preferClaude: false });
    loadModels(managerSelect, { preferClaude: true });

    const runMetaState = {};
    function _applyRunMeta() {
        updateRunMeta(overlay, runMetaState);
        const indexMeta = overlay.querySelector('#qp-index-meta');
        if (!indexMeta) return;
        const fmt = s => s ? String(s).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : null;
        const projectType  = fmt(runMetaState.project_type);
        const cScore = runMetaState.plan_completeness?.score;
        const completeness = cScore != null ? `${cScore}%` : null;
        const html = [
            projectType  ? `<span class="qp-run-meta-item">${_esc(projectType)}</span>`  : '',
            completeness ? `<span class="qp-run-meta-item">Completeness: ${_esc(completeness)}</span>` : '',
        ].join('');
        indexMeta.innerHTML = html;
        indexMeta.style.display = html ? 'flex' : 'none';
    }

    const AUTO_MODE_KEY = 'qp_auto_mode';
    const headerAutoCheck = overlay.querySelector('#qp-auto-mode-header');
    if (headerAutoCheck) {
        // Default from localStorage until the server's actual value comes back below —
        // the server is authoritative for this specific run, localStorage is just a guess.
        headerAutoCheck.checked = localStorage.getItem(AUTO_MODE_KEY) !== 'false';
        fetch(`/api/quick_proposal/runs/${runId}/status`, { credentials: 'same-origin' })
            .then(r => r.ok ? r.json() : null)
            .then(status => {
                if (status && typeof status.auto_mode === 'boolean') {
                    headerAutoCheck.checked = status.auto_mode;
                }
            })
            .catch(() => {});
        headerAutoCheck.addEventListener('change', () => {
            localStorage.setItem(AUTO_MODE_KEY, headerAutoCheck.checked ? 'true' : 'false');
            fetch(`/api/quick_proposal/runs/${runId}/auto-mode`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ auto_mode: headerAutoCheck.checked }),
                credentials: 'same-origin',
            }).catch(err => console.warn('[quick_proposal] auto-mode push error:', err));
        });
    }

    function _startPhase5Stream(streamRunId, isResume) {
        runControlsEl.style.display = 'none';
        cancelBtn.style.display = '';
        cancelBtn.disabled = false;
        cancelBtn.onclick = () => {
            cancelBtn.disabled = true;
            fetch(`/api/quick_proposal/runs/${streamRunId}/cancel`, { method: 'POST', credentials: 'same-origin' })
                .catch(() => {});
        };

        startStream(streamRunId, {
            onPhaseStart(data) {
                const label = data.cached
                    ? `${data.label || data.phase} (prompt cached)`
                    : (data.label || `Phase: ${data.phase}`);
                setStatus(statusEl, label, true);
                if (data.phase === 'phase3') {
                    _runningPhase = 'completeness';
                    _donePhases.add('classification');
                    _setActivePhase('completeness');
                    if (!mainOutput.querySelector('#qp-extraction-log')) renderExtractionPanel(mainOutput);
                } else if (data.phase === 'phase5') {
                    _runningPhase = 'extraction';
                    _donePhases.add('classification');
                    _donePhases.add('completeness');
                    _setActivePhase('extraction');
                    renderExtractionPanel(mainOutput);
                }
            },
            onPhaseComplete(data) {
                if (data.phase === 'phase5') {
                    _donePhases.add('extraction');
                    setStatus(statusEl, 'Extraction complete.');
                    cancelBtn.style.display = 'none';
                } else if (data.phase === 'phase3') {
                    _donePhases.add('completeness');
                    setStatus(statusEl, 'Completeness scoring done.');
                    if (completenessBtn) completenessBtn.style.display = 'none';
                }
            },
            onExtractionMessage(data)  { appendExtractionMessage(mainOutput, data); },
            onIndexUpdate(data) {
                updateLiveIndex(mainOutput, data);
                if (data.key === 'project_type' || data.key === 'plan_completeness') {
                    runMetaState[data.key] = data.value;
                    _applyRunMeta();
                }
            },
            onRegionPreview(data)      { addRegionPreview(mainOutput, data); },
            onContextUsage(data)       { updateContextMeter(mainOutput, data); },
            onDone() {
                cancelBtn.style.display = 'none';
                runControlsEl.style.display = 'flex';
                phase5RunBtn.disabled = false;
                if (resumeBtn) resumeBtn.style.display = 'none';
            },
            onError(data) {
                setStatus(statusEl, `Error: ${data.message}`);
                cancelBtn.style.display = 'none';
                runControlsEl.style.display = 'flex';
                phase5RunBtn.disabled = false;
                if (resumeBtn) resumeBtn.disabled = false;
                if (completenessBtn) completenessBtn.disabled = false;
            },
        });
    }

    async function _restorePhase3Log(extractedValues) {
        const hasValues = Object.keys(extractedValues).length > 0;
        try {
            const logRes = await fetch(`/api/quick_proposal/runs/${runId}/phase5_log`, { credentials: 'same-origin' });
            const log = logRes.ok ? await logRes.json() : [];
            if (log.length > 0 || hasValues) {
                renderExtractionPanel(mainOutput);
                for (const entry of log) {
                    if (entry.type === 'extraction_message') appendExtractionMessage(mainOutput, entry);
                    else if (entry.type === 'region_preview')  addRegionPreview(mainOutput, entry);
                }
                for (const [key, meta] of Object.entries(extractedValues)) {
                    updateLiveIndex(mainOutput, { key, value: meta.value, confidence: meta.confidence, source_bbox_id: meta.source_bbox_id });
                }
                // Ensure extraction pane is visible if there's log data
                if (log.length > 0) {
                    const activeStep = overlay.querySelector('.qp-phase-step.active');
                    if (activeStep?.dataset.phase !== 'classification') {
                        overlay.querySelectorAll('.qp-phase-pane').forEach(p => {
                            p.style.display = p.dataset.phase === 'extraction' ? 'flex' : 'none';
                        });
                    }
                }
            }
        } catch (_) { /* log not available — no-op */ }
    }

    async function _handleRunPhase5() {
        phase5RunBtn.disabled = true;
        setStatus(statusEl, 'Starting extraction…', true);
        try {
            const res = await fetch(`/api/quick_proposal/runs/${runId}/phase5`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ manager_model: managerSelect.value, gemini_model: geminiSelect.value, gemini_retry_attempts: parseInt(p3RetryInput.value, 10) || 3, gemini_fallback_models: [], holdout_kp_path: holdoutInput?.value.trim() || '', memory_recall_count: _intOrDefault(p3MemoryRecallInput.value, 12) }),
                credentials: 'same-origin',
            });
            if (!res.ok) throw new Error(`Phase 5 start failed: ${res.status}`);
            const { run_id: streamRunId } = await res.json();
            _startPhase5Stream(streamRunId, false);
        } catch (err) {
            setStatus(statusEl, `Error: ${err.message}`);
            runControlsEl.style.display = 'flex';
            phase5RunBtn.disabled = false;
        }
    }

    async function _handleResumePhase5() {
        resumeBtn.disabled = true;
        setStatus(statusEl, 'Resuming extraction…', true);
        try {
            const res = await fetch(`/api/quick_proposal/runs/${runId}/phase5`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ manager_model: managerSelect.value, gemini_model: geminiSelect.value, gemini_retry_attempts: parseInt(p3RetryInput.value, 10) || 3, gemini_fallback_models: [], holdout_kp_path: holdoutInput?.value.trim() || '', resume: true, memory_recall_count: _intOrDefault(p3MemoryRecallInput.value, 12) }),
                credentials: 'same-origin',
            });
            if (!res.ok) throw new Error(`Resume failed: ${res.status}`);
            const { run_id: streamRunId } = await res.json();
            _startPhase5Stream(streamRunId, true);
        } catch (err) {
            setStatus(statusEl, `Error: ${err.message}`);
            runControlsEl.style.display = 'flex';
            resumeBtn.disabled = false;
        }
    }

    async function _handleRunCompleteness() {
        if (completenessBtn) completenessBtn.disabled = true;
        setStatus(statusEl, 'Running completeness scoring…', true);
        try {
            const res = await fetch(`/api/quick_proposal/runs/${runId}/phase5`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ gemini_model: geminiSelect.value, gemini_retry_attempts: parseInt(p3RetryInput.value, 10) || 3, gemini_fallback_models: [], completeness_only: true }),
                credentials: 'same-origin',
            });
            if (!res.ok) throw new Error(`Completeness start failed: ${res.status}`);
            const { run_id: streamRunId } = await res.json();
            _startPhase5Stream(streamRunId, false);
        } catch (err) {
            setStatus(statusEl, `Error: ${err.message}`);
            if (completenessBtn) completenessBtn.disabled = false;
        }
    }

    phase5RunBtn.addEventListener('click', _handleRunPhase5);
    resumeBtn?.addEventListener('click', _handleResumePhase5);
    completenessBtn?.addEventListener('click', _handleRunCompleteness);

    try {
        const res = await fetch(`/api/quick_proposal/runs/${runId}`, { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const run = await res.json();

        overlay.querySelector('.qp-view-rerun-btn').addEventListener('click', () =>
            onRerun?.(run.upload_id, run.filename || run.upload_id), { once: true });

        if (holdoutInput && run.holdout_kp_path) {
            holdoutInput.value = run.holdout_kp_path;
        }

        // Determine which phases are complete and set initial phase view
        const ev = run.extracted_values || {};
        const hasExtractionValues = Object.keys(ev).some(k => k !== 'project_type' && k !== 'plan_completeness');
        const hasCompleteness = ev.plan_completeness != null;
        const hasClassification = (run.pages || []).some(p => p.sheet_type);

        if (hasExtractionValues) {
            _donePhases.add('classification');
            _donePhases.add('completeness');
            _setActivePhase('extraction');
        } else if (hasCompleteness) {
            _donePhases.add('classification');
            _setActivePhase('completeness');
        } else {
            _setActivePhase('classification');
        }

        // Show run controls when run is not actively running
        if (run.status !== 'running') {
            runControlsEl.style.display = 'flex';
        }

        if (resumeBtn && (run.status === 'error' || run.status === 'cancelled') &&
                Object.keys(ev).length > 0) {
            resumeBtn.style.display = '';
        }

        if (completenessBtn && !ev.plan_completeness) {
            completenessBtn.style.display = '';
        }

        if (ev.project_type?.value)      runMetaState.project_type     = ev.project_type.value;
        if (ev.plan_completeness?.value != null) runMetaState.plan_completeness = ev.plan_completeness.value;
        _applyRunMeta();

        const pages   = run.pages  || [];
        const bboxMap = run.bboxes || {};

        // Reconstruct regions array for a page from saved bbox_ids + bboxes dict
        const buildRegions = p => (p.bbox_ids || []).map(bid => {
            const b = bboxMap[bid];
            if (!b) return null;
            const colonIdx = (b.description || '').indexOf(': ');
            const label = colonIdx >= 0 ? b.description.slice(0, colonIdx) : (b.description || bid);
            const hint  = colonIdx >= 0 ? b.description.slice(colonIdx + 2) : '';
            return { id: bid, label, bbox: [b.x1, b.y1, b.x2, b.y2], extraction_hint: hint, importance: b.importance || 'medium' };
        }).filter(Boolean);

        // Seed pageClassifications so the editor and sidebar pre-populate correctly.
        pages.forEach(p => {
            if (p.sheet_type) {
                pageClassifications[p.idx] = {
                    sheet_type:  p.sheet_type,
                    importance:  p.importance || 'low',
                    description: p.description || '',
                    regions:     buildRegions(p),
                };
            }
        });

        // Callback that immediately persists a single page edit to results.json.
        const persistSave = (pageIdx, sheet_type, importance) => {
            fetch(`/api/quick_proposal/runs/${runId}/classifications`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pages: [{ idx: pageIdx, sheet_type, importance }] }),
                credentials: 'same-origin',
            }).catch(e => console.warn('[quick_proposal] classification save failed:', e));
        };

        pages.forEach(p => {
            const url = `/api/quick_proposal/pages/${runId}/${p.idx}`;
            addThumbnail(sidebarPages, idxMainOutput || mainOutput, p.idx, url);
            if (p.sheet_type) {
                updateThumbnailClassification(sidebarPages, {
                    page_idx:    p.idx,
                    sheet_type:  p.sheet_type,
                    importance:  p.importance || 'low',
                    description: p.description || '',
                    regions:     buildRegions(p),
                }, persistSave);
            }
        });

        placeholder.textContent = pages.length ? 'Click a page to preview' : 'No pages saved for this run.';

        await _restorePhase3Log(run.extracted_values || {});
    } catch (e) {
        placeholder.textContent = `Error loading run: ${e.message}`;
    }

    return overlay;
}

export function buildRunsPanel({ onClose, onSelectRun, onOpenRun }) {
    injectStyles();

    const overlay = document.createElement('div');
    overlay.className = 'qp-overlay';
    overlay.id = 'qp-runs-overlay';

    overlay.innerHTML = `
        <div class="qp-header">
            <span class="qp-title">Quick Proposal — Runs</span>
            <button class="qp-close-btn" id="qp-runs-close-btn" title="Close">✕</button>
        </div>
        <div class="qp-runs-body" id="qp-runs-body">
            <div class="qp-main-placeholder"><span class="qp-spinner"></span>Loading runs…</div>
        </div>
    `;

    overlay.querySelector('#qp-runs-close-btn').addEventListener('click', onClose);
    const body = overlay.querySelector('#qp-runs-body');

    fetch('/api/quick_proposal/runs', { credentials: 'same-origin' })
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then(runs => {
            body.innerHTML = '';
            if (!runs.length) {
                body.innerHTML = `<div class="qp-runs-empty">No proposal runs yet.</div>`;
                return;
            }
            runs.forEach(run => {
                const date = run.timestamp
                    ? new Date(run.timestamp * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
                    : '—';
                const statusClass = `qp-run-status-${run.status || 'unknown'}`;
                const row = document.createElement('div');
                row.className = 'qp-run-row';
                row.innerHTML = `
                    <div class="qp-run-info">
                        <div class="qp-run-filename">${_escHtml(run.run_name || run.filename || run.upload_id)}</div>
                        ${run.run_name && run.filename ? `<div class="qp-run-pdf-name">${_escHtml(run.filename)}</div>` : ''}
                        <div class="qp-run-meta">${_escHtml(date)} · <span class="${statusClass}">${_escHtml(run.status || 'unknown')}</span></div>
                        ${run.notes ? `<div class="qp-run-notes">${_escHtml(run.notes)}</div>` : ''}
                    </div>
                    <div class="qp-run-actions">
                        ${(run.status === 'complete' || run.status === 'error' || run.status === 'cancelled' || run.status === 'running') ? '<button class="qp-run-open-btn">Open</button>' : ''}
                        <button class="qp-run-rerun-btn">Re-run</button>
                        <button class="qp-run-delete-btn" title="Delete run">Delete</button>
                    </div>
                `;
                row.querySelector('.qp-run-open-btn')?.addEventListener('click', () => {
                    onOpenRun?.(run.id || run.run_id);
                });
                row.querySelector('.qp-run-rerun-btn').addEventListener('click', () => {
                    onSelectRun(run.upload_id, run.filename || run.upload_id);
                });
                row.querySelector('.qp-run-delete-btn').addEventListener('click', () => {
                    const label = run.run_name || run.filename || run.run_id;
                    if (!confirm(`Delete run "${label}"? This cannot be undone.`)) return;
                    fetch(`/api/quick_proposal/runs/${run.run_id || run.id}`, {
                        method: 'DELETE', credentials: 'same-origin',
                    }).then(r => {
                        if (!r.ok) throw new Error(`HTTP ${r.status}`);
                        row.remove();
                        if (!body.querySelector('.qp-run-row')) {
                            body.innerHTML = `<div class="qp-runs-empty">No proposal runs yet.</div>`;
                        }
                    }).catch(e => alert(`Delete failed: ${e.message}`));
                });
                body.appendChild(row);
            });
        })
        .catch(e => {
            body.innerHTML = `<div class="qp-runs-empty">Error loading runs: ${_escHtml(e.message)}</div>`;
        });

    return overlay;
}


// TODO_WW: "Jobs" header button (present in both QP overlay headers) deep-links
// to the brain window's Jobs tab. Delegated so it works for every overlay build.
if (!window.__qp_jobs_btn_bound) {
    window.__qp_jobs_btn_bound = true;
    document.addEventListener('click', (e) => {
        if (!e.target.closest?.('.qp-header-jobs-btn')) return;
        import('../qp_jobs.js').then(m => m.openJobsTab && m.openJobsTab());
    });
}
