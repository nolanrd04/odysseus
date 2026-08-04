"""
Arc-inclusive length measurement and duplicate/near-duplicate entity dedup.

Validated by:
  - Key Finding #4, session 2026_7_28.2 (and the equivalent finding in
    2026_7_28.1 / 2026_7_27.2): arc-inclusive polyline/line/arc length is the
    single most reliable technique across all 4 jobs studied for anything
    with a dedicated per-material-per-size layer — sewer/water mains and
    services, storm pipe, irrigation sleeving all land within 0-2.3% (often
    <0.1%) once arcs (including LWPOLYLINE bulges) are included, not just
    straight-segment distance.
  - Key Finding #11, session 2026_7_28.2 (same-day follow-up): a raw
    per-layer length sum can be wrong even before any qty-sheet-scope
    question comes up, because the layer itself can contain literal
    duplicate/near-duplicate entities (Kildere's `P-ROAD-CURB`: 5 pairs,
    334 ft of redundant geometry, cutting the error from +38% to +19%
    before any scope question was even relevant). This is the SECOND time
    this project hit a raw-sum bug needing a structural fix before the qty
    sheet comparison was meaningful (after Settlement MT's curb /2
    correction) — dedup is now treated as a standard pre-processing step,
    not an ad hoc check triggered only when a number looks suspiciously off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ezdxf.layouts import Modelspace


def _bulge_segment_length(p1: tuple[float, float], p2: tuple[float, float], bulge: float) -> float:
    """Length of one LWPOLYLINE segment, straight if bulge==0 else arc length."""
    chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if bulge == 0:
        return chord
    included_angle = 4 * math.atan(abs(bulge))
    if included_angle == 0:
        return chord
    radius = chord / (2 * math.sin(included_angle / 2))
    return radius * included_angle


def entity_length(e) -> float:
    """
    Arc-inclusive length of a LINE, ARC, or LWPOLYLINE (bulge-aware) entity.
    Raises ValueError for unsupported entity types.
    """
    t = e.dxftype()
    if t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        return math.hypot(en[0] - s[0], en[1] - s[1])

    if t == "ARC":
        r = e.dxf.radius
        sweep = (e.dxf.end_angle - e.dxf.start_angle) % 360
        return r * math.radians(sweep)

    if t == "LWPOLYLINE":
        pts = list(e.get_points("xyb"))  # (x, y, bulge)
        if e.closed and pts:
            pts = pts + [pts[0]]
        total = 0.0
        for (x1, y1, b1), (x2, y2, _b2) in zip(pts, pts[1:]):
            total += _bulge_segment_length((x1, y1), (x2, y2), b1)
        return total

    raise ValueError(f"entity_length: unsupported entity type {t!r}")


def _endpoints(e) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Start/end points for LINE (used by the dedup pass); None for other types."""
    if e.dxftype() == "LINE":
        s, en = e.dxf.start, e.dxf.end
        return (s[0], s[1]), (en[0], en[1])
    return None


def _pt_dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class DedupResult:
    raw_total: float
    deduped_total: float
    redundant_length: float
    duplicate_line_pairs: list[tuple[str, str, float, float]] = field(default_factory=list)
    duplicate_arc_pairs: list[tuple[str, str, float, float]] = field(default_factory=list)


def find_duplicate_lines(entities: list, tol: float = 1.5) -> list[tuple]:
    """
    Find LINE entities whose endpoints (forward or reversed) both fall
    within `tol` of another LINE's endpoints, with near-equal length.
    Returns [(handle_a, handle_b, len_a, len_b), ...]. Each entity appears
    in at most one pair (greedy first-match, matching the validated
    Key Finding #11 script's behavior).
    """
    lines = [e for e in entities if e.dxftype() == "LINE"]
    used: set[str] = set()
    pairs: list[tuple] = []

    info = [(e, *_endpoints(e), entity_length(e)) for e in lines]

    for i in range(len(info)):
        e_a, s_a, en_a, len_a = info[i]
        if e_a.dxf.handle in used:
            continue
        for j in range(i + 1, len(info)):
            e_b, s_b, en_b, len_b = info[j]
            if e_b.dxf.handle in used:
                continue
            same_dir = _pt_dist(s_a, s_b) < tol and _pt_dist(en_a, en_b) < tol
            rev_dir = _pt_dist(s_a, en_b) < tol and _pt_dist(en_a, s_b) < tol
            if (same_dir or rev_dir) and abs(len_a - len_b) < tol:
                pairs.append((e_a.dxf.handle, e_b.dxf.handle, len_a, len_b))
                used.add(e_a.dxf.handle)
                used.add(e_b.dxf.handle)
                break
    return pairs


