"""
Corpus builder CLI — regenerates the gitignored derived-data files under
src/dwg_pipeline/corpus/ from the Terra Underground business data root.

    python -m src.dwg_pipeline.build_corpus [--data-root PATH] [--skip-census]

Outputs (all derived, none committed to git):
    corpus/qty_tbl_records.json     one parsed qty_tbl per job (DQ-7)
    corpus/vocabulary.json          canonical closed row-label vocabulary (DQ-7)
    corpus/census_fingerprints.json per-DWG-job census payloads (DQ-8 analogs)
    corpus/dxf_cache/<job>/         converted DXFs, cached so reruns skip ODA

The data root defaults to the DWG_DATA_ROOT env var, falling back to the
known local path. Census fingerprints only cover jobs that actually have
DWG source files (8 today); qty_tbl records cover all ~16.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
DEFAULT_DATA_ROOT = os.environ.get(
    "DWG_DATA_ROOT",
    r"c:\Users\nolan\Desktop\Work\TERRA_UNDERGROUND\TERRA_UNDERGROUND\HEAVY_BID_RAG_PROJECT\data",
)


def _rank_variant(parts: tuple[str, ...]) -> tuple[tuple[int, int], str]:
    if "newest" in parts:
        return (2, 0), "newest"
    if "legacy" in parts:
        idx = parts.index("legacy")
        try:
            n = int(parts[idx + 1])
        except (IndexError, ValueError):
            n = 0
        return (1, n), f"legacy/{n}"
    return (0, 0), ""


def discover_dwg_jobs(data_root: Path) -> dict[str, dict]:
    """{job_folder: {variant, dwg_paths}} for every non-test job with DWGs."""
    per_job: dict[str, dict[str, list[Path]]] = {}
    for p in sorted(data_root.rglob("*.dwg")):
        parts = p.relative_to(data_root).parts
        if parts[0] == "test_copies":
            continue
        job = parts[0]
        if job.upper().endswith("_IGNORE"):
            continue
        _, variant = _rank_variant(parts[1:])
        per_job.setdefault(job, {}).setdefault(variant, []).append(p)

    result: dict[str, dict] = {}
    for job, variants in per_job.items():
        best = max(variants, key=lambda v: _rank_variant(tuple(v.split("/")))[0])
        result[job] = {"variant": best, "dwg_paths": variants[best]}
    return result


def build(data_root: Path, skip_census: bool = False) -> None:
    from src.dwg_pipeline.qty_tbl_parser import build_vocabulary, discover_qty_tbls

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Parsing qty_tbl files under {data_root} ...")
    tables = discover_qty_tbls(data_root)
    records = [t.to_dict() for t in tables]
    (CORPUS_DIR / "qty_tbl_records.json").write_text(
        json.dumps(records, indent=2), encoding="utf-8"
    )
    print(f"  {len(tables)} jobs -> corpus/qty_tbl_records.json")

    vocab = build_vocabulary(tables)
    (CORPUS_DIR / "vocabulary.json").write_text(
        json.dumps(vocab, indent=2), encoding="utf-8"
    )
    print(f"  {len(vocab)} canonical rows -> corpus/vocabulary.json")

    if skip_census:
        print("Skipping census fingerprints (--skip-census).")
        return

    from src.dwg_pipeline.census import census_job
    from src.dwg_qty.convert import convert_dwg_to_dxf

    dwg_jobs = discover_dwg_jobs(data_root)
    qty_jobs = {t.job_folder for t in tables}
    fingerprints: list[dict] = []
    for job, info in sorted(dwg_jobs.items()):
        cache_dir = CORPUS_DIR / "dxf_cache" / job
        cache_dir.mkdir(parents=True, exist_ok=True)
        dxf_paths: list[Path] = []
        for dwg in info["dwg_paths"]:
            cached = cache_dir / (dwg.stem + ".dxf")
            if not cached.exists():
                print(f"  converting {job}/{dwg.name} ...")
                try:
                    convert_dwg_to_dxf(dwg, cache_dir, timeout=300)
                except Exception as e:  # keep going — one bad file shouldn't sink the corpus
                    print(f"    FAILED: {e}", file=sys.stderr)
                    continue
            dxf_paths.append(cached)
        if not dxf_paths:
            print(f"  {job}: no convertible DWGs, skipped", file=sys.stderr)
            continue
        print(f"  censusing {job} ({len(dxf_paths)} file(s)) ...")
        fingerprints.append(
            {
                "job_folder": job,
                "variant": info["variant"],
                "has_qty_tbl": job in qty_jobs,
                "census": census_job(dxf_paths),
            }
        )

    (CORPUS_DIR / "census_fingerprints.json").write_text(
        json.dumps(fingerprints, indent=2), encoding="utf-8"
    )
    print(f"  {len(fingerprints)} jobs -> corpus/census_fingerprints.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--skip-census", action="store_true")
    args = ap.parse_args()

    data_root = Path(args.data_root)
    if not data_root.exists():
        ap.error(f"data root not found: {data_root}")
    build(data_root, skip_census=args.skip_census)


if __name__ == "__main__":
    main()
