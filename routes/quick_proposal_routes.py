import asyncio
import base64
import io
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.constants import DATA_DIR, UPLOAD_DIR
from core.database import SessionLocal, ModelEndpoint
from src.endpoint_resolver import resolve_endpoint_runtime, build_chat_url, build_headers

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

RUNS_DIR = os.path.join(DATA_DIR, "quick_proposal_runs")

_QP_DIR            = Path(__file__).resolve().parent.parent / "src" / "quick_proposal"
_PROMPTS_DIR       = _QP_DIR / "prompts"
_CASE_LIBRARY_DIR  = _QP_DIR / "case_library"
_KP_PATH           = _QP_DIR / "knowledge_pack" / "knowledge_pack.json"

# ── Gemini ─────────────────────────────────────────────────────────────────────

_GEMINI_COMPLETIONS = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_GEMINI_CACHES      = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
GEMINI_MODEL        = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# ── Constants ──────────────────────────────────────────────────────────────────

PHASE2_JOBS = {"Forest Grove PH2"}


def _get_gemini_endpoint() -> tuple:
    """Return (completions_url, headers, api_key, model) for the Gemini endpoint.
    Uses the same DB record and URL-building logic as the main chat so the URL
    and auth always match what actually works.
    """
    try:
        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(
                ModelEndpoint.base_url.ilike("%googleapis.com%")
            ).first()
            if ep:
                base, api_key = resolve_endpoint_runtime(ep)
                url     = build_chat_url(base)
                headers = build_headers(api_key, base)
                model   = getattr(ep, "model", None) or GEMINI_MODEL
                logger.info(f"[quick_proposal] Gemini endpoint: {url} model={model}")
                return url, headers, api_key, model
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[quick_proposal] could not load Gemini endpoint from DB: {e}")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    return _GEMINI_COMPLETIONS, {"Authorization": f"Bearer {api_key}"}, api_key, GEMINI_MODEL

# ── Claude ─────────────────────────────────────────────────────────────────────

_CLAUDE_URL   = "https://api.anthropic.com/v1/messages"
_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


def _get_endpoint_for_model(model_id: str) -> Optional[tuple]:
    """Return (url, headers, api_key, model_id) for whichever endpoint advertises model_id.
    Searches cached_models and pinned_models on every endpoint.
    Returns None if no match is found.
    """
    try:
        db = SessionLocal()
        try:
            for ep in db.query(ModelEndpoint).all():
                candidates: list[str] = []
                for col in (ep.cached_models, ep.pinned_models):
                    if col:
                        try:
                            candidates.extend(json.loads(col))
                        except (json.JSONDecodeError, TypeError):
                            pass
                if model_id in candidates:
                    base, api_key = resolve_endpoint_runtime(ep)
                    url     = build_chat_url(base)
                    headers = build_headers(api_key, base)
                    logger.info(f"[quick_proposal] model {model_id!r} → endpoint {url}")
                    return url, headers, api_key, model_id
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[quick_proposal] _get_endpoint_for_model error: {e}")
    return None


def _get_claude_endpoint() -> tuple[str, str, str]:
    """Return (url, api_key, model) for Claude."""
    try:
        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(
                ModelEndpoint.base_url.ilike("%anthropic.com%")
            ).first()
            if ep:
                _, api_key = resolve_endpoint_runtime(ep)
                model = getattr(ep, "model", None) or _CLAUDE_MODEL
                logger.info(f"[quick_proposal] Claude endpoint model={model}")
                return _CLAUDE_URL, api_key, model
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[quick_proposal] could not load Claude endpoint from DB: {e}")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return _CLAUDE_URL, api_key, _CLAUDE_MODEL


# run_id → asyncio.Queue of SSE event dicts (None = end-of-stream sentinel)
_active_runs: dict[str, asyncio.Queue] = {}
# run_id → asyncio.Task (so we can cancel in-flight pipelines)
_active_tasks: dict[str, asyncio.Task] = {}


# ── Request model ──────────────────────────────────────────────────────────────

class Phase3OnlyRequest(BaseModel):
    manager_model: str = ""
    gemini_model: str = ""


class ReclassifyPageRequest(BaseModel):
    gemini_model: str = ""


class ClassificationsUpdate(BaseModel):
    pages: List[dict] = []


class RunRequest(BaseModel):
    upload_id: str
    job_type: str = ""
    notes: str = ""
    selected_jobs: List[str] = []
    gemini_model: str = ""
    manager_model: str = ""
    filename: str = ""


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _emit(queue: asyncio.Queue, event_type: str, **kwargs):
    await queue.put({"type": event_type, **kwargs})


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        inner = parts[1]
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return text


def _save_run_meta(run_id: str, **fields) -> None:
    meta_path = Path(RUNS_DIR) / run_id / "meta.json"
    try:
        existing: dict = {}
        if meta_path.is_file():
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        existing.update(fields)
        meta_path.write_text(json.dumps(existing), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] meta save failed run={run_id}: {e}")


def _log_phase3_event(log_path: str, event: dict) -> None:
    """Append one phase3 event to the run's JSONL log, stripping large image data."""
    entry = {k: v for k, v in event.items() if k != "image_url"}
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"[quick_proposal] phase3 log write failed: {e}")


def _save_extracted_values(run_id: str, extracted_values: dict) -> None:
    """Patch extracted_values into results.json without touching other keys."""
    results_path = Path(RUNS_DIR) / run_id / "results.json"
    try:
        existing: dict = {}
        if results_path.is_file():
            existing = json.loads(results_path.read_text(encoding="utf-8"))
        existing["extracted_values"] = extracted_values
        results_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] extracted_values save failed run={run_id}: {e}")


async def _run_phase3_only(index, queue: asyncio.Queue, run_id: str, manager_model: str = "", gemini_model: str = "") -> None:
    """Re-run phase2 index build + phase3 extraction loop using saved page classifications."""
    try:
        await _emit(queue, "phase_start", phase="phase2", label="Building extraction index…")
        index.extracted_data["phase1_summary"] = _build_phase1_summary(index)
        await _emit(queue, "phase_complete", phase="phase2")

        await phase3_claude_gemini_loop(index, queue, manager_model=manager_model, gemini_model=gemini_model)
        _save_run_results(run_id, index)
        _save_run_meta(run_id, status="complete")
    except Exception as e:
        logger.error(f"[quick_proposal] phase3-only error run={run_id}: {e}", exc_info=True)
        await _emit(queue, "error", message=str(e), phase="unknown")
        _save_run_meta(run_id, status="error")
    finally:
        await queue.put(None)


