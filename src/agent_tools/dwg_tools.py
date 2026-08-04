"""
dwg_corpus_lookup — one flexible retrieval tool over the DWG pipeline corpus
(DQ-7 part 2 / DQ-8), following the kp_lookup lesson: one tool with a small
query language instead of N narrow tools.

Serves three derived, gitignored data sets built by
`python -m src.dwg_pipeline.build_corpus`:
  - the canonical qty_tbl row-label vocabulary,
  - per-job census fingerprints (the DQ-8 "ghost firm" behavioral data —
    no real firm identity exists anywhere in the corpus, deliberately),
  - per-job parsed qty_tbl takeoff records (few-shot mapping grounding).

Retrieval is context-stuffing by design at the current corpus size (8-9 DWG
jobs): the model judges which past job is the closest convention analog from
raw fingerprint data. No embeddings, no hardcoded naming semantics.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "dwg_pipeline" / "corpus"

_NOT_BUILT = (
    "DWG corpus not built on this machine. Run "
    "`python -m src.dwg_pipeline.build_corpus` (requires the custom folder data root)."
)


def _load(name: str):
    path = CORPUS_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _active_holdout_job() -> str:
    """Job-folder substring to exclude from this turn's corpus results.

    Set once per job at provisioning time (jobs.py) when the uploaded DWG is
    itself one of the corpus jobs — testing the pipeline against a job whose
    completed qty_tbl is already in the corpus must not let the model read
    its own answer key. Enforced here (not left to the model to remember to
    pass an exclude arg) so it applies regardless of how the tool is called.
    """
    try:
        from src.dwg_pipeline.sandbox import active_dwg_job_dir

        job_dir = active_dwg_job_dir()
        if not job_dir:
            return ""
        return (Path(job_dir) / "holdout_job.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _is_holdout(job_folder: str, holdout: str) -> bool:
    return bool(holdout) and holdout.lower() in job_folder.lower()


def _fingerprint_summary(fp: dict) -> dict:
    """Compact per-job view for the cross-job comparison pass: layer names,
    block names, and headline counts — enough to judge convention similarity
    without the full per-layer entity-type breakdown."""
    files = []
    for f in fp["census"]["files"]:
        files.append(
            {
                "file": f["file"],
                "layer_count": f["layer_count"],
                "entity_count": f["entity_count"],
                "layer_names": sorted(f["layers"].keys()),
                "centerline_layer_candidates": f["centerline_layer_candidates"],
                "proxy_entity_layers": f["proxy_entity_layers"],
                "block_names": sorted(f["block_attributes"].keys()),
                "anonymous_block_count": len(f["anonymous_blocks"]),
                "annotation_layouts": f.get("annotation_layouts", {}),
            }
        )
    return {
        "job_folder": fp["job_folder"],
        "has_qty_tbl": fp["has_qty_tbl"],
        "files": files,
    }


class DwgCorpusLookupTool:
    async def execute(self, content: str, ctx: dict | None = None) -> dict:
        try:
            args = json.loads(content) if content and content.strip() else {}
        except json.JSONDecodeError:
            args = {}
        mode = str(args.get("mode", "")).strip() or "jobs"
        job = str(args.get("job", "")).strip()

        try:
            result = self._dispatch(mode, job)
        except Exception as e:
            logger.exception("dwg_corpus_lookup failed")
            return {"error": f"dwg_corpus_lookup: {e}", "exit_code": 1}

        if isinstance(result, str):  # error message
            return {"error": result, "exit_code": 1}
        return {"output": json.dumps(result, indent=1), "exit_code": 0}

    def _dispatch(self, mode: str, job: str):
        holdout = _active_holdout_job()

        if mode == "jobs":
            fps = _load("census_fingerprints.json")
            tables = _load("qty_tbl_records.json")
            if fps is None and tables is None:
                return _NOT_BUILT
            dwg_jobs = {f["job_folder"] for f in (fps or []) if not _is_holdout(f["job_folder"], holdout)}
            qty_jobs = {t["job_folder"] for t in (tables or []) if not _is_holdout(t["job_folder"], holdout)}
            result = {
                "jobs": sorted(
                    {
                        j: {
                            "census_fingerprint": j in dwg_jobs,
                            "qty_tbl": j in qty_jobs,
                        }
                        for j in dwg_jobs | qty_jobs
                    }.items()
                ),
                "modes": {
                    "jobs": "this listing",
                    "vocabulary": "canonical qty_tbl row-label vocabulary",
                    "fingerprints": "compact census fingerprints for ALL past jobs (convention comparison)",
                    "fingerprint": "full census fingerprint for one job (requires job)",
                    "qty_tbl": "one job's completed takeoff sheet records (requires job)",
                },
            }
            if holdout:
                result["note"] = (
                    f"'{holdout}' is held out for this test run (it's the job being tested) "
                    "and is excluded from this listing and every other mode."
                )
            return result

        if mode == "vocabulary":
            vocab = _load("vocabulary.json")
            return vocab if vocab is not None else _NOT_BUILT

        if mode == "fingerprints":
            fps = _load("census_fingerprints.json")
            if fps is None:
                return _NOT_BUILT
            return [
                _fingerprint_summary(fp) for fp in fps
                if not _is_holdout(fp["job_folder"], holdout)
            ]

        if mode in ("fingerprint", "qty_tbl"):
            if not job:
                return f"mode '{mode}' requires a job (use mode 'jobs' to list them)"
            if _is_holdout(job, holdout):
                return (
                    f"'{job}' is held out for this test run (it's the job being tested) — "
                    "its own data is not available via dwg_corpus_lookup. Use another job as the analog."
                )
            data = _load(
                "census_fingerprints.json" if mode == "fingerprint" else "qty_tbl_records.json"
            )
            if data is None:
                return _NOT_BUILT
            matches = [
                d for d in data
                if job.lower() in d["job_folder"].lower() and not _is_holdout(d["job_folder"], holdout)
            ]
            if not matches:
                return f"no job matching '{job}' (use mode 'jobs' to list them)"
            return matches[0]

        return f"unknown mode '{mode}' (use mode 'jobs' for the mode list)"
