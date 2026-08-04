"""
Run the real DWG extraction agent end-to-end against a known job and score
the predicted qty_tbl against the completed one on disk (DQ-13).

    python -m src.dwg_pipeline.eval.run_eval --job KILDERE \\
        --endpoint http://localhost:11434/v1 --model qwen2.5:32b [--owner you]

Uses the corpus dxf_cache for the job's converted DXFs (run
`python -m src.dwg_pipeline.build_corpus` first), provisions a throwaway
job folder under DATA_DIR/dwg_jobs/, and drives stream_agent_loop exactly
like a chat turn would (same system prompt, sandbox, tools; bash disabled).

Evaluation only: the diff never feeds back into the run (DQ-10, no QC loop).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import time
from pathlib import Path

from src.constants import DATA_DIR, DWG_JOBS_DIR
from src.dwg_pipeline.build_corpus import CORPUS_DIR
from src.dwg_pipeline.qty_tbl_parser import TOLERANCE, parse_predicted_table, score

EVAL_RUNS_DIR = Path(DATA_DIR) / "dwg_eval_runs"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_MESSAGE = (_PROMPTS_DIR / "eval_default_message.txt").read_text(encoding="utf-8").strip()


def _find_job(job_query: str) -> tuple[str, list[Path], list[dict]]:
    """Resolve a job substring to (job_folder, cached dxf paths, actual rows)."""
    records = json.loads((CORPUS_DIR / "qty_tbl_records.json").read_text(encoding="utf-8"))
    matches = [r for r in records if job_query.lower() in r["job_folder"].lower()]
    if not matches:
        raise SystemExit(f"no qty_tbl record matches {job_query!r}")
    job_folder = matches[0]["job_folder"]
    dxf_dir = CORPUS_DIR / "dxf_cache" / job_folder
    dxfs = sorted(dxf_dir.glob("*.dxf"))
    if not dxfs:
        raise SystemExit(
            f"no cached DXFs for {job_folder} — run `python -m src.dwg_pipeline.build_corpus`"
        )
    return job_folder, dxfs, matches[0]["rows"]


async def _drive_agent(endpoint: str, model: str, message: str,
                       job_dir: str, dxf_files: list[str], census: dict,
                       owner: str | None) -> tuple[str, int, int]:
    from src.agent_loop import stream_agent_loop
    from src.dwg_pipeline.context import build_dwg_system_prompt

    prompt = build_dwg_system_prompt(census, job_dir, dxf_files, forced=True)
    final_text: list[str] = []
    rounds = 0
    tool_calls = 0
    async for chunk in stream_agent_loop(
        endpoint,
        model,
        [{"role": "user", "content": message}],
        workspace=job_dir,
        disabled_tools={"bash"},
        owner=owner,
        extra_system=prompt,
        session_id=None,
    ):
        if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(chunk[6:])
        except json.JSONDecodeError:
            continue
        if "delta" in data and not data.get("thinking"):
            final_text.append(data["delta"])
        elif data.get("type") == "agent_step":
            rounds = max(rounds, data.get("round", 1))
            print(f"  [round {rounds}]", flush=True)
        elif data.get("type") == "tool_start":
            tool_calls += 1
            print(f"  [tool {tool_calls}] {data.get('tool', '?')}", flush=True)
    return "".join(final_text), rounds, tool_calls


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job", required=True, help="job folder substring, e.g. KILDERE")
    ap.add_argument("--endpoint", required=True, help="OpenAI-compatible endpoint URL")
    ap.add_argument("--model", required=True)
    ap.add_argument("--owner", default=None, help="odysseus admin username (omit in single-user mode)")
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--keep-job-dir", action="store_true")
    args = ap.parse_args()

    job_folder, dxfs, actual_rows = _find_job(args.job)
    ts = time.strftime("%Y%m%d_%H%M%S")
    job_dir = Path(DWG_JOBS_DIR) / f"eval_{ts}_{job_folder[:32]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    for p in dxfs:
        shutil.copy2(p, job_dir / p.name)
    # The job under test is, by definition, already in the corpus — exclude
    # its own answer key from dwg_corpus_lookup so the agent can't just read
    # off the completed qty_tbl instead of doing real extraction.
    (job_dir / "holdout_job.txt").write_text(job_folder, encoding="utf-8")

    from src.dwg_pipeline.census import census_job
    census = census_job(sorted(job_dir.glob("*.dxf")))
    (job_dir / "census.json").write_text(json.dumps(census, indent=1), encoding="utf-8")

    print(f"Running agent on {job_folder} ({len(dxfs)} DXF) via {args.model} ...")
    reply, rounds, tool_calls = asyncio.run(
        _drive_agent(args.endpoint, args.model, args.message,
                     str(job_dir), [p.name for p in dxfs], census, args.owner)
    )

    predicted = parse_predicted_table(reply)
    metrics = score(predicted, actual_rows)
    run_record = {
        "timestamp": ts,
        "job_folder": job_folder,
        "endpoint": args.endpoint,
        "model": args.model,
        "agent_rounds": rounds,
        "agent_tool_calls": tool_calls,
        "predicted_rows": len(predicted),
        "metrics": metrics,
        "final_reply": reply,
    }
    EVAL_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = EVAL_RUNS_DIR / f"{ts}_{job_folder[:32]}_{re.sub(r'[^A-Za-z0-9._-]+', '_', args.model)}.json"
    out.write_text(json.dumps(run_record, indent=1), encoding="utf-8")

    m = metrics
    print(f"\n{'=' * 60}")
    print(f"lines compared: {m['lines_compared']}/{m['actual_populated_rows']} populated rows")
    print(f"within {int(TOLERANCE * 100)}% tolerance: {m['lines_within_tolerance']}/{m['lines_compared']}")
    print(f"mean abs pct error: {m['mean_abs_pct_error']}")
    print(f"misses (Case B): {len(m['misses'])}   extras (Case A): {len(m['extras'])}")
    print(f"run record: {out}")

    if not args.keep_job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