def _save_run_results(run_id: str, index) -> None:
    results_path = Path(RUNS_DIR) / run_id / "results.json"
    try:
        results = {
            "pages": [
                {
                    "idx":         p.idx,
                    "sheet_type":  p.classification,
                    "importance":  p.importance,
                    "description": p.description,
                    "bbox_ids":    p.bbox_ids,
                }
                for p in index.pages
            ],
            "bboxes": {
                bid: {
                    "id":          rec.id,
                    "page_idx":    rec.page_idx,
                    "x1":          rec.x1,
                    "y1":          rec.y1,
                    "x2":          rec.x2,
                    "y2":          rec.y2,
                    "parent_id":      rec.parent_id,
                    "depth":          rec.depth,
                    "description":    rec.description,
                    "element_type":    rec.element_type,
                    "element_subtype": rec.element_subtype,
                    "importance":      rec.importance,
                }
                for bid, rec in index.bboxes.items()
            },
            "extracted_data":   index.extracted_data,
            "extracted_values": index.extracted_values,
        }
        results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] results save failed run={run_id}: {e}")


def _resolve_upload_path(upload_id: str) -> str:
    direct = os.path.join(UPLOAD_DIR, upload_id)
    if os.path.isfile(direct):
        return direct
    for root, _, files in os.walk(UPLOAD_DIR):
        if upload_id in files:
            path = os.path.join(root, upload_id)
            if os.path.isfile(path):
                return path
    raise FileNotFoundError(f"Upload {upload_id} not found")


# ── Case library ───────────────────────────────────────────────────────────────

def _compact_case(data: dict) -> dict | None:
    proposals = data.get("proposals", [])
    if not proposals:
        return None
    idx      = data.get("primary_proposal_index", 0)
    proposal = proposals[idx] if idx < len(proposals) else proposals[0]
    scale    = data.get("derived", {}).get("scale_metrics", {})
    rollup   = data.get("derived", {}).get("rollups", {})
    job_name = data.get("job_name", "")
    return {
        "job_name":           job_name,
        "job_type":           data.get("classification", {}).get("job_type"),
        "proposal_date":      proposal.get("proposal_date"),
        "total":              proposal.get("total_reconciled"),
        "lot_count":          scale.get("lot_count"),
        "road_LF":            scale.get("road_LF"),
        "ROW_SF":             scale.get("ROW_SF"),
        "stripping_depth_in": scale.get("stripping_depth_in"),
        "road_subgrade_SY":   scale.get("road_subgrade_SY"),
        "road_paving_SY":     scale.get("road_paving_SY"),
        "ballast_CY":         scale.get("ballast_CY"),
        "dollar_per_ROW_SF":  rollup.get("dollar_per_ROW_SF"),
        "dollar_per_lot":     rollup.get("dollar_per_lot"),
        "is_phase2":          job_name in PHASE2_JOBS,
        "exclusions_text":    proposal.get("exclusions_text"),
        "line_items": [
            {
                "description": item["description"],
                "unit":        item["unit"],
                "qty":         item["qty"],
                "unit_price":  item["unit_price"],
                "ext_price":   item["ext_price"],
            }
            for item in proposal.get("line_items", [])
            if not item.get("is_optional")
        ],
    }


def _load_case_library() -> list[dict]:
    cases = []
    for path in sorted(_CASE_LIBRARY_DIR.glob("*.json")):
        try:
            data    = json.loads(path.read_text(encoding="utf-8"))
            compact = _compact_case(data)
            if compact:
                cases.append(compact)
        except Exception as e:
            logger.warning(f"[quick_proposal] case library parse error {path.name}: {e}")
    return cases


def _load_knowledge_pack() -> dict:
    return json.loads(_KP_PATH.read_text(encoding="utf-8"))


# ── Phase 2: index construction ────────────────────────────────────────────────

def _build_phase1_summary(index) -> str:
    """Build a structured text index of Phase 1 classifications for Phase 3 context."""
    importance_order = {"high": 0, "medium": 1, "low": 2}
    sorted_pages = sorted(
        index.pages,
        key=lambda p: (importance_order.get(p.importance or "low", 3), p.idx),
    )
    lines = ["# Phase 1 Classification Summary"]
    current_imp = None
    for page in sorted_pages:
        imp = page.importance or "low"
        if imp != current_imp:
            current_imp = imp
            lines.append(f"\n## {imp.title()}-Importance Pages")
        sheet = page.classification or "other"
        lines.append(f"\nPage {page.idx + 1} (0-indexed: {page.idx}) — {sheet}")
        if page.description:
            lines.append(f"  Description: {page.description}")
        for bbox_id in page.bbox_ids:
            rec = index.bboxes.get(bbox_id)
            if rec:
                et = f" [{rec.element_type}]" if rec.element_type else ""
                st = f"/{rec.element_subtype}" if rec.element_subtype else ""
                lines.append(f"  Region {bbox_id}{et}{st}: {rec.description}")
    return "\n".join(lines)


# ── Gemini context cache ───────────────────────────────────────────────────────

async def _gemini_cache_create(system_text: str, api_key: str) -> Optional[str]:
    """Create a Gemini context cache for the Phase 1 system instruction.
    Returns the cache name or None if below the minimum token threshold
    (in which case callers fall back to uncached requests).
    """
    payload = {
        "model": f"models/{GEMINI_MODEL}",
        "systemInstruction": {
            "role": "system",
            "parts": [{"text": system_text}],
        },
        "ttl": "3600s",
    }
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(_GEMINI_CACHES, headers=headers, json=payload)
            if not r.is_success:
                logger.info(
                    f"[quick_proposal] Gemini cache create failed ({r.status_code}) "
                    f"— falling back to uncached: {r.text[:200]}"
                )
                return None
            name = r.json().get("name")
            logger.info(f"[quick_proposal] Gemini cache created: {name}")
            return name
    except Exception as e:
        logger.info(f"[quick_proposal] Gemini cache create error — falling back: {e}")
        return None


