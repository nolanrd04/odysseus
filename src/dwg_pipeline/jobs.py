"""
Per-job folder provisioning for the DWG extraction flow (DQ-5 / DQ-15).

On a .dwg upload the flow gets a dedicated folder under DATA_DIR/dwg_jobs/
holding the converted DXF(s), the census payload, and any agent outputs.
That folder is the turn's `workspace` (existing confinement plumbing) and
the sandbox root for LLM-written python (sandbox.py keys off it).

Retention per DQ-15: the converted DXF persists in the job folder; the
original uploaded DWG is NOT copied here (the upload subsystem keeps its
own record under UPLOAD_DIR — the pipeline itself retains only the DXF).
Runs are disposable: no versioned run entity, a run's record is its chat
session; sessions.json here only maps session → job folder so later turns
in the same chat re-attach the same workspace and census.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.constants import DWG_JOBS_DIR

logger = logging.getLogger(__name__)

_SESSIONS_INDEX = Path(DWG_JOBS_DIR) / "sessions.json"


@dataclass
class DwgJobContext:
    job_dir: str
    job_id: str
    dxf_files: list[str] = field(default_factory=list)  # basenames inside job_dir
    census: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    newly_provisioned: bool = False
    holdout_job: str = ""  # corpus job substring to exclude from dwg_corpus_lookup (self-test)
    # As-uploaded DWG filenames, before the upload subsystem's content-hash
    # renaming (dxf_files are named after that hash, e.g. "d6999bed....dxf",
    # which carries no job identity). This is what generations.py's corpus
    # matcher needs — the hash basenames never substring-match a corpus
    # job_folder. Persisted so session re-attach keeps it too.
    original_filenames: list[str] = field(default_factory=list)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_")[:48] or "job"


def _load_index() -> dict:
    try:
        return json.loads(_SESSIONS_INDEX.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_index(index: dict) -> None:
    _SESSIONS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _SESSIONS_INDEX.write_text(json.dumps(index, indent=1), encoding="utf-8")


def job_for_session(session_id: str) -> DwgJobContext | None:
    """Re-attach a prior turn's job for this chat session, if one exists."""
    if not session_id:
        return None
    job_id = _load_index().get(str(session_id))
    if not job_id:
        return None
    job_dir = Path(DWG_JOBS_DIR) / job_id
    census_path = job_dir / "census.json"
    if not job_dir.is_dir() or not census_path.exists():
        return None
    try:
        census = json.loads(census_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("dwg job %s: unreadable census.json", job_id)
        return None
    return DwgJobContext(
        job_dir=str(job_dir),
        job_id=job_id,
        dxf_files=sorted(p.name for p in job_dir.glob("*.dxf")),
        census=census,
        holdout_job=_read_holdout(job_dir),
        original_filenames=_read_original_filenames(job_dir),
    )


def _read_holdout(job_dir: Path) -> str:
    try:
        return (job_dir / "holdout_job.txt").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _read_original_filenames(job_dir: Path) -> list[str]:
    try:
        return json.loads((job_dir / "original_filenames.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def provision_dwg_job(
    dwg_paths: list[str],
    session_id: str | None = None,
    holdout_job: str = "",
    original_filenames: list[str] | None = None,
) -> DwgJobContext:
    """Create a job folder for the uploaded DWG(s): convert each to DXF into
    the folder, run the mandatory census (DQ-6), persist census.json, and
    bind the folder to the chat session for later turns.

    Conversion failures are collected per-file in `errors` rather than
    raised, so one bad file doesn't sink a multi-file upload (a job with
    zero converted DXFs simply has an empty census and its errors listed).

    `holdout_job`: when the uploaded DWG is itself one of the corpus jobs
    (testing the pipeline against a job whose completed qty_tbl is already
    in the corpus), name it here so `dwg_corpus_lookup` excludes its own
    answer key from analog/vocabulary lookups for this job's whole lifetime
    (persisted as `holdout_job.txt`, so session re-attach keeps it too).

    `original_filenames`: the as-uploaded DWG filenames (before the upload
    subsystem's content-hash renaming) — the only place a real job identity
    survives, since the hash-named files/dxf conversions carry none.
    Persisted as `original_filenames.json` so generations.py's corpus
    matcher has something to match on, on this turn and any later re-attach.
    """
    from src.dwg_pipeline.census import census_job
    from src.dwg_qty.convert import convert_dwg_to_dxf

    first = _slug(dwg_paths[0]) if dwg_paths else "job"
    job_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{first}"
    job_dir = Path(DWG_JOBS_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    holdout_job = (holdout_job or "").strip()
    if holdout_job:
        (job_dir / "holdout_job.txt").write_text(holdout_job, encoding="utf-8")

    original_filenames = [n for n in (original_filenames or []) if n]
    if original_filenames:
        (job_dir / "original_filenames.json").write_text(
            json.dumps(original_filenames, indent=1), encoding="utf-8"
        )

    ctx = DwgJobContext(
        job_dir=str(job_dir), job_id=job_id, newly_provisioned=True, holdout_job=holdout_job,
        original_filenames=original_filenames,
    )

    for dwg in dwg_paths:
        try:
            dxf_path = convert_dwg_to_dxf(dwg, job_dir, timeout=300)
            ctx.dxf_files.append(dxf_path.name)
        except Exception as e:
            msg = f"{Path(dwg).name}: conversion failed — {e}"
            logger.warning("dwg job %s: %s", job_id, msg)
            ctx.errors.append(msg)

    if ctx.dxf_files:
        try:
            ctx.census = census_job([job_dir / n for n in ctx.dxf_files])
        except Exception as e:
            logger.exception("dwg job %s: census failed", job_id)
            ctx.errors.append(f"census failed — {e}")

    (job_dir / "census.json").write_text(
        json.dumps(ctx.census, indent=1), encoding="utf-8"
    )

    if session_id:
        index = _load_index()
        index[str(session_id)] = job_id
        _save_index(index)

    logger.info(
        "dwg job %s provisioned: %d dxf, %d errors, session=%s",
        job_id, len(ctx.dxf_files), len(ctx.errors), session_id,
    )
    return ctx
