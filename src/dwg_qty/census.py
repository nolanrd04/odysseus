"""
Layer census, block-attribute census, and anonymous dynamic-block resolution.

Validated by:
  - Key Finding #1, session 2026_7_28.1: anonymous dynamic blocks (`*U###`)
    ARE resolvable by file parsing via the `AcDbBlockRepBTag` XDATA appid on
    the anonymous block's own BLOCK_RECORD — a group-1005 handle reference
    pointing at the true named master block's BLOCK_RECORD. Validated on
    Settlement MT: 35 anonymous instances all resolved to `SNG-SEW_SERV`.
  - Key Finding #2, session 2026_7_28.2: this does NOT always fire. Raghorn's
    2 anonymous instances were empty placeholder block definitions (zero
    entities, zero XDATA of any kind) on `_XREF-*` layers — stand-ins for
    missing/unresolved external references, not real dynamic-block
    anonymization. Kildere had zero anonymous blocks at all. A pipeline
    needs to distinguish "resolved", "empty XREF placeholder", and
    "unresolved" (has geometry, no AcDbBlockRepBTag) as three different
    outcomes, not treat a resolution failure as one uniform case.
  - Key Finding #3, session 2026_7_28.2: block-count matching needs to read
    block name AND attribute values, not just block name. Several qty-sheet
    subtypes are drawn as one generic block distinguished only by an
    ATTRIBUTE value (sign type, drywell size, valve size) — confirmed on
    both Raghorn and Kildere independently. Also: two DIFFERENT block names
    can sit on the same layer representing different billed items (Kildere's
    `P-WV` mainline valves vs. `WTR-VALV` service curb-stops on one layer) —
    "one layer = one countable thing" cannot be assumed.

NOT re-tested in this module's own validation pass (see building/README.md) —
the anonymous-block resolution logic is carried over from the session
findings verbatim since no anonymous-block-bearing DXF was reconverted
during this consolidation pass. Re-validate against Settlement MT before
relying on it in a real pipeline run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import ezdxf
from ezdxf.document import Drawing
from ezdxf.layouts import Modelspace


def layer_census(msp: Modelspace) -> dict[str, dict[str, int]]:
    """
    Return {layer_name: {dxftype: count}} for every entity in modelspace.

    Entities whose DXF namespace has no "layer" attribute (seen in the wild:
    ARCALIGNEDTEXT, an Express Tools type ezdxf loads without a layer mapping
    — Mountainside at Canfield, 2026-07-30) are bucketed under
    "(no layer attribute)" rather than crashing the census.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in msp:
        try:
            layer = e.dxf.layer
        except ezdxf.lldxf.const.DXFAttributeError:
            # ezdxf raises even from .get() when the attribute isn't in the
            # entity's DXF schema at all, not merely unset.
            layer = "(no layer attribute)"
        counts[layer][e.dxftype()] += 1
    return {layer: dict(types) for layer, types in counts.items()}


def block_attribute_census(
    msp: Modelspace, layer: str | None = None
) -> dict[str, dict[str, set[str]]]:
    """
    Census every INSERT's block name and, for each, every attribute tag and
    the distinct values seen for it: {block_name: {attr_tag: {values}}}.

    This is the "generic block + attribute" check from Key Finding #3 — run
    this BEFORE assuming a block name census is sufficient to split subtypes.
    A block with a non-empty attribute dict here (e.g. `SIGN`, `DDW`) means
    count-matching must group by (block_name, attr_value), not block_name alone.
    """
    result: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for e in msp.query("INSERT"):
        if layer is not None and e.dxf.layer != layer:
            continue
        name = e.dxf.name
        result[name]  # ensure key exists even with zero attribs
        if e.attribs:
            for attrib in e.attribs:
                result[name][attrib.dxf.tag].add(attrib.dxf.text)
    return {name: dict(attrs) for name, attrs in result.items()}


@dataclass
class AnonymousBlockResolution:
    anon_name: str
    status: str  # "resolved" | "empty_placeholder" | "unresolved"
    true_name: str | None = None
    entity_count: int = 0
    instance_count: int = 0
    instance_layers: set[str] = field(default_factory=set)


def resolve_anonymous_block(doc: Drawing, anon_name: str) -> AnonymousBlockResolution:
    """
    Attempt to resolve one anonymous block (e.g. "*U152") to its true named
    master block via AcDbBlockRepBTag XDATA on the anonymous BLOCK_RECORD.

    Classification:
      - "resolved": AcDbBlockRepBTag XDATA present and a matching named
        BLOCK_RECORD handle was found — this is a real dynamic block.
      - "empty_placeholder": the anonymous block definition has zero
        entities and no XDATA at all — almost certainly an unresolved
        external reference stand-in (Key Finding #2), not a dynamic block.
      - "unresolved": the block has real geometry but no AcDbBlockRepBTag
        XDATA — a genuine dynamic block this method can't resolve.
    """
    block = doc.blocks[anon_name]
    entity_count = len(block)

    xdata = None
    try:
        xdata = block.block_record.get_xdata("AcDbBlockRepBTag")
    except Exception:
        xdata = None

    if xdata is not None:
        target_handle = None
        for tag in xdata:
            if tag.code == 1005:
                target_handle = tag.value
                break
        if target_handle is not None:
            for candidate in doc.blocks:
                if candidate.block_record.dxf.handle == target_handle:
                    return AnonymousBlockResolution(
                        anon_name=anon_name,
                        status="resolved",
                        true_name=candidate.name,
                        entity_count=entity_count,
                    )

    if entity_count == 0:
        return AnonymousBlockResolution(
            anon_name=anon_name, status="empty_placeholder", entity_count=0
        )

    return AnonymousBlockResolution(
        anon_name=anon_name, status="unresolved", entity_count=entity_count
    )


def resolve_all_anonymous_blocks(doc: Drawing) -> dict[str, AnonymousBlockResolution]:
    """
    Resolve every anonymous block (name starting with "*U") in the document,
    and cross-reference modelspace INSERTs to attach instance counts/layers.
    """
    msp = doc.modelspace()
    anon_names = [b.name for b in doc.blocks if b.name.startswith("*U")]
    results = {name: resolve_anonymous_block(doc, name) for name in anon_names}

    for e in msp.query("INSERT"):
        if e.dxf.name in results:
            r = results[e.dxf.name]
            r.instance_count += 1
            r.instance_layers.add(e.dxf.layer)

    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m dwg_qty.census <dxf_path>")
        raise SystemExit(1)

    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()

    print("=== Layer census ===")
    for layer, types in sorted(layer_census(msp).items()):
        print(f"  {layer}: {types}")

    print("\n=== Anonymous block resolution ===")
    anon = resolve_all_anonymous_blocks(doc)
    if not anon:
        print("  (none found)")
    for name, r in anon.items():
        print(
            f"  {name}: status={r.status} true_name={r.true_name} "
            f"entities={r.entity_count} instances={r.instance_count} "
            f"layers={sorted(r.instance_layers)}"
        )