async def _gemini_cache_delete(cache_name: str, api_key: str) -> None:
    url = f"https://generativelanguage.googleapis.com/v1beta/{cache_name}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.delete(url, headers={"x-goog-api-key": api_key})
            logger.info(f"[quick_proposal] Gemini cache deleted: {cache_name}")
    except Exception as e:
        logger.warning(f"[quick_proposal] Gemini cache delete failed: {e}")


# ── Phase 1: per-page Gemini classification ────────────────────────────────────

async def _classify_one_page(
    image_path: str,
    prompt: str,
    url: str,
    headers: dict,
    model: str,
    cache_name: Optional[str],
) -> dict:
    img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    req_headers = {**headers, "Content-Type": "application/json"}

    if cache_name:
        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": "Classify this page per your instructions."},
            ],
        }]
        payload = {
            "model":           model,
            "messages":        messages,
            "max_tokens":      8192,
            "response_format": {"type": "json_object"},
            "cached_content":  cache_name,
        }
    else:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            ]},
        ]
        payload = {
            "model":           model,
            "messages":        messages,
            "max_tokens":      8192,
            "response_format": {"type": "json_object"},
        }

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers=req_headers, json=payload)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(_strip_fences(text))
        except json.JSONDecodeError:
            logger.warning(f"[quick_proposal] JSON parse failed, raw response: {text[:500]}")
            raise


async def phase1_classify_pages(index, queue: asyncio.Queue, model_override: str = "") -> None:
    """Classify each rendered page via Gemini and emit page_classified events."""
    from src.quick_proposal.index import BboxRecord

    if model_override:
        found = _get_endpoint_for_model(model_override)
        if found:
            url, headers, api_key, model = found
        else:
            logger.warning(f"[quick_proposal] no endpoint found for model {model_override!r}, falling back to Gemini")
            url, headers, api_key, model = _get_gemini_endpoint()
            model = model_override
            if not api_key:
                await _emit(queue, "error", message="No Gemini API key found — add a googleapis.com endpoint in Settings", phase="phase1")
                return
    else:
        url, headers, api_key, model = _get_gemini_endpoint()
        if not api_key:
            await _emit(queue, "error", message="No Gemini API key found — add a googleapis.com endpoint in Settings", phase="phase1")
            return

    prompt     = (_PROMPTS_DIR / "gemini_phase1.txt").read_text(encoding="utf-8")
    cache_name = await _gemini_cache_create(prompt, api_key)

    await _emit(queue, "phase_start", phase="phase1",
                label="Classifying pages…",
                cached=cache_name is not None)

    try:
        for page in index.pages:
            last_err = None
            result = None
            for attempt in range(3):
                try:
                    result = await _classify_one_page(page.image_path, prompt, url, headers, model, cache_name)
                    last_err = None
                    break
                except httpx.HTTPStatusError as e:
                    last_err = e
                    status = e.response.status_code
                    retryable = status == 400 or status >= 500
                    if not retryable or attempt == 2:
                        break
                    logger.warning(f"[quick_proposal] phase1 page={page.idx} HTTP {status} attempt={attempt}: {e.response.text[:500]}")
                    await asyncio.sleep(2 ** (attempt + 1))
                except Exception as e:
                    last_err = e
                    break
            if last_err is not None:
                logger.warning(f"[quick_proposal] phase1 page={page.idx} error: {last_err}", exc_info=True)
                await _emit(queue, "page_classified",
                            page_idx=page.idx, sheet_type="other",
                            importance="low", description="(classification failed)",
                            regions=[], error=str(last_err))
                continue

            page.classification = result.get("sheet_type", "other")
            page.importance     = result.get("importance", "low")
            page.description    = result.get("description", "")

            regions_out = []
            for region in result.get("regions", []):
                bbox_id = f"{page.idx}_{region['id']}"
                bbox    = region.get("bbox", [0, 0, 100, 100])
                if len(bbox) < 4:
                    bbox = bbox + [0] * (4 - len(bbox))
                record  = BboxRecord(
                    id=bbox_id, page_idx=page.idx,
                    x1=bbox[0], y1=bbox[1], x2=bbox[2], y2=bbox[3],
                    parent_id=None, depth=0,
                    description=(
                        f"{region.get('label', '')}: {region.get('extraction_hint', '')}"
                    ),
                    element_type=region.get("element_type"),
                    element_subtype=region.get("element_subtype"),
                    importance=region.get("importance", "medium"),
                )
                index.bboxes[bbox_id] = record
                page.bbox_ids.append(bbox_id)
                regions_out.append({
                    "id":              bbox_id,
                    "label":           region.get("label", ""),
                    "bbox":            bbox,
                    "extraction_hint": region.get("extraction_hint", ""),
                    "importance":      region.get("importance", "medium"),
                })

            await _emit(queue, "page_classified",
                        page_idx=page.idx,
                        sheet_type=page.classification,
                        importance=page.importance,
                        description=page.description,
                        regions=regions_out)
    finally:
        if cache_name:
            await _gemini_cache_delete(cache_name, api_key)

    await _emit(queue, "phase_complete", phase="phase1")


# ── Phase 3: Gemini extraction tools ───────────────────────────────────────────

