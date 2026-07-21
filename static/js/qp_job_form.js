// qp_job_form.js — friendly form editor for a case-library job record.
// Renders the human-meaningful, derivation-relevant fields as labelled inputs
// + an editable line-item table, and merges edits back into the FULL original
// JSON so every machine-generated field (quantities_dwg, derived.dollar_per_unit,
// links, *_raw mirrors, …) is preserved untouched. All form↔JSON mapping lives
// here so qp_jobs.js never touches raw JSON.
//
// A record can hold MULTIPLE proposal revisions in `proposals[]`, with
// `primary_proposal_index` marking the authoritative one (the one pricing,
// derivation and actuals-pairing read). The Proposal section exposes a selector
// to switch which revision you're editing, a ★ "Set as primary" toggle, and
// Add/Delete. All proposal-scoped edits are staged into an in-memory working
// record (host.__qpJobForm.record) so switching between revisions never loses
// unsaved edits; collectJobForm() flushes the visible revision and returns it.

const DEFAULT_JOB_TYPES = ['subdivision_road', 'private_drive', 'commercial_site', 'road_widening', 'mixed_use'];
const JOB_TYPES_LS_KEY  = 'qp_job_types_custom';   // user-added job types (persisted per browser)
const DEFAULT_REVISION_LABEL  = 'base revision';   // default label for a new/first revision
const DEFAULT_REVISION_LABELS = [DEFAULT_REVISION_LABEL, 'template', 'final', 'rev', 'rev2'];
const REV_LABELS_LS_KEY = 'qp_revision_labels_custom';   // user-added revision labels
const UNITS       = ['LS', 'EA', 'LF', 'SF', 'SY', 'CY', 'HR', 'TON'];
const UNITS_LS_KEY = 'qp_line_units_custom';        // user-added line-item units

function _esc(v) {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const _num = (v) => (v === null || v === undefined || v === '' ? '' : v);

// A line item's tax_rate is stored as a decimal fraction (0.075) but the table
// shows it as a whole-number percent (7.5). Convert fraction → percent for
// display, stripping float noise (0.075*100 = 7.500000000000001) and trailing
// zeros. Missing/zero renders as "0" since tax defaults to 0.
function _taxPctDisplay(item) {
  const r = item && item.tax_rate;
  const n = Number(r);
  if (r === null || r === undefined || r === '' || !Number.isFinite(n) || n === 0) return '0';
  return String(+(n * 100).toFixed(4));
}

// Total $ is calculated, never entered: $ per Unit × (1 + tax fraction) × Qty.
// `taxRate` is the stored decimal fraction (0.075); missing tax → 0. Returns ''
// when unit price or qty is blank/non-numeric so the cell stays empty.
function _lineTotal(unitPrice, taxRate, qty) {
  const u = Number(unitPrice), q = Number(qty);
  if (!Number.isFinite(u) || !Number.isFinite(q) || unitPrice === '' || unitPrice == null
      || qty === '' || qty == null) return '';
  const t = Number(taxRate);
  const tax = Number.isFinite(t) ? t : 0;
  return +(u * (1 + tax) * q).toFixed(2);
}

// ── path helpers (merge form values back into the cloned record) ─────────────

function _clone(obj) {
  return typeof structuredClone === 'function' ? structuredClone(obj)
    : JSON.parse(JSON.stringify(obj));
}

function _ensureObj(parent, key) {
  if (parent[key] === null || typeof parent[key] !== 'object' || Array.isArray(parent[key])) {
    parent[key] = {};
  }
  return parent[key];
}

function _setPath(root, dottedPath, value) {
  const parts = dottedPath.split('.');
  let cur = root;
  for (let i = 0; i < parts.length - 1; i++) cur = _ensureObj(cur, parts[i]);
  cur[parts[parts.length - 1]] = value;
}

function _parseScalar(input) {
  const raw = input.value.trim();
  const nullable = input.dataset.jfNullable === '1';
  if (input.dataset.jfNum === '1') {
    if (raw === '') return nullable ? null : 0;
    const n = Number(raw);
    if (Number.isNaN(n)) throw new Error(`"${input.dataset.jfLabel || input.dataset.jfPath}" must be a number`);
    return n;
  }
  if (raw === '') return nullable ? null : '';
  return raw;
}

// ── field spec (label + path/prop + type), grouped into sections ─────────────
// Record-level sections (dotted paths). The Proposal section is rendered
// separately (PROPOSAL_FIELDS) because it's scoped to the selected revision.

const SECTIONS = [
  {
    title: 'Details',
    fields: [
      { label: 'Job Name',        path: 'job_name',                required: true },
      { label: 'Client',          path: 'identity.client',         nullable: true },
      { label: 'Client Location', path: 'identity.client_location', nullable: true },
      { label: 'Engineering Firm', path: 'identity.engineering_firm', nullable: true },
      { label: 'Job Location',    path: 'identity.job_location',   nullable: true },
      { label: 'Job Type',        path: 'classification.job_type', select: true,
        vocab: { defaults: DEFAULT_JOB_TYPES, lsKey: JOB_TYPES_LS_KEY, optsKey: 'jobTypes' } },
    ],
  },
  {
    title: 'Scale Metrics',
    hint: 'Numeric drivers used to derive knowledge-pack rules. Leave blank if unknown.',
    fields: [
      { label: 'Lot Count',         path: 'derived.scale_metrics.lot_count',        num: true, nullable: true },
      { label: 'Parcel Count',      path: 'derived.scale_metrics.parcel_count',      num: true, nullable: true },
      { label: 'Road LF Total',     path: 'derived.scale_metrics.road_LF',           num: true, nullable: true },
      { label: 'Road LF Fronting Existing Roads', path: 'derived.scale_metrics.fronting_LF', num: true, nullable: true },
      { label: 'ROW SF',            path: 'derived.scale_metrics.ROW_SF',           num: true, nullable: true },
      { label: 'Lot Area SF',       path: 'derived.scale_metrics.lot_area_sf',      num: true, nullable: true },
      { label: 'Stripping Depth (in)', path: 'derived.scale_metrics.stripping_depth_in', num: true, nullable: true },
      { label: 'Road Subgrade SY',  path: 'derived.scale_metrics.road_subgrade_SY',  num: true, nullable: true,
        tip: 'Road subgrade area (SY) — read from the plan set’s quantity sheet.' },
      { label: 'Road Paving SY',    path: 'derived.scale_metrics.road_paving_SY',    num: true, nullable: true,
        tip: 'Total road paving quantity (SY) — read from the plan set’s quantity sheet.' },
      { label: 'Ballast CY',        path: 'derived.scale_metrics.ballast_CY',        num: true, nullable: true,
        tip: 'Ballast quantity (CY) — calculated from the plans.' },
    ],
  },
];

// Proposal-scoped fields (addressed by `prop` key on the selected revision)
const PROPOSAL_FIELDS = [
  { label: 'Revision Label',  prop: 'revision_label', select: true, allowBlank: true,
    vocab: { defaults: DEFAULT_REVISION_LABELS, lsKey: REV_LABELS_LS_KEY } },
  { label: 'Proposal Date',   prop: 'proposal_date', placeholder: 'YYYY-MM-DD' },
  { label: 'Grand Total ($)', prop: 'grand_total', num: true, nullable: true, calc: true },
];

// Registry of every dropdown field, keyed by its stable selkey (path or prop),
// so the shared populate/add-value machinery can look up each one's vocabulary.
const SELECT_CONFIG = {};
[...SECTIONS.flatMap(s => s.fields), ...PROPOSAL_FIELDS].forEach(f => {
  if (f.select) SELECT_CONFIG[f.path || f.prop] = f;
});

// dotted-path getter for reading initial values out of the record
function _get(root, dotted) {
  return dotted.split('.').reduce((o, k) => (o == null ? undefined : o[k]), root);
}

// ── dropdown vocabularies (defaults ∪ existing records ∪ user-added) ──────────
// Shared by every `select` field (Job Type, Revision Label). User-added values
// persist per-browser under each field's localStorage key.

function _loadCustomVocab(lsKey) {
  if (!lsKey) return [];
  try { const v = JSON.parse(localStorage.getItem(lsKey) || '[]'); return Array.isArray(v) ? v : []; }
  catch { return []; }
}
function _saveCustomVocab(lsKey, arr) {
  if (!lsKey) return;
  try { localStorage.setItem(lsKey, JSON.stringify(arr)); } catch { /* ignore */ }
}
// normalize to the snake_case lowercase convention the existing values use
function _normToken(s) {
  return String(s).trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '').replace(/_+/g, '_').replace(/^_|_$/g, '');
}
// Line-item units/categories use UPPERCASE tokens (LF, SIGNS_STRIPING). Normalize
// a user-typed value to that convention so a new entry matches the existing ones.
function _upperToken(s) {
  return String(s).trim().toUpperCase().replace(/\s+/g, '_').replace(/[^A-Z0-9_]/g, '').replace(/_+/g, '_').replace(/^_|_$/g, '');
}

