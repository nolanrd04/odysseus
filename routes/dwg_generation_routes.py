"""
DWG Generations API — browse the automatic live-run captures from
src/dwg_pipeline/generations.py (save_dwg_generation()), mirroring the
QP Generations endpoints in routes/quick_proposal_routes.py.

Unlike QP, there's no separate "actual" table to pair after the fact: a
DwgGeneration row is only ever written once the job matched a known corpus
job (src/dwg_pipeline/generations.py's conservative name-substring match),
so `metrics` (per-line predicted-vs-actual scoring, qty_tbl_parser.score())
is already computed and stored at capture time.
"""

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)


def setup_dwg_generation_routes():
    router = APIRouter(prefix="/api/dwg", tags=["dwg_generations"])

    @router.get("/generations")
    async def list_dwg_generations():
        """Browse every DWG job that has at least one captured generation,
        with a quick tolerance-hit-rate glance at the latest attempt."""
        from core.database import SessionLocal, DwgGeneration

        db = SessionLocal()
        try:
            gens = (
                db.query(DwgGeneration)
                .order_by(DwgGeneration.job_id, DwgGeneration.generation_index)
                .all()
            )
            by_job: dict = {}
            for g in gens:
                by_job.setdefault(g.job_id, []).append(g)

            jobs = []
            for job_id, job_gens in by_job.items():
                latest = max(job_gens, key=lambda g: g.generation_index)
                m = latest.metrics or {}
                jobs.append({
                    "job_id": job_id,
                    "corpus_job_folder": latest.corpus_job_folder,
                    "generation_count": len(job_gens),
                    "latest_generation_index": latest.generation_index,
                    "model": latest.model,
                    "lines_compared": m.get("lines_compared"),
                    "lines_within_tolerance": m.get("lines_within_tolerance"),
                    "mean_abs_pct_error": m.get("mean_abs_pct_error"),
                    "misses": len(m.get("misses", [])),
                    "extras": len(m.get("extras", [])),
                    "created_at": latest.created_at.isoformat() if latest.created_at else None,
                })
            jobs.sort(key=lambda j: j["created_at"] or "", reverse=True)
            return {"jobs": jobs}
        finally:
            db.close()

    @router.get("/generations/{job_id}")
    async def get_dwg_generation_detail(job_id: str):
        """Full detail for one DWG job: every captured generation attempt,
        each with its own predicted-vs-actual scoring."""
        from core.database import SessionLocal, DwgGeneration

        db = SessionLocal()
        try:
            gens = (
                db.query(DwgGeneration)
                .filter(DwgGeneration.job_id == job_id)
                .order_by(DwgGeneration.generation_index)
                .all()
            )
            if not gens:
                raise HTTPException(404, f"No generations found for job {job_id}")

            return {
                "job_id": job_id,
                "corpus_job_folder": gens[-1].corpus_job_folder,
                "generations": [
                    {
                        "id": g.id,
                        "generation_index": g.generation_index,
                        "model": g.model,
                        "dxf_files": g.dxf_files,
                        "predicted_rows": g.predicted_rows,
                        "metrics": g.metrics,
                        "created_at": g.created_at.isoformat() if g.created_at else None,
                    }
                    for g in gens
                ],
            }
        finally:
            db.close()

    @router.get("/reliability")
    async def get_dwg_reliability():
        """Cross-run reliability report: per-work-type mean/median % error and
        sample counts, built from the LATEST generation per job_id (avoids
        double-counting reruns of the same job) — informational only, not
        wired into any prompt or gate. Mirrors QP's actuals-reliability shape."""
        from core.database import SessionLocal, DwgGeneration

        db = SessionLocal()
        try:
            gens = db.query(DwgGeneration).order_by(DwgGeneration.generation_index).all()
            latest_by_job: dict = {}
            for g in gens:
                latest_by_job[g.job_id] = g  # last write wins == highest generation_index

            per_field_pct: dict = {}
            miss_counts: dict = {}
            for g in latest_by_job.values():
                m = g.metrics or {}
                for row in m.get("per_line", []):
                    if row.get("pct_error") is None:
                        continue
                    per_field_pct.setdefault(row["work_type"], []).append(row["pct_error"])
                for miss in m.get("misses", []):
                    miss_counts[miss["work_type"]] = miss_counts.get(miss["work_type"], 0) + 1

            fields = []
            for work_type, pcts in per_field_pct.items():
                n = len(pcts)
                pcts_sorted = sorted(pcts)
                median = (
                    pcts_sorted[n // 2] if n % 2
                    else (pcts_sorted[n // 2 - 1] + pcts_sorted[n // 2]) / 2.0
                )
                fields.append({
                    "work_type": work_type,
                    "sample_count": n,
                    "mean_pct_error": sum(pcts) / n,
                    "median_pct_error": median,
                    "min_pct_error": min(pcts),
                    "max_pct_error": max(pcts),
                    "missed_count": miss_counts.get(work_type, 0),
                })
            fields.sort(key=lambda f: -f["sample_count"])
            return {"fields": fields, "jobs_scored": len(latest_by_job)}
        finally:
            db.close()

    return router
