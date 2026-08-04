"""
Positional/proximity disambiguation for tie-in, connection, and "which
feature does this actually belong to" qty-sheet items.

Validated by:
  - Key Finding #7, session 2026_7_28.2 (building on Settlement MT's
    `teef`/hydrant and `WA-PLIN-PROP-SYM` checks in 2026_7_28.1): this is
    the most consistently decisive technique across all jobs for
    "CONNECT TO EXISTING X" / "TIE INTO EXISTING X" items — endpoint
    coincidence checks landed exact (0.00-4.3 ft) on both Raghorn and
    Kildere.
  - Same finding, real refinement: a tie-in can be a MID-SPAN TAP, not an
    end-to-end junction (Kildere's irrigation-to-existing-water tie-in).
    Naive endpoint-to-endpoint distance checks fail on these (~122-650 ft
    off) until switched to point-to-full-linestring distance. Always check
    BOTH — endpoint coincidence for terminal connections, point-to-
    linestring for mid-span taps — rather than assuming which kind a given
    "TIE INTO" item is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point


def _entity_to_linestring(e) -> LineString | None:
    """Best-effort LineString for LINE/LWPOLYLINE; None for unsupported types."""
    t = e.dxftype()
    if t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        return LineString([(s[0], s[1]), (en[0], en[1])])
    if t == "LWPOLYLINE":
        pts = list(e.get_points("xy"))
        if e.closed and pts:
            pts = pts + [pts[0]]
        if len(pts) >= 2:
            return LineString(pts)
    return None


@dataclass
class TieInResult:
    kind: str  # "endpoint" | "midspan" | "none"
    distance: float
    target_handle: str | None = None


def find_tie_in(
    point: tuple[float, float],
    candidate_entities: list,
    endpoint_tol: float = 5.0,
    midspan_tol: float = 5.0,
) -> TieInResult:
    """
    Determine whether `point` (e.g. the free end of a proposed pipe run)
    connects to any of `candidate_entities` (e.g. all entities on an
    "existing utility" layer), checking BOTH connection styles:

      1. Endpoint coincidence — point sits at/near one candidate's endpoint
         (a terminal junction, e.g. "CONNECT TO EXISTING SEWER").
      2. Point-to-linestring distance — point sits at/near ANY point along
         a candidate's full length, not just its ends (a mid-span tap, e.g.
         "TIE INTO EXISTING WATER" cutting into a running main).

    Returns the closest match across both checks, "none" if nothing is
    within tolerance of either. Endpoint matches are preferred when both
    are within tolerance and comparably close, since a true terminal
    junction should coincide with a drawn endpoint.
    """
    pt = Point(point)
    best_endpoint: tuple[float, str] | None = None
    best_midspan: tuple[float, str] | None = None

    for e in candidate_entities:
        ls = _entity_to_linestring(e)
        if ls is None or len(ls.coords) < 2:
            continue
        handle = e.dxf.handle

        for end in (Point(ls.coords[0]), Point(ls.coords[-1])):
            d = pt.distance(end)
            if d <= endpoint_tol and (best_endpoint is None or d < best_endpoint[0]):
                best_endpoint = (d, handle)

        d_line = pt.distance(ls)
        if d_line <= midspan_tol and (best_midspan is None or d_line < best_midspan[0]):
            best_midspan = (d_line, handle)

    if best_endpoint is not None:
        return TieInResult(kind="endpoint", distance=best_endpoint[0], target_handle=best_endpoint[1])
    if best_midspan is not None:
        return TieInResult(kind="midspan", distance=best_midspan[0], target_handle=best_midspan[1])
    return TieInResult(kind="none", distance=math.inf)


if __name__ == "__main__":
    import sys

    import ezdxf

    if len(sys.argv) != 6:
        print(
            "Usage: python -m dwg_qty.proximity <dxf_path> <candidate_layer> "
            "<point_x> <point_y> <tolerance>"
        )
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()
    layer = sys.argv[2]
    px, py, tol = float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])

    candidates = [e for e in msp if e.dxf.layer == layer]
    result = find_tie_in((px, py), candidates, endpoint_tol=tol, midspan_tol=tol)
    print(f"kind={result.kind} distance={result.distance:.2f} target={result.target_handle}")
