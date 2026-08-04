"""
Automatic live-run tracking for the DWG extraction pipeline, mirroring
Quick Proposal's QpGeneration/actuals-pairing pattern (routes/quick_proposal_
routes.py, src/quick_proposal/actuals_matcher.py) so DWG output isn't only
ever scored by manually invoking eval/run_eval.py.

Two jobs:
  1. `match_corpus_job()` — conservative name-substring match of a live job's
     filenames against the corpus, the DWG analog of
     actuals_matcher.find_actual_match. Matches against each corpus job's
     real per-file DWG source names (census_fingerprints.json), not just the
     job_folder itself — Terra's internal DWG filenames are job-number based
     (e.g. "22066-C-BASE.dwg") and routinely bear no resemblance to the
     corpus folder naming (sequence+name, e.g. "26008-2_WOODMAN"), so
     matching only against job_folder misses real matches. No fuzzy
     fallback: a wrong match would silently poison the reliability stats
     with a bogus "actual," which is worse than leaving a run unmatched.
  2. `save_dwg_generation()` — called once per completed live DWG agent turn
     (routes/chat_routes.py) that produced a qty_tbl table. Only persists when
     the job matches a known corpus job (the only case an "actual" exists to
     score against); other turns are intentionally left uncaptured rather than
     accumulating rows with nothing to compare.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from functools import lru_cache
from pathlib import Path

from src.dwg_pipeline.qty_tbl_parser import parse_predicted_table, score

logger = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def _normalize_job_id(name: str) -> str:
    return _NON_ALNUM_RE.sub("", (name or "").upper())


@lru_cache(maxsize=1)
def _load_qty_tbl_records() -> list[dict]:
    path = CORPUS_DIR / "qty_tbl_records.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _load_census_fingerprints() -> list[dict]:
    path = CORPUS_DIR / "census_fingerprints.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def match_corpus_job(dxf_files: list[str], original_filenames: list[str] | None = None) -> str | None:
    """Given a live job's filenames, find the corpus job_folder it most
    likely belongs to (substring match either direction, normalized to
    alnum-only uppercase). Returns None if nothing lines up — deliberately no
    fuzzy fallback, see module docstring.

    `original_filenames` (the as-uploaded names, before the upload
    subsystem's content-hash renaming) are tried first — they're the only
    ones that ever carry a real job identity. `dxf_files` are the converted
    basenames inside the job folder; for live uploads these are hash-named
    (e.g. "d6999bed....dxf") and won't match anything, but they're kept as a
    fallback for the offline eval harness, where they're real job filenames.
    """
    candidates = [
        _normalize_job_id(Path(f).stem)
        for f in list(original_filenames or []) + list(dxf_files)
    ]
    candidates = [c for c in candidates if c]
    if not candidates:
        return None

    # Primary: each corpus job's real per-file DWG source names — only
    # consider jobs that actually have a completed qty_tbl (has_qty_tbl),
    # since a match with no ground truth to score against isn't useful.
    for fp in _load_census_fingerprints():
        if not fp.get("has_qty_tbl"):
            continue
        job_folder = fp.get("job_folder", "")
        for f in fp.get("census", {}).get("files", []):
            file_key = _normalize_job_id(Path(f.get("file", "")).stem)
            if not file_key:
                continue
            for cand in candidates:
                if file_key in cand or cand in file_key:
                    return job_folder

    # Fallback: the corpus job_folder name itself (covers the offline eval
    # harness, whose dxf_cache basenames equal the corpus job_folder's own
    # naming, and any live upload that happens to include it).
    for record in _load_qty_tbl_records():
        job_key = _normalize_job_id(record.get("job_folder", ""))
        if not job_key:
            continue
        for cand in candidates:
            if job_key in cand or cand in job_key:
                return record["job_folder"]
    return None


def _actual_rows_for(corpus_job_folder: str) -> list[dict]:
    for record in _load_qty_tbl_records():
        if record.get("job_folder") == corpus_job_folder:
            return record.get("rows", [])
    return []


def save_dwg_generation(
    job_id: str,
    session_id: str | None,
    model: str | None,
    dxf_files: list[str],
    final_reply: str,
    original_filenames: list[str] | None = None,
) -> None:
    """Best-effort auto-capture of a completed live DWG extraction turn.

    Never raises — any failure here must not break a turn's completion, same
    convention as quick_proposal_routes._save_generation_snapshot /
    _auto_pair_actual.
    """
    try:
        predicted_rows = parse_predicted_table(final_reply or "")
        if not predicted_rows:
            return  # this turn didn't produce a qty_tbl table — nothing to track

        corpus_job_folder = match_corpus_job(dxf_files, original_filenames)
        if not corpus_job_folder:
            return  # no known ground truth for this job — out of scope for auto-tracking

        metrics = score(predicted_rows, _actual_rows_for(corpus_job_folder))

        from core.database import SessionLocal, DwgGeneration

        db = SessionLocal()
        try:
            generation_index = (
                db.query(DwgGeneration).filter(DwgGeneration.job_id == job_id).count() + 1
            )
            db.add(DwgGeneration(
                id=str(uuid.uuid4()),
                job_id=job_id,
                session_id=session_id or None,
                generation_index=generation_index,
                model=model or None,
                dxf_files=list(dxf_files),
                corpus_job_folder=corpus_job_folder,
                predicted_rows=predicted_rows,
                final_reply=final_reply,
                metrics=metrics,
            ))
            db.commit()
            logger.info(
                "dwg generation captured: job=%s gen=%d matched=%s tolerance_hit=%s/%s",
                job_id, generation_index, corpus_job_folder,
                metrics["lines_within_tolerance"], metrics["lines_compared"],
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception:
        logger.warning("dwg generation capture failed for job=%s", job_id, exc_info=True)