// ── render ───────────────────────────────────────────────────────────────────

function _fieldHtml(f, content, prop) {
  const val = f.prop ? (prop ? prop[f.prop] : undefined) : _get(content, f.path);
  const dataAttrs = [
    f.path ? `data-jf-path="${f.path}"` : '',
    f.prop ? `data-jf-prop="${f.prop}"` : '',
    `data-jf-label="${_esc(f.label)}"`,
  ].filter(Boolean).join(' ');

  // Dropdown field (Job Type, Revision Label): a <select> populated later + an
  // add-value button. Keyed on `path || prop` so the shared wiring finds it.
  // Not wrapped in <label> so the button doesn't re-focus the select on click.
  if (f.select) {
    const key = f.path || f.prop;
    return `<div class="qp-jf-field">
      <span class="qp-jf-label">${_esc(f.label)}${f.required ? ' *' : ''}<button type="button" class="qp-jf-add-opt" data-add-for="${key}" title="Add a new ${_esc(f.label)}" aria-label="Add a new ${_esc(f.label)}">+</button></span>
      <select class="qp-jf-input qp-jf-select" data-jf-selkey="${key}" ${dataAttrs}></select>
      <div class="qp-jf-add-box hidden" data-add-box-for="${key}">
        <input type="text" class="qp-jf-add-input" placeholder="new ${_esc(f.label).toLowerCase()}…" spellcheck="false">
        <button type="button" class="qp-jf-add-confirm" title="Add">✓</button>
        <button type="button" class="qp-jf-add-cancel" title="Cancel">✕</button>
      </div>
    </div>`;
  }

  // Calculated field (Grand Total): read-only + fully inert, value derived from
  // the line items. Still carries data-jf-prop so flush/collect persist it.
  if (f.calc) {
    return `<label class="qp-jf-field">
      <span class="qp-jf-label">${_esc(f.label)}</span>
      <input type="text" class="qp-jf-input qp-jf-calc" value="${_esc(_num(val))}" ${dataAttrs} data-jf-num="1" data-jf-nullable="1" readonly tabindex="-1" title="Calculated from the line item totals (optional items excluded)" spellcheck="false">
    </label>`;
  }

  const attrs = [
    dataAttrs,
    f.num ? 'data-jf-num="1" inputmode="decimal"' : '',
    f.nullable ? 'data-jf-nullable="1"' : '',
    f.placeholder ? `placeholder="${_esc(f.placeholder)}"` : '',
    f.required ? 'data-jf-required="1"' : '',
  ].filter(Boolean).join(' ');
  // Optional hover help: a title on the label (hovering the name shows it) plus a
  // small ⓘ cue so the user knows there's an explanation.
  const tipSpan = f.tip ? ` <span class="qp-jf-info" title="${_esc(f.tip)}">ⓘ</span>` : '';
  const labelTitle = f.tip ? ` title="${_esc(f.tip)}"` : '';
  return `<label class="qp-jf-field">
      <span class="qp-jf-label"${labelTitle}>${_esc(f.label)}${f.required ? ' *' : ''}${tipSpan}</span>
      <input type="text" class="qp-jf-input" value="${_esc(_num(val))}" ${attrs} spellcheck="false">
    </label>`;
}

function _recordSectionHtml(sec, content) {
  return `<fieldset class="qp-jf-section">
      <legend class="qp-jf-legend">${_esc(sec.title)}</legend>
      ${sec.hint ? `<p class="qp-jf-hint">${_esc(sec.hint)}</p>` : ''}
      <div class="qp-jf-grid">
        ${sec.fields.map(f => _fieldHtml(f, content, null)).join('')}
      </div>
    </fieldset>`;
}

function _proposalSectionHtml(prop) {
  return `<fieldset class="qp-jf-section">
      <legend class="qp-jf-legend">Revision</legend>
      <p class="qp-jf-hint">A job can have multiple revisions. Pick which one to edit; the ★ marks the authoritative one used for pricing &amp; comparisons.</p>
      <div class="qp-jf-prop-bar">
        <label class="qp-jf-prop-pick">
          <span class="qp-jf-prop-pick-label">Editing revision</span>
          <select id="qp-jf-prop-select" class="qp-jf-input qp-jf-prop-select-input"></select>
        </label>
        <button type="button" id="qp-jf-set-primary" class="qp-jf-set-primary" title="Mark the selected revision as this job's authoritative revision"></button>
        <button type="button" id="qp-jf-add-prop" class="qp-jf-prop-btn" title="Add a new revision">+ Add</button>
        <button type="button" id="qp-jf-del-prop" class="qp-jf-prop-btn qp-jf-prop-del" title="Delete the selected revision">Delete</button>
      </div>
      <div class="qp-jf-grid">
        ${PROPOSAL_FIELDS.map(f => _fieldHtml(f, null, prop)).join('')}
      </div>
    </fieldset>`;
}

// ── line-item Unit / Category dropdown vocabularies ───────────────────────────
// Category and Unit are constrained pickers (not free text). Each cell is an
// in-app combobox — a value button that opens the SAME overlay picker as "Add
// a line item" (search a list of existing values + create a new one), rather
// than a native <select> with fixed browser chrome.
//
// Unit's vocabulary is defaults ∪ every value already used in this record ∪
// user-added values (persisted per-browser) — built once per form open and
// staged on the host.
//
// Category's vocabulary is sourced from the database instead of a hardcoded
// list — every category value actually in use across the whole case library
// (see GET /case_library/categories), same principle as the Description
// picker's DB-backed catalog (GET /case_library/line_items). It's fetched
// once per form open (see _loadCategoryVocab) and merged into the live array
// so an in-flight picker open sees it land; no per-browser persistence.
//
// Either way the picker reads the live array so a newly-created value is
// immediately offered everywhere in the open form.

