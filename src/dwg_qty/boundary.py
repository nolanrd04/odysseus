"""
Polygon closure testing (polygonize_full wrapper) for area-based qty items
like ROW, lot grading, and tract boundaries.

Validated by:
  - Key Finding #8, session 2026_7_28.2: a consistent pattern across all 3
    subdivision jobs tested (Settlement MT, Raghorn, Kildere) — INDIVIDUAL
    ROW and lot/grading boundary layers do NOT close into valid polygons
    under `polygonize_full` at any tested snap tolerance (genuine gaps, not
    tolerance artifacts — swept 0.001-5.0 ft depending on job), while the
    COMBINED total (ROW+lot, or the overall tract boundary) reliably closes
    and matches the qty sheet's combined total within a few percent. Treat
    this as an expected characteristic of how these firms draw subdivision
    plats: expect the ROW/lot split to be undrawn as closed geometry, but
    expect the combined total to be recoverable.
  - Key Finding #10, session 2026_7_28.2 (same-day follow-up): a
    `polygonize_full` failure on the INDIVIDUAL split is not necessarily the
    end of the road — Kildere's individual ROW area WAS recoverable via
    centerline ray-casting (see centerline.py) even though it never closes
    as a polygon. Try boundary closure first for the combined total (cheap,
    reliable); if the individual split doesn't close, ray-casting off a
    validated-complete centerline is a working alternative before assuming
    the split is a genuine dead end.

Update, session 2026_7_28.6 (EXPO): that escalation from Key Finding #10 kept
getting skipped in practice across multiple jobs -- a failed closure test was
reported as "unresolved" instead of triggering the ray-cast fallback that was
already documented above. `row_area()` makes the escalation automatic: try
closure, then ray-cast if given a centerline + edge layer, rather than
depending on remembering to do it by hand. Validated on EXPO: `V-DSGN-BNDY`
et al. don't close (as expected, too sparse), but ray-casting the real
driveway centerline out to `P-UTIL-ESMT` gave a floating-point-stable 30/20 ft
split (50.0 ft total width) x 665.8 ft centerline length = 33,290 SF vs. the
qty sheet's 32,674 SF, +1.9%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ezdxf.layouts import Modelspace
from shapely.geometry import LineString
from shapely.ops import polygonize_full, unary_union

from src.dwg_qty.centerline import WidthMeasurement, entities_to_geometry, ray_cast_width


def _arc_to_points(e, n: int = 16) -> list[tuple[float, float]]:
    c, r = e.dxf.center, e.dxf.radius
    a1, a2 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
    if a2 < a1:
        a2 += 2 * math.pi
    return [
        (c[0] + r * math.cos(a1 + (a2 - a1) * i / n), c[1] + r * math.sin(a1 + (a2 - a1) * i / n))
        for i in range(n + 1)
    ]


def layer_edges(msp: Modelspace, layer: str) -> list[LineString]:
    """LINE/ARC/LWPOLYLINE entities on `layer` as shapely LineStrings, for polygonize_full input."""
    edges = []
    for e in msp:
        if e.dxf.layer != layer:
            continue
        t = e.dxftype()
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            edges.append(LineString([(s[0], s[1]), (en[0], en[1])]))
        elif t == "ARC":
            edges.append(LineString(_arc_to_points(e)))
        elif t == "LWPOLYLINE":
            pts = list(e.get_points("xy"))
            if e.closed and pts:
                pts = pts + [pts[0]]
            if len(pts) >= 2:
                edges.append(LineString(pts))
    return edges


@dataclass
class ClosureResult:
    tolerance: float
    polygon_count: int
    total_area: float
    dangle_count: int
    cut_count: int


def closure_sweep(
    msp: Modelspace,
    layer: str,
    tolerances: tuple[float, ...] = (0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
) -> list[ClosureResult]:
    """
    Attempt to close `layer`'s linework into polygons at a range of snap
    tolerances. If the polygon count/area stays at 0 across the whole
    sweep, that's a genuine topology gap (not a tolerance artifact) per
    Key Finding #8 — the layer's linework simply doesn't form a closed loop.
    A single merged geometry is reused across all tolerances since
    `polygonize_full` itself doesn't snap; if node-snapping is required,
    pre-snap the input with `shapely.set_precision` before calling this
    (not automated here — grid snapping can silently distort real geometry,
    so it's left as a deliberate caller decision, not a default).
    """
    edges = layer_edges(msp, layer)
    merged = unary_union(edges)

    results = []
    for tol in tolerances:
        polys, cuts, dangles, _invalid = polygonize_full(merged)
        total_area = sum(p.area for p in polys.geoms)
        results.append(
            ClosureResult(
                tolerance=tol,
                polygon_count=len(polys.geoms),
                total_area=total_area,
                dangle_count=len(dangles.geoms),
                cut_count=len(cuts.geoms),
            )
        )
    return results


def single_polygon_area(msp: Modelspace, layer: str) -> float:
    """
    Convenience wrapper for the common case (per Key Finding #8): a
    combined/tract-boundary layer that closes cleanly into exactly one
    polygon with no snapping needed. Raises ValueError if it doesn't close
    to exactly one polygon — use `closure_sweep()` to diagnose instead.
    """
    edges = layer_edges(msp, layer)
    merged = unary_union(edges)
    polys, cuts, dangles, _invalid = polygonize_full(merged)
    if len(polys.geoms) != 1:
        raise ValueError(
            f"Layer {layer!r} did not close to exactly one polygon "
            f"({len(polys.geoms)} polygons, {len(dangles.geoms)} dangles). "
            f"Use closure_sweep() to diagnose, or try centerline ray-casting instead."
        )
    return polys.geoms[0].area


@dataclass
class RowAreaResult:
    area: float | None
    method: str  # "closure" | "ray_cast" | "unresolved"
    centerline_length: float | None = None
    width: float | None = None
    width_measurements: list[WidthMeasurement] = field(default_factory=list)
    message: str = ""


def row_area(
    msp: Modelspace,
    row_layer: str,
    centerline: LineString | tuple[tuple[float, float], tuple[float, float]] | None = None,
    edge_layer: str | None = None,
    ray_cast_stations: tuple[float, ...] = (0.1, 0.3, 0.5, 0.7, 0.9),
    ray_cast_max_dist: float = 100.0,
    width_stability_tol: float = 1.0,
) -> RowAreaResult:
    """
    ROW/right-of-way area, trying boundary closure first and falling back to
    centerline ray-casting -- the escalation Key Finding #10 (session
    2026_7_28.2) already documented but which prior sessions kept stopping
    short of: a failed `closure_sweep()` was repeatedly reported as
    "unresolved" instead of triggering the ray-cast fallback that was sitting
    right there. This function makes that escalation automatic instead of
    dependent on remembering to do it (session 2026_7_28.6, EXPO).

    Step 1 -- try `row_layer` as closed linework (cheap, reliable when the
    ROW is actually drawn as a closed polygon). If it closes to exactly one
    polygon, return that area with method="closure".

    Step 2 -- if it doesn't close AND both `centerline` and `edge_layer` are
    given, ray-cast perpendicular from `centerline` out to `edge_layer`'s
    geometry (curb, easement, or ROW-edge linework) at `ray_cast_stations`,
    keep only stations where both +/- distances are present and within
    `width_stability_tol` of each other's station-to-station median (the
    same "stable across stations = real feature" signal used everywhere
    else ray-casting appears in this project -- see centerline.py), average
    those, and multiply by the centerline's length. Returns method="ray_cast".

    `centerline` MUST be resolved by the CALLER to the single real feature --
    either a shapely LineString or a (start, end) tuple for one straight
    segment. This function does NOT attempt to auto-select "the right one"
    among several candidates on a shared layer. EXPO's `C-ROAD-ALGN-CNTR`
    layer held 3 unrelated alignments (the real driveway plus two
    offsite/frontage segments sharing the same layer name) -- silently
    using "the whole layer" or "the longest entity" would have been wrong
    here and could be wrong elsewhere for different reasons. Picking the
    right centerline is exactly the kind of silent auto-correction this
    project avoids (see centerline.py's own docstring caveat on `buffer`).

    Returns method="unresolved" (area=None) if closure fails and no usable
    centerline/edge_layer/stable-width was available -- `message` explains
    what's missing, per this project's "flag honestly, don't force" practice.
    """
    closure = closure_sweep(msp, row_layer, tolerances=(0.01,))[0]
    if closure.polygon_count == 1:
        return RowAreaResult(
            area=closure.total_area,
            method="closure",
            message=f"{row_layer!r} closed to exactly one polygon.",
        )

    if centerline is None or edge_layer is None:
        return RowAreaResult(
            area=None,
            method="unresolved",
            message=(
                f"{row_layer!r} did not close to exactly one polygon "
                f"({closure.polygon_count} polygons, {closure.dangle_count} dangles) "
                f"and no centerline/edge_layer was given to try ray-casting instead."
            ),
        )

    if isinstance(centerline, LineString):
        coords = list(centerline.coords)
        start, end = coords[0][:2], coords[-1][:2]
        centerline_length = centerline.length
    else:
        start, end = centerline
        centerline_length = math.hypot(end[0] - start[0], end[1] - start[1])

    edge_geom = entities_to_geometry([e for e in msp if e.dxf.layer == edge_layer])
    measurements = ray_cast_width(start, end, edge_geom, stations=ray_cast_stations, max_dist=ray_cast_max_dist)

    totals = [
        (m.station_fraction, m.distance_plus + m.distance_minus)
        for m in measurements
        if m.distance_plus is not None and m.distance_minus is not None
    ]
    if not totals:
        return RowAreaResult(
            area=None,
            method="unresolved",
            centerline_length=centerline_length,
            width_measurements=measurements,
            message=(
                f"{row_layer!r} did not close, and ray-casting from the given centerline "
                f"to {edge_layer!r} found no station with both +/- distances present."
            ),
        )

    widths_only = [w for _, w in totals]
    median = sorted(widths_only)[len(widths_only) // 2]
    stable = [w for w in widths_only if abs(w - median) <= width_stability_tol]

    if not stable:
        return RowAreaResult(
            area=None,
            method="unresolved",
            centerline_length=centerline_length,
            width_measurements=measurements,
            message=(
                f"ray-cast widths from {len(totals)} station(s) never agreed within "
                f"{width_stability_tol} ft of each other -- not stable enough to trust."
            ),
        )

    width = sum(stable) / len(stable)
    return RowAreaResult(
        area=width * centerline_length,
        method="ray_cast",
        centerline_length=centerline_length,
        width=width,
        width_measurements=measurements,
        message=(
            f"{row_layer!r} did not close; used {len(stable)}/{len(totals)} stable-width "
            f"station(s) (width={width:.2f} ft) x centerline length ({centerline_length:.2f} ft)."
        ),
    )


if __name__ == "__main__":
    import sys

    import ezdxf

    if len(sys.argv) != 3:
        print("Usage: python -m dwg_qty.boundary <dxf_path> <layer_name>")
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()
    for r in closure_sweep(msp, sys.argv[2]):
        print(
            f"tol={r.tolerance}: {r.polygon_count} polys, area={r.total_area:.1f}, "
            f"dangles={r.dangle_count}, cuts={r.cut_count}"
        )