def find_duplicate_arcs(entities: list, center_tol: float = 0.5, sweep_tol: float = 1.0) -> list[tuple]:
    """
    Find ARC entities sharing the same center (within `center_tol`) and
    sweep angle (within `sweep_tol` degrees) but possibly different radii —
    the classic "drawn twice at slightly different radius" duplicate pattern
    seen in Key Finding #11. Returns [(handle_a, handle_b, len_a, len_b), ...].
    """
    arcs = [e for e in entities if e.dxftype() == "ARC"]
    used: set[str] = set()
    pairs: list[tuple] = []

    for i in range(len(arcs)):
        a = arcs[i]
        if a.dxf.handle in used:
            continue
        ca = (a.dxf.center[0], a.dxf.center[1])
        sweep_a = (a.dxf.end_angle - a.dxf.start_angle) % 360
        for j in range(i + 1, len(arcs)):
            b = arcs[j]
            if b.dxf.handle in used:
                continue
            cb = (b.dxf.center[0], b.dxf.center[1])
            sweep_b = (b.dxf.end_angle - b.dxf.start_angle) % 360
            if _pt_dist(ca, cb) < center_tol and abs(sweep_a - sweep_b) < sweep_tol:
                pairs.append((a.dxf.handle, b.dxf.handle, entity_length(a), entity_length(b)))
                used.add(a.dxf.handle)
                used.add(b.dxf.handle)
                break
    return pairs


def layer_length(
    msp: Modelspace,
    layer: str,
    dedupe: bool = True,
    line_tol: float = 1.5,
    arc_tol: float = 0.5,
) -> DedupResult:
    """
    Total arc-inclusive length of all LINE/ARC/LWPOLYLINE entities on `layer`,
    with an optional duplicate-entity dedup pass (recommended by default —
    see Key Finding #11; only disable if you've already verified the layer
    is clean).
    """
    entities = [e for e in msp if e.dxf.layer == layer and e.dxftype() in ("LINE", "ARC", "LWPOLYLINE")]
    raw_total = sum(entity_length(e) for e in entities)

    if not dedupe:
        return DedupResult(raw_total=raw_total, deduped_total=raw_total, redundant_length=0.0)

    line_pairs = find_duplicate_lines(entities, tol=line_tol)
    arc_pairs = find_duplicate_arcs(entities, center_tol=arc_tol)

    redundant = sum(min(l1, l2) for _, _, l1, l2 in line_pairs)
    redundant += sum(min(l1, l2) for _, _, l1, l2 in arc_pairs)

    return DedupResult(
        raw_total=raw_total,
        deduped_total=raw_total - redundant,
        redundant_length=redundant,
        duplicate_line_pairs=line_pairs,
        duplicate_arc_pairs=arc_pairs,
    )


if __name__ == "__main__":
    import sys

    import ezdxf

    if len(sys.argv) != 3:
        print("Usage: python -m dwg_qty.length <dxf_path> <layer_name>")
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()
    result = layer_length(msp, sys.argv[2])

    print(f"Layer: {sys.argv[2]}")
    print(f"Raw total:     {result.raw_total:.1f}")
    print(f"Deduped total: {result.deduped_total:.1f}")
    print(f"Redundant:     {result.redundant_length:.1f}")
    if result.duplicate_line_pairs:
        print("Duplicate lines:")
        for h1, h2, l1, l2 in result.duplicate_line_pairs:
            print(f"  {h1} <-> {h2}  ({l1:.1f} / {l2:.1f})")
    if result.duplicate_arc_pairs:
        print("Duplicate arcs:")
        for h1, h2, l1, l2 in result.duplicate_arc_pairs:
            print(f"  {h1} <-> {h2}  ({l1:.1f} / {l2:.1f})")