function _buildLineVocab(defaults, lsKey, record, field) {
  const out = [];
  const add = (v) => { const n = _upperToken(v); if (n && !out.includes(n)) out.push(n); };
  defaults.forEach(add);
  _loadCustomVocab(lsKey).forEach(add);
  (record?.proposals || []).forEach(p => (p?.line_items || []).forEach(li => add(li?.[field])));
  return out;
}

async function _fetchCategoryCatalog() {
  try {
    const r = await fetch('/api/quick_proposal/case_library/categories', { credentials: 'same-origin' });
    if (!r.ok) return [];
    const list = await r.json();
    return Array.isArray(list) ? list : [];
  } catch { return []; }
}

// Populate host.__qpCatVocab (pre-seeded synchronously with this record's own
// values by renderJobForm) with every category used anywhere in the case
// library. Fire-and-forget from renderJobForm — the picker only reads the
// array when opened, well after this resolves in practice.
async function _loadCategoryVocab(host) {
  const fetched = await _fetchCategoryCatalog();
  const vocab = host.__qpCatVocab || (host.__qpCatVocab = []);
  fetched.forEach(v => { const n = _upperToken(v); if (n && !vocab.includes(n)) vocab.push(n); });
}

function _rowVocab(host) {
  return { units: host.__qpUnitVocab || UNITS, cats: host.__qpCatVocab || [] };
}

// One line-item picker cell: a visible value button (styled like the inline
// inputs) + a hidden input holding the value (so _collectLineItems reads it via
// the same `.qp-jf-cat`/`.qp-jf-unit` selector as before). The button opens the
// overlay picker; `kindCls` differentiates the column width in CSS.
function _comboCell(cls, kindCls, current) {
  const cur = current == null ? '' : String(current).toUpperCase();
  return `<div class="qp-jf-combo ${kindCls}">
    <button type="button" class="qp-jf-combo-btn${cur ? '' : ' is-empty'}" title="Choose from the list or add a new value"><span class="qp-jf-combo-val">${cur ? _esc(cur) : '—'}</span></button>
    <input type="hidden" class="${cls}" value="${_esc(cur)}">
  </div>`;
}

// Description cell: a picker button (opens the library overlay to choose/create
// a line item) + a hidden input holding the value, so it can't be free-typed.
// Unlike Unit/Category the value is kept verbatim (descriptions aren't tokens).
function _descCell(current) {
  const cur = current == null ? '' : String(current);
  return `<div class="qp-jf-combo qp-jf-combo-desc">
    <button type="button" class="qp-jf-combo-btn qp-jf-desc-btn${cur ? '' : ' is-empty'}" title="Choose a line item from the library or create a new one"><span class="qp-jf-combo-val">${cur ? _esc(cur) : 'Choose a line item…'}</span></button>
    <input type="hidden" class="qp-jf-desc" value="${_esc(cur)}">
  </div>`;
}

function _lineItemRowHtml(item = {}, vocab = { units: UNITS, cats: [] }) {
  return `<tr class="qp-jf-item">
    <td class="qp-jf-sel-cell"><input type="checkbox" class="qp-jf-sel" title="Select for bulk tax rate apply"></td>
    <td>${_descCell(item.description)}</td>
    <td>${_comboCell('qp-jf-cat', 'qp-jf-combo-cat', item.category)}</td>
    <td><input class="qp-jf-qty" value="${_esc(_num(item.qty))}" inputmode="decimal" placeholder="0"></td>
    <td>${_comboCell('qp-jf-unit', 'qp-jf-combo-unit', item.unit)}</td>
    <td><input class="qp-jf-up" value="${_esc(_num(item.unit_price))}" inputmode="decimal" placeholder="0"></td>
    <td><input class="qp-jf-tax" value="${_esc(_taxPctDisplay(item))}" inputmode="decimal" placeholder="0" title="Tax rate for this item, as a percent (e.g. 7 or 7.5)"></td>
    <td><input class="qp-jf-ext" value="${_esc(_num(_lineTotal(item.unit_price, item.tax_rate, item.qty)))}" inputmode="decimal" placeholder="0" readonly tabindex="-1" title="Total = $ per Unit × (1 + Tax %) × Qty (calculated, not editable)"></td>
    <td class="qp-jf-opt-cell"><input type="checkbox" class="qp-jf-opt" ${item.is_optional ? 'checked' : ''} title="Optional item"></td>
    <td><button type="button" class="qp-jf-row-del" title="Remove line item">✕</button></td>
  </tr>`;
}

// ── proposal working-state helpers (in-memory, staged on the host element) ────

function _proposalLabel(prop, i) {
  const lbl = prop && typeof prop.revision_label === 'string' ? prop.revision_label.trim() : '';
  return lbl || `revision ${i + 1}`;
}

function _updateCount(host) {
  const el = host.querySelector('#qp-jf-item-count');
  const tbody = host.querySelector('#qp-jf-items');
  if (el && tbody) el.textContent = `(${tbody.querySelectorAll('.qp-jf-item').length})`;
  _syncItemsEmpty(host);
}

// With no placeholder row anymore, show a friendly empty-state line while the
// proposal has zero line items so the table doesn't read as blank/broken. The
// empty row carries no `.qp-jf-item` class, so _collectLineItems never sees it.
const _EMPTY_ROW_HTML =
  '<tr class="qp-jf-items-empty"><td colspan="10">No line items yet — use “+ Add line item” below to add one.</td></tr>';
function _syncItemsEmpty(host) {
  const tbody = host.querySelector('#qp-jf-items');
  if (!tbody) return;
  const hasRows = tbody.querySelector('.qp-jf-item');
  const emptyRow = tbody.querySelector('.qp-jf-items-empty');
  if (!hasRows && !emptyRow) tbody.insertAdjacentHTML('beforeend', _EMPTY_ROW_HTML);
  else if (hasRows && emptyRow) emptyRow.remove();
}

// Add a value to a line-item vocabulary array (no DOM work — the picker renders
// from the live array). Returns the normalized token, or '' if empty.
function _ensureVocab(host, isUnit, rawValue) {
  const v = _upperToken(rawValue);
  if (!v) return '';
  const vocab = isUnit ? (host.__qpUnitVocab ||= []) : (host.__qpCatVocab ||= []);
  if (!vocab.includes(v)) vocab.push(v);
  return v;
}

// ── Category / Unit value-picker overlay (mirrors the "Add a line item" picker) ─
// A single overlay, reused for whichever cell was clicked. State (which hidden
// input + which vocabulary) is staged on host.__qpVp while it's open.

const _vpLabel = (isUnit) => (isUnit ? 'unit' : 'category');
const _vpVocab = (host, isUnit) => (isUnit ? host.__qpUnitVocab : host.__qpCatVocab) || [];

// Write a chosen/created value into a combo cell (hidden input + visible button).
function _applyComboValue(input, val) {
  if (!input) return;
  input.value = val || '';
  const label = input.parentElement?.querySelector('.qp-jf-combo-val');
  const btn = input.parentElement?.querySelector('.qp-jf-combo-btn');
  if (label) label.textContent = val || '—';
  if (btn) btn.classList.toggle('is-empty', !val);
}

