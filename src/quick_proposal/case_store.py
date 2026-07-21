"""DB-backed persistence for the Quick Proposal case library (TODO_YY).

The canonical job record is a deeply nested, machine-generated document. It is
stored **normalized** across five tables (see ``core/database.py``):

    QpJobData ─┬─ QpJobDetails        (identity + classification, 1:1)
               ├─ QpJobScaleMetrics   (derived.scale_metrics,     1:1)
               └─ QpJobRevision ──── QpLineItem   (one proposal / its line items)

Every table keeps a per-level ``extra`` JSON column holding any keys that were
**not** promoted to typed columns, so ``shred`` → ``reassemble`` is lossless: the
job form (``qp_job_form.js``) and the knowledge-pack derivation both consume the
whole reassembled dict, so nothing may be dropped.

Round-trip contract (validated by ``tests/test_qp_case_store_roundtrip.py`` over
every shipped job): reassembly reinserts a promoted key only when its column is
non-null — consumers treat a null the same as an absent key (``d.get(k)`` → None),
and numeric values are compared as floats, so SQLite type-affinity coercion
(int↔float) is a non-issue.
"""

import copy
import uuid

from core.database import (
    QpJobData, QpJobDetails, QpJobScaleMetrics, QpJobRevision, QpLineItem,
)

# ── Field-promotion maps: which keys of each sub-object become typed columns ──
_IDENTITY_COLS = {"client", "client_location", "local_folder", "true_job_number",
                  "proposal_numbers"}
_CLASSIFICATION_COLS = {"job_type", "job_type_source"}
_SCALE_COLS = {"lot_count", "lot_area_sf", "road_LF", "ROW_SF", "stripping_depth_in",
               "road_subgrade_SY", "road_paving_SY", "ballast_CY", "fronting_LF"}
_REVISION_COLS = {"source_file", "revision_label", "estimate_label", "proposal_date",
                  "total_reconciled", "grand_total", "grand_total_raw",
                  "grand_total_mismatch", "total_unreconciled_delta", "line_items_sum",
                  "line_items_sum_matches_total", "exclusions_text"}
_LINE_ITEM_COLS = {"description", "category", "unit", "qty", "unit_price", "ext_price",
                   "tax_rate", "is_optional", "mismatch"}


def _split(d: dict, cols: set) -> tuple[dict, dict]:
    """Return (promoted, extra): promoted = the keys of `cols` present in `d`;
    extra = every other key. `d` is not mutated."""
    d = d or {}
    promoted = {k: d[k] for k in cols if k in d}
    extra = {k: v for k, v in d.items() if k not in cols}
    return promoted, extra


def _merge(extra: dict, promoted: dict) -> dict:
    """`extra` overlaid with `promoted`, skipping None-valued promoted keys
    (consumers treat null == absent). Returns a fresh dict."""
    out = dict(extra or {})
    for k, v in promoted.items():
        if v is not None:
            out[k] = v
    return out


# ── shred: content dict → ORM object graph ───────────────────────────────────

def _root_extra_and_scale(content: dict) -> tuple[dict, dict]:
    """Return (root_extra, scale_metrics): root_extra is the top-level record
    minus the keys owned by child tables / promoted columns (and minus
    ``derived.scale_metrics``, which moves to its own table)."""
    root_extra = copy.deepcopy(content)
    for k in ("schema_version", "job_name", "built_at", "primary_proposal_index",
              "identity", "classification", "proposals"):
        root_extra.pop(k, None)
    derived = root_extra.get("derived")
    scale_metrics = {}
    if isinstance(derived, dict):
        scale_metrics = derived.pop("scale_metrics", {}) or {}
    return root_extra, scale_metrics


def _build_details(content: dict) -> QpJobDetails:
    id_prom, id_extra = _split(content.get("identity") or {}, _IDENTITY_COLS)
    cl_prom, cl_extra = _split(content.get("classification") or {}, _CLASSIFICATION_COLS)
    return QpJobDetails(
        client=id_prom.get("client"),
        client_location=id_prom.get("client_location"),
        local_folder=id_prom.get("local_folder"),
        true_job_number=id_prom.get("true_job_number"),
        proposal_numbers=id_prom.get("proposal_numbers"),
        identity_extra=id_extra,
        job_type=cl_prom.get("job_type"),
        job_type_source=cl_prom.get("job_type_source"),
        classification_extra=cl_extra,
    )


