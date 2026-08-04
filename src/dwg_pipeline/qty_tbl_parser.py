"""
Parser for Terra Underground's qty_tbl_*.txt takeoff sheets (DQ-7 part 1).

The qty_tbl files are markdown pipe-tables with a fixed master template:

    | General plan group | Work type | Unit | TAKEOFF | QTY | Notes | OTHER1 | OTHER2 | OTHER3 |

Empirical structure (confirmed line-by-line on Woodman + Kildere, 2026_7_30.2):
  - The row-label set is a fixed, closed master template (~60-70 rows under
    ~7 group buckets), versioned slightly over time — NOT per-job free text.
  - `General plan group` appears only on the first row of a group section;
    continuation rows leave it blank (forward-fill).
  - The same group label can open multiple distinct sections (GRADING appears
    for ROW grading, cut/fill, and LOT grading) — sections are positional.
  - Unpopulated rows are left blank (empty QTY), not omitted.

This module is pure deterministic extraction — no LLM anywhere (the ledger's
standing deterministic-data/LLM-judgment split).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class QtyRow:
    group: str            # forward-filled General plan group ("" for MOB-style preamble rows)
    work_type: str        # raw row label, exactly as written
    unit: str             # "LF" / "EA" / "SF" / "SY" / "CY" / "LS" / ""
    takeoff: str          # who took it off (e.g. "CPH", "BR") — provenance, not qty
    qty: float | None     # None when the row is unpopulated (blank on the sheet)
    notes: str
    section_index: int    # 0-based index of the group *section* this row sits in


@dataclass
class QtyTable:
    job_folder: str       # e.g. "25012-2_KILDERE_MEADOWS"
    variant: str          # e.g. "legacy/1", "newest", or "" (file at job root)
    source_path: str
    rows: list[QtyRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _split_md_row(line: str) -> list[str] | None:
    """Split one markdown pipe-table line into stripped cells, or None."""
    line = line.rstrip()
    if not line.startswith("|"):
        return None
    # drop leading/trailing empty cells produced by the outer pipes
    cells = [c.strip() for c in line.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


_SEPARATOR_RE = re.compile(r"^:?-{2,}:?$")


def _parse_qty(cell: str) -> float | None:
    cell = cell.strip().replace(",", "")
    if not cell:
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def parse_qty_tbl(path: Path | str, job_folder: str = "", variant: str = "") -> QtyTable:
    """
    Parse one qty_tbl_*.txt file into a QtyTable.

    Tolerant of missing OTHER columns and stray blank lines. Rows with an
    empty Work type are skipped. The header row is located by looking for
    "work type" in the second column rather than assumed to be line 1.
    """
    path = Path(path)
    table = QtyTable(job_folder=job_folder, variant=variant, source_path=str(path))

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    header_seen = False
    current_group = ""
    section_index = -1

    for line in lines:
        cells = _split_md_row(line)
        if cells is None or not cells:
            continue
        if not header_seen:
            if len(cells) >= 2 and "work type" in cells[1].lower():
                header_seen = True
            continue
        if all(_SEPARATOR_RE.match(c) for c in cells if c):
            continue

        # pad so indexing is safe regardless of how many OTHER columns exist
        cells += [""] * (9 - len(cells))
        group_cell, work_type, unit, takeoff, qty_cell, notes = cells[:6]

        if group_cell:
            current_group = group_cell
            section_index += 1
        elif section_index < 0:
            # preamble rows (e.g. MOB) before any group label
            section_index = 0

        if not work_type:
            continue

        table.rows.append(
            QtyRow(
                group=current_group,
                work_type=work_type,
                unit=unit,
                takeoff=takeoff,
                qty=_parse_qty(qty_cell),
                notes=notes,
                section_index=max(section_index, 0),
            )
        )
    return table


def normalize_label(label: str) -> str:
    """Matching key for a Work type label: uppercase, collapsed whitespace."""
    return re.sub(r"\s+", " ", label).strip().upper()


TOLERANCE = 0.10  # DQ-12: 10% per line item


def parse_predicted_table(text: str) -> list[dict]:
    """Pull the last qty_tbl-shaped markdown table out of an agent's reply.

    Rows: {group, work_type, unit, qty}. Group forward-fills; separator and
    header rows are skipped; rows with an empty/non-numeric QTY parse to None.
    Shared by the offline eval harness (eval/run_eval.py) and the live
    auto-capture hook (generations.py) so both score identically.
    """
    rows: list[dict] = []
    current: list[dict] = []
    in_table = False
    header_cols: list[str] | None = None
    group = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table and current:
                rows = current  # keep the LAST complete table seen
            in_table = False
            header_cols = None
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not in_table:
            in_table = True
            current = []
            group = ""
            header_cols = [c.lower() for c in cells]
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        # map columns by header when it looks like a qty_tbl; else positional
        def col(name: str, default_idx: int) -> str:
            if header_cols:
                for i, h in enumerate(header_cols):
                    if name in h:
                        return cells[i] if i < len(cells) else ""
            return cells[default_idx] if default_idx < len(cells) else ""

        g = col("group", 0)
        wt = col("work type", 1)
        unit = col("unit", 2)
        qty_raw = col("qty", 3).replace(",", "")
        if g:
            group = g
        if not wt:
            continue
        try:
            qty = float(qty_raw) if qty_raw else None
        except ValueError:
            qty = None
        current.append({"group": group, "work_type": wt, "unit": unit, "qty": qty})
    if in_table and current:
        rows = current
    return rows


def score(predicted: list[dict], actual_rows: list[dict], tolerance: float = TOLERANCE) -> dict:
    """DQ-12 metrics: per-line pct distance, tolerance hit-rate, misses/extras.

    `predicted`/`actual_rows` are lists of {work_type, qty, ...} dicts (the
    shape parse_predicted_table() and QtyTable.rows produce). Shared by the
    offline eval harness and the live auto-capture hook.
    """
    actual = {
        normalize_label(r["work_type"]): r
        for r in actual_rows
        if r.get("qty") is not None
    }
    pred = {
        normalize_label(r["work_type"]): r
        for r in predicted
        if r.get("qty") is not None
    }

    per_line = []
    for key, arow in sorted(actual.items()):
        prow = pred.get(key)
        if prow is None:
            continue
        a, p = float(arow["qty"]), float(prow["qty"])
        pct = abs(p - a) / abs(a) if a else (0.0 if p == 0 else float("inf"))
        per_line.append(
            {
                "work_type": arow["work_type"],
                "group": arow.get("group", ""),
                "unit": arow.get("unit", ""),
                "actual": a,
                "predicted": p,
                "pct_error": round(pct, 4) if pct != float("inf") else None,
                "within_tolerance": pct <= tolerance,
            }
        )

    misses = [  # Case B: populated in reality, absent/blank in prediction
        {"work_type": r["work_type"], "group": r.get("group", ""), "actual": r["qty"]}
        for k, r in sorted(actual.items())
        if k not in pred
    ]
    extras = [  # Case A: predicted with a value, not populated in reality
        {"work_type": r["work_type"], "group": r.get("group", ""), "predicted": r["qty"]}
        for k, r in sorted(pred.items())
        if k not in actual
    ]

    finite = [l["pct_error"] for l in per_line if l["pct_error"] is not None]
    return {
        "tolerance": tolerance,
        "lines_compared": len(per_line),
        "lines_within_tolerance": sum(1 for l in per_line if l["within_tolerance"]),
        "mean_abs_pct_error": round(sum(finite) / len(finite), 4) if finite else None,
        "actual_populated_rows": len(actual),
        "per_line": per_line,
        "misses": misses,
        "extras": extras,
    }


def discover_qty_tbls(data_root: Path | str) -> list[QtyTable]:
    """
    Find and parse every job's qty_tbl under `data_root`
    (HEAVY_BID_RAG_PROJECT/data), one table per job:

      - `test_copies/` is skipped entirely.
      - When a job has multiple variants, prefer `newest`, else the
        highest-numbered `legacy/N`, else a file at the job folder root.
    """
    data_root = Path(data_root)
    per_job: dict[str, list[tuple[tuple[int, int], str, Path]]] = {}

    for p in sorted(data_root.rglob("qty_tbl_*.txt")):
        rel = p.relative_to(data_root)
        parts = rel.parts
        if parts[0] == "test_copies":
            continue
        job_folder = parts[0]
        if job_folder.upper().endswith("_IGNORE"):
            continue
        # rank: newest > legacy/N (higher N first) > root file
        if "newest" in parts:
            rank = (2, 0)
            variant = "newest"
        elif "legacy" in parts:
            idx = parts.index("legacy")
            try:
                n = int(parts[idx + 1])
            except (IndexError, ValueError):
                n = 0
            rank = (1, n)
            variant = f"legacy/{n}"
        else:
            rank = (0, 0)
            variant = ""
        per_job.setdefault(job_folder, []).append((rank, variant, p))

    tables: list[QtyTable] = []
    for job_folder, candidates in sorted(per_job.items()):
        rank, variant, path = max(candidates, key=lambda c: c[0])
        tables.append(parse_qty_tbl(path, job_folder=job_folder, variant=variant))
    return tables


def build_vocabulary(tables: list[QtyTable]) -> list[dict]:
    """
    Union all jobs' rows into the canonical closed row-label vocabulary
    (DQ-7 part 1): ordered list of
      {group, work_type, units: [..], jobs_present, jobs_populated}.

    Ordering: the table with the most rows becomes the base sequence
    (template versions only ever *add* rows); any (group, work_type) pair
    unseen in the base is appended immediately after the last row of the
    same group, preserving that group's internal order, or at the end if
    the group itself is new.
    """
    if not tables:
        return []

    base = max(tables, key=lambda t: len(t.rows))
    vocab: list[dict] = []
    index: dict[tuple[str, str], dict] = {}

    def add_entry(row: QtyRow, position: int | None = None) -> dict:
        entry = {
            "group": row.group,
            "work_type": row.work_type,
            "units": [row.unit] if row.unit else [],
            "jobs_present": 0,
            "jobs_populated": 0,
        }
        if position is None:
            vocab.append(entry)
        else:
            vocab.insert(position, entry)
        index[(normalize_label(row.group), normalize_label(row.work_type))] = entry
        return entry

    for row in base.rows:
        key = (normalize_label(row.group), normalize_label(row.work_type))
        if key not in index:
            add_entry(row)

    for table in tables:
        for row in table.rows:
            key = (normalize_label(row.group), normalize_label(row.work_type))
            if key not in index:
                # insert after the last vocab row of the same group, else append
                gkey = normalize_label(row.group)
                position = None
                for i in range(len(vocab) - 1, -1, -1):
                    if normalize_label(vocab[i]["group"]) == gkey:
                        position = i + 1
                        break
                add_entry(row, position)

    # populate stats + unit variants
    for table in tables:
        seen_in_job: set[tuple[str, str]] = set()
        for row in table.rows:
            key = (normalize_label(row.group), normalize_label(row.work_type))
            entry = index[key]
            if key not in seen_in_job:
                entry["jobs_present"] += 1
                seen_in_job.add(key)
                if row.qty is not None:
                    entry["jobs_populated"] += 1
            if row.unit and row.unit not in entry["units"]:
                entry["units"].append(row.unit)

    return vocab


def vocabulary_as_template(vocab: list[dict]) -> str:
    """
    Render the vocabulary as a blank qty_tbl-shaped markdown table — the
    exact output template the extraction agent fills in (DQ-17: output must
    match Terra's qty_tbl format).
    """
    lines = [
        "| General plan group | Work type | Unit | QTY | Notes |",
        "|:--|:--|:--|--:|:--|",
    ]
    prev_group = None
    for entry in vocab:
        group = entry["group"] if entry["group"] != prev_group else ""
        prev_group = entry["group"]
        unit = entry["units"][0] if entry["units"] else ""
        lines.append(f"| {group} | {entry['work_type']} | {unit} |  |  |")
    return "\n".join(lines)