function _renderVpList(host, query) {
  const list = host.querySelector('#qp-jf-vp-list');
  if (!list) return;
  const st = host.__qpVp || {};
  const vocab = _vpVocab(host, st.isUnit);
  const q = String(query || '').trim().toUpperCase();
  const items = q ? vocab.filter(v => v.includes(q)) : vocab.slice();
  const cur = String(st.input?.value || '').toUpperCase();
  const row = (val, label, muted) =>
    `<button type="button" class="qp-jf-li-row qp-jf-vp-row" data-val="${_esc(val)}">
      <span class="qp-jf-li-row-desc"${muted ? ' style="opacity:0.6"' : ''}>${_esc(label)}</span>
      ${val.toUpperCase() === cur ? '<span class="qp-jf-li-row-meta">current</span>' : ''}
    </button>`;
  const rows = [];
  if (!q) rows.push(row('', '— None —', true));          // clear the value
  items.forEach(v => rows.push(row(v, v, false)));
  if (!rows.length) {
    list.innerHTML = `<div class="qp-jf-li-empty">No matching ${_vpLabel(st.isUnit)} — create it below.</div>`;
    return;
  }
  list.innerHTML = rows.join('');
}

// Live block/allow for the create box: block an exact existing value, else allow.
function _updateVpCreateState(host) {
  const input = host.querySelector('#qp-jf-vp-new');
  const btn   = host.querySelector('#qp-jf-vp-create-btn');
  const hint  = host.querySelector('#qp-jf-vp-create-hint');
  if (!input || !btn || !hint) return;
  const st = host.__qpVp || {};
  const raw  = input.value.trim();
  const norm = _upperToken(raw);
  const exists = !!norm && _vpVocab(host, st.isUnit).includes(norm);
  btn.disabled = !norm || exists;
  if (!raw)        { hint.textContent = ''; hint.className = 'qp-jf-li-create-hint'; }
  else if (exists) { hint.textContent = `“${norm}” already exists — pick it from the list.`; hint.className = 'qp-jf-li-create-hint is-block'; }
  else             { hint.textContent = `Create new ${_vpLabel(st.isUnit)} “${norm}”.`; hint.className = 'qp-jf-li-create-hint is-ok'; }
}

function _closeVpOverlay(host) {
  host.querySelector('#qp-jf-vp-overlay')?.classList.add('hidden');
  host.__qpVp = null;
}

function _openVpOverlay(host, input, isUnit) {
  const overlay = host.querySelector('#qp-jf-vp-overlay');
  if (!overlay) return;
  host.__qpVp = { input, isUnit };
  host.querySelector('#qp-jf-vp-title').textContent = `Choose a ${_vpLabel(isUnit)}`;
  const search = host.querySelector('#qp-jf-vp-search');
  const neu = host.querySelector('#qp-jf-vp-new');
  search.value = ''; neu.value = '';
  neu.placeholder = `Or create a new ${_vpLabel(isUnit)}…`;
  overlay.classList.remove('hidden');
  _renderVpList(host, '');
  _updateVpCreateState(host);
  search.focus({ preventScroll: true });   // don't yank the scrolled form to the picker
}

// Create a brand-new value, fold it into the vocab, apply to the cell. A new
// Unit is persisted per-browser (like Job Type/Revision Label); a new
// Category is not — it becomes part of the DB-wide catalog once this job is
// saved, same as a newly-created line-item description.
function _vpDoCreate(host) {
  const st = host.__qpVp;
  if (!st) return;
  const raw = host.querySelector('#qp-jf-vp-new').value;
  const v = _upperToken(raw);
  _updateVpCreateState(host);
  if (!v || _vpVocab(host, st.isUnit).includes(v)) return;   // empty or exact duplicate → blocked
  if (st.isUnit) {
    const custom = _loadCustomVocab(UNITS_LS_KEY);
    if (!custom.includes(v)) { custom.push(v); _saveCustomVocab(UNITS_LS_KEY, custom); }
  }
  _ensureVocab(host, st.isUnit, v);
  _applyComboValue(st.input, v);
  _closeVpOverlay(host);
}

// Grand Total is calculated: sum of every line item's Total $, excluding
// optional items. Blank when no line item has a numeric total. Keeps the
// read-only grand_total input in sync so flush/collect persist the sum.
function _recalcGrandTotal(host) {
  const gt = host.querySelector('[data-jf-prop="grand_total"]');
  if (!gt) return;
  let sum = 0, any = false;
  host.querySelectorAll('#qp-jf-items .qp-jf-item').forEach(row => {
    if (row.querySelector('.qp-jf-opt')?.checked) return;   // optional → not in grand total
    const v = parseFloat(row.querySelector('.qp-jf-ext').value);
    if (Number.isFinite(v)) { sum += v; any = true; }
  });
  gt.value = any ? +sum.toFixed(2) : '';
}

// ── "add line item" library picker (overlay over the line-item table) ─────────

