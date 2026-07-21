#!/usr/bin/env python3
"""Pair real-bid actuals files to Quick Proposal runs that don't have one yet.

Why: QpActual rows (routes/quick_proposal_routes.py's `PUT /actuals/{run_id}`,
TODO_B_NEW) were only ever populated by a one-time, uncommitted scratchpad
script run on 2026-07-15/16 against the run_ids that existed at that moment.
Any run created since then, or any actuals .txt dropped into
documentation/temp/actuals/ since then, was never paired. This script is the
repeatable version of that load — reuses src/quick_proposal/actuals_matcher.py
so the same conservative name-matching logic backs both this script and the
live auto-pair-on-generation-end hook in _save_generation_snapshot.

Matching is intentionally conservative (normalized-name substring match, no
fuzzy fallback) and NEVER overwrites an existing QpActual row — an estimator-
or script-entered actual always wins over an auto-match.

Usage:
    python scripts/backfill_qp_actuals.py              # dry run, report only
    python scripts/backfill_qp_actuals.py --apply       # write matches to DB
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid

from core.database import SessionLocal, QpGeneration, QpActual
from routes.quick_proposal_routes import _candidate_names_for_run
from src.quick_proposal import actuals_matcher


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="Write matches to the DB (default: dry run, report only).")
    args = p.parse_args()

    db = SessionLocal()
    try:
        already_paired = {a.run_id for a in db.query(QpActual).all()}

        latest_gen_by_run = {}
        for g in db.query(QpGeneration).order_by(QpGeneration.generation_index).all():
            latest_gen_by_run[g.run_id] = g  # last write wins == highest generation_index

        unpaired_run_ids = sorted(rid for rid in latest_gen_by_run if rid not in already_paired)
        print(f"{len(latest_gen_by_run)} runs have a generation snapshot; "
              f"{len(already_paired)} already have an actual; "
              f"{len(unpaired_run_ids)} unpaired.\n")

        matched = 0
        for run_id in unpaired_run_ids:
            names = _candidate_names_for_run(run_id, latest_gen_by_run[run_id].results_snapshot)
            match = actuals_matcher.find_actual_match(names)
            if not match:
                continue
            matched += 1
            label = " / ".join(n for n in names if n) or "(no name found)"
            print(f"MATCH  {run_id}  [{label}]  -> job {match['job_number']} "
                  f"{match['job_name']}  (${match['grand_total']:,.2f}, "
                  f"{len(match['line_items'])} line items)")
            if args.apply:
                db.add(QpActual(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    actual_total=match["grand_total"],
                    actual_values=actuals_matcher.build_actual_values(match),
                    notes=(
                        f"Auto-paired — job {match['job_number']} {match['job_name']}, "
                        f"source: documentation/temp/actuals/{match['source_file']} "
                        f"({len(match['line_items'])} line items)"
                    ),
                ))

        print(f"\n{matched} new match(es) out of {len(unpaired_run_ids)} unpaired runs.")
        if args.apply:
            db.commit()
            print("Applied — written to the DB.")
        else:
            print("Dry run — nothing written. Re-run with --apply to persist these matches.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