_GEMINI_PHASE3_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "enhance_region",
            "description": "Crops a region of the source plan page at high DPI and returns the image for visual inspection. Always use this before writing a value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bbox_id": {"type": "string", "description": "Region identifier from the Phase 1 index, e.g. '4_r0'"},
                    "dpi":     {"type": "integer", "description": "Render DPI (default 200)"},
                },
                "required": ["bbox_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "index_read",
            "description": "Returns all currently extracted values as JSON. Call this to see what has already been written before starting a new area of extraction.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_bbox",
            "description": "Re-renders a page crop at adjusted coordinates without mutating the stored bbox. Use when the Phase 1 bbox is misaligned. Coordinates are percentages (0-100).",
            "parameters": {
                "type": "object",
                "properties": {
                    "bbox_id": {"type": "string", "description": "Source bbox_id to identify which page to render from"},
                    "x1": {"type": "number", "description": "Adjusted left edge (0-100)"},
                    "y1": {"type": "number", "description": "Adjusted top edge (0-100)"},
                    "x2": {"type": "number", "description": "Adjusted right edge (0-100)"},
                    "y2": {"type": "number", "description": "Adjusted bottom edge (0-100)"},
                    "dpi": {"type": "integer", "description": "Render DPI (default 200)"},
                },
                "required": ["bbox_id", "x1", "y1", "x2", "y2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "index_write",
            "description": "Writes an extracted value to the shared index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key":           {"type": "string"},
                    "value":         {"description": "The extracted value (string, number, or bool)"},
                    "source_bbox_id":{"type": "string", "description": "bbox_id this value was read from"},
                    "confidence":    {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["key", "value", "source_bbox_id", "confidence"],
            },
        },
    },
]


