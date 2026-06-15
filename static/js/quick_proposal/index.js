import { buildPanel, buildRunsPanel, buildViewPanel, buildPromptsPanel } from './ui.js';

let _savedChildren = [];
let _currentPanel  = null;

function _mount(el) {
    _unmount();
    const container = document.getElementById('chat-container');
    if (!container) return;

    _savedChildren = [];
    Array.from(container.children).forEach(child => {
        if (child.style.display === 'none') return;
        child.dataset.qpHidden = '1';
        child.style.display = 'none';
        _savedChildren.push(child);
    });

    _currentPanel = el;
    container.appendChild(el);
    el.style.display = 'flex';
    document.getElementById('rail-quick-proposal')?.classList.add('active');
}

function _unmount() {
    if (_currentPanel?.parentNode) {
        _currentPanel.parentNode.removeChild(_currentPanel);
    }
    _currentPanel = null;
    _savedChildren.forEach(child => {
        delete child.dataset.qpHidden;
        child.style.display = '';
    });
    _savedChildren = [];
    document.getElementById('rail-quick-proposal')?.classList.remove('active');
    document.getElementById('qp-new-proposal-btn')?.classList.remove('active');
    document.getElementById('qp-runs-btn')?.classList.remove('active');
}

function open() {
    _mount(buildPanel({ onClose: close }));
    document.getElementById('qp-new-proposal-btn')?.classList.add('active');
}

function openRuns() {
    _mount(buildRunsPanel({
        onClose: close,
        onSelectRun(uploadId, filename) {
            openWithUpload(uploadId, filename);
        },
        onOpenRun(runId) {
            openView(runId);
        },
    }));
    document.getElementById('qp-runs-btn')?.classList.add('active');
}

async function openView(runId) {
    const panel = await buildViewPanel({
        runId,
        onClose: close,
        onRerun(uploadId, filename) {
            openWithUpload(uploadId, filename);
        },
    });
    _mount(panel);
    document.getElementById('qp-new-proposal-btn')?.classList.add('active');
}

function openWithUpload(uploadId, filename) {
    _mount(buildPanel({ onClose: close, prefillUploadId: uploadId, prefillFilename: filename }));
    document.getElementById('qp-new-proposal-btn')?.classList.add('active');
}

function close() {
    _unmount();
}

async function openPrompts() {
    const panel = await buildPromptsPanel({ onClose: close });
    _mount(panel);
}

document.getElementById('qp-new-proposal-btn')?.addEventListener('click', open);
document.getElementById('qp-runs-btn')?.addEventListener('click', openRuns);
document.getElementById('qp-prompts-btn')?.addEventListener('click', openPrompts);

window.quickProposalModule = { open, close };