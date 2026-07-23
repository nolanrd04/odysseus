#!/usr/bin/env python3
"""Export the Quick Proposal case library (QpJobData + children) to a single JSON file.

Why: the reference job data lives normalized across 5 DB tables
(QpJobData/QpJobDetails/QpJobScaleMetrics/QpJobRevision/QpLineItem, see
src/quick_proposal/case_store.py) rather than as files, so it can't just be
copied off disk. This dumps every job back into the same nested content-dict
shape the app itself uses (via case_store.reassemble), keyed by slug, so it
can be moved to another Odysseus instance and loaded with
scripts/import_case_library.py there.

Usage:
    python scripts/export_case_library.py                       # all jobs -> scripts/qp_case_library_export.json
    python scripts/export_case_library.py --output C:\\path\\out.json
    python scripts/export_case_library.py --slugs job-a job-b    # only specific jobs
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import SessionLocal, QpJobData
from src.quick_proposal import case_store


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", default=os.path.join(os.path.dirname(__file__), "qp_case_library_export.json"),
                    help="Path to write the export JSON to.")
    p.add_argument("--slugs", nargs="*", default=None,
                    help="Only export these job slugs (default: every job).")
    args = p.parse_args()

    db = SessionLocal()
    try:
        query = db.query(QpJobData).order_by(QpJobData.slug)
        if args.slugs:
            query = query.filter(QpJobData.slug.in_(args.slugs))
        jobs = query.all()

        if args.slugs:
            missing = set(args.slugs) - {j.slug for j in jobs}
            for slug in sorted(missing):
                print(f"WARNING: slug not found, skipped: {slug}")

        records = {j.slug: case_store.reassemble(j) for j in jobs}
    finally:
        db.close()

    payload = {
        "export_format": "qp_case_library_v1",
        "job_count": len(records),
        "jobs": records,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(records)} job(s) to {args.output}")
    for slug in sorted(records):
        print(f"  - {slug}")


if __name__ == "__main__":
    main()
