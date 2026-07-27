"""
One-off repair for QP phase-6 chat sessions poisoned by an orphaned tool_use
block (see routes/quick_proposal_routes.py::_qp_continuation_task /
phase5_extraction_loop — a tool call whose result never got persisted because
of a mid-dispatch exception or an unrecognized tool name, both fixed going
forward in this same change).

An orphaned tool_use permanently breaks every future turn in that session:
Anthropic rejects the whole history with

    400 invalid_request_error: tool_use ids were found without tool_result
    blocks immediately after

This script finds any assistant ChatMessage row whose tool_calls were never
all answered by a following tool-role row, and inserts a synthetic tool
result for each missing id so the history becomes valid again. It does NOT
touch anything else — no run data, no proposal numbers, no other messages.

Usage:
    python scripts/repair_orphaned_tool_use.py            # dry run, reports only
    python scripts/repair_orphaned_tool_use.py --apply     # writes the fix
    python scripts/repair_orphaned_tool_use.py --apply --db /path/to/app.db

By default it resolves the DB path the same way core/database.py does
(DATABASE_URL env var, falling back to data/app.db under the repo root).
A timestamped .bak copy of the sqlite file is made before any write.
"""
import argparse
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path


def resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    import os
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", "", 1))
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "data" / "app.db"


def find_orphans(conn: sqlite3.Connection):
    """Mirror the exact reload logic in _qp_continuation_task / phase5_extraction_loop:
    skip system rows, skip tool rows with no tool_call_id (gemini subtool rows),
    and track which tool_call_ids from an assistant turn are still unanswered
    once the next user/assistant message (or end of session) arrives.
    """
    cur = conn.cursor()
    cur.execute("SELECT id, session_id, role, content, metadata, timestamp "
                "FROM chat_messages ORDER BY session_id, timestamp, id")
    rows = cur.fetchall()

    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r[1], []).append(r)

    orphans = []  # (session_id, tool_call_id, tool_name, assistant_row, next_row_or_None)

    for session_id, msgs in by_session.items():
        pending: dict[str, tuple] = {}  # tool_call_id -> (tool_name, assistant_row)

        def flush(next_row):
            for tcid, (tname, arow) in pending.items():
                orphans.append((session_id, tcid, tname, arow, next_row))
            pending.clear()

        for row in msgs:
            role, metadata = row[2], row[4]
            meta = json.loads(metadata) if metadata else {}
            if role == "system":
                continue
            if role == "assistant":
                flush(row)  # anything still pending from a prior turn is orphaned
                for tc in (meta.get("tool_calls") or []):
                    tcid = tc.get("id")
                    tname = (tc.get("function") or {}).get("name", "")
                    if tcid:
                        pending[tcid] = (tname, row)
            elif role == "tool":
                tcid = meta.get("tool_call_id")
                if tcid:
                    pending.pop(tcid, None)
                # rows with no tool_call_id are gemini subtool rows — ignored,
                # same as the live reload code.
            elif role == "user":
                flush(row)
        flush(None)  # end of session — still-pending ids are orphaned too

    return orphans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="Path to app.db (sqlite). Defaults to DATABASE_URL / data/app.db")
    ap.add_argument("--apply", action="store_true", help="Write the fix. Without this flag, only reports.")
    args = ap.parse_args()

    db_path = resolve_db_path(args.db)
    if not db_path.is_file():
        print(f"DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    orphans = find_orphans(conn)

    if not orphans:
        print(f"No orphaned tool_use blocks found in {db_path}. Nothing to do.")
        return

    print(f"Found {len(orphans)} orphaned tool_use block(s) in {db_path}:")
    for session_id, tcid, tname, arow, next_row in orphans:
        boundary = f"before next msg_id={next_row[0]}" if next_row is not None else "end of session"
        print(f"  session={session_id}  tool={tname or '?'}  tool_call_id={tcid}  "
              f"after_msg_id={arow[0]}  ({boundary})")

    if not args.apply:
        print("\nDry run only — re-run with --apply to insert synthetic tool_result rows.")
        return

    backup_path = db_path.with_suffix(db_path.suffix + f".bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(db_path, backup_path)
    print(f"\nBacked up DB to {backup_path}")

    cur = conn.cursor()
    for session_id, tcid, tname, arow, next_row in orphans:
        assistant_ts = arow[5]
        try:
            assistant_dt = datetime.fromisoformat(assistant_ts)
            # Land strictly between the assistant's tool_calls row and whatever
            # comes next (if anything) so ordering by timestamp stays correct —
            # a blind "+1ms" can overshoot a next row that arrived faster than
            # that (these all-local-DB calls often land within a few ms of
            # each other).
            if next_row is not None:
                next_dt = datetime.fromisoformat(next_row[5])
                synth_dt = assistant_dt + (next_dt - assistant_dt) / 2
            else:
                synth_dt = assistant_dt + timedelta(milliseconds=1)
            synth_ts = synth_dt.isoformat(sep=" ")
        except (ValueError, TypeError):
            synth_ts = assistant_ts  # best effort — ordering by id will still be stable enough

        content = (
            "[No result was recorded for this tool call — a prior app bug let the "
            "assistant's tool call persist without its result. This placeholder was "
            "inserted by scripts/repair_orphaned_tool_use.py so the conversation "
            "history is valid again.]"
        )
        meta = json.dumps({"tool_call_id": tcid, "tool_name": tname, "repaired": True})
        cur.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, metadata, timestamp) "
            "VALUES (?, ?, 'tool', ?, ?, ?)",
            (uuid.uuid4().hex, session_id, content, meta, synth_ts),
        )
    conn.commit()
    print(f"Inserted {len(orphans)} synthetic tool_result row(s). Restart the app if it caches sessions in memory.")


if __name__ == "__main__":
    main()