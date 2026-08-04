"""
dwg_qty — reusable, tested implementations of the deterministic DWG->quantity
extraction techniques validated across the DWG Quantity Pipeline investigation
(sessions 2026_7_27.1 through 2026_7_28.2, see
documentation/.SESSION_HANDOFFS/dwg_to_qty_sheet/project_planning/).

Every function here is pure geometry/DXF-structure logic with no LLM calls —
this is the deterministic layer the project's stated goal calls for. Mapping
a layer/block name to a specific Terra Underground cost code is NOT handled
here (that step is firm-convention-dependent and still needs either a
per-firm config or AI assistance) — this package only answers "how much of
X is drawn", not "which bid line X corresponds to".

Modules:
    convert     - DWG -> DXF conversion via ODA File Converter
    census      - layer/block/attribute census, anonymous dynamic block resolution
    annotation  - DIMENSION cached text and MULTILEADER content (paperspace notes)
    length      - arc-inclusive polyline/line/arc length, duplicate-entity dedup
    area        - HATCH area extraction with disjoint-loop sum-vs-net test
    proximity   - endpoint/mid-span tie-in and connection disambiguation
    centerline  - ray-cast width measurement, centerline-completeness validation
    boundary    - polygon closure testing (polygonize_full wrapper)

Each module's docstring cites the Key Finding(s) that validated the technique
it implements, so behavior can be traced back to the session that established it.

Every public name below is re-exported at package level (``from src.dwg_qty
import hatch_area``, not just ``from src.dwg_qty.area import hatch_area``) so
that ``import src.dwg_qty; dir(src.dwg_qty)`` — a model's natural first probe
of an unfamiliar library — actually shows the API instead of an empty
namespace. A live run without this re-export saw exactly that: the model
concluded from an empty ``dir()`` that the library was a non-functional
placeholder and fell back to ad hoc ``ezdxf`` code for the whole extraction,
never touching the (fully working) library the prompt told it to prefer.
"""

from src.dwg_qty.convert import convert_dwg_to_dxf, convert_job_dwgs
from src.dwg_qty.census import (
    AnonymousBlockResolution,
    block_attribute_census,
    layer_census,
    resolve_all_anonymous_blocks,
    resolve_anonymous_block,
)
from src.dwg_qty.annotation import (
    DimensionText,
    LegendSection,
    LotRecord,
    MultiLeaderContent,
    StationLabel,
    annotation_layout_census,
    keynote_legend_sections,
    read_dimension_texts,
    read_lot_records,
    read_multileader_contents,
    read_station_labels,
)
from src.dwg_qty.length import (
    DedupResult,
    entity_length,
    find_duplicate_arcs,
    find_duplicate_lines,
    layer_length,
)
from src.dwg_qty.area import (
    HatchAreaResult,
    hatch_area,
    hatch_boundary_polygons,
    layer_hatch_area,
)
from src.dwg_qty.proximity import TieInResult, find_tie_in
from src.dwg_qty.centerline import (
    CompletenessReport,
    WidthMeasurement,
    entities_to_geometry,
    ray_cast_width,
    validate_centerline_completeness,
)
from src.dwg_qty.boundary import (
    ClosureResult,
    RowAreaResult,
    closure_sweep,
    layer_edges,
    row_area,
    single_polygon_area,
)

__all__ = [
    "convert_dwg_to_dxf", "convert_job_dwgs",
    "AnonymousBlockResolution", "block_attribute_census", "layer_census",
    "resolve_all_anonymous_blocks", "resolve_anonymous_block",
    "DimensionText", "MultiLeaderContent", "annotation_layout_census",
    "read_dimension_texts", "read_multileader_contents",
    "LegendSection", "LotRecord", "StationLabel", "keynote_legend_sections",
    "read_lot_records", "read_station_labels",
    "DedupResult", "entity_length", "find_duplicate_arcs", "find_duplicate_lines",
    "layer_length",
    "HatchAreaResult", "hatch_area", "hatch_boundary_polygons", "layer_hatch_area",
    "TieInResult", "find_tie_in",
    "CompletenessReport", "WidthMeasurement", "entities_to_geometry",
    "ray_cast_width", "validate_centerline_completeness",
    "ClosureResult", "RowAreaResult", "closure_sweep", "layer_edges",
    "row_area", "single_polygon_area",
]

__version__ = "0.1.0"
