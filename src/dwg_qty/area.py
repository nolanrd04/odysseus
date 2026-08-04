"""
HATCH area extraction with a runtime sum-vs-net geometric test.

Validated by:
  - Key Finding #5, session 2026_7_28.2: `HATCH.area` (ezdxf's built-in
    property) returned 0.0 for every hatch in Kildere's DXF because all
    boundaries are EdgePath (line-edge) rather than PolylinePath — falls
    back to `ezdxf.disassemble.make_primitive()` -> flattened polygon here.
  - Same finding: many HATCH entities bundle multiple geometrically-disjoint
    boundary loops representing separate adjacent panels drawn as one
    entity, NOT nested holes. The correct area is sum-of-all-loops in that
    case, not "outer minus holes" — confirmed empirically (sum-all-loops
    matched Kildere's ROAD SUBGRADE to 0.005%; the naive net-of-holes
    interpretation would have been >99% wrong). Settlement MT's
    `SF-PVMT-PROP-HAT` boundary artifacts (2026_7_28.1) showed the opposite
    failure mode can also occur (nested loops that are NOT legitimate
    disjoint panels). Rule: NEVER assume sum-vs-net from loop count alone —
    always geometrically test loop disjointness/containment per hatch. This
    module implements that test as the default behavior, not an assumption.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ezdxf.layouts import Modelspace
from shapely.geometry import Polygon
from shapely.ops import unary_union

# NOTE on implementation choice: an earlier version of this module built
# per-loop polygons via `ezdxf.disassemble.to_primitives()`. That was wrong
# in a way that directly defeats the sum-vs-net disjointness test this
# module exists to implement: for a HATCH with multiple BOUNDARY PATHS
# (e.g. 3 separate EdgePath loops representing 3 disjoint panels),
# `to_primitives()` silently concatenated all of them into ONE merged
# vertex ring instead of returning 3 separate primitives — collapsing
# exactly the multi-loop structure the disjointness test needs to see.
# Confirmed by direct inspection during this module's validation pass
# (2026-07-28): a hatch with 3 EdgePath boundary paths produced 1 primitive
# with 50 vertices, not 3. This module instead walks `hatch.paths` directly
# and builds one polygon PER BOUNDARY PATH, so multi-loop hatches are
# represented correctly before the disjointness test ever runs.


def _bulge_arc_points(p1: tuple[float, float], p2: tuple[float, float], bulge: float, n: int = 12) -> list[tuple[float, float]]:
    """Intermediate points along a bulge-defined arc segment from p1 to p2 (excludes p1, includes p2)."""
    if bulge == 0:
        return [p2]
    x1, y1 = p1
    x2, y2 = p2
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord == 0:
        return [p2]
    included_angle = 4 * math.atan(abs(bulge))
    radius = chord / (2 * math.sin(included_angle / 2))
    # midpoint of chord, then offset perpendicular to find the arc center
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = (x2 - x1) / chord, (y2 - y1) / chord
    sagitta = radius - math.sqrt(max(radius * radius - (chord / 2) ** 2, 0.0))
    # perpendicular direction; sign of bulge determines which side the center falls on
    perp = (-dy, dx) if bulge > 0 else (dy, -dx)
    offset = radius - sagitta
    cx, cy = mx + perp[0] * offset, my + perp[1] * offset

    start_angle = math.atan2(y1 - cy, x1 - cx)
    end_angle = math.atan2(y2 - cy, x2 - cx)
    ccw = bulge > 0
    if ccw and end_angle < start_angle:
        end_angle += 2 * math.pi
    if not ccw and end_angle > start_angle:
        end_angle -= 2 * math.pi

    pts = []
    for i in range(1, n + 1):
        a = start_angle + (end_angle - start_angle) * i / n
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return pts


def _arc_edge_points(edge, n: int = 16) -> list[tuple[float, float]]:
    """Flattened points for an ArcEdge (excludes the true start point; caller supplies it)."""
    c = edge.center
    r = edge.radius
    a1, a2 = math.radians(edge.start_angle), math.radians(edge.end_angle)
    if edge.ccw:
        if a2 < a1:
            a2 += 2 * math.pi
    else:
        if a2 > a1:
            a2 -= 2 * math.pi
    return [
        (c[0] + r * math.cos(a1 + (a2 - a1) * i / n), c[1] + r * math.sin(a1 + (a2 - a1) * i / n))
        for i in range(1, n + 1)
    ]


def _polyline_path_polygon(path) -> Polygon | None:
    verts = list(path.vertices)  # [(x, y, bulge), ...]
    if len(verts) < 2:
        return None
    pts = [(verts[0][0], verts[0][1])]
    ring = verts + [verts[0]] if path.is_closed else verts
    for (x1, y1, b1), (x2, y2, _b2) in zip(ring, ring[1:]):
        pts.extend(_bulge_arc_points((x1, y1), (x2, y2), b1))
    if len(pts) < 3:
        return None
    return Polygon(pts)


def _edge_path_polygon(path) -> Polygon | None:
    pts: list[tuple[float, float]] = []
    for edge in path.edges:
        et = type(edge).__name__
        if et == "LineEdge":
            if not pts:
                pts.append((edge.start[0], edge.start[1]))
            pts.append((edge.end[0], edge.end[1]))
        elif et == "ArcEdge":
            start_pt = (
                edge.center[0] + edge.radius * math.cos(math.radians(edge.start_angle)),
                edge.center[1] + edge.radius * math.sin(math.radians(edge.start_angle)),
            )
            if not pts:
                pts.append(start_pt)
            pts.extend(_arc_edge_points(edge))
        else:
            # EllipseEdge / SplineEdge: not seen in this project's DWGs so far.
            # Fail loudly rather than silently under-representing the loop.
            raise NotImplementedError(
                f"_edge_path_polygon: unsupported edge type {et!r} — extend this "
                f"function before trusting hatch areas on layers using it."
            )
    if len(pts) < 3:
        return None
    return Polygon(pts)


def hatch_boundary_polygons(hatch) -> list[Polygon]:
    """
    Extract every boundary loop of a HATCH as its OWN shapely Polygon — one
    polygon per boundary path (`hatch.paths`), built directly from
    PolylinePath vertices (bulge-aware) or EdgePath edges (Line/Arc). See
    the module-level note above for why this does NOT go through
    `ezdxf.disassemble`.
    """
    polygons: list[Polygon] = []
    for path in hatch.paths:
        poly = _polyline_path_polygon(path) if hasattr(path, "vertices") else _edge_path_polygon(path)
        if poly is None:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty and poly.area > 0:
            polygons.append(poly)
    return polygons


@dataclass
class HatchAreaResult:
    area: float
    loop_count: int
    method: str  # "sum_disjoint" | "net_of_holes" | "single_loop"
    loop_areas: list[float] = field(default_factory=list)


def hatch_area(hatch, overlap_fraction_tol: float = 0.01) -> HatchAreaResult:
    """
    Correct area of one HATCH entity, deciding sum-vs-net by an actual
    geometric OVERLAP-AREA test — not shapely's strict `.disjoint()`
    predicate, and not loop count.

    Why not `.disjoint()`: it returns False for two polygons that merely
    TOUCH along a shared edge (e.g. two adjacent street-pavement panels
    meeting exactly at a T-intersection, sharing a boundary line but zero
    interior area) — a very common, entirely legitimate case for this
    project's road/pavement hatches. Treating "touches" the same as
    "overlaps" would wrongly route real disjoint-panel hatches into the
    net-of-holes branch and silently drop most of their area (caught during
    this module's validation pass: it undercounted Kildere's ROAD SUBGRADE
    by ~31% before this fix). Testing actual intersection AREA instead
    correctly treats touching-but-not-overlapping loops as separate panels.

    - 1 loop: that loop's area.
    - >1 loop, every pairwise intersection area is negligible relative to
      the smaller loop in the pair (< `overlap_fraction_tol`, default 1%):
      sum of all loop areas (separate panels drawn as one entity, whether
      fully separate or edge-touching).
    - >1 loop, some pair has real overlapping area: treat the largest loop
      as the outer boundary and subtract any loop substantially contained
      within it (legitimate holes) — the classic "outer minus holes" case.
    """
    loops = hatch_boundary_polygons(hatch)
    if not loops:
        return HatchAreaResult(area=0.0, loop_count=0, method="single_loop", loop_areas=[])
    if len(loops) == 1:
        return HatchAreaResult(area=loops[0].area, loop_count=1, method="single_loop", loop_areas=[loops[0].area])

    def negligible_overlap(a: Polygon, b: Polygon) -> bool:
        overlap = a.intersection(b).area
        smaller = min(a.area, b.area)
        return smaller == 0 or (overlap / smaller) < overlap_fraction_tol

    all_effectively_disjoint = all(
        negligible_overlap(loops[i], loops[j])
        for i in range(len(loops))
        for j in range(i + 1, len(loops))
    )

    if all_effectively_disjoint:
        total = sum(p.area for p in loops)
        return HatchAreaResult(area=total, loop_count=len(loops), method="sum_disjoint", loop_areas=[p.area for p in loops])

    loops_sorted = sorted(loops, key=lambda p: p.area, reverse=True)
    outer = loops_sorted[0]
    holes_area = sum(
        p.area for p in loops_sorted[1:]
        if p.area > 0 and (outer.intersection(p).area / p.area) >= (1 - overlap_fraction_tol)
    )
    net = outer.area - holes_area
    return HatchAreaResult(area=net, loop_count=len(loops), method="net_of_holes", loop_areas=[p.area for p in loops])


def layer_hatch_area(msp: Modelspace, layer: str) -> dict:
    """Sum of `hatch_area()` across every HATCH on `layer`, with per-hatch detail."""
    per_hatch = []
    total = 0.0
    for e in msp:
        if e.dxf.layer == layer and e.dxftype() == "HATCH":
            r = hatch_area(e)
            per_hatch.append({"handle": e.dxf.handle, **r.__dict__})
            total += r.area
    return {"total_area": total, "hatch_count": len(per_hatch), "per_hatch": per_hatch}


if __name__ == "__main__":
    import sys

    import ezdxf

    if len(sys.argv) != 3:
        print("Usage: python -m dwg_qty.area <dxf_path> <layer_name>")
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()
    result = layer_hatch_area(msp, sys.argv[2])

    print(f"Layer: {sys.argv[2]}")
    print(f"Total area (SF): {result['total_area']:.1f}")
    print(f"Total area (SY): {result['total_area']/9:.1f}")
    print(f"Hatch count: {result['hatch_count']}")
    for h in result["per_hatch"]:
        print(f"  {h['handle']}: area={h['area']:.1f} loops={h['loop_count']} method={h['method']}")
