#!/usr/bin/env python3
"""Import a Quick Proposal case-library export (see scripts/export_case_library.py)
into THIS machine's DB (whatever core.database.DATABASE_URL / DATABASE_URL env
var points to on the machine this script runs on).

Run this ON the target Odysseus instance (e.g. the company server), after
copying the export JSON there — it writes through case_store.upsert_job, the
same path the app's own case-library routes use, so normal
create/update semantics apply per slug: a slug not already present is
created; a slug already present is replaced in place (created_at preserved).

Dry run by default — nothing is written until --apply is passed.

Usage:
    python scripts/import_case_library.py qp_case_library_export.json              # dry run, report only
    python scripts/import_case_library.py qp_case_library_export.json --apply       # write to DB
    python scripts/import_case_library.py qp_case_library_export.json --apply --skip-existing
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import SessionLocal
from src.quick_proposal import case_store


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="Path to the export JSON produced by export_case_library.py")
    p.add_argument("--apply", action="store_true", help="Write to the DB (default: dry run, report only).")
    p.add_argument("--skip-existing", action="store_true",
                    help="Don't touch jobs whose slug already exists on this machine (default: overwrite them).")
    args = p.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        payload = json.load(f)

    jobs = payload.get("jobs")
    if jobs is None:
        print("ERROR: input file has no top-level 'jobs' key — is this an export_case_library.py file?")
        sys.exit(1)

    db = SessionLocal()
    try:
        created, updated, skipped = [], [], []
        for slug, content in sorted(jobs.items()):
            existing = case_store.get_job(db, slug)
            if existing is not None and args.skip_existing:
                skipped.append(slug)
                continue
            if args.apply:
                case_store.upsert_job(db, slug, content)
            (updated if existing is not None else created).append(slug)

        if args.apply:
            db.commit()

        print(f"{'Applied' if args.apply else 'Dry run'}: "
              f"{len(created)} to create, {len(updated)} to update, {len(skipped)} skipped "
              f"(existing, --skip-existing set).")
        for label, slugs in (("CREATE", created), ("UPDATE", updated), ("SKIP", skipped)):
            for slug in slugs:
                print(f"  {label}  {slug}")

        if not args.apply:
            print("\nDry run only — nothing written. Re-run with --apply to persist.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
