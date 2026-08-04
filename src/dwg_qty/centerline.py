"""
Centerline ray-casting for road/ROW width measurement, plus a
centerline-completeness validation step.

Validated by:
  - Key Finding #6, session 2026_7_28.1: ray-casting perpendicular from the
    TRUE SURVEYED CENTERLINE (not same-layer segment pairing) gives exact,
    unambiguous, floating-point-stable distances to curb/back-of-curb/
    sidewalk/ROW — validated on Settlement MT across 6 centerline chains,
    stable at every sampled station.
  - Key Finding #6, session 2026_7_28.2: this technique is situational —
    it only applies when (a) the qty sheet actually expresses width-based
    quantities and (b) the DWG has a COMPLETE centerline network. Raghorn's
    `P-ROAD-CNTR` (6 entities / 2,083.6 ft for what should be 8 streets)
    was correctly left unattempted rather than forced.
  - Key Finding #10, session 2026_7_28.2 (same-day follow-up correction):
    "correctly left unattempted" in the prior bullet turned out to be too
    cautious in Kildere's case — a LOW ENTITY COUNT on a centerline layer is
    NOT proof the network is genuinely small. Kildere's `P-ROAD-CNTR` (2
    entities, looked like a complete 2-street "L") was actually missing an
    entire third street, only found by noticing curb/pavement geometry
    extending past what the 2-entity centerline could explain. Once ALL
    streets were counted (including the reconstructed third), ray-cast
    ROW-width x length totaled 74,308 SF vs. stated 75,869 SF (-2.1%) —
    recoverable after all. `validate_centerline_completeness()` below
    implements the cross-check that should have caught this the first time:
    compare the centerline network's extent against a denser, harder-to-
    omit reference layer (curb or edge-of-pavement) on the same feature.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, MultiLineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


def entities_to_geometry(entities: list, arc_segments: int = 16) -> BaseGeometry:
    """Union LINE/ARC/LWPOLYLINE entities into one shapely geometry for ray-casting/comparison."""
    geoms = []
    for e in entities:
        t = e.dxftype()
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            geoms.append(LineString([(s[0], s[1]), (en[0], en[1])]))
        elif t == "ARC":
            c, r = e.dxf.center, e.dxf.radius
            a1, a2 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
            if a2 < a1:
                a2 += 2 * math.pi
            pts = [
                (c[0] + r * math.cos(a1 + (a2 - a1) * i / arc_segments), c[1] + r * math.sin(a1 + (a2 - a1) * i / arc_segments))
                for i in range(arc_segments + 1)
            ]
            geoms.append(LineString(pts))
        elif t == "LWPOLYLINE":
            pts = list(e.get_points("xy"))
            if e.closed and pts:
                pts = pts + [pts[0]]
            if len(pts) >= 2:
                geoms.append(LineString(pts))
    return unary_union(geoms) if geoms else LineString()


@dataclass
class CompletenessReport:
    is_complete: bool
    centerline_bbox: tuple[float, float, float, float]
    reference_bbox: tuple[float, float, float, float]
    overhang: float
    message: str


def validate_centerline_completeness(
    centerline_geom: BaseGeometry,
    reference_geom: BaseGeometry,
    buffer: float = 10.0,
) -> CompletenessReport:
    """
    Sanity-check that a "centerline" layer is actually complete before
    trusting it for ray-casting or street-count census — see Key Finding
    #10. Buffers the centerline network by `buffer` ft and checks whether
    any of `reference_geom` (curb or edge-of-pavement — a layer far less
    likely to have an entire street silently omitted) falls entirely
    outside that buffer. If so, the centerline is probably missing a
    segment: the reference layer wouldn't exist somewhere with no nearby
    road to belong to.

    This is a heuristic, not a proof — a genuinely large `buffer` value
    can hide a real gap, and an unusually wide right-of-way can trip a
    false positive. Use it as a "check before trusting", not a silent
    auto-correct.
    """
    if centerline_geom.is_empty:
        return CompletenessReport(
            is_complete=False,
            centerline_bbox=(0, 0, 0, 0),
            reference_bbox=reference_geom.bounds,
            overhang=math.inf,
            message="Centerline geometry is empty - cannot be complete.",
        )

    corridor = centerline_geom.buffer(buffer)
    outside = reference_geom.difference(corridor)

    overhang_length = outside.length if hasattr(outside, "length") else 0.0
    reference_length = reference_geom.length if hasattr(reference_geom, "length") else 0.0
    overhang_fraction = (overhang_length / reference_length) if reference_length else 0.0

    is_complete = overhang_fraction < 0.05  # <5% of reference geometry sits outside the corridor

    return CompletenessReport(
        is_complete=is_complete,
        centerline_bbox=centerline_geom.bounds,
        reference_bbox=reference_geom.bounds,
        overhang=overhang_length,
        message=(
            "Centerline corridor covers the reference layer - looks complete."
            if is_complete
            else f"{overhang_length:.0f} ft ({overhang_fraction*100:.0f}%) of the reference layer "
            f"sits outside the centerline's buffered corridor - the centerline is likely "
            f"missing a segment. Do not trust it for street-count census or ray-casting "
            f"without reconstructing the missing piece first (Key Finding #10)."
        ),
    )


@dataclass
class WidthMeasurement:
    station_fraction: float
    station_point: tuple[float, float]
    distance_plus: float | None
    distance_minus: float | None


def ray_cast_width(
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
    target_geom: BaseGeometry,
    stations: tuple[float, ...] = (0.15, 0.35, 0.5, 0.65, 0.85),
    max_dist: float = 200.0,
) -> list[WidthMeasurement]:
    """
    Ray-cast perpendicular to a straight centerline segment at several
    stations, measuring distance to `target_geom` (e.g. curb, walk, or ROW
    layer geometry) in both perpendicular directions. Stability across
    stations (near-identical +/- distances at every station) is itself the
    validation signal used throughout this project — a real, consistently-
    offset feature gives floating-point-stable results; noise or an
    incomplete feature gives inconsistent ones.
    """
    x1, y1 = segment_start
    x2, y2 = segment_end
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return []
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    perp = (-uy, ux)

    results = []
    for t in stations:
        px, py = x1 + ux * length * t, y1 + uy * length * t
        dists: dict[str, float | None] = {"+": None, "-": None}
        for sign, label in ((1, "+"), (-1, "-")):
            ray = LineString([(px, py), (px + perp[0] * sign * max_dist, py + perp[1] * sign * max_dist)])
            inter = ray.intersection(target_geom)
            if inter.is_empty:
                continue
            origin = Point(px, py)
            pts = []
            if inter.geom_type == "Point":
                pts = [inter]
            elif hasattr(inter, "geoms"):
                for g in inter.geoms:
                    if g.geom_type == "Point":
                        pts.append(g)
                    elif hasattr(g, "coords"):
                        pts.extend(Point(c) for c in g.coords)
            elif hasattr(inter, "coords"):
                pts = [Point(c) for c in inter.coords]
            if pts:
                dists[label] = min(origin.distance(p) for p in pts)

        results.append(WidthMeasurement(station_fraction=t, station_point=(px, py), distance_plus=dists["+"], distance_minus=dists["-"]))
    return results


if __name__ == "__main__":
    import sys

    import ezdxf

    if len(sys.argv) != 8:
        print(
            "Usage: python -m dwg_qty.centerline <dxf_path> <target_layer> "
            "<x1> <y1> <x2> <y2> <n_stations>"
        )
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()
    layer = sys.argv[2]
    x1, y1, x2, y2 = (float(v) for v in sys.argv[3:7])

    target = entities_to_geometry([e for e in msp if e.dxf.layer == layer])
    for m in ray_cast_width((x1, y1), (x2, y2), target):
        print(f"t={m.station_fraction:.2f} @ {m.station_point}: + {m.distance_plus} - {m.distance_minus}")
