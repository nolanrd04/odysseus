"""Parses Terra-format real-bid actuals .txt files and matches them to QP runs
by normalized job name — TODO_B_NEW-2 auto-pairing.

Session 31 built the QpActual pairing infra (routes/quick_proposal_routes.py)
but only ever populated it via a one-time, uncommitted scratchpad script that
matched documentation/temp/actuals/*.txt against the run_ids that existed at
the time (2026-07-15/16). Nothing paired actuals to runs created afterward —
this module is the reusable matching/parsing core for both the live
auto-pair-on-generation-end hook and a repeatable backfill script, so newly
dropped actuals files or newly run jobs get paired going forward instead of
requiring another one-off load.
"""
import re
from pathlib import Path
from typing import Optional

ACTUALS_DIR = Path(__file__).resolve().parent.parent.parent / "documentation" / "temp" / "actuals"

_HEADER_NUM_RE = re.compile(r"^JOB NUMBER:\s*(\S+)", re.M)
_HEADER_NAME_RE = re.compile(r"^JOB NAME:\s*(.+?)\s*$", re.M)
_GRAND_TOTAL_RE = re.compile(r"^GRAND TOTAL\s+\$?([\d,]+\.\d{2})\s*$", re.M)
# Line items after this marker are optional/alternate — NOT included in the
# file's own GRAND TOTAL, so they're excluded from the parsed line_items too.
_OPTIONAL_SECTION_MARKER = "OPTIONAL / ALTERNATE"
_LINE_ITEM_RE = re.compile(
    r"^(?P<desc>\S.*?)\s{2,}(?P<qty>[\d,]+(?:\.\d+)?)\s+(?P<unit>[A-Za-z]{1,5})\s+"
    r"\$(?P<unit_price>[\d,]+\.\d+)\s+\$(?P<ext_price>[\d,]+\.\d+)\s*$",
    re.M,
)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def _num(s: str) -> Optional[float]:
    return float(s.replace(",", "")) if s else None


def parse_actuals_file(path: Path) -> Optional[dict]:
    """Parses one Terra-format actuals .txt into
    {job_number, job_name, grand_total, line_items, source_file}.

    Returns None if the file is missing the minimum required fields (job
    number/name/grand total/at least one line item) — callers should treat
    that as "not a usable actuals file", not an error.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    num_m = _HEADER_NUM_RE.search(text)
    name_m = _HEADER_NAME_RE.search(text)
    gt_m = _GRAND_TOTAL_RE.search(text)
    if not (num_m and name_m and gt_m):
        return None

    optional_idx = text.find(_OPTIONAL_SECTION_MARKER)
    line_items = [
        {
            "description": m.group("desc").strip(),
            "quantity": _num(m.group("qty")),
            "unit": m.group("unit"),
            "unit_price": _num(m.group("unit_price")),
            "ext_price": _num(m.group("ext_price")),
        }
        for m in _LINE_ITEM_RE.finditer(text)
        if optional_idx == -1 or m.start() < optional_idx
    ]
    if not line_items:
        return None

    return {
        "job_number": num_m.group(1),
        "job_name": name_m.group(1),
        "grand_total": _num(gt_m.group(1)),
        "line_items": line_items,
        "source_file": path.name,
    }


def list_parsed_actuals() -> list:
    """Parses every .txt in ACTUALS_DIR (gitignored — returns [] if absent,
    not an error, since a fresh checkout won't have it)."""
    if not ACTUALS_DIR.is_dir():
        return []
    parsed = []
    for f in sorted(ACTUALS_DIR.glob("*.txt")):
        r = parse_actuals_file(f)
        if r:
            parsed.append(r)
    return parsed


def normalize_job_name(name: str) -> str:
    return _NON_ALNUM_RE.sub("", (name or "").upper())


def find_actual_match(candidate_names: list) -> Optional[dict]:
    """Given candidate names for a run (its run_name, its extracted job_name
    if present, etc.), finds a parsed actuals file whose JOB NAME normalizes
    to a substring match (either direction) of any candidate.

    Deliberately conservative — no fuzzy/token-overlap fallback. A wrong
    pairing here silently poisons the reliability report with a bogus actual,
    which is worse than leaving a run unpaired (see the session 31 handoff's
    "don't trust the obvious identifier if a more reliable one exists" note
    on the same risk with raw upload filenames). Returns the first match, or
    None if nothing lines up.
    """
    normalized_candidates = [normalize_job_name(n) for n in candidate_names if n]
    normalized_candidates = [n for n in normalized_candidates if n]
    if not normalized_candidates:
        return None
    for actual in list_parsed_actuals():
        actual_key = normalize_job_name(actual["job_name"])
        if not actual_key:
            continue
        for cand in normalized_candidates:
            if actual_key in cand or cand in actual_key:
                return actual
    return None


def build_actual_values(parsed: dict) -> dict:
    """Mirrors the {job_number, job_name, line_items} shape already stored in
    QpActual.actual_values by the original session-31 backfill, so existing
    consumers (_diff_generation_vs_actual, the TODO_B_NEW-2 rule-based
    matchers) keep working unchanged."""
    return {
        "job_number": parsed["job_number"],
        "job_name": parsed["job_name"],
        "line_items": parsed["line_items"],
    }
