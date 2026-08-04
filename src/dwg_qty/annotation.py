"""
Paperspace/annotation reading: DIMENSION cached text and MULTILEADER content.

Validated 2026_8_4.2 against 15 files across ~9 firms (10 corpus-cached +
5 freshly ODA-converted, including a firm — Painted Rock — never tested
against this technique before): `DIMENSION` entities cache their exact
rendered string in a private anonymous `*D####` block's `MTEXT`, independent
of raw scale-factor math (`get_measurement()` alone is not what's printed on
the sheet). `MULTILEADER` entities carry either a free-text spec callout
(`has_mtext_content`) or a keynote-circle block reference with attribute
overrides like `TAGNUMBER` (`has_block_content`). This is where typical-
section values (ROW/road/sidewalk/swale/easement width) and pipe-spec
callouts actually live on Civil3D-authored files whose plan annotation is
otherwise all `ACAD_PROXY_ENTITY` placeholders with no literal text —
confirmed against real ground truth on Woodman (`68'` ROW width, `"5'
SIDEWALK"`, `"11.5' SWALE"` all read back exactly via this path).

Both entity types were found living in EVERY layout, not just paperspace, on
the files checked — one job (Foundry) had 13 `MULTILEADER` entities sitting
in modelspace instead. Every function here scans every layout via
`doc.layouts`, never assumes paperspace-only.

Coverage caveat: only ~5 of the 15 files checked had any DIMENSION/
MULTILEADER content at all. This closes a real gap for firms that annotate
this way — it is not a universal fix. A genuine zero for a job (see
`annotation_layout_census` / the census's `annotation_layouts` field) is real
evidence of absence, not a search failure, for firms whose convention doesn't
use these entity types.

Known open question (not yet resolved): a prior investigation (session
2026_7_27.1) proposed filtering MULTILEADER callouts to leader-bearing ones
only (`n_leaders >= 1`) to separate real plan callouts from leaderless
legend echoes. A later spot-check (2026_8_4.1) found keynote block-content
MULTILEADERs with `n_leaders == 0` on Woodman, contradicting that filter as
stated. `n_leaders` is still surfaced here for cross-checking (rule 3), but
do not treat it as a hard real/not-real filter without re-verifying on the
job at hand.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ezdxf.document import Drawing

_UNDERLINE_CODES = re.compile(r"%%[uUoOkK]")
_STATION_PATTERN = re.compile(r"\d+\+\d+(?:\.\d+)?")
_LOT_PATTERN = re.compile(r"^\s*(\d+)\s*\n\s*([\d,]+(?:\.\d+)?)\s*S\.?F\.?\s*$", re.I)


def _iter_layouts(doc: Drawing, layout_name: str | None):
    if layout_name is not None:
        return [doc.layouts.get(layout_name)]
    return list(doc.layouts)


@dataclass
class DimensionText:
    layout: str
    layer: str
    handle: str
    text: str


def read_dimension_texts(
    doc: Drawing, layout_name: str | None = None
) -> list[DimensionText]:
    """
    Read every DIMENSION's literal cached rendered text, across every layout
    by default (pass `layout_name` to scope to one sheet).

    Reads `doc.blocks.get(dim.dxf.geometry)` (the DIMENSION's private
    anonymous `*D####` cache block) and pulls the `MTEXT` inside it — the
    exact string as printed, not a recomputed measurement. A DIMENSION with
    no MTEXT in its cache block (rare — a plain unlabeled dimension) is
    silently skipped, not an error.
    """
    results: list[DimensionText] = []
    for layout in _iter_layouts(doc, layout_name):
        for d in layout.query("DIMENSION"):
            try:
                block = doc.blocks.get(d.dxf.geometry)
            except Exception:
                continue
            for e in block:
                if e.dxftype() == "MTEXT":
                    results.append(
                        DimensionText(
                            layout=layout.name,
                            layer=d.dxf.layer,
                            handle=d.dxf.handle,
                            text=e.plain_text(),
                        )
                    )
    return results


@dataclass
class MultiLeaderContent:
    layout: str
    layer: str
    handle: str
    kind: str  # "mtext" | "block" | "none"
    text: str | None = None
    block_attribs: dict[str, str] = field(default_factory=dict)
    n_leaders: int = 0


def read_multileader_contents(
    doc: Drawing, layout_name: str | None = None
) -> list[MultiLeaderContent]:
    """
    Read every MULTILEADER's content, across every layout by default (pass
    `layout_name` to scope to one sheet): free-text spec callouts
    (`kind="mtext"`) or keynote-circle block attribute overrides like
    TAGNUMBER (`kind="block"`). `n_leaders` is included for cross-checking
    real-callout-vs-legend-icon (see module docstring's open question — do
    not treat it as a settled filter yet).
    """
    results: list[MultiLeaderContent] = []
    for layout in _iter_layouts(doc, layout_name):
        for m in layout.query("MULTILEADER"):
            try:
                n_leaders = len(list(m.context.leaders))
            except Exception:
                n_leaders = 0

            if m.has_mtext_content:
                results.append(
                    MultiLeaderContent(
                        layout=layout.name,
                        layer=m.dxf.layer,
                        handle=m.dxf.handle,
                        kind="mtext",
                        text=m.get_mtext_content(),
                        n_leaders=n_leaders,
                    )
                )
            elif m.has_block_content:
                try:
                    attribs = m.get_block_content()
                except Exception:
                    attribs = {}
                results.append(
                    MultiLeaderContent(
                        layout=layout.name,
                        layer=m.dxf.layer,
                        handle=m.dxf.handle,
                        kind="block",
                        block_attribs=attribs,
                        n_leaders=n_leaders,
                    )
                )
            else:
                results.append(
                    MultiLeaderContent(
                        layout=layout.name,
                        layer=m.dxf.layer,
                        handle=m.dxf.handle,
                        kind="none",
                        n_leaders=n_leaders,
                    )
                )
    return results


def annotation_layout_census(doc: Drawing) -> dict[str, dict]:
    """
    Per-layout DIMENSION/MULTILEADER counts across every layout, including
    modelspace (see module docstring — MULTILEADER is not paperspace-only).
    Cheap structural summary meant for the mandatory job census (DQ-6), so a
    job's annotation convention is visible from the start instead of needing
    to be discovered mid-turn. Only layouts with a nonzero count are
    included; an empty dict means this job's firm doesn't annotate this way
    at all (real absence, not unexplored — see module docstring).

    Call `read_dimension_texts()` / `read_multileader_contents()` (scoped to
    a specific layout via the names surfaced here) to pull the actual text.
    """
    counts: dict[str, dict] = {}
    for layout in doc.layouts:
        dim_count = len(layout.query("DIMENSION"))
        mleader_count = len(layout.query("MULTILEADER"))
        if dim_count or mleader_count:
            counts[layout.name] = {
                "is_paperspace": layout.is_any_paperspace,
                "dimension_count": dim_count,
                "multileader_count": mleader_count,
            }
    return counts


@dataclass
class LegendSection:
    layout: str
    header: str
    header_handle: str
    body: str | None = None
    body_handle: str | None = None
    pair_distance: float | None = None


def keynote_legend_sections(
    doc: Drawing,
    layer: str | None = None,
    layout_name: str | None = None,
    max_pair_distance: float = 5.0,
) -> list[LegendSection]:
    """
    Pair each single-line TEXT entity (a candidate legend header — "KEYNOTES",
    "SEWER KEYNOTES", "NOTES", etc.) with its nearest MTEXT entity on the same
    layout — the actual paragraph body a `MULTILEADER` keynote's bare
    `TAGNUMBER` (from `read_multileader_contents()`) refers to.

    There is no explicit DXF link between a legend header and its body; this
    pairs by 2D distance between insertion points. Validated on Woodman: a
    real header/body pair sits 0.03-0.4 units apart, with the next-nearest
    candidate several units further — a clean gap in that file, hence the
    generous default `max_pair_distance`. A header with no MTEXT within range
    is still returned (`body=None`) rather than dropped, per this project's
    flag-don't-silently-drop convention.

    Pass `layer` (recommended) to restrict candidate headers/bodies to one
    layer once you've identified it from the census — leaving it unset scans
    every TEXT/MTEXT on the layout and WILL pick up unrelated title-block
    fields as false-positive "headers" (confirmed on Woodman: `prj:`, `sht:`,
    `DATE`, `PRELIMINARY`, revision-cloud numbers all got spuriously paired
    when unscoped, because a title block packs many small TEXT/MTEXT entities
    close together too). On Woodman, every genuine legend header sits on
    `C-ANNO` while title-block noise sits on separate `C-BRDR-*` /
    `C-ANNO-REVS-TEXT` layers — passing `layer="C-ANNO"` there fully
    eliminates the false positives. This layer name is one firm's
    convention, not hardcoded here — inspect the layout's real TEXT layers
    first (rule 15).

    Does NOT attempt to split a body into per-TAGNUMBER paragraphs or map a
    specific TAGNUMBER to a specific paragraph — bodies seen so far are
    blank-line-separated with no explicit numbering, and TAGNUMBER schemes
    vary (bare numbers, letter-prefixed like "W1"-"W4", sub-lettered like
    "1A"/"1B") in ways not safe to hardcode a resolution rule for. That
    correlation (rule 3: cross-check, never assume) is left as a judgment
    call for whoever consumes this, with the header text as the category
    signal and the ordered paragraphs as candidate matches.
    """
    results: list[LegendSection] = []
    for layout in _iter_layouts(doc, layout_name):
        headers = [e for e in layout.query("TEXT") if layer is None or e.dxf.layer == layer]
        bodies = [e for e in layout.query("MTEXT") if layer is None or e.dxf.layer == layer]
        for h in headers:
            header_text = _UNDERLINE_CODES.sub("", h.dxf.text).strip()
            if not header_text:
                continue
            hx, hy = h.dxf.insert[0], h.dxf.insert[1]
            best = None
            best_dist = None
            for b in bodies:
                bx, by = b.dxf.insert[0], b.dxf.insert[1]
                dist = math.hypot(hx - bx, hy - by)
                if dist <= max_pair_distance and (best_dist is None or dist < best_dist):
                    best, best_dist = b, dist
            results.append(
                LegendSection(
                    layout=layout.name,
                    header=header_text,
                    header_handle=h.dxf.handle,
                    body=best.plain_text() if best is not None else None,
                    body_handle=best.dxf.handle if best is not None else None,
                    pair_distance=best_dist,
                )
            )
    return results


@dataclass
class LotRecord:
    layout: str
    layer: str
    handle: str
    lot_number: str
    area_sf: float


def read_lot_records(
    doc: Drawing, layer: str, layout_name: str | None = None
) -> list[LotRecord]:
    """
    Parse MTEXT on `layer` matching the pattern "<lot number>\\n<area> S.F."
    (e.g. "5\\n9107 S.F.") into structured lot records — the literal platted
    lot count/area, no clustering or inference needed. Validated on Woodman's
    `C-PROP-SFAM-TEXT` layer: 23 lots, exact match to the model's separately
    inferred sewer-service connected-component count — use this as the real
    ground truth for cross-checking service/meter counts (rule 3) instead of
    trusting geometric clustering alone.

    Takes `layer` as a caller-supplied parameter rather than a hardcoded name
    (consistent with the rest of this library) — this specific layer name is
    one firm's convention, not assumed universal. Non-matching MTEXT on the
    given layer is silently skipped, not an error — pass the actual layer to
    parse after confirming its content shape via `read_dimension_texts`-style
    inspection or a raw MTEXT dump; do not assume a lot-bearing layer exists
    on a new job.
    """
    results: list[LotRecord] = []
    for layout in _iter_layouts(doc, layout_name):
        for e in layout.query("MTEXT"):
            if e.dxf.layer != layer:
                continue
            m = _LOT_PATTERN.match(e.plain_text())
            if not m:
                continue
            results.append(
                LotRecord(
                    layout=layout.name,
                    layer=layer,
                    handle=e.dxf.handle,
                    lot_number=m.group(1),
                    area_sf=float(m.group(2).replace(",", "")),
                )
            )
    return results


@dataclass
class StationLabel:
    layout: str
    layer: str
    handle: str
    station_text: str
    station_ft: float


def _station_to_feet(station_text: str) -> float:
    whole, _, part = station_text.partition("+")
    return float(whole) * 100 + float(part)


def read_station_labels(
    doc: Drawing, layer: str, layout_name: str | None = None
) -> list[StationLabel]:
    """
    Extract every civil-stationing token (`XX+YY.YY` format, e.g. "11+43.35")
    out of every MTEXT/TEXT entity on `layer` — one entity can hold a single
    station or a newline-separated list of several, both seen in the wild
    (Woodman's `C-UTSS-SRVC-LABL` has one station per entity across 24
    entities; a plan-sheet annotation layer had one entity listing 8 stations
    on separate lines). `station_ft` converts to a plain feet value
    (`XX+YY.YY` = `XX*100 + YY.YY`) for sorting/spacing math.

    Intended use: counting real per-feature station callouts (e.g. one label
    per sewer/water service tap) as a direct, literal count — a cross-check
    against, or replacement for, endpoint-clustering-based counts, which are
    known to undercount when two service runs' endpoints fall inside the
    same clustering tolerance (see project history — a 14-vs-23 undercount
    on this same job's water services in an earlier run). Takes `layer` as a
    caller-supplied parameter, same reasoning as `read_lot_records`.
    """
    results: list[StationLabel] = []
    for layout in _iter_layouts(doc, layout_name):
        entities = list(layout.query("MTEXT")) + list(layout.query("TEXT"))
        for e in entities:
            if e.dxf.layer != layer:
                continue
            text = e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
            for match in _STATION_PATTERN.finditer(text):
                station_text = match.group(0)
                results.append(
                    StationLabel(
                        layout=layout.name,
                        layer=layer,
                        handle=e.dxf.handle,
                        station_text=station_text,
                        station_ft=_station_to_feet(station_text),
                    )
                )
    return results


if __name__ == "__main__":
    import sys

    import ezdxf

    if len(sys.argv) != 2:
        print("Usage: python -m dwg_qty.annotation <dxf_path>")
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])

    print("=== Annotation layout census ===")
    census = annotation_layout_census(doc)
    if not census:
        print("  (no DIMENSION/MULTILEADER content anywhere in this file)")
    for name, counts in census.items():
        print(f"  {name}: {counts}")

    print("\n=== Sample DIMENSION texts ===")
    for dt in read_dimension_texts(doc)[:10]:
        print(f"  [{dt.layout}] {dt.layer}: {dt.text!r}")

    print("\n=== Sample MULTILEADER contents ===")
    for mc in read_multileader_contents(doc)[:10]:
        if mc.kind == "mtext":
            print(f"  [{mc.layout}] {mc.layer}: mtext={mc.text!r}")
        elif mc.kind == "block":
            print(f"  [{mc.layout}] {mc.layer}: block={mc.block_attribs!r}")
        else:
            print(f"  [{mc.layout}] {mc.layer}: (no content)")
