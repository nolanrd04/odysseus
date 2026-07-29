"""
Throwaway smoke-test route for DQ-1 (dwg_to_qty_sheet deployment scoping):
proves the ODA File Converter DWG->DXF path validated in
documentation/.SESSION_HANDOFFS/dwg_to_qty_sheet/building/dwg_qty/ actually
runs from inside the odysseus process on this machine, not just standalone
scripts. Not the real pipeline integration -- see DQ-4 in that ledger for
how dwg_qty functions should actually be exposed once this is settled.

dwg_qty lives under documentation/, not a normal package location, so it's
reached via a sys.path insert rather than a real import -- fine for a
temporary test harness, not something to build on.
"""

import sys
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse

_DWG_QTY_BUILDING_DIR = (
    Path(__file__).resolve().parent.parent
    / "documentation" / ".SESSION_HANDOFFS" / "dwg_to_qty_sheet" / "building"
)
if str(_DWG_QTY_BUILDING_DIR) not in sys.path:
    sys.path.insert(0, str(_DWG_QTY_BUILDING_DIR))


def setup_dwg_test_routes():
    router = APIRouter(prefix="/api/dwgtest", tags=["dwgtest"])

    @router.post("/convert")
    async def convert(file: UploadFile = File(...)):
        if not file.filename.lower().endswith(".dwg"):
            return JSONResponse(status_code=400, content={"error": "Upload a .dwg file"})

        import ezdxf
        from dwg_qty.convert import convert_dwg_to_dxf
        from dwg_qty.census import layer_census, resolve_all_anonymous_blocks

        with tempfile.TemporaryDirectory(prefix="dwg_test_") as tmp:
            tmp_dir = Path(tmp)
            dwg_path = tmp_dir / file.filename
            dwg_path.write_bytes(await file.read())

            t0 = time.monotonic()
            try:
                dxf_path = convert_dwg_to_dxf(dwg_path, tmp_dir / "out")
            except (FileNotFoundError, RuntimeError) as e:
                return JSONResponse(status_code=500, content={"error": str(e)})
            convert_ms = round((time.monotonic() - t0) * 1000)

            doc = ezdxf.readfile(dxf_path)
            msp = doc.modelspace()
            layers = layer_census(msp)
            anon = resolve_all_anonymous_blocks(doc)

            return {
                "success": True,
                "convert_ms": convert_ms,
                "dxf_filename": dxf_path.name,
                "entity_count": sum(sum(types.values()) for types in layers.values()),
                "layer_count": len(layers),
                "layers": layers,
                "anonymous_blocks": {
                    name: {
                        "status": r.status,
                        "true_name": r.true_name,
                        "instance_count": r.instance_count,
                    }
                    for name, r in anon.items()
                },
            }

    return router
