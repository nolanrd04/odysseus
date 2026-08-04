"""
Actuals-reliability report (DQ-14, minimal v1): aggregate every persisted
eval run under DATA_DIR/dwg_eval_runs/ into one markdown report.

    python -m src.dwg_pipeline.eval.report [--out report.md]

Groups runs by (job, model) and reports tolerance hit-rate, mean error, and
miss/extra counts, plus the most-frequently-missed rows across all runs —
the signal for which template rows the pipeline is least reliable on.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict

from src.dwg_pipeline.eval.run_eval import EVAL_RUNS_DIR


def build_report() -> str:
    runs = []
    for p in sorted(EVAL_RUNS_DIR.glob("*.json")):
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    if not runs:
        return (
            "# DWG pipeline reliability report\n\nNo eval runs found. Run "
            "`python -m src.dwg_pipeline.eval.run_eval` first.\n"
        )

    lines = [
        "# DWG pipeline reliability report",
        "",
        f"{len(runs)} eval run(s) on record.",
        "",
        "| job | model | run | compared | within tol | mean abs % err | misses | extras |",
        "|:--|:--|:--|--:|--:|--:|--:|--:|",
    ]
    missed_counter: Counter = Counter()
    by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in runs:
        m = r["metrics"]
        by_group[(r["job_folder"], r["model"])].append(r)
        for miss in m["misses"]:
            missed_counter[miss["work_type"]] += 1
        pct = (
            f"{m['mean_abs_pct_error'] * 100:.1f}%"
            if m["mean_abs_pct_error"] is not None
            else "—"
        )
        lines.append(
            f"| {r['job_folder']} | {r['model']} | {r['timestamp']} "
            f"| {m['lines_compared']}/{m['actual_populated_rows']} "
            f"| {m['lines_within_tolerance']}/{m['lines_compared']} "
            f"| {pct} | {len(m['misses'])} | {len(m['extras'])} |"
        )

    if missed_counter:
        lines += [
            "",
            "## Most-missed rows (Case B, across all runs)",
            "",
            "| work_type | missed in N runs |",
            "|:--|--:|",
        ]
        for wt, n in missed_counter.most_common(20):
            lines.append(f"| {wt} | {n} |")

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="write to a file instead of stdout")
    args = ap.parse_args()
    report = build_report()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"wrote {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