def _build_scale(scale_metrics: dict) -> QpJobScaleMetrics:
    sm_prom, sm_extra = _split(scale_metrics, _SCALE_COLS)
    return QpJobScaleMetrics(
        lot_count=sm_prom.get("lot_count"),
        lot_area_sf=sm_prom.get("lot_area_sf"),
        road_LF=sm_prom.get("road_LF"),
        ROW_SF=sm_prom.get("ROW_SF"),
        stripping_depth_in=sm_prom.get("stripping_depth_in"),
        road_subgrade_SY=sm_prom.get("road_subgrade_SY"),
        road_paving_SY=sm_prom.get("road_paving_SY"),
        ballast_CY=sm_prom.get("ballast_CY"),
        fronting_LF=sm_prom.get("fronting_LF"),
        extra=sm_extra,
    )


def _build_revisions(content: dict) -> list:
    primary_idx = content.get("primary_proposal_index") or 0
    revs = []
    for r_i, prop in enumerate(content.get("proposals") or []):
        prop = prop or {}
        rev_prom, rev_extra = _split(prop, _REVISION_COLS)
        rev_extra.pop("line_items", None)
        rev_extra.pop("optional_items", None)
        rev = QpJobRevision(
            id=uuid.uuid4().hex,
            revision_index=r_i,
            is_primary=(r_i == primary_idx),
            has_optional_items=("optional_items" in prop),
            source_file=rev_prom.get("source_file"),
            revision_label=rev_prom.get("revision_label"),
            estimate_label=rev_prom.get("estimate_label"),
            proposal_date=rev_prom.get("proposal_date"),
            total_reconciled=rev_prom.get("total_reconciled"),
            grand_total=rev_prom.get("grand_total"),
            grand_total_raw=rev_prom.get("grand_total_raw"),
            grand_total_mismatch=rev_prom.get("grand_total_mismatch"),
            total_unreconciled_delta=rev_prom.get("total_unreconciled_delta"),
            line_items_sum=rev_prom.get("line_items_sum"),
            line_items_sum_matches_total=rev_prom.get("line_items_sum_matches_total"),
            exclusions_text=rev_prom.get("exclusions_text"),
            extra=rev_extra,
        )
        pos = 0
        for kind, key in (("line_item", "line_items"), ("optional", "optional_items")):
            for item in (prop.get(key) or []):
                item = item or {}
                li_prom, li_extra = _split(item, _LINE_ITEM_COLS)
                rev.line_items.append(QpLineItem(
                    id=uuid.uuid4().hex,
                    position=pos,
                    list_kind=kind,
                    description=li_prom.get("description"),
                    category=li_prom.get("category"),
                    unit=li_prom.get("unit"),
                    qty=li_prom.get("qty"),
                    unit_price=li_prom.get("unit_price"),
                    ext_price=li_prom.get("ext_price"),
                    tax_rate=li_prom.get("tax_rate"),
                    is_optional=li_prom.get("is_optional"),
                    mismatch=li_prom.get("mismatch"),
                    extra=li_extra,
                ))
                pos += 1
        revs.append(rev)
    return revs


def _populate(job: QpJobData, slug: str, content: dict) -> QpJobData:
    """Populate `job` (new or existing) from a full record dict. Reassigning the
    child collections on an existing row clears the old children via delete-orphan
    — so this is a create-or-replace on a single parent, never a second parent
    with a conflicting PK."""
    content = content or {}
    root_extra, scale_metrics = _root_extra_and_scale(content)
    job.slug = slug
    job.job_name = content.get("job_name") or slug
    job.schema_version = content.get("schema_version")
    job.built_at = content.get("built_at")
    job.primary_proposal_index = content.get("primary_proposal_index") or 0
    job.extra = root_extra
    job.details = _build_details(content)
    job.scale = _build_scale(scale_metrics)
    job.revisions = _build_revisions(content)
    return job


def build_job_rows(slug: str, content: dict) -> QpJobData:
    """Build a fresh QpJobData with child rows attached (used by the seed import
    and tests). Does NOT add anything to a session."""
    return _populate(QpJobData(), slug, content)


# ── reassemble: ORM object graph → content dict ──────────────────────────────

