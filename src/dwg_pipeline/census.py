"""
Mandatory server-side census step (DQ-6) — the deterministic "orient yourself
on an unfamiliar job" pass, mirroring building/examples/01_new_job_first_look.py:

    layer census → centerline-name heuristic scan → anonymous-block
    resolution → block-attribute census

Runs automatically before the extraction agent's first turn; the agent cannot
skip it (the recurring "technique existed, wasn't applied" bug class from
session 2026_7_28.6). Its JSON payload has three consumers per the ledger:
the extraction agent's starting context (DQ-6), the ghost-firm convention
fingerprint (DQ-8), and the Case A "unclaimed evidence" diff (DQ-9).

Deliberately data-only: no naming semantics are interpreted here (DQ-8 —
"never hardcode `C-` means Civil3D"). The one heuristic (centerline-looking
layer names) is flagged as a *candidate list to verify*, matching the
example script's own framing, not a classification.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf

from src.dwg_qty.annotation import annotation_layout_census
from src.dwg_qty.census import (
    block_attribute_census,
    layer_census,
    resolve_all_anonymous_blocks,
)


def looks_like_centerline_layer(name: str) -> bool:
    n = name.upper()
    return "CNTR" in n or "CENTERLINE" in n or "CNTL" in n


def census_dxf(dxf_path: Path | str) -> dict:
    """Full census payload for one DXF file (JSON-serializable)."""
    dxf_path = Path(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    layers = layer_census(msp)
    entity_total = sum(sum(t.values()) for t in layers.values())
    proxy_layers = {
        layer: types["ACAD_PROXY_ENTITY"]
        for layer, types in layers.items()
        if "ACAD_PROXY_ENTITY" in types
    }

    anon = resolve_all_anonymous_blocks(doc)
    anon_json = {
        name: {
            "status": r.status,
            "true_name": r.true_name,
            "entity_count": r.entity_count,
            "instance_count": r.instance_count,
            "instance_layers": sorted(r.instance_layers),
        }
        for name, r in anon.items()
        if r.instance_count > 0  # unused orphaned definitions are noise
    }

    block_attrs = block_attribute_census(msp)
    block_attrs_json = {
        name: {tag: sorted(values) for tag, values in attrs.items()}
        for name, attrs in block_attrs.items()
    }

    annotation_layouts = annotation_layout_census(doc)

    return {
        "file": dxf_path.name,
        "layer_count": len(layers),
        "entity_count": entity_total,
        "layers": {layer: types for layer, types in sorted(layers.items())},
        "centerline_layer_candidates": sorted(
            l for l in layers if looks_like_centerline_layer(l)
        ),
        "proxy_entity_layers": proxy_layers,
        "anonymous_blocks": anon_json,
        "block_attributes": block_attrs_json,
        "annotation_layouts": annotation_layouts,
    }


def census_job(dxf_paths: list[Path | str]) -> dict:
    """Census payload for a whole job (one or more converted DXF files)."""
    files = [census_dxf(p) for p in dxf_paths]
    return {
        "file_count": len(files),
        "total_entities": sum(f["entity_count"] for f in files),
        "files": files,
    }