function _debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// Same normalization rule as the backend `_norm_line_item_desc` so dedup/match
// agree: lowercase, trim, collapse inner whitespace (digits/quotes preserved).
function _normLineItemDesc(s) {
  return String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

async function _fetchLineItemCatalog() {
  try {
    const r = await fetch('/api/quick_proposal/case_library/line_items', { credentials: 'same-origin' });
    if (!r.ok) return [];
    const list = await r.json();
    return Array.isArray(list) ? list : [];
  } catch { return []; }
}

// Set of descriptions that already "exist" for duplicate-blocking on create:
// the library catalog PLUS whatever rows are already in the current proposal
// (so an unsaved just-added item can't be created twice in one session).
function _existingDescSet(host) {
  const set = new Set((host.__qpLiCatalog || []).map(it => _normLineItemDesc(it.description)));
  host.querySelectorAll('#qp-jf-items .qp-jf-desc').forEach(inp => {
    const n = _normLineItemDesc(inp.value);
    if (n) set.add(n);
  });
  return set;
}

function _renderLiList(host, query) {
  const list = host.querySelector('#qp-jf-li-list');
  if (!list) return;
  const catalog = host.__qpLiCatalog || [];
  const q = _normLineItemDesc(query);
  const items = q ? catalog.filter(it => _normLineItemDesc(it.description).includes(q)) : catalog;
  if (!items.length) {
    list.innerHTML = `<div class="qp-jf-li-empty">${catalog.length ? 'No matching line items.' : 'No line items in the library yet.'}</div>`;
    return;
  }
  list.innerHTML = items.map(it => `
    <button type="button" class="qp-jf-li-row" data-desc="${_esc(it.description)}" data-unit="${_esc(it.unit || '')}" data-cat="${_esc(it.category || '')}">
      <span class="qp-jf-li-row-desc">${_esc(it.description)}</span>
      <span class="qp-jf-li-row-meta">${_esc(it.unit || '')}${it.category ? ' · ' + _esc(it.category) : ''}</span>
      <span class="qp-jf-li-row-count" title="Used in ${it.job_count} job(s)">${it.job_count}</span>
    </button>`).join('');
}

// Reflect the create box's live existence check: block (disable) on an exact
// normalized match, otherwise allow creating a brand-new line item.
function _updateCreateState(host) {
  const input = host.querySelector('#qp-jf-li-new');
  const btn   = host.querySelector('#qp-jf-li-create-btn');
  const hint  = host.querySelector('#qp-jf-li-create-hint');
  if (!input || !btn || !hint) return;
  const raw  = input.value.trim();
  const norm = _normLineItemDesc(raw);
  const exists = !!norm && _existingDescSet(host).has(norm);
  btn.disabled = !norm || exists;
  if (!raw)        { hint.textContent = ''; hint.className = 'qp-jf-li-create-hint'; }
  else if (exists) { hint.textContent = `“${raw}” already exists — pick it from the list above.`; hint.className = 'qp-jf-li-create-hint is-block'; }
  else             { hint.textContent = `Create new line item “${raw}”.`; hint.className = 'qp-jf-li-create-hint is-ok'; }
}

// Append a new line-item row (optionally pre-filled) and refresh count/total.
function _addLineItemRow(host, fields = {}) {
  const tbody = host.querySelector('#qp-jf-items');
  // A library-picked item may carry a unit/category not yet in the shared vocab;
  // fold them in first so this and every other cell's picker offers them.
  if (fields.unit) _ensureVocab(host, true, fields.unit);
  if (fields.category) _ensureVocab(host, false, fields.category);
  tbody.insertAdjacentHTML('beforeend', _lineItemRowHtml(fields, _rowVocab(host)));
  _updateCount(host);
  _recalcGrandTotal(host);
  tbody.lastElementChild.querySelector('.qp-jf-qty')?.focus();
}

// Apply a library pick/creation. If the overlay was opened to edit a row's
// Description (host.__qpLiTarget set), update that row in place; otherwise append
// a new row. A catalog pick carries unit/category; a fresh creation carries only
// the description (leaving the row's unit/category untouched).
function _applyLiPick(host, fields) {
  const target = host.__qpLiTarget;
  if (!target) { _addLineItemRow(host, fields); return; }
  const rowEl = target.closest('tr');
  _applyComboValue(target, fields.description || '');
  if (rowEl && fields.unit) {
    const u = _ensureVocab(host, true, fields.unit);
    _applyComboValue(rowEl.querySelector('.qp-jf-unit'), u);
  }
  if (rowEl && fields.category) {
    const c = _ensureVocab(host, false, fields.category);
    _applyComboValue(rowEl.querySelector('.qp-jf-cat'), c);
  }
}

function _closeLiOverlay(host) {
  host.querySelector('#qp-jf-li-overlay')?.classList.add('hidden');
  host.__qpLiTarget = null;
}

// Copy the visible line items as a TSV (header + one row each) to the clipboard —
// tabs/newlines paste straight into Google Sheets / Excel as a grid. Reads the
// live DOM (incl. the calculated Total $) so it matches exactly what's on screen.
async function _copyLineItemsTable(host) {
  const headers = ['Description', 'Category', 'Qty', 'Unit', '$ per Unit', 'Tax %', 'Total $', 'Optional'];
  const bodyRows = [];   // each entry is an array of cell strings
  let nonTaxed = 0, taxed = 0;   // Total $ split by whether the row carries a tax rate
  host.querySelectorAll('#qp-jf-items .qp-jf-item').forEach(row => {
    const cell = (sel) => String(row.querySelector(sel)?.value ?? '').trim();
    bodyRows.push([
      cell('.qp-jf-desc'), cell('.qp-jf-cat'), cell('.qp-jf-qty'), cell('.qp-jf-unit'),
      cell('.qp-jf-up'), cell('.qp-jf-tax'), cell('.qp-jf-ext'),
      row.querySelector('.qp-jf-opt')?.checked ? 'Yes' : '',
    ]);
    const total = parseFloat(cell('.qp-jf-ext'));
    if (Number.isFinite(total)) {
      const taxPct = parseFloat(cell('.qp-jf-tax'));
      if (Number.isFinite(taxPct) && taxPct !== 0) taxed += total;
      else nonTaxed += total;
    }
  });

  const btn = host.querySelector('#qp-jf-copy-table');
  const flash = (msg) => {
    if (!btn) return;
    btn.textContent = msg;
    setTimeout(() => { btn.textContent = 'Copy as table'; }, 1200);
  };
  if (!bodyRows.length) { flash('No items'); return; }

  // Summary rows (label + value) below the table — paste into cols A/B. Optional
  // items are intentionally included in these totals (user preference — the
  // pasted table should reflect everything on the proposal, not just the
  // non-optional Grand Total).
  bodyRows.push(['']);                                         // blank separator row
  bodyRows.push(['total non taxed:', nonTaxed.toFixed(2)]);
  bodyRows.push(['total taxed:',     taxed.toFixed(2)]);
  bodyRows.push(['total',            (nonTaxed + taxed).toFixed(2)]);

  // Plain-text TSV — the fallback flavor + what non-spreadsheet targets receive.
  const clean = (v) => String(v).replace(/\t/g, ' ').replace(/\r?\n/g, ' ');
  const tsv = [headers, ...bodyRows].map(r => r.map(clean).join('\t')).join('\n');

  // HTML flavor — a real table whose header cells are <th> (bold), so Google
  // Sheets / Excel paste the column names in bold. Cells are HTML-escaped.
  const thead = `<tr>${headers.map(h => `<th>${_esc(h)}</th>`).join('')}</tr>`;
  const tbody = bodyRows.map(r => `<tr>${r.map(c => `<td>${_esc(c)}</td>`).join('')}</tr>`).join('');
  const html = `<table><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;

  try {
    // Rich clipboard keeps the bold header; plain text rides along for other apps.
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html':  new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([tsv],  { type: 'text/plain' }),
      })]);
    } else {
      await navigator.clipboard.writeText(tsv);
    }
    flash('Copied!');
  } catch {
    // fallback for non-secure contexts / older browsers (plain text only)
    try {
      const ta = document.createElement('textarea');
      ta.value = tsv;
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      flash('Copied!');
    } catch { flash('Copy failed'); }
  }
}

// targetInput=null → add a new row; otherwise edit that row's Description input.
async function _openLiOverlay(host, targetInput = null) {
  const overlay = host.querySelector('#qp-jf-li-overlay');
  if (!overlay) return;
  host.__qpLiTarget = targetInput;
  const title = host.querySelector('#qp-jf-li-title');
  if (title) title.textContent = targetInput ? 'Change line item' : 'Add a line item';
  host.querySelector('#qp-jf-li-search').value = '';
  host.querySelector('#qp-jf-li-new').value = '';
  host.querySelector('#qp-jf-li-list').innerHTML = '<div class="qp-jf-li-empty">Loading…</div>';
  overlay.classList.remove('hidden');
  _updateCreateState(host);
  host.querySelector('#qp-jf-li-search').focus({ preventScroll: true });   // keep the form's scroll position
  host.__qpLiCatalog = await _fetchLineItemCatalog();
  _renderLiList(host, '');
}

function _rowNum(row, sel, lenient) {
  const raw = row.querySelector(sel).value.trim();
  if (raw === '') return null;
  const n = Number(raw);
  if (Number.isNaN(n)) {
    if (lenient) return null;
    throw new Error(`"${raw}" in a line item is not a number`);
  }
  return n;
}

function _collectLineItems(host, lenient) {
  // rows with a blank description are dropped
  const items = [];
  host.querySelectorAll('#qp-jf-items .qp-jf-item').forEach(row => {
    const desc = row.querySelector('.qp-jf-desc').value.trim();
    if (!desc) return;
    const qty = _rowNum(row, '.qp-jf-qty', lenient);
    const up  = _rowNum(row, '.qp-jf-up', lenient);
    const ext = _rowNum(row, '.qp-jf-ext', lenient);
    // tax is entered as a whole-number percent; blank defaults to 0. Sent as the
    // percent the user typed — the backend route converts it to a decimal
    // fraction before persisting (see _normalize_case_tax_rates).
    const taxPct = _rowNum(row, '.qp-jf-tax', lenient);
    // Category/Unit values live in the combo cell's hidden input.
    const _sel = (cls) => row.querySelector(cls).value.trim() || null;
    items.push({
      description: desc,
      category:    _sel('.qp-jf-cat'),
      qty, qty_raw: qty,
      unit:        _sel('.qp-jf-unit'),
      unit_price:  up, unit_price_raw: up,
      ext_price:   ext, ext_price_raw: ext,
      tax_rate:    taxPct == null ? 0 : taxPct,
      is_optional: row.querySelector('.qp-jf-opt').checked,
      mismatch:    false,
    });
  });
  return items;
}

// Write the visible proposal-scoped inputs into record.proposals[editingIdx].
// validate=false (switching) tolerates bad numbers by keeping the prior value;
// validate=true (saving) lets _parseScalar throw so the user sees the error.
function _flushProposalDom(host, st, validate) {
  const props = st.record.proposals || (st.record.proposals = []);
  if (!props[st.editingIdx] || typeof props[st.editingIdx] !== 'object') props[st.editingIdx] = {};
  const prop = props[st.editingIdx];
  host.querySelectorAll('[data-jf-prop]').forEach(input => {
    if (validate) {
      prop[input.dataset.jfProp] = _parseScalar(input);
    } else {
      try { prop[input.dataset.jfProp] = _parseScalar(input); } catch { /* keep prior value */ }
    }
  });
  prop.line_items = _collectLineItems(host, !validate);
}

// Load record.proposals[editingIdx] into the proposal-scoped inputs + line items.
function _loadProposalIntoDom(host, st) {
  const prop = (st.record.proposals || [])[st.editingIdx] || {};
  host.querySelectorAll('[data-jf-prop]').forEach(input => {
    const v = _num(prop[input.dataset.jfProp]);
    if (input.tagName === 'SELECT') {
      const sv = v === '' ? '' : String(v);
      if (![...input.options].some(o => o.value === sv)) {   // unlisted label → add an option
        input.appendChild(Object.assign(document.createElement('option'),
          { value: sv, textContent: sv === '' ? '(none)' : sv }));
      }
      input.value = sv;
    } else {
      input.value = v;
    }
  });
  const tbody = host.querySelector('#qp-jf-items');
  const items = Array.isArray(prop.line_items) ? prop.line_items : [];
  const vocab = _rowVocab(host);
  tbody.innerHTML = items.map(it => _lineItemRowHtml(it, vocab)).join('');   // empty → empty-state via _updateCount
  _updateCount(host);
  _recalcGrandTotal(host);
}

// Repopulate the revision selector + primary/delete button state from st.
function _refreshProposalControls(host) {
  const st = host.__qpJobForm;
  const props = st.record.proposals || [];
  const sel = host.querySelector('#qp-jf-prop-select');
  sel.innerHTML = props.map((p, i) =>
    `<option value="${i}"${i === st.editingIdx ? ' selected' : ''}>${_esc(_proposalLabel(p, i))}${i === st.primaryIdx ? '  ★' : ''}</option>`).join('');
  sel.value = String(st.editingIdx);

  const setBtn = host.querySelector('#qp-jf-set-primary');
  const isPrimary = st.editingIdx === st.primaryIdx;
  setBtn.textContent = isPrimary ? '★ Primary' : '☆ Set as primary';
  setBtn.classList.toggle('is-primary', isPrimary);
  setBtn.disabled = isPrimary;

  const delBtn = host.querySelector('#qp-jf-del-prop');
  if (delBtn) delBtn.disabled = props.length <= 1;
}

function _switchProposal(host, newIdx) {
  const st = host.__qpJobForm;
  if (newIdx === st.editingIdx || !st.record.proposals[newIdx]) return;
  _flushProposalDom(host, st, false);
  st.editingIdx = newIdx;
  _loadProposalIntoDom(host, st);
  _refreshProposalControls(host);
}

function _addProposal(host) {
  const st = host.__qpJobForm;
  _flushProposalDom(host, st, false);
  st.record.proposals.push({ revision_label: '', proposal_date: null, grand_total: null, line_items: [] });
  st.editingIdx = st.record.proposals.length - 1;
  _loadProposalIntoDom(host, st);
  _refreshProposalControls(host);
}

function _deleteProposal(host) {
  const st = host.__qpJobForm;
  const props = st.record.proposals || [];
  if (props.length <= 1) return;
  props.splice(st.editingIdx, 1);   // discarding the visible one — no flush needed
  if (st.primaryIdx === st.editingIdx) st.primaryIdx = 0;
  else if (st.primaryIdx > st.editingIdx) st.primaryIdx -= 1;
  if (st.editingIdx >= props.length) st.editingIdx = props.length - 1;
  _loadProposalIntoDom(host, st);
  _refreshProposalControls(host);
}

export function renderJobForm(host, content, opts = {}) {
  // Working copy — every proposal-scoped edit stages into this, never the original.
  const state = { record: _clone(content || {}), editingIdx: 0, primaryIdx: 0 };
  const props = Array.isArray(state.record.proposals) ? state.record.proposals : (state.record.proposals = []);
  if (props.length === 0) props.push({ revision_label: DEFAULT_REVISION_LABEL });
  const rawIdx = state.record.primary_proposal_index;
  state.primaryIdx = (Number.isInteger(rawIdx) && rawIdx >= 0 && rawIdx < props.length) ? rawIdx : 0;
  state.editingIdx = state.primaryIdx;
  host.__qpJobForm = state;

  const prop0 = props[state.editingIdx] || {};
  const items = Array.isArray(prop0.line_items) ? prop0.line_items : [];

  // Build the Unit dropdown vocabulary (defaults ∪ record values ∪ user-added)
  // and stage it on the host so every row's select shares it.
  host.__qpUnitVocab = _buildLineVocab(UNITS, UNITS_LS_KEY, state.record, 'unit');
  // Category vocabulary: seed synchronously with this record's own values (so
  // it's never empty even before the fetch below resolves), then merge in
  // every category actually used across the whole case library.
  host.__qpCatVocab = _buildLineVocab([], null, state.record, 'category');
  _loadCategoryVocab(host);
  const liVocab = _rowVocab(host);

  host.innerHTML = `
    ${_recordSectionHtml(SECTIONS[0], state.record)}
    ${_proposalSectionHtml(prop0)}
    ${_recordSectionHtml(SECTIONS[1], state.record)}
    <fieldset class="qp-jf-section qp-jf-li-section">
      <legend class="qp-jf-legend">Line Items <span class="qp-jf-count" id="qp-jf-item-count"></span></legend>
      <p class="qp-jf-hint">Line items belong to the revision selected above. Total $ is calculated as $ per Unit × (1 + Tax %) × Qty and isn't editable.</p>
      <div class="qp-jf-table-wrap">
        <table class="qp-jf-items-table">
          <thead><tr>
            <th><input type="checkbox" class="qp-jf-sel-all" id="qp-jf-sel-all" title="Select/deselect all rows"></th>
            <th>Description</th><th>Category</th><th>Qty</th><th>Unit</th>
            <th>$&nbsp;per&nbsp;Unit</th><th>Tax&nbsp;%</th><th>Total&nbsp;$</th><th>Optional</th><th></th>
          </tr></thead>
          <tbody id="qp-jf-items">${items.map(it => _lineItemRowHtml(it, liVocab)).join('')}</tbody>
        </table>
      </div>
      <div class="qp-jf-li-bulk-tax">
        <span class="qp-jf-li-bulk-tax-label">Apply tax % to selected rows:</span>
        <input type="text" class="qp-jf-bulk-tax-input" id="qp-jf-bulk-tax-input" inputmode="decimal" placeholder="e.g. 9" title="Tax rate as a percent, e.g. 9 for 9%">
        <button type="button" class="qp-jf-bulk-tax-apply" id="qp-jf-bulk-tax-apply" title="Set the Tax % of every checked row to this value">Apply</button>
      </div>
      <div class="qp-jf-li-actions">
        <button type="button" class="qp-jf-add-item" id="qp-jf-add-item">+ Add line item</button>
        <button type="button" class="qp-jf-copy-table" id="qp-jf-copy-table" title="Copy the line items as a tab-separated table you can paste into Google Sheets / Excel">Copy as table</button>
      </div>
      <div class="qp-jf-li-overlay hidden" id="qp-jf-li-overlay">
        <div class="qp-jf-li-panel">
          <div class="qp-jf-li-head">
            <span class="qp-jf-li-title" id="qp-jf-li-title">Add a line item</span>
            <button type="button" class="qp-jf-li-close" id="qp-jf-li-close" title="Close">✕</button>
          </div>
          <input type="text" class="qp-jf-li-search" id="qp-jf-li-search" placeholder="Search line items…" spellcheck="false" autocomplete="off">
          <div class="qp-jf-li-list" id="qp-jf-li-list"></div>
          <div class="qp-jf-li-create">
            <input type="text" class="qp-jf-li-new" id="qp-jf-li-new" placeholder="Or create a new line item…" spellcheck="false" autocomplete="off">
            <button type="button" class="qp-jf-li-create-btn" id="qp-jf-li-create-btn" disabled>Create</button>
          </div>
          <div class="qp-jf-li-create-hint" id="qp-jf-li-create-hint"></div>
        </div>
      </div>
      <div class="qp-jf-li-overlay hidden" id="qp-jf-vp-overlay">
        <div class="qp-jf-li-panel">
          <div class="qp-jf-li-head">
            <span class="qp-jf-li-title" id="qp-jf-vp-title">Choose a value</span>
            <button type="button" class="qp-jf-li-close" id="qp-jf-vp-close" title="Close">✕</button>
          </div>
          <input type="text" class="qp-jf-li-search" id="qp-jf-vp-search" placeholder="Search…" spellcheck="false" autocomplete="off">
          <div class="qp-jf-li-list" id="qp-jf-vp-list"></div>
          <div class="qp-jf-li-create">
            <input type="text" class="qp-jf-li-new" id="qp-jf-vp-new" placeholder="Or create a new value…" spellcheck="false" autocomplete="off">
            <button type="button" class="qp-jf-li-create-btn" id="qp-jf-vp-create-btn" disabled>Create</button>
          </div>
          <div class="qp-jf-li-create-hint" id="qp-jf-vp-create-hint"></div>
        </div>
      </div>
    </fieldset>`;

  const tbody = host.querySelector('#qp-jf-items');
  _updateCount(host);   // shows the empty-state line when there are no items
  _recalcGrandTotal(host);

  // ── Dropdown fields (Job Type, Revision Label): populate + wire "+ add" ────
  host.querySelectorAll('.qp-jf-select').forEach(sel => {
    const cfg = SELECT_CONFIG[sel.dataset.jfSelkey] || {};
    const vocab = cfg.vocab || {};
    let current = sel.dataset.jfPath ? _get(state.record, sel.dataset.jfPath)
               : (sel.dataset.jfProp ? prop0[sel.dataset.jfProp] : '');
    current = current == null ? '' : String(current);

    const options = [];
    const add = v => { const n = v == null ? '' : String(v); if (!options.includes(n)) options.push(n); };
    if (cfg.allowBlank) add('');
    (vocab.defaults || []).forEach(add);
    if (vocab.optsKey) (opts[vocab.optsKey] || []).forEach(add);   // values used across the library
    _loadCustomVocab(vocab.lsKey).forEach(add);
    // for a proposal-scoped select, seed every label this record already uses
    if (sel.dataset.jfProp) (state.record.proposals || []).forEach(p => add(p?.[sel.dataset.jfProp]));
    add(current);                          // the record's own value, even if custom/legacy
    // drop a stray empty option unless the field explicitly allows blank
    const list = cfg.allowBlank ? options : options.filter(v => v !== '' || v === current);
    sel.innerHTML = list.map(v =>
      `<option value="${_esc(v)}"${v === current ? ' selected' : ''}>${v === '' ? '(none)' : _esc(v)}</option>`).join('');
    sel.value = current;
  });
  host.querySelectorAll('.qp-jf-add-opt').forEach(btn => {
    const key = btn.dataset.addFor;
    const vocab = (SELECT_CONFIG[key] || {}).vocab || {};
    const sel = host.querySelector(`select[data-jf-selkey="${key}"]`);
    const box = host.querySelector(`.qp-jf-add-box[data-add-box-for="${key}"]`);
    const input = box?.querySelector('.qp-jf-add-input');
    if (!sel || !box || !input) return;

    const open = () => { box.classList.remove('hidden'); sel.classList.add('hidden'); input.value = ''; input.focus(); };
    const close = () => { box.classList.add('hidden'); sel.classList.remove('hidden'); };
    const commit = () => {
      const v = _normToken(input.value);
      if (!v) { close(); return; }
      const custom = _loadCustomVocab(vocab.lsKey);
      if (!custom.includes(v)) { custom.push(v); _saveCustomVocab(vocab.lsKey, custom); }
      if (![...sel.options].some(o => o.value === v)) {
        sel.appendChild(Object.assign(document.createElement('option'), { value: v, textContent: v }));
      }
      sel.value = v;
      close();
    };

    btn.addEventListener('click', open);
    box.querySelector('.qp-jf-add-confirm').addEventListener('click', commit);
    box.querySelector('.qp-jf-add-cancel').addEventListener('click', close);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
  });

  // ── Proposal-revision selector + primary/add/delete ────────────────────────
  host.querySelector('#qp-jf-prop-select').addEventListener('change', (e) => _switchProposal(host, Number(e.target.value)));
  host.querySelector('#qp-jf-set-primary').addEventListener('click', () => {
    const st = host.__qpJobForm;
    st.primaryIdx = st.editingIdx;
    _refreshProposalControls(host);
  });
  host.querySelector('#qp-jf-add-prop').addEventListener('click', () => _addProposal(host));
  host.querySelector('#qp-jf-del-prop').addEventListener('click', () => _deleteProposal(host));
  _refreshProposalControls(host);

  // ── Line items ─────────────────────────────────────────────────────────────
  // "Add line item" opens the library picker overlay (search existing / create new).
  host.querySelector('#qp-jf-add-item').addEventListener('click', () => _openLiOverlay(host));
  host.querySelector('#qp-jf-copy-table').addEventListener('click', () => _copyLineItemsTable(host));

  // Select all/none (header checkbox) — mirrors its checked state onto every row.
  host.querySelector('#qp-jf-sel-all').addEventListener('change', (e) => {
    host.querySelectorAll('#qp-jf-items .qp-jf-sel').forEach(cb => { cb.checked = e.target.checked; });
  });
  // Bulk tax apply: set the Tax % of every checked row to the entered value,
  // then dispatch 'input' on each so the existing per-row recompute (Total $ /
  // Grand Total) picks it up — see the tbody 'input' listener below.
  host.querySelector('#qp-jf-bulk-tax-apply').addEventListener('click', () => {
    const input = host.querySelector('#qp-jf-bulk-tax-input');
    const btn = host.querySelector('#qp-jf-bulk-tax-apply');
    const pct = input.value.trim();
    if (pct !== '' && !Number.isFinite(Number(pct))) { input.focus(); return; }
    const rows = [...host.querySelectorAll('#qp-jf-items .qp-jf-item')]
      .filter(row => row.querySelector('.qp-jf-sel')?.checked);
    const flash = (msg) => { btn.textContent = msg; setTimeout(() => { btn.textContent = 'Apply'; }, 1200); };
    if (!rows.length) { flash('Select rows first'); return; }
    rows.forEach(row => {
      const taxInput = row.querySelector('.qp-jf-tax');
      taxInput.value = pct;
      taxInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    flash(`Applied to ${rows.length}`);
  });

  host.querySelector('#qp-jf-li-close').addEventListener('click', () => _closeLiOverlay(host));
  // Click the dimmed backdrop (outside the panel) to close.
  host.querySelector('#qp-jf-li-overlay').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) _closeLiOverlay(host);
  });
  // Search box: debounced live filter of the library list.
  host.querySelector('#qp-jf-li-search').addEventListener('input',
    _debounce((e) => _renderLiList(host, e.target.value), 200));
  host.querySelector('#qp-jf-li-search').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); _closeLiOverlay(host); }
  });
  // Delegated: click a library row → add or (edit mode) update, then close.
  host.querySelector('#qp-jf-li-list').addEventListener('click', (e) => {
    const row = e.target.closest('.qp-jf-li-row');
    if (!row) return;
    _applyLiPick(host, { description: row.dataset.desc, unit: row.dataset.unit, category: row.dataset.cat });
    _closeLiOverlay(host);
  });
  // Create box: also a search — live-updates the list AND the block/allow state.
  const liNew = host.querySelector('#qp-jf-li-new');
  liNew.addEventListener('input', _debounce(() => { _renderLiList(host, liNew.value); _updateCreateState(host); }, 200));
  const doCreate = () => {
    const btn = host.querySelector('#qp-jf-li-create-btn');
    const raw = liNew.value.trim();
    _updateCreateState(host);                 // re-check against the latest state
    if (btn.disabled || !raw) return;         // exact duplicate or empty → blocked
    _applyLiPick(host, { description: raw });
    _closeLiOverlay(host);
  };
  host.querySelector('#qp-jf-li-create-btn').addEventListener('click', doCreate);
  liNew.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')  { e.preventDefault(); doCreate(); }
    else if (e.key === 'Escape') { e.preventDefault(); _closeLiOverlay(host); }
  });

  // delegated: remove row, or open the Category/Unit value picker
  tbody.addEventListener('click', (e) => {
    if (e.target.classList.contains('qp-jf-row-del')) {
      e.target.closest('tr')?.remove();
      _updateCount(host);
      _recalcGrandTotal(host);
      return;
    }
    const comboBtn = e.target.closest('.qp-jf-combo-btn');
    if (comboBtn) {
      const input = comboBtn.parentElement.querySelector('input');
      if (!input) return;
      if (input.classList.contains('qp-jf-desc')) _openLiOverlay(host, input);   // library picker, edit mode
      else _openVpOverlay(host, input, input.classList.contains('qp-jf-unit'));
    }
  });

  // ── Category / Unit value picker (same overlay UI as "Add a line item") ─────
  host.querySelector('#qp-jf-vp-close').addEventListener('click', () => _closeVpOverlay(host));
  host.querySelector('#qp-jf-vp-overlay').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) _closeVpOverlay(host);
  });
  const vpSearch = host.querySelector('#qp-jf-vp-search');
  vpSearch.addEventListener('input', _debounce((e) => _renderVpList(host, e.target.value), 150));
  vpSearch.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); _closeVpOverlay(host); }
  });
  host.querySelector('#qp-jf-vp-list').addEventListener('click', (e) => {
    const row = e.target.closest('.qp-jf-vp-row');
    if (!row) return;
    if (host.__qpVp) _applyComboValue(host.__qpVp.input, row.dataset.val);
    _closeVpOverlay(host);
  });
  const vpNew = host.querySelector('#qp-jf-vp-new');
  vpNew.addEventListener('input', _debounce(() => { _renderVpList(host, vpNew.value); _updateVpCreateState(host); }, 150));
  host.querySelector('#qp-jf-vp-create-btn').addEventListener('click', () => _vpDoCreate(host));
  vpNew.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _vpDoCreate(host); }
    else if (e.key === 'Escape') { e.preventDefault(); _closeVpOverlay(host); }
  });

  tbody.addEventListener('input', (e) => {
    const row = e.target.closest('tr');
    if (!row) return;
    // Total $ is always calculated — recompute when qty, $/unit, or tax changes.
    if (e.target.classList.contains('qp-jf-qty') || e.target.classList.contains('qp-jf-up')
        || e.target.classList.contains('qp-jf-tax')) {
      const q = parseFloat(row.querySelector('.qp-jf-qty').value);
      const u = parseFloat(row.querySelector('.qp-jf-up').value);
      const taxPct = parseFloat(row.querySelector('.qp-jf-tax').value);
      const tax = Number.isFinite(taxPct) ? taxPct / 100 : 0;
      row.querySelector('.qp-jf-ext').value =
        (Number.isFinite(q) && Number.isFinite(u)) ? +(u * (1 + tax) * q).toFixed(2) : '';
    }
    // Grand Total tracks the line totals — recompute on any relevant edit,
    // including toggling an item's Optional checkbox (fires 'input').
    if (e.target.classList.contains('qp-jf-qty') || e.target.classList.contains('qp-jf-up')
        || e.target.classList.contains('qp-jf-tax') || e.target.classList.contains('qp-jf-opt')) {
      _recalcGrandTotal(host);
    }
  });
}

// ── collect (form → merged full record) ──────────────────────────────────────

export function collectJobForm(host, originalContent) {
  const st = host.__qpJobForm;
  const clone = st ? st.record : _clone(originalContent || {});

  // record-level scalar fields (Details + Scale Metrics) — always in the DOM
  host.querySelectorAll('[data-jf-path]').forEach(input => {
    if (input.dataset.jfRequired === '1' && input.value.trim() === '') {
      throw new Error(`"${input.dataset.jfLabel}" is required`);
    }
    _setPath(clone, input.dataset.jfPath, _parseScalar(input));
  });

  // keep identity.job_name mirrored with the top-level job_name
  if (typeof clone.job_name === 'string') {
    clone.identity = _ensureObj(clone, 'identity');
    clone.identity.job_name = clone.job_name;
  }

  if (st) {
    // flush the visible revision (validating), then stamp the primary index
    _flushProposalDom(host, st, true);
    const props = clone.proposals || (clone.proposals = []);
    let pIdx = st.primaryIdx;
    if (!Number.isInteger(pIdx) || pIdx < 0 || pIdx >= props.length) pIdx = 0;
    clone.primary_proposal_index = pIdx;
  } else {
    // legacy fallback (renderJobForm never ran): old single-proposal behavior
    const props = Array.isArray(clone.proposals) ? clone.proposals : (clone.proposals = []);
    let idx = Number.isInteger(clone.primary_proposal_index) ? clone.primary_proposal_index : 0;
    if (idx < 0 || idx >= props.length) idx = 0;
    if (!props[idx]) props[idx] = {};
    clone.primary_proposal_index = idx;
    const prop = props[idx];
    host.querySelectorAll('[data-jf-prop]').forEach(input => { prop[input.dataset.jfProp] = _parseScalar(input); });
    prop.line_items = _collectLineItems(host, false);
  }

  return clone;
}