async def _execute_gemini_tool(
    name: str, args: dict, index, queue: asyncio.Queue
) -> list:
    """Execute a Gemini Phase 3 tool. Returns a list of OpenAI content blocks."""
    if name == "enhance_region":
        bbox_id = args.get("bbox_id", "")
        dpi     = int(args.get("dpi", 200))
        rec     = index.bboxes.get(bbox_id)
        if not rec:
            return [{"type": "text", "text": f"Error: bbox_id '{bbox_id}' not found in index."}]
        try:
            from PIL import Image as PilImage

            if index.source_is_pdf:
                from pdf2image import convert_from_path
                images = await asyncio.to_thread(
                    convert_from_path,
                    index.source_path,
                    dpi=dpi,
                    first_page=rec.page_idx + 1,
                    last_page=rec.page_idx + 1,
                )
                if not images:
                    return [{"type": "text", "text": "Error: could not render page"}]
                img = images[0]
            else:
                img = await asyncio.to_thread(PilImage.open, index.pages[rec.page_idx].image_path)

            w, h  = img.size
            x1    = max(0, int(rec.x1 / 100 * w))
            y1    = max(0, int(rec.y1 / 100 * h))
            x2    = min(w, int(rec.x2 / 100 * w))
            y2    = min(h, int(rec.y2 / 100 * h))
            if x2 <= x1 or y2 <= y1:
                return [{"type": "text", "text": f"Error: invalid bbox coords ({x1},{y1},{x2},{y2})"}]
            cropped = img.crop((x1, y1, x2, y2))
            buf     = io.BytesIO()
            cropped.save(buf, "JPEG", quality=90)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return [
                {"type": "text", "text": f"Region {bbox_id} at {dpi} DPI:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        except Exception as e:
            logger.warning(f"[quick_proposal] enhance_region error: {e}", exc_info=True)
            return [{"type": "text", "text": f"Error rendering region: {e}"}]

    elif name == "index_read":
        return [{"type": "text", "text": json.dumps(index.extracted_values) if index.extracted_values else "{}"}]

    elif name == "move_bbox":
        bbox_id = args.get("bbox_id", "")
        dpi     = int(args.get("dpi", 200))
        rec     = index.bboxes.get(bbox_id)
        if not rec:
            return [{"type": "text", "text": f"Error: bbox_id '{bbox_id}' not found in index."}]
        try:
            from PIL import Image as PilImage
            x1_pct = float(args.get("x1", rec.x1))
            y1_pct = float(args.get("y1", rec.y1))
            x2_pct = float(args.get("x2", rec.x2))
            y2_pct = float(args.get("y2", rec.y2))

            if index.source_is_pdf:
                from pdf2image import convert_from_path
                images = await asyncio.to_thread(
                    convert_from_path,
                    index.source_path,
                    dpi=dpi,
                    first_page=rec.page_idx + 1,
                    last_page=rec.page_idx + 1,
                )
                if not images:
                    return [{"type": "text", "text": "Error: could not render page"}]
                img = images[0]
            else:
                img = await asyncio.to_thread(PilImage.open, index.pages[rec.page_idx].image_path)

            w, h  = img.size
            x1    = max(0, int(x1_pct / 100 * w))
            y1    = max(0, int(y1_pct / 100 * h))
            x2    = min(w, int(x2_pct / 100 * w))
            y2    = min(h, int(y2_pct / 100 * h))
            if x2 <= x1 or y2 <= y1:
                return [{"type": "text", "text": f"Error: invalid adjusted bbox coords ({x1},{y1},{x2},{y2})"}]
            cropped = img.crop((x1, y1, x2, y2))
            buf     = io.BytesIO()
            cropped.save(buf, "JPEG", quality=90)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return [
                {"type": "text", "text": f"Adjusted view of page {rec.page_idx} at ({x1_pct:.1f},{y1_pct:.1f},{x2_pct:.1f},{y2_pct:.1f}) {dpi} DPI:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        except Exception as e:
            logger.warning(f"[quick_proposal] move_bbox error: {e}", exc_info=True)
            return [{"type": "text", "text": f"Error rendering adjusted region: {e}"}]

    elif name == "index_write":
        key           = args.get("key", "")
        value         = args.get("value")
        source_bbox_id = args.get("source_bbox_id")
        confidence    = args.get("confidence", "medium")
        index.extracted_values[key] = {
            "value":          value,
            "source_bbox_id": source_bbox_id,
            "confidence":     confidence,
        }
        await _emit(queue, "index_update",
                    key=key, value=value,
                    source_bbox_id=source_bbox_id, confidence=confidence)
        _save_extracted_values(index.run_id, index.extracted_values)
        return [{"type": "text", "text": f"Wrote {key} = {json.dumps(value)} (confidence={confidence})"}]

    else:
        return [{"type": "text", "text": f"Unknown tool: {name}"}]


async def _run_gemini_with_tools(
    user_message: str,
    gemini_state: dict,
    index,
    queue: asyncio.Queue,
) -> str:
    """Append user_message to Gemini history, call Gemini, handle tool loops, return final text."""
    gemini_state["messages"].append({"role": "user", "content": user_message})
    req_headers = {**gemini_state["headers"], "Content-Type": "application/json"}

    for _ in range(200):
        payload: dict = {
            "model":    gemini_state["model"],
            "messages": gemini_state["messages"],
            "tools":    _GEMINI_PHASE3_TOOLS,
            "max_tokens": 8192,
        }
        if gemini_state.get("cache_name"):
            payload["cached_content"] = gemini_state["cache_name"]

        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(gemini_state["url"], headers=req_headers, json=payload)
            if not r.is_success:
                logger.error(f"[quick_proposal] Gemini phase3 error {r.status_code} model={payload['model']} url={gemini_state['url']}: {r.text[:500]}")
            r.raise_for_status()

        resp_data  = r.json()
        choice     = resp_data.get("choices", [{}])[0]
        msg        = choice.get("message", {})
        tool_calls = msg.get("tool_calls") or []
        text_out   = msg.get("content") or ""

        if tool_calls:
            gemini_state["messages"].append({
                "role": "assistant",
                "content": text_out or None,
                "tool_calls": tool_calls,
            })
        else:
            gemini_state["messages"].append({
                "role": "assistant",
                "content": text_out,
            })

        if not tool_calls:
            return text_out

        for tc in tool_calls:
            fn        = tc.get("function", {})
            tool_name = fn.get("name", "")
            raw_args  = fn.get("arguments", "{}")
            try:
                tc_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                tc_args = {}
            await _emit(queue, "extraction_message",
                        role="tool_call",
                        tool=tool_name,
                        args=json.dumps(tc_args))
            if log_path := gemini_state.get("log_path"):
                _log_phase3_event(log_path, {"type": "extraction_message", "role": "tool_call", "tool": tool_name, "args": json.dumps(tc_args)})

        pending_images: list = []  # list of (bbox_id, image_block)
        for tc in tool_calls:
            tc_id     = tc.get("id", "")
            fn        = tc.get("function", {})
            tool_name = fn.get("name", "")
            raw_args  = fn.get("arguments", "{}")
            try:
                tc_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                tc_args = {}
            result_blocks = await _execute_gemini_tool(tool_name, tc_args, index, queue)
            # Gemini OpenAI-compat rejects image_url in tool messages; keep only text
            # here and carry images forward as a user message instead.
            text_parts = [b["text"] for b in result_blocks if b.get("type") == "text"]
            bbox_id = tc_args.get("bbox_id", "")
            pending_images.extend((bbox_id, b) for b in result_blocks if b.get("type") == "image_url")
            gemini_state["messages"].append({
                "role":         "tool",
                "tool_call_id": tc_id,
                "content":      "\n".join(text_parts) if text_parts else "(no output)",
            })
        if pending_images:
            for bid, img_block in pending_images:
                await _emit(queue, "region_preview", bbox_id=bid,
                            image_url=img_block["image_url"]["url"])
                if log_path := gemini_state.get("log_path"):
                    _log_phase3_event(log_path, {"type": "region_preview", "bbox_id": bid})
            # Pair images with a continuation instruction — a bare image message
            # causes Gemini to return empty content and kill the loop.
            gemini_state["messages"].append({
                "role": "user",
                "content": [b for _, b in pending_images] + [{"type": "text", "text": "Above are the requested region images. Continue your extraction."}],
            })

    logger.warning("[quick_proposal] Gemini tool loop hit 20-round limit")
    return "(extraction loop limit reached)"


def _call_manager_sync(url: str, headers: dict, payload: dict) -> dict:
    """Synchronous OpenAI-compatible completions call (for asyncio.to_thread)."""
    with httpx.Client(timeout=300.0) as client:
        r = client.post(url, headers={**headers, "content-type": "application/json"}, json=payload)
        r.raise_for_status()
        return r.json()


async def phase3_claude_gemini_loop(index, queue: asyncio.Queue, manager_model: str = "", gemini_model: str = "") -> None:
    """Phase 3: Manager LLM orchestrates Gemini extraction via send_to_gemini tool (OpenAI-compatible format)."""
    # Resolve manager endpoint — prefer the explicitly selected model, fall back to Anthropic endpoint.
    mgr_url = mgr_headers = mgr_api_key = mgr_model_id = None

    if manager_model:
        found = _get_endpoint_for_model(manager_model)
        if found:
            mgr_url, mgr_headers, mgr_api_key, mgr_model_id = found

    if not mgr_url:
        try:
            db = SessionLocal()
            try:
                ep = db.query(ModelEndpoint).filter(
                    ModelEndpoint.base_url.ilike("%anthropic.com%")
                ).first()
                if ep:
                    base, mgr_api_key = resolve_endpoint_runtime(ep)
                    mgr_url      = build_chat_url(base)
                    mgr_headers  = build_headers(mgr_api_key, base)
                    mgr_model_id = getattr(ep, "model", None) or _CLAUDE_MODEL
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[quick_proposal] could not load Claude endpoint from DB: {e}")

        if not mgr_url:
            mgr_api_key  = os.environ.get("ANTHROPIC_API_KEY", "")
            mgr_url      = "https://api.anthropic.com/v1/chat/completions"
            mgr_headers  = {"Authorization": f"Bearer {mgr_api_key}", "anthropic-version": "2023-06-01"}
            mgr_model_id = _CLAUDE_MODEL

    if mgr_url is None:
        await _emit(queue, "error",
                    message="No manager endpoint found — add an endpoint in Settings",
                    phase="phase3")
        return

    if gemini_model:
        found = _get_endpoint_for_model(gemini_model)
        if found:
            gemini_url, gemini_headers, gemini_api_key, _unused = found
        else:
            gemini_url, gemini_headers, gemini_api_key, _unused = _get_gemini_endpoint()
    else:
        gemini_url, gemini_headers, gemini_api_key, gemini_model = _get_gemini_endpoint()

    # Cache gemini_phase3.txt system prompt for reuse across all Gemini turns.
    gemini_phase3_prompt = (_PROMPTS_DIR / "gemini_phase3.txt").read_text(encoding="utf-8")
    gemini_cache_name    = await _gemini_cache_create(gemini_phase3_prompt, gemini_api_key)

    gemini_state: dict = {
        "messages":   [],
        "url":        gemini_url,
        "headers":    gemini_headers,
        "model":      gemini_model,
        "cache_name": gemini_cache_name,
        "log_path":   str(Path(RUNS_DIR) / index.run_id / "phase3_log.jsonl"),
    }

    if not gemini_cache_name:
        gemini_state["messages"].append({
            "role":    "system",
            "content": gemini_phase3_prompt,
        })

    # Seed Gemini's history with the Phase 1 index as initial context.
    phase1_summary = index.extracted_data.get("phase1_summary", "")
    gemini_state["messages"].extend([
        {
            "role":    "user",
            "content": (
                "Here is the Phase 1 classification index for this plan set:\n\n"
                + phase1_summary
                + "\n\nStand by for extraction instructions."
            ),
        },
        {
            "role":    "assistant",
            "content": "Understood. I have reviewed the Phase 1 index and am ready to begin extraction.",
        },
    ])

    manager_system = (_PROMPTS_DIR / "manager_system.txt").read_text(encoding="utf-8")

    send_to_gemini_tool = {
        "type": "function",
        "function": {
            "name":        "send_to_gemini",
            "description": (
                "Sends a message to the Gemini extraction sub-agent and returns its response. "
                "The sub-agent has access to plan images via enhance_region, and reads/writes "
                "the shared index via index_read / index_write."
            ),
            "parameters": {
                "type":       "object",
                "properties": {
                    "message": {"type": "string", "description": "Instruction or question to send to Gemini."},
                },
                "required": ["message"],
            },
        },
    }

    mgr_messages = [
        {"role": "system", "content": manager_system},
        {"role": "user",   "content": "Here is the Phase 1 index from the plan set. Begin extraction.\n\n" + phase1_summary},
    ]

    await _emit(queue, "phase_start", phase="phase3", label="Extracting values from plans…")

    try:
        for _ in range(60):
            payload = {
                "model":      mgr_model_id,
                "max_tokens": 8192,
                "messages":   mgr_messages,
                "tools":      [send_to_gemini_tool],
            }
            resp = await asyncio.to_thread(_call_manager_sync, mgr_url, mgr_headers, payload)

            if "error" in resp:
                err_msg = resp.get("error", {}).get("message", str(resp))
                logger.error(f"[quick_proposal] manager error in phase3: {err_msg}")
                await _emit(queue, "error", message=f"Manager error: {err_msg}", phase="phase3")
                return

            choice        = resp.get("choices", [{}])[0]
            finish_reason = choice.get("finish_reason")
            msg           = choice.get("message", {})
            text_content  = msg.get("content") or ""
            tool_calls    = msg.get("tool_calls") or []

            log_path = gemini_state.get("log_path", "")
            if text_content.strip():
                await _emit(queue, "extraction_message", role="claude", text=text_content, model=mgr_model_id)
                if log_path:
                    _log_phase3_event(log_path, {"type": "extraction_message", "role": "claude", "text": text_content, "model": mgr_model_id})

            assistant_msg: dict = {"role": "assistant", "content": text_content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            mgr_messages.append(assistant_msg)

            if finish_reason != "tool_calls":
                break

            for tc in tool_calls:
                tool_id   = tc.get("id", "")
                tool_name = tc.get("function", {}).get("name", "")
                try:
                    tool_input = json.loads(tc.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_input = {}

                if tool_name == "send_to_gemini":
                    msg_text = tool_input.get("message", "")
                    await _emit(queue, "extraction_message", role="claude_to_gemini", text=msg_text, model=mgr_model_id)
                    if log_path:
                        _log_phase3_event(log_path, {"type": "extraction_message", "role": "claude_to_gemini", "text": msg_text, "model": mgr_model_id})
                    gemini_resp = await _run_gemini_with_tools(msg_text, gemini_state, index, queue)
                    if gemini_resp.strip():
                        await _emit(queue, "extraction_message", role="gemini", text=gemini_resp)
                        if log_path:
                            _log_phase3_event(log_path, {"type": "extraction_message", "role": "gemini", "text": gemini_resp})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": gemini_resp or "(extraction complete)"})
                else:
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": f"Unknown tool: {tool_name}"})

    finally:
        if gemini_cache_name:
            await _gemini_cache_delete(gemini_cache_name, gemini_api_key)

    await _emit(queue, "phase_complete", phase="phase3")


# ── PDF / image rendering ──────────────────────────────────────────────────────

async def render_pdf_pages(pdf_path: str, run_id: str, queue: asyncio.Queue) -> list:
    """Render all PDF pages to JPEG at 224 DPI using pdf2image. Emits page_ready per page."""
    from pdf2image import convert_from_path
    from src.quick_proposal.index import PageRecord

    pages_dir = os.path.join(RUNS_DIR, run_id, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    # Run blocking conversion in a thread so the event loop stays responsive.
    images = await asyncio.to_thread(convert_from_path, pdf_path, dpi=224)
    logger.info(f"[quick_proposal] run={run_id} pages={len(images)} dpi=224")

    pages = []
    for i, img in enumerate(images):
        img_path = os.path.join(pages_dir, f"page_{i:04d}.jpg")
        img.save(img_path, "JPEG", quality=85)
        pages.append(PageRecord(
            idx=i, image_path=img_path,
            classification="", importance="", description="", bbox_ids=[],
        ))
        await _emit(queue, "page_ready",
                    page_idx=i,
                    url=f"/api/quick_proposal/pages/{run_id}/{i}")
        await asyncio.sleep(0)  # yield so SSE flushes per page

    return pages


async def handle_single_image(image_path: str, run_id: str, queue: asyncio.Queue) -> list:
    """Copy a single image into the pages dir and emit page_ready."""
    from src.quick_proposal.index import PageRecord

    pages_dir = os.path.join(RUNS_DIR, run_id, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    dest = os.path.join(pages_dir, "page_0000.jpg")
    shutil.copy2(image_path, dest)

    await _emit(queue, "page_ready",
                page_idx=0,
                url=f"/api/quick_proposal/pages/{run_id}/0")
    return [PageRecord(idx=0, image_path=dest,
                       classification="", importance="", description="", bbox_ids=[])]


# ── Pipeline ───────────────────────────────────────────────────────────────────

async def run_pipeline(
    run_id: str,
    upload_id: str,
    queue: asyncio.Queue,
    notes: str = "",
    selected_jobs: Optional[List[str]] = None,
    gemini_model: str = "",
    manager_model: str = "",
) -> None:
    _success = False
    try:
        # Step 1 — render pages
        await _emit(queue, "phase_start", phase="load", label="Rendering pages…")
        source_path = _resolve_upload_path(upload_id)

        from src.quick_proposal.index import ChatIndex
        index = ChatIndex(
            run_id=run_id,
            source_path=source_path,
            job_notes=notes,
            selected_jobs=selected_jobs or [],
        )
        index.source_is_pdf = source_path.lower().endswith(".pdf")

        index.pages = (
            await render_pdf_pages(source_path, run_id, queue)
            if index.source_is_pdf
            else await handle_single_image(source_path, run_id, queue)
        )
        await _emit(queue, "phase_complete", phase="load")
        _save_run_results(run_id, index)  # persist page list so view panel works if run is cancelled before phase3

        # Step 2 — load knowledge base
        await _emit(queue, "phase_start", phase="index", label="Loading knowledge base…")
        try:
            index.knowledge_pack = _load_knowledge_pack()
            index.case_library   = _load_case_library()
        except Exception as e:
            raise RuntimeError(f"Failed to load knowledge base: {e}")

        await _emit(queue, "index_loaded",
                    jobs=[{"id": c["job_name"], "name": c["job_name"]}
                          for c in index.case_library])
        await _emit(queue, "phase_complete", phase="index")

        # Step 3 — Phase 1 per-page Gemini classification
        await phase1_classify_pages(index, queue, model_override=gemini_model)
        _save_run_results(run_id, index)  # persist phase1 classifications + bboxes

        # Step 4 — Phase 2: build text index from Phase 1 results (pure Python, no LLM)
        await _emit(queue, "phase_start", phase="phase2", label="Building extraction index…")
        index.extracted_data["phase1_summary"] = _build_phase1_summary(index)
        await _emit(queue, "phase_complete", phase="phase2")

        # Step 5 — Phase 3: manager LLM + Gemini extraction tool loop
        await phase3_claude_gemini_loop(index, queue, manager_model=manager_model, gemini_model=gemini_model)
        _success = True

    except Exception as e:
        logger.error(f"[quick_proposal] pipeline error run={run_id}: {e}", exc_info=True)
        await _emit(queue, "error", message=str(e), phase="unknown")
        _save_run_meta(run_id, status="error")
    finally:
        if _success:
            _save_run_meta(run_id, status="complete")
            _save_run_results(run_id, index)
        await queue.put(None)


# ── Routes ─────────────────────────────────────────────────────────────────────

def setup_quick_proposal_routes():
    router = APIRouter(prefix="/api/quick_proposal", tags=["quick_proposal"])

    @router.post("/run")
    async def start_run(req: RunRequest):
        run_id = str(uuid.uuid4())
        os.makedirs(os.path.join(RUNS_DIR, run_id, "pages"), exist_ok=True)

        _save_run_meta(
            run_id,
            id=run_id,
            upload_id=req.upload_id,
            filename=req.filename or req.upload_id,
            notes=req.notes,
            timestamp=int(time.time()),
            status="running",
        )

        queue: asyncio.Queue = asyncio.Queue()
        _active_runs[run_id] = queue

        task = asyncio.create_task(run_pipeline(
            run_id, req.upload_id, queue,
            notes=req.notes,
            selected_jobs=req.selected_jobs or None,
            gemini_model=req.gemini_model,
            manager_model=req.manager_model,
        ))
        _active_tasks[run_id] = task
        return {"run_id": run_id}

    @router.get("/runs")
    async def list_runs():
        runs = []
        runs_dir = Path(RUNS_DIR)
        if runs_dir.is_dir():
            for run_dir in runs_dir.iterdir():
                meta_path = run_dir / "meta.json"
                if meta_path.is_file():
                    try:
                        runs.append(json.loads(meta_path.read_text(encoding="utf-8")))
                    except Exception:
                        pass
        runs.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return runs[:50]

    @router.get("/prompts")
    async def list_prompts():
        prompts = []
        for path in sorted(_PROMPTS_DIR.glob("*.txt")):
            prompts.append({"name": path.stem, "content": path.read_text(encoding="utf-8")})
        return prompts

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str):
        meta_path    = Path(RUNS_DIR) / run_id / "meta.json"
        results_path = Path(RUNS_DIR) / run_id / "results.json"
        if not meta_path.is_file():
            raise HTTPException(status_code=404, detail="Run not found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        results: dict = {}
        if results_path.is_file():
            results = json.loads(results_path.read_text(encoding="utf-8"))
        return {**meta, **results}

    @router.patch("/runs/{run_id}/classifications")
    async def update_classifications(run_id: str, req: ClassificationsUpdate):
        results_path = Path(RUNS_DIR) / run_id / "results.json"
        if not results_path.is_file():
            raise HTTPException(status_code=404, detail="No results found for this run")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        updates_by_idx = {p["idx"]: p for p in req.pages}
        for page in results.get("pages", []):
            if page["idx"] in updates_by_idx:
                upd = updates_by_idx[page["idx"]]
                if "sheet_type"  in upd: page["sheet_type"]  = upd["sheet_type"]
                if "importance"  in upd: page["importance"]  = upd["importance"]
                if "description" in upd: page["description"] = upd["description"]
        results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return {"ok": True}

    @router.post("/runs/{run_id}/phase3")
    async def start_phase3_only(run_id: str, req: Phase3OnlyRequest):
        results_path = Path(RUNS_DIR) / run_id / "results.json"
        meta_path    = Path(RUNS_DIR) / run_id / "meta.json"
        if not meta_path.is_file():
            raise HTTPException(status_code=404, detail="Run not found")
        if not results_path.is_file():
            raise HTTPException(status_code=400, detail="No saved page results — run the full pipeline first")

        meta    = json.loads(meta_path.read_text(encoding="utf-8"))
        results = json.loads(results_path.read_text(encoding="utf-8"))

        from src.quick_proposal.index import ChatIndex, PageRecord, BboxRecord
        index = ChatIndex(
            run_id=run_id,
            source_path=_resolve_upload_path(meta["upload_id"]),
            job_notes=meta.get("notes", ""),
            selected_jobs=[],
        )
        index.knowledge_pack = _load_knowledge_pack()
        index.case_library   = _load_case_library()

        pages_dir = Path(RUNS_DIR) / run_id / "pages"

        for bid, bdata in results.get("bboxes", {}).items():
            index.bboxes[bid] = BboxRecord(
                id=bdata["id"],
                page_idx=bdata["page_idx"],
                x1=bdata["x1"],
                y1=bdata["y1"],
                x2=bdata["x2"],
                y2=bdata["y2"],
                parent_id=bdata.get("parent_id"),
                depth=bdata.get("depth", 0),
                description=bdata.get("description", ""),
                element_type=bdata.get("element_type"),
                element_subtype=bdata.get("element_subtype"),
                importance=bdata.get("importance"),
            )

        index.pages = [
            PageRecord(
                idx=p["idx"],
                image_path=str(pages_dir / f"page_{p['idx']:04d}.jpg"),
                classification=p.get("sheet_type", ""),
                importance=p.get("importance", ""),
                description=p.get("description", ""),
                bbox_ids=p.get("bbox_ids", []),
            )
            for p in results.get("pages", [])
        ]

        queue: asyncio.Queue = asyncio.Queue()
        _active_runs[run_id] = queue
        _save_run_meta(run_id, status="running")

        task = asyncio.create_task(_run_phase3_only(index, queue, run_id, manager_model=req.manager_model, gemini_model=req.gemini_model))
        _active_tasks[run_id] = task
        return {"run_id": run_id}

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        task = _active_tasks.pop(run_id, None)
        if task and not task.done():
            task.cancel()
        queue = _active_runs.get(run_id)
        if queue:
            await queue.put(None)
        _save_run_meta(run_id, status="cancelled")
        return {"ok": True}

    @router.post("/runs/{run_id}/reclassify/{page_idx}")
    async def reclassify_page(run_id: str, page_idx: int, req: ReclassifyPageRequest = None):
        if req is None:
            req = ReclassifyPageRequest()
        img_path = Path(RUNS_DIR) / run_id / "pages" / f"page_{page_idx:04d}.jpg"
        if not img_path.is_file():
            raise HTTPException(status_code=404, detail=f"Page {page_idx} image not found for run {run_id}")

        if req.gemini_model:
            found = _get_endpoint_for_model(req.gemini_model)
            if found:
                url, headers, api_key, model = found
            else:
                url, headers, api_key, model = _get_gemini_endpoint()
                model = req.gemini_model
        else:
            url, headers, api_key, model = _get_gemini_endpoint()

        if not api_key:
            raise HTTPException(status_code=400, detail="No Gemini API key found")

        prompt = (_PROMPTS_DIR / "gemini_phase1.txt").read_text(encoding="utf-8")
        try:
            result = await _classify_one_page(str(img_path), prompt, url, headers, model, None)
        except Exception as e:
            logger.error(f"[quick_proposal] reclassify run={run_id} page={page_idx}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Classification failed: {e}")

        # Build region output and update results.json
        regions_out = []
        new_bbox_ids = []
        for region in result.get("regions", []):
            bbox_id = f"{page_idx}_{region['id']}"
            bbox = region.get("bbox", [0, 0, 100, 100])
            if len(bbox) < 4:
                bbox = bbox + [0] * (4 - len(bbox))
            regions_out.append({
                "id": bbox_id,
                "label":           region.get("label", ""),
                "bbox":            bbox,
                "extraction_hint": region.get("extraction_hint", ""),
                "importance":      region.get("importance", "medium"),
            })
            new_bbox_ids.append(bbox_id)

        results_path = Path(RUNS_DIR) / run_id / "results.json"
        if results_path.is_file():
            results = json.loads(results_path.read_text(encoding="utf-8"))
            # Remove old bboxes for this page
            for p in results.get("pages", []):
                if p["idx"] == page_idx:
                    for old_bid in p.get("bbox_ids", []):
                        results.get("bboxes", {}).pop(old_bid, None)
                    p["sheet_type"]  = result.get("sheet_type", "other")
                    p["importance"]  = result.get("importance", "low")
                    p["description"] = result.get("description", "")
                    p["bbox_ids"]    = new_bbox_ids
                    break
            bboxes = results.setdefault("bboxes", {})
            for r, bid in zip(result.get("regions", []), new_bbox_ids):
                bbox = r.get("bbox", [0, 0, 100, 100])
                if len(bbox) < 4:
                    bbox = bbox + [0] * (4 - len(bbox))
                bboxes[bid] = {
                    "id": bid, "page_idx": page_idx,
                    "x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3],
                    "parent_id": None, "depth": 0,
                    "description": f"{r.get('label', '')}: {r.get('extraction_hint', '')}",
                    "element_type":    r.get("element_type"),
                    "element_subtype": r.get("element_subtype"),
                    "importance":      r.get("importance", "medium"),
                }
            results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

        return {
            "page_idx":   page_idx,
            "sheet_type": result.get("sheet_type", "other"),
            "importance": result.get("importance", "low"),
            "description": result.get("description", ""),
            "regions":    regions_out,
        }

    @router.get("/runs/{run_id}/phase3_log")
    async def get_phase3_log(run_id: str):
        log_path = Path(RUNS_DIR) / run_id / "phase3_log.jsonl"
        if not log_path.is_file():
            return []
        entries = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries

    @router.get("/stream/{run_id}")
    async def stream_run(run_id: str):
        queue = _active_runs.get(run_id)
        if queue is None:
            raise HTTPException(404, f"Run {run_id} not found")

        async def sse_generator():
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ":\n\n"
                    continue
                if item is None:
                    break
                yield f"event: {item['type']}\ndata: {json.dumps(item)}\n\n"

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/pages/{run_id}/{page_idx}")
    async def get_page(run_id: str, page_idx: int):
        path = os.path.join(RUNS_DIR, run_id, "pages", f"page_{page_idx:04d}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, "Page not found")
        return FileResponse(path, media_type="image/jpeg")

    return router