def reassemble(job: QpJobData) -> dict:
    """Rebuild the full job-record dict from a QpJobData object graph."""
    content = copy.deepcopy(job.extra or {})
    if job.schema_version is not None:
        content["schema_version"] = job.schema_version
    content["job_name"] = job.job_name
    if job.built_at is not None:
        content["built_at"] = job.built_at
    content["primary_proposal_index"] = job.primary_proposal_index or 0

    d = job.details
    if d is not None:
        content["identity"] = _merge(d.identity_extra, {
            "client": d.client, "client_location": d.client_location,
            "local_folder": d.local_folder, "true_job_number": d.true_job_number,
            "proposal_numbers": d.proposal_numbers,
        })
        content["classification"] = _merge(d.classification_extra, {
            "job_type": d.job_type, "job_type_source": d.job_type_source,
        })

    s = job.scale
    if s is not None:
        sm = _merge(s.extra, {
            "lot_count": s.lot_count, "lot_area_sf": s.lot_area_sf, "road_LF": s.road_LF,
            "ROW_SF": s.ROW_SF, "stripping_depth_in": s.stripping_depth_in,
            "road_subgrade_SY": s.road_subgrade_SY, "road_paving_SY": s.road_paving_SY,
            "ballast_CY": s.ballast_CY, "fronting_LF": s.fronting_LF,
        })
        derived = content.get("derived")
        if not isinstance(derived, dict):
            derived = {}
        derived["scale_metrics"] = sm
        content["derived"] = derived

    props = []
    for rev in sorted(job.revisions, key=lambda r: r.revision_index):
        prop = _merge(rev.extra, {
            "source_file": rev.source_file, "revision_label": rev.revision_label,
            "estimate_label": rev.estimate_label, "proposal_date": rev.proposal_date,
            "total_reconciled": rev.total_reconciled, "grand_total": rev.grand_total,
            "grand_total_raw": rev.grand_total_raw, "grand_total_mismatch": rev.grand_total_mismatch,
            "total_unreconciled_delta": rev.total_unreconciled_delta,
            "line_items_sum": rev.line_items_sum,
            "line_items_sum_matches_total": rev.line_items_sum_matches_total,
            "exclusions_text": rev.exclusions_text,
        })
        line_items, optional_items = [], []
        for li in sorted(rev.line_items, key=lambda x: x.position):
            item = _merge(li.extra, {
                "description": li.description, "category": li.category, "unit": li.unit,
                "qty": li.qty, "unit_price": li.unit_price, "ext_price": li.ext_price,
                "tax_rate": li.tax_rate, "is_optional": li.is_optional, "mismatch": li.mismatch,
            })
            (optional_items if li.list_kind == "optional" else line_items).append(item)
        prop["line_items"] = line_items
        if rev.has_optional_items:
            prop["optional_items"] = optional_items
        props.append(prop)
    content["proposals"] = props
    return content


# ── session-level CRUD helpers (the routes call these) ───────────────────────

def get_job(db, slug: str) -> QpJobData | None:
    return db.get(QpJobData, slug)


def get_job_by_name(db, job_name: str, exclude_slug: str = "") -> QpJobData | None:
    q = db.query(QpJobData).filter(QpJobData.job_name.ilike(job_name.strip()))
    if exclude_slug:
        q = q.filter(QpJobData.slug != exclude_slug)
    return q.first()


def diff_sections(old: dict, new: dict) -> list[str]:
    """Return the top-level content sections that differ between `old` and `new`
    (both full record dicts, e.g. from `reassemble`). Sections map to the tables
    `upsert_job` rewrites: identity/classification -> qp_job_details,
    scale_metrics -> qp_job_scale_metrics, proposals -> qp_job_revisions/qp_line_items.
    `upsert_job` always rewrites all child rows regardless of what changed (delete-orphan
    cascade), so this is for logging/diagnostics only, not a partial-write optimization."""
    old = old or {}
    new = new or {}
    changed = []
    if (old.get("identity") or {}) != (new.get("identity") or {}):
        changed.append("identity")
    if (old.get("classification") or {}) != (new.get("classification") or {}):
        changed.append("classification")
    old_scale = (old.get("derived") or {}).get("scale_metrics") or {}
    new_scale = (new.get("derived") or {}).get("scale_metrics") or {}
    if old_scale != new_scale:
        changed.append("scale_metrics")
    if (old.get("proposals") or []) != (new.get("proposals") or []):
        changed.append("proposals")
    return changed


def upsert_job(db, slug: str, content: dict) -> QpJobData:
    """Create-or-replace the job at `slug` from a full content dict. On update the
    row is populated in place (created_at preserved, PK stable) and the old child
    rows are cleared via delete-orphan cascade."""
    existing = db.get(QpJobData, slug)
    if existing is None:
        job = _populate(QpJobData(), slug, content)
        db.add(job)
        return job
    return _populate(existing, slug, content)


def load_all_records(db) -> list[dict]:
    """Every job reassembled into its full content dict, ordered by slug."""
    jobs = db.query(QpJobData).order_by(QpJobData.slug).all()
    return [reassemble(j) for j in jobs]
