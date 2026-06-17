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
    gemini_retry_attempts: int = 3
    gemini_fallback_models: List[str] = []


class ReclassifyPageRequest(BaseModel):
    gemini_model: str = ""


class ClassificationsUpdate(BaseModel):
    pages: List[dict] = []


class RunRequest(BaseModel):
    upload_id: str
    job_type: str = ""
    run_name: str = ""
    notes: str = ""
    selected_jobs: List[str] = []
    gemini_model: str = ""
    manager_model: str = ""
    filename: str = ""
    gemini_retry_attempts: int = 3
    gemini_fallback_models: List[str] = []
    holdout_kp_path: str = ""
    import_from_run_id: str = ""


class ValidateRequest(BaseModel):
    gemini_model: str = ""
    manager_model: str = ""


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _emit(queue: asyncio.Queue, event_type: str, **kwargs):
    await queue.put({"type": event_type, **kwargs})


def _to_openai_compat_url(url: str) -> str:
    """Anthropic's native endpoint is /v1/messages but the manager uses OpenAI-compat
    format (/v1/chat/completions). Swap the suffix when needed."""
    if url and url.endswith("/v1/messages"):
        return url[: -len("/v1/messages")] + "/v1/chat/completions"
    return url


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


async def _run_phase3_only(index, queue: asyncio.Queue, run_id: str, manager_model: str = "", gemini_model: str = "", retry_attempts: int = 3, gemini_fallback_models: list | None = None) -> None:
    """Re-run phase2 index build + phase3 extraction loop using saved page classifications."""
    try:
        await _emit(queue, "phase_start", phase="phase2", label="Building extraction index…")
        index.extracted_data["phase1_summary"] = _build_phase1_summary(index)
        await _emit(queue, "phase_complete", phase="phase2")

        await phase3_claude_gemini_loop(index, queue, manager_model=manager_model, gemini_model=gemini_model, retry_attempts=retry_attempts, gemini_fallback_models=gemini_fallback_models)
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
        "ROW_width_ft":       scale.get("ROW_width_ft"),
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


def _load_knowledge_pack(path: str | None = None) -> dict:
    target = Path(path) if path else _KP_PATH
    return json.loads(target.read_text(encoding="utf-8"))


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


async def phase1_classify_pages(index, queue: asyncio.Queue, model_override: str = "", retry_attempts: int = 3, fallback_models: list | None = None) -> None:
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

    # Fallbacks use None for cache_name — the cache is tied to the primary endpoint's API key.
    fallback_configs: list = []
    for fb_model in (fallback_models or []):
        found = _get_endpoint_for_model(fb_model)
        if found:
            fallback_configs.append((found[0], found[1], fb_model, None))
        else:
            fallback_configs.append((url, headers, fb_model, None))

    all_configs = [(url, headers, model, cache_name)] + fallback_configs

    await _emit(queue, "phase_start", phase="phase1",
                label="Classifying pages…",
                cached=cache_name is not None)
    auto_redone: set[int] = set()

    try:
        for page in index.pages:
            result = None
            last_err = None

            for cfg_idx, (cfg_url, cfg_headers, cfg_model, cfg_cache) in enumerate(all_configs):
                n_tries = retry_attempts if cfg_idx == 0 else 1
                delay = 2.0

                for attempt in range(n_tries):
                    if attempt > 0:
                        logger.warning(f"[quick_proposal] phase1 page={page.idx} retrying {cfg_model} attempt={attempt + 1}/{n_tries}: {last_err}")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
                    elif cfg_idx > 0:
                        logger.warning(f"[quick_proposal] phase1 page={page.idx} trying fallback model {cfg_model}")

                    try:
                        result = await _classify_one_page(page.image_path, prompt, cfg_url, cfg_headers, cfg_model, cfg_cache)
                        last_err = None
                        break
                    except httpx.HTTPStatusError as e:
                        last_err = e
                        status = e.response.status_code
                        if status not in _RETRYABLE_STATUS and status != 400:
                            break  # non-retryable HTTP error
                        logger.warning(f"[quick_proposal] phase1 page={page.idx} HTTP {status}: {e.response.text[:200]}")
                    except (json.JSONDecodeError, httpx.TimeoutException) as e:
                        last_err = e
                        logger.warning(f"[quick_proposal] phase1 page={page.idx} {type(e).__name__}: {e}")
                    except Exception as e:
                        last_err = e
                        break  # unknown error — don't retry

                if last_err is None:
                    break  # success, stop trying fallback configs

            if last_err is not None:
                logger.warning(f"[quick_proposal] phase1 page={page.idx} failed after all retries+fallbacks: {last_err}", exc_info=True)
                await _emit(queue, "page_classified",
                            page_idx=page.idx, sheet_type="other",
                            importance="low", description="(classification failed)",
                            regions=[], error=str(last_err))
                continue

            # Auto-redo once if Gemini returned out-of-range (1000-based) coords
            if page.idx not in auto_redone:
                raw_bboxes = [r.get("bbox", []) for r in result.get("regions", [])]
                if any(len(b) >= 4 and max(b) > 100 for b in raw_bboxes):
                    auto_redone.add(page.idx)
                    logger.info(f"[quick_proposal] phase1 page={page.idx} auto-redo: out-of-range bbox coords")
                    try:
                        result = await _classify_one_page(page.image_path, prompt, url, headers, model, cache_name)
                    except Exception as e:
                        logger.warning(f"[quick_proposal] phase1 page={page.idx} auto-redo failed: {e}")

            page.classification = result.get("sheet_type", "other")
            page.importance     = result.get("importance", "low")
            page.description    = result.get("description", "")

            regions_out = []
            for region in result.get("regions", []):
                bbox_id = f"{page.idx}_{region['id']}"
                bbox    = region.get("bbox", [0, 0, 100, 100])
                if len(bbox) < 4:
                    bbox = bbox + [0] * (4 - len(bbox))
                # Gemini's native format is 0-1000; normalize when it bleeds through
                if max(bbox) > 100:
                    bbox = [v / 10.0 for v in bbox]
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
            "name": "crop_page",
            "description": "Renders a free-form crop of any page area from the source PDF at high DPI. Use when information lies outside a Phase 1 bbox or you need to pan to an adjacent area. No bbox_id needed. Coordinates are percentages (0–100): x1=left, y1=top, x2=right, y2=bottom.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_idx": {"type": "integer", "description": "0-based page index"},
                    "x1": {"type": "number", "description": "Left edge, 0–100%"},
                    "y1": {"type": "number", "description": "Top edge, 0–100%"},
                    "x2": {"type": "number", "description": "Right edge, 0–100%"},
                    "y2": {"type": "number", "description": "Bottom edge, 0–100%"},
                    "dpi": {"type": "integer", "description": "Render DPI (default 200)"},
                },
                "required": ["page_idx", "x1", "y1", "x2", "y2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enhance_subregion",
            "description": "Zooms into a sub-area of an existing Phase 1 bbox. Coordinates are percentages (0–100) relative to the parent region — no page-level math required. The backend maps child coordinates to page space and renders from the source PDF at high DPI. Use this to iteratively narrow in on a specific callout, label, or dimension within an already-enhanced image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_bbox_id": {"type": "string", "description": "bbox_id of the parent region (e.g. '4_r2')"},
                    "x1": {"type": "number", "description": "Left edge relative to parent, 0–100%"},
                    "y1": {"type": "number", "description": "Top edge relative to parent, 0–100%"},
                    "x2": {"type": "number", "description": "Right edge relative to parent, 0–100%"},
                    "y2": {"type": "number", "description": "Bottom edge relative to parent, 0–100%"},
                    "dpi": {"type": "integer", "description": "Render DPI (default 300)"},
                },
                "required": ["parent_bbox_id", "x1", "y1", "x2", "y2"],
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
    {
        "type": "function",
        "function": {
            "name": "list_images",
            "description": "List all images already available in this session — includes Phase 1 page thumbnails (p0_thumbnail, p1_thumbnail, …) and every image rendered so far via enhance_region, crop_page, or enhance_subregion. Returns a JSON array of {image_id, desc}. Call this to find an image_id before calling get_image, or to orient on what pages are already loaded.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_image",
            "description": "Retrieve a previously rendered image by image_id without re-rendering. Every tool response that returns an image includes [image_id: ...] in its text — use that ID here. Faster than re-calling crop_page or enhance_region for a view you already requested.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string", "description": "The image_id shown in a previous tool response as [image_id: ...], or from list_images()"},
                },
                "required": ["image_id"],
            },
        },
    },
]


def _render_pdf_crop(source_path: str, page_idx: int, x1: float, y1: float, x2: float, y2: float, dpi: int) -> bytes:
    """Render a clip from a PDF page using fitz. Coords are 0–100 percentages (xmin,ymin,xmax,ymax)."""
    import fitz
    doc  = fitz.open(source_path)
    page = doc[page_idx]
    pw, ph = page.rect.width, page.rect.height
    clip = fitz.Rect(x1 / 100 * pw, y1 / 100 * ph, x2 / 100 * pw, y2 / 100 * ph)
    pix  = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip)
    doc.close()
    return pix.tobytes("jpeg")


async def _execute_gemini_tool(
    name: str, args: dict, index, queue: asyncio.Queue, image_store: dict
) -> list:
    """Execute a Gemini Phase 3 tool. Returns a list of OpenAI content blocks."""
    if name == "enhance_region":
        bbox_id  = args.get("bbox_id", "")
        dpi      = int(args.get("dpi", 200))
        rec      = index.bboxes.get(bbox_id)
        if not rec:
            return [{"type": "text", "text": f"Error: bbox_id '{bbox_id}' not found in index."}]
        image_id = bbox_id
        try:
            if image_id in image_store:
                jpeg_bytes  = image_store[image_id]["bytes"]
                cached_note = " (from cache)"
            else:
                if index.source_is_pdf:
                    jpeg_bytes = await asyncio.to_thread(
                        _render_pdf_crop, index.source_path,
                        rec.page_idx, rec.x1, rec.y1, rec.x2, rec.y2, dpi,
                    )
                else:
                    from PIL import Image as PilImage
                    img  = await asyncio.to_thread(PilImage.open, index.pages[rec.page_idx].image_path)
                    w, h = img.size
                    x1   = max(0, int(rec.x1 / 100 * w))
                    y1   = max(0, int(rec.y1 / 100 * h))
                    x2   = min(w, int(rec.x2 / 100 * w))
                    y2   = min(h, int(rec.y2 / 100 * h))
                    if x2 <= x1 or y2 <= y1:
                        return [{"type": "text", "text": f"Error: invalid bbox coords ({x1},{y1},{x2},{y2})"}]
                    buf = io.BytesIO()
                    img.crop((x1, y1, x2, y2)).save(buf, "JPEG", quality=90)
                    jpeg_bytes = buf.getvalue()
                image_store[image_id] = {"bytes": jpeg_bytes, "desc": f"Region {bbox_id} at {dpi} DPI"}
                cached_note = ""
            b64 = base64.b64encode(jpeg_bytes).decode()
            return [
                {"type": "text", "text": f"Region {bbox_id} at {dpi} DPI{cached_note} [image_id: {image_id}]:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        except Exception as e:
            logger.warning(f"[quick_proposal] enhance_region error: {e}", exc_info=True)
            return [{"type": "text", "text": f"Error rendering region: {e}"}]

    elif name == "index_read":
        return [{"type": "text", "text": json.dumps(index.extracted_values) if index.extracted_values else "{}"}]

    elif name == "crop_page":
        page_idx = int(args.get("page_idx", 0))
        x1       = float(args.get("x1", 0))
        y1       = float(args.get("y1", 0))
        x2       = float(args.get("x2", 100))
        y2       = float(args.get("y2", 100))
        dpi      = int(args.get("dpi", 200))
        if page_idx >= len(index.pages):
            return [{"type": "text", "text": f"Error: page_idx {page_idx} out of range ({len(index.pages)} pages)"}]
        # Stable image_id: full-page crops get a clean name; partial crops encode coords
        if x1 == 0 and y1 == 0 and x2 == 100 and y2 == 100:
            image_id = f"p{page_idx}_full"
        else:
            image_id = f"p{page_idx}_{x1:.0f}_{y1:.0f}_{x2:.0f}_{y2:.0f}"
        try:
            if image_id in image_store:
                jpeg_bytes  = image_store[image_id]["bytes"]
                cached_note = " (from cache)"
            else:
                if index.source_is_pdf:
                    jpeg_bytes = await asyncio.to_thread(
                        _render_pdf_crop, index.source_path,
                        page_idx, x1, y1, x2, y2, dpi,
                    )
                else:
                    from PIL import Image as PilImage
                    img  = await asyncio.to_thread(PilImage.open, index.pages[page_idx].image_path)
                    w, h = img.size
                    px1  = max(0, int(x1 / 100 * w))
                    py1  = max(0, int(y1 / 100 * h))
                    px2  = min(w, int(x2 / 100 * w))
                    py2  = min(h, int(y2 / 100 * h))
                    if px2 <= px1 or py2 <= py1:
                        return [{"type": "text", "text": "Error: invalid crop coords"}]
                    buf = io.BytesIO()
                    img.crop((px1, py1, px2, py2)).save(buf, "JPEG", quality=90)
                    jpeg_bytes = buf.getvalue()
                image_store[image_id] = {
                    "bytes": jpeg_bytes,
                    "desc":  f"Page {page_idx} crop [{x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f}]% at {dpi} DPI",
                }
                cached_note = ""
            b64 = base64.b64encode(jpeg_bytes).decode()
            return [
                {"type": "text", "text": f"Page {page_idx} crop [{x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f}]% at {dpi} DPI{cached_note} [image_id: {image_id}]:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        except Exception as e:
            logger.warning(f"[quick_proposal] crop_page error: {e}", exc_info=True)
            return [{"type": "text", "text": f"Error rendering crop: {e}"}]

    elif name == "enhance_subregion":
        parent_id = args.get("parent_bbox_id", "")
        parent    = index.bboxes.get(parent_id)
        if not parent:
            return [{"type": "text", "text": f"Error: parent_bbox_id '{parent_id}' not found in index."}]
        x1_rel = float(args.get("x1", 0))
        y1_rel = float(args.get("y1", 0))
        x2_rel = float(args.get("x2", 100))
        y2_rel = float(args.get("y2", 100))
        dpi    = int(args.get("dpi", 300))
        image_id = f"{parent_id}_sub_{x1_rel:.0f}_{y1_rel:.0f}_{x2_rel:.0f}_{y2_rel:.0f}"
        # Map child coords (0-100% relative to parent) → page-level coords (0-100%)
        pw = parent.x2 - parent.x1
        ph = parent.y2 - parent.y1
        x1_page = parent.x1 + x1_rel / 100 * pw
        y1_page = parent.y1 + y1_rel / 100 * ph
        x2_page = parent.x1 + x2_rel / 100 * pw
        y2_page = parent.y1 + y2_rel / 100 * ph
        try:
            if image_id in image_store:
                jpeg_bytes  = image_store[image_id]["bytes"]
                cached_note = " (from cache)"
            else:
                if index.source_is_pdf:
                    jpeg_bytes = await asyncio.to_thread(
                        _render_pdf_crop, index.source_path,
                        parent.page_idx, x1_page, y1_page, x2_page, y2_page, dpi,
                    )
                else:
                    from PIL import Image as PilImage
                    img  = await asyncio.to_thread(PilImage.open, index.pages[parent.page_idx].image_path)
                    w, h = img.size
                    px1  = max(0, int(x1_page / 100 * w))
                    py1  = max(0, int(y1_page / 100 * h))
                    px2  = min(w, int(x2_page / 100 * w))
                    py2  = min(h, int(y2_page / 100 * h))
                    if px2 <= px1 or py2 <= py1:
                        return [{"type": "text", "text": "Error: invalid subregion coords"}]
                    buf = io.BytesIO()
                    img.crop((px1, py1, px2, py2)).save(buf, "JPEG", quality=90)
                    jpeg_bytes = buf.getvalue()
                image_store[image_id] = {
                    "bytes": jpeg_bytes,
                    "desc":  f"Sub-region of {parent_id} [{x1_rel:.0f},{y1_rel:.0f},{x2_rel:.0f},{y2_rel:.0f}]% at {dpi} DPI",
                }
                cached_note = ""
            b64 = base64.b64encode(jpeg_bytes).decode()
            return [
                {"type": "text", "text": f"Sub-region of {parent_id} [{x1_rel:.0f},{y1_rel:.0f},{x2_rel:.0f},{y2_rel:.0f}]% → page [{x1_page:.1f},{y1_page:.1f},{x2_page:.1f},{y2_page:.1f}]% at {dpi} DPI{cached_note} [image_id: {image_id}]:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        except Exception as e:
            logger.warning(f"[quick_proposal] enhance_subregion error: {e}", exc_info=True)
            return [{"type": "text", "text": f"Error rendering subregion: {e}"}]

    elif name == "list_images":
        entries = [{"image_id": k, "desc": v["desc"]} for k, v in image_store.items()]
        return [{"type": "text", "text": json.dumps(entries)}]

    elif name == "get_image":
        image_id = args.get("image_id", "")
        if image_id not in image_store:
            available = list(image_store.keys())
            return [{"type": "text", "text": f"No image found with id '{image_id}'. Available ids: {available}"}]
        entry = image_store[image_id]
        b64   = base64.b64encode(entry["bytes"]).decode()
        return [
            {"type": "text", "text": f"Retrieved: {entry['desc']} [image_id: {image_id}]:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]

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


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _gemini_call_with_retry(
    url: str,
    headers: dict,
    payload: dict,
    retry_attempts: int = 3,
    fallback_models_info: list | None = None,
    queue: asyncio.Queue | None = None,
) -> "httpx.Response":
    """POST to Gemini, retrying transient errors then falling through to fallback models."""
    base_headers = {**headers, "Content-Type": "application/json"}
    # (url, headers, model) — primary first, then fallbacks
    configs = [(url, base_headers, payload["model"])]
    for fb_url, fb_hdrs, fb_model in (fallback_models_info or []):
        configs.append((fb_url, {**fb_hdrs, "Content-Type": "application/json"}, fb_model))

    last_response: "httpx.Response | None" = None
    delay = 2.0

    for cfg_idx, (cfg_url, cfg_hdrs, cfg_model) in enumerate(configs):
        n_tries = retry_attempts if cfg_idx == 0 else 1
        attempt_payload = {**payload, "model": cfg_model}

        for attempt in range(n_tries):
            if attempt > 0:
                notice = f"Gemini error — retrying {cfg_model} (attempt {attempt + 1}/{n_tries})…"
                logger.warning(f"[quick_proposal] {notice}")
                if queue:
                    await _emit(queue, "extraction_message", role="retry_notice", text=notice)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            elif cfg_idx > 0:
                notice = f"Switching to fallback model: {cfg_model}…"
                logger.warning(f"[quick_proposal] {notice}")
                if queue:
                    await _emit(queue, "extraction_message", role="retry_notice", text=notice)

            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(cfg_url, headers=cfg_hdrs, json=attempt_payload)

            if r.is_success:
                return r

            last_response = r
            if r.status_code not in _RETRYABLE_STATUS:
                logger.error(f"[quick_proposal] Gemini non-retryable {r.status_code} model={cfg_model}: {r.text}")
                r.raise_for_status()

            logger.warning(f"[quick_proposal] Gemini {r.status_code} model={cfg_model} attempt={attempt + 1}/{n_tries}: {r.text[:200]}")

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("[quick_proposal] Gemini call failed: all attempts exhausted")


async def _run_gemini_with_tools(
    user_message: str,
    gemini_state: dict,
    index,
    queue: asyncio.Queue,
    retry_attempts: int = 3,
    fallback_models_info: list | None = None,
) -> str:
    """Append user_message to Gemini history, call Gemini, handle tool loops, return final text."""
    image_store = gemini_state.setdefault("image_store", {})
    gemini_state["messages"].append({"role": "user", "content": user_message})

    for _ in range(200):
        payload: dict = {
            "model":    gemini_state["model"],
            "messages": gemini_state["messages"],
            "tools":    _GEMINI_PHASE3_TOOLS,
            "max_tokens": 8192,
        }
        # cached_content is a native Gemini API field; the OpenAI-compat endpoint rejects it
        if gemini_state.get("cache_name") and "/openai/" not in gemini_state["url"]:
            payload["cached_content"] = gemini_state["cache_name"]

        r = await _gemini_call_with_retry(
            gemini_state["url"], gemini_state["headers"], payload,
            retry_attempts=retry_attempts,
            fallback_models_info=fallback_models_info,
            queue=queue,
        )

        resp_data  = r.json()
        g_usage = resp_data.get("usage", {})
        if g_usage:
            await _emit(queue, "context_usage",
                        role="gemini",
                        model=gemini_state.get("model", ""),
                        input_tokens=g_usage.get("prompt_tokens", 0),
                        output_tokens=g_usage.get("completion_tokens", 0),
                        context_window=1048576)
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

        if text_out and text_out.strip():
            await _emit(queue, "extraction_message", role="gemini", text=text_out)
            if log_path := gemini_state.get("log_path"):
                _log_phase3_event(log_path, {"type": "extraction_message", "role": "gemini", "text": text_out})

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
                        tool_id=tc.get("id", ""),
                        tool=tool_name,
                        model=gemini_state.get("model", ""),
                        args=json.dumps(tc_args))
            if log_path := gemini_state.get("log_path"):
                _log_phase3_event(log_path, {"type": "extraction_message", "role": "tool_call", "tool_id": tc.get("id", ""), "tool": tool_name, "args": json.dumps(tc_args)})

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
            result_blocks = await _execute_gemini_tool(tool_name, tc_args, index, queue, image_store)
            # Emit tool result so the UI can update the running node to done.
            result_text = "\n".join(b["text"] for b in result_blocks if b.get("type") == "text") or "(no output)"
            result_image = next((b["image_url"]["url"] for b in result_blocks if b.get("type") == "image_url"), None)
            await _emit(queue, "extraction_message",
                        role="tool_result",
                        tool_id=tc_id,
                        tool=tool_name,
                        model=gemini_state.get("model", ""),
                        result=result_text,
                        **({"image_url": result_image} if result_image else {}))
            if log_path := gemini_state.get("log_path"):
                _log_phase3_event(log_path, {"type": "extraction_message", "role": "tool_result", "tool_id": tc_id, "tool": tool_name, "result": result_text})
            # Gemini OpenAI-compat rejects image_url in tool messages; keep only text
            # here and carry images forward as a user message instead.
            text_parts = [b["text"] for b in result_blocks if b.get("type") == "text"]
            if tool_name == "crop_page":
                _pi = tc_args.get("page_idx", "?")
                _cx1, _cy1, _cx2, _cy2 = (tc_args.get(k, d) for k, d in [("x1",0),("y1",0),("x2",100),("y2",100)])
                bbox_id = f"p{_pi}_full" if (_cx1==0 and _cy1==0 and _cx2==100 and _cy2==100) else f"p{_pi}_{_cx1:.0f}_{_cy1:.0f}_{_cx2:.0f}_{_cy2:.0f}"
            elif tool_name == "enhance_subregion":
                _par = tc_args.get("parent_bbox_id", "?")
                _sx1, _sy1, _sx2, _sy2 = (tc_args.get(k, d) for k, d in [("x1",0),("y1",0),("x2",100),("y2",100)])
                bbox_id = f"{_par}_sub_{_sx1:.0f}_{_sy1:.0f}_{_sx2:.0f}_{_sy2:.0f}"
            elif tool_name == "get_image":
                bbox_id = tc_args.get("image_id", "retrieved")
            else:
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


def _kp_lookup(knowledge_pack: dict, query: str) -> str:
    """Fuzzy lookup for unit price distributions, price trends, item pair detail, or named KP sections."""
    import difflib

    distributions = knowledge_pack.get("unit_price_distributions", {})
    price_trends  = knowledge_pack.get("price_trends", {})
    pairs         = knowledge_pack.get("item_pairs", {}).get("pairs", [])

    q = query.strip()

    # Named section lookup — "SECTION: <section_name>"
    if q.lower().startswith("section:"):
        section_name = q.split(":", 1)[1].strip()
        section_data = knowledge_pack.get(section_name)
        if section_data is None:
            available = sorted(k for k in knowledge_pack if k not in {"unit_price_distributions", "price_trends"})
            return f"No section '{section_name}' found. Available sections: {', '.join(available)}"
        return json.dumps(section_data)

    # item_pairs detail lookup — prefix "item_pairs: <item name>"
    if q.lower().startswith("item_pair"):
        parts = q.split(":", 1)
        item_name = parts[1].strip() if len(parts) > 1 else ""
        if not item_name:
            return json.dumps(pairs[:5])
        item_up = item_name.upper()
        matching = [
            p for p in pairs
            if item_up in p.get("item_a", "").upper() or item_up in p.get("item_b", "").upper()
        ]
        if not matching:
            return f"No pairs found involving '{item_name}'."
        return json.dumps(matching)

    if q.upper() == "LIST":
        return "Available items in the `unit_price_distributions` section:\n" + "\n".join(sorted(distributions.keys()))
    
    if q.upper() == "PREVALENCE_FILTER":
        items = knowledge_pack.get("item_prevalence", {})
        scored = sorted(
            ((p.get("prevalence", 0), item) for item, p in items.items() if item != "_thresholds"),
            reverse=True,
        )
        lines = [f"{score:.0%}  {item}" for score, item in scored]
        return (
            "All KP items with portfolio prevalence scores (highest first).\n"
            "Group semantically similar items (e.g. all paving variants → PAVING) and filter "
            "aggregated groups >= 0.70 to build your Gemini detection list.\n\n"
            + "\n".join(lines)
        )

    def _build_result(key: str) -> dict:
        entry = {"matched_item": key, "unit_price_distribution": distributions[key]}
        if key in price_trends:
            entry["price_trend"] = price_trends[key]
        return entry

    # Exact match
    if q in distributions:
        return json.dumps(_build_result(q))

    # Case-insensitive exact
    q_up = q.upper()
    for key in distributions:
        if key.upper() == q_up:
            return json.dumps(_build_result(key))

    # Fuzzy
    all_keys = list(distributions.keys())
    matches = difflib.get_close_matches(q, all_keys, n=3, cutoff=0.4)

    # Substring fallback
    if not matches:
        q_low = q.lower()
        matches = [k for k in all_keys if q_low in k.lower() or k.lower() in q_low][:3]

    if not matches:
        return f"No item found matching '{q}'. Call kp_lookup with item='LIST' to see all available items."

    if len(matches) == 1:
        return json.dumps(_build_result(matches[0]))

    return json.dumps({"query": q, "matches": {k: _build_result(k) for k in matches}})


def _call_manager_sync(url: str, headers: dict, payload: dict) -> dict:
    """Synchronous OpenAI-compatible completions call (for asyncio.to_thread)."""
    with httpx.Client(timeout=300.0) as client:
        r = client.post(url, headers={**headers, "content-type": "application/json"}, json=payload)
        r.raise_for_status()
        return r.json()


async def phase3_claude_gemini_loop(index, queue: asyncio.Queue, manager_model: str = "", gemini_model: str = "", retry_attempts: int = 3, gemini_fallback_models: list | None = None, holdout_kp_path: str = "") -> None:
    """Phase 3: Manager LLM orchestrates Gemini extraction via send_to_gemini tool."""
    # Resolve manager endpoint — prefer the explicitly selected model, fall back to Anthropic endpoint.
    mgr_url = mgr_headers = mgr_api_key = mgr_model_id = None

    if manager_model:
        found = _get_endpoint_for_model(manager_model)
        if found:
            mgr_url, mgr_headers, mgr_api_key, mgr_model_id = found
            mgr_url = _to_openai_compat_url(mgr_url)

    if not mgr_url:
        try:
            db = SessionLocal()
            try:
                ep = db.query(ModelEndpoint).filter(
                    ModelEndpoint.base_url.ilike("%anthropic.com%")
                ).first()
                if ep:
                    base, mgr_api_key = resolve_endpoint_runtime(ep)
                    mgr_url      = build_chat_url(base)  # keep /v1/messages for Anthropic
                    mgr_headers  = build_headers(mgr_api_key, base)
                    mgr_model_id = getattr(ep, "model", None) or _CLAUDE_MODEL
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[quick_proposal] could not load Claude endpoint from DB: {e}")

        if not mgr_url:
            mgr_api_key  = os.environ.get("ANTHROPIC_API_KEY", "")
            mgr_url      = "https://api.anthropic.com/v1/messages"
            mgr_headers  = {}
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

    # Resolve fallback model endpoints once — reused by every _run_gemini_with_tools call.
    fallback_models_info: list = []
    for fb_model in (gemini_fallback_models or []):
        found = _get_endpoint_for_model(fb_model)
        if found:
            fallback_models_info.append((found[0], found[1], fb_model))
        else:
            fallback_models_info.append((gemini_url, gemini_headers, fb_model))

    # Cache gemini_phase3.txt system prompt for reuse across all Gemini turns.
    # cached_content is only supported on the native Gemini API, not the OpenAI-compat endpoint.
    gemini_phase3_prompt = (_PROMPTS_DIR / "gemini_phase3.txt").read_text(encoding="utf-8")
    gemini_cache_name    = (
        await _gemini_cache_create(gemini_phase3_prompt, gemini_api_key)
        if "/openai/" not in gemini_url
        else None
    )

    # Pre-populate image store with Phase 1 page thumbnails so Gemini can access
    # them via list_images()/get_image() without re-rendering.
    image_store: dict = {}
    for page in index.pages:
        try:
            image_store[f"p{page.idx}_thumbnail"] = {
                "bytes": Path(page.image_path).read_bytes(),
                "desc":  f"Page {page.idx} thumbnail (Phase 1, 224 DPI)",
            }
        except Exception:
            pass

    gemini_state: dict = {
        "messages":    [],
        "url":         gemini_url,
        "headers":     gemini_headers,
        "model":       gemini_model,
        "cache_name":  gemini_cache_name,
        "log_path":    str(Path(RUNS_DIR) / index.run_id / "phase3_log.jsonl"),
        "image_store": image_store,
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

    manager_system    = (_PROMPTS_DIR / "manager_system.txt").read_text(encoding="utf-8")
    system_prompt_txt = (_PROMPTS_DIR / "system_prompt.txt").read_text(encoding="utf-8")
    combined_system   = manager_system + "\n\n---\n\n" + system_prompt_txt

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

    read_index_tool = {
        "type": "function",
        "function": {
            "name":        "read_index",
            "description": (
                "Returns the current state of all extracted values in the shared index as JSON. "
                "Call this before asking Gemini to re-read something — the value may already be extracted."
            ),
            "parameters": {
                "type":       "object",
                "properties": {},
                "required":   [],
            },
        },
    }

    end_generation_tool = {
        "type": "function",
        "function": {
            "name":        "end_generation",
            "description": (
                "Call this tool once the Phase B proposal is fully written. "
                "Pass the Grand Total dollar amount as a number. "
                "This is the required final step — do not call it before the complete "
                "line-item table, Grand Total, Confidence Band, and Sanity Check are written."
            ),
            "parameters": {
                "type":       "object",
                "properties": {
                    "grand_total": {
                        "type":        "number",
                        "description": "The final Grand Total from the Phase B budget estimate as a dollar amount (e.g. 1234567.89).",
                    },
                },
                "required": ["grand_total"],
            },
        },
    }

    kp_lookup_tool = {
        "type": "function",
        "function": {
            "name":        "kp_lookup",
            "description": (
                "Look up knowledge pack data on demand. Four query forms: "
                "(1) item name e.g. '8\" SEWER MAIN' — returns unit price distribution + price trend; "
                "(2) 'item_pairs: <item name>' e.g. 'item_pairs: ROLLED CURB' — returns full pair metadata "
                "(r, n_shared_jobs, median_ratio, shared_jobs) for all pairs involving that item; "
                "(3) 'LIST' — lists all available unit price distribution item names; "
                "(4) 'SECTION: <name>' — returns a full named KP section not injected into opening context. "
                "Phase B sections to fetch before pricing: 'SECTION: qty_scale_correlations', "
                "'SECTION: item_scaling', 'SECTION: ls_item_variance', "
                "'SECTION: ls_earthwork_rates', 'SECTION: paving_rates'. "
                "Use (1) and (3) during Phase A for dynamic field scanning, and (1) during Phase B for every line item you price."
            ),
            "parameters": {
                "type":       "object",
                "properties": {
                    "item": {
                        "type":        "string",
                        "description": "Line item name to look up, e.g. '8\" SEWER MAIN'. Pass 'LIST' to list all items.",
                    },
                },
                "required": ["item"],
            },
        },
    }

    # Build a context-optimised KP: strip heavy/Phase-B-only sections (available via
    # kp_lookup SECTION queries), and compact item_pairs for Phase A co-occurrence use.
    _HEAVY_KP_SECTIONS = {
        "unit_price_distributions", "price_trends",   # item-name queries
        "item_prevalence",                             # 67k — not referenced in any prompt
        "derivation_rules",                            # 12k — duplicated as prose in system_prompt.txt
        "qty_scale_correlations",                      # 14k — Phase B only
        "item_scaling",                                # 5k  — Phase B only
        "ls_item_variance",                            # 2k  — Phase B only
        "ls_earthwork_rates",                          # 2k  — Phase B only
        "paving_rates",                                # 3k  — Phase B only
    }
    kp_for_context = {k: v for k, v in (index.knowledge_pack or {}).items() if k not in _HEAVY_KP_SECTIONS}
    if "item_pairs" in kp_for_context:
        raw_pairs = kp_for_context["item_pairs"].get("pairs", [])
        kp_for_context["item_pairs"] = {
            "pairs": [
                {"item_a": p["item_a"], "item_b": p["item_b"], "co_occurrence_rate": p["co_occurrence_rate"]}
                for p in raw_pairs
            ],
            "_note": "Compact form. Call kp_lookup('item_pairs: <item name>') for full pair metadata.",
        }
    kp_json = json.dumps(kp_for_context) if kp_for_context else "{}"

    # Anthropic native format: system is top-level; messages are content-block arrays.
    mgr_system = combined_system
    mgr_messages = [
        {"role": "user",      "content": (
            "Here is the knowledge pack containing unit price distributions, "
            "derivation rules, and analog job data. Use this for all pricing and "
            "sanity checks when generating Phase B estimates.\n\n"
            "```json\n" + kp_json + "\n```"
        )},
        {"role": "assistant", "content": "Understood. I have reviewed the knowledge pack and will use its unit price distributions, derivation rules, and analog jobs for all Phase B estimation."},
        {"role": "user",      "content": "Here is the Phase 1 index from the plan set. Begin extraction.\n\n" + phase1_summary},
    ]

    for i, m in enumerate(mgr_messages):
        logger.info(f"[quick_proposal] init mgr_messages[{i}] role={m['role']} chars={len(str(m.get('content') or ''))}")

    all_tools = [send_to_gemini_tool, read_index_tool, kp_lookup_tool, end_generation_tool]

    await _emit(queue, "phase_start", phase="phase3", label="Extracting values from plans…")

    phase_b_complete = False
    phase_b_nudges   = 0

    try:
        for _ in range(60):
            oai_messages = [{"role": "system", "content": mgr_system}] + mgr_messages
            payload = {
                "model":      mgr_model_id,
                "max_tokens": 32000,
                "messages":   oai_messages,
                "tools":      all_tools,
            }
            resp = await asyncio.to_thread(_call_manager_sync, mgr_url, mgr_headers or {}, payload)

            log_path = gemini_state.get("log_path", "")

            if "error" in resp:
                err_msg = resp.get("error", {}).get("message", str(resp))
                logger.error(f"[quick_proposal] manager error in phase3: {err_msg}")
                await _emit(queue, "error", message=f"Manager error: {err_msg}", phase="phase3")
                return

            # ── Parse response ─────────────────────────────────────────────────
            usage         = resp.get("usage", {})
            choice        = resp.get("choices", [{}])[0]
            finish_reason = choice.get("finish_reason")
            msg           = choice.get("message", {})
            text_content  = msg.get("content") or ""
            tool_calls    = msg.get("tool_calls") or []

            if usage:
                await _emit(queue, "context_usage",
                            role="claude", model=mgr_model_id,
                            input_tokens=usage.get("prompt_tokens", 0),
                            output_tokens=usage.get("completion_tokens", 0),
                            context_window=200000)

            if text_content.strip():
                await _emit(queue, "extraction_message", role="claude", text=text_content, model=mgr_model_id)
                if log_path:
                    _log_phase3_event(log_path, {"type": "extraction_message", "role": "claude",
                                                  "text": text_content, "model": mgr_model_id})

            assistant_msg: dict = {"role": "assistant", "content": text_content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            mgr_messages.append(assistant_msg)

            logger.info(f"[quick_proposal] manager finish_reason={finish_reason} output_tokens={usage.get('completion_tokens', '?')}")

            if finish_reason == "length":
                logger.warning("[quick_proposal] manager output truncated (finish_reason=length) — sending continuation")
                await _emit(queue, "extraction_message", role="claude",
                            text="*(output truncated — continuing…)*", model=mgr_model_id)
                mgr_messages.append({"role": "user", "content": "Continue exactly where you left off. Do not repeat anything already written."})
                continue

            if not tool_calls:
                # Text-only stop with no tool calls. Nudge the model to generate
                # Phase B (if not done) and call end_generation to close the loop.
                if phase_b_nudges < 2:
                    phase_b_nudges += 1
                    mgr_messages.append({"role": "user", "content": (
                        "Generate the complete Phase B proposal if not already done, "
                        "then call end_generation(grand_total) with the final Grand Total "
                        "to complete the pipeline."
                    )})
                    continue
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
                        _log_phase3_event(log_path, {"type": "extraction_message", "role": "claude_to_gemini",
                                                      "text": msg_text, "model": mgr_model_id})
                    gemini_resp = await _run_gemini_with_tools(
                        msg_text, gemini_state, index, queue,
                        retry_attempts=retry_attempts,
                        fallback_models_info=fallback_models_info or None,
                    )
                    if gemini_resp.strip():
                        await _emit(queue, "extraction_message", role="gemini", text=gemini_resp)
                        if log_path:
                            _log_phase3_event(log_path, {"type": "extraction_message", "role": "gemini", "text": gemini_resp})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id,
                                         "content": gemini_resp or "(extraction complete)"})
                elif tool_name == "read_index":
                    index_json = json.dumps(index.extracted_values, indent=2)
                    await _emit(queue, "extraction_message",
                                role="tool_call", tool_id=tool_id, tool="read_index", model=mgr_model_id, args="{}")
                    await _emit(queue, "extraction_message",
                                role="tool_result", tool_id=tool_id, tool="read_index", model=mgr_model_id, result=index_json)
                    if log_path:
                        _log_phase3_event(log_path, {"type": "extraction_message", "role": "tool_result",
                                                      "tool_id": tool_id, "tool": "read_index", "result": index_json})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": index_json})
                elif tool_name == "kp_lookup":
                    item_query    = tool_input.get("item", "").strip()
                    lookup_result = _kp_lookup(index.knowledge_pack or {}, item_query)
                    await _emit(queue, "extraction_message",
                                role="tool_call", tool_id=tool_id, tool="kp_lookup",
                                model=mgr_model_id, args=json.dumps({"item": item_query}))
                    await _emit(queue, "extraction_message",
                                role="tool_result", tool_id=tool_id, tool="kp_lookup",
                                model=mgr_model_id, result=lookup_result)
                    if log_path:
                        _log_phase3_event(log_path, {"type": "extraction_message", "role": "tool_result",
                                                      "tool_id": tool_id, "tool": "kp_lookup", "result": lookup_result})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": lookup_result})
                elif tool_name == "end_generation":
                    grand_total = tool_input.get("grand_total")
                    await _emit(queue, "grand_total", amount=grand_total, model=mgr_model_id)
                    if log_path:
                        _log_phase3_event(log_path, {"type": "grand_total", "amount": grand_total})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id,
                                         "content": f"Pipeline complete. Grand Total recorded: ${grand_total:,.2f}"})
                    phase_b_complete = True
                    break
                else:
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id,
                                         "content": f"Unknown tool: {tool_name}"})

            if phase_b_complete:
                break

    finally:
        if gemini_cache_name:
            await _gemini_cache_delete(gemini_cache_name, gemini_api_key)

    await _emit(queue, "phase_complete", phase="phase3")


# ── PDF / image rendering ──────────────────────────────────────────────────────

async def render_pdf_pages(pdf_path: str, run_id: str, queue: asyncio.Queue) -> list:
    """Render all PDF pages to JPEG at 224 DPI using pdf2image. Emits page_ready per page."""
    from pdf2image import convert_from_path, pdfinfo_from_path
    from src.quick_proposal.index import PageRecord

    pages_dir = os.path.join(RUNS_DIR, run_id, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    # Get page count without loading all pages into memory — large plan sets
    # (80+ MB PDFs) will OOM the process if convert_from_path loads everything at once.
    info = await asyncio.to_thread(pdfinfo_from_path, pdf_path)
    n_pages = info["Pages"]
    logger.info(f"[quick_proposal] run={run_id} pages={n_pages} dpi=224")

    pages = []
    for i in range(n_pages):
        [img] = await asyncio.to_thread(
            convert_from_path, pdf_path, dpi=224,
            first_page=i + 1, last_page=i + 1,
        )
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
    gemini_retry_attempts: int = 3,
    gemini_fallback_models: list | None = None,
    holdout_kp_path: str = "",
    import_from_run_id: str = "",
) -> None:
    _success = False
    try:
        # Step 1 — render pages
        await _emit(queue, "phase_start", phase="load", label="Rendering pages…")
        source_path = _resolve_upload_path(upload_id)

        from src.quick_proposal.index import ChatIndex, PageRecord, BboxRecord
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
            kp_path_used = holdout_kp_path or str(_KP_PATH)
            index.knowledge_pack = _load_knowledge_pack(holdout_kp_path or None)
            logger.info(f"[quick_proposal] knowledge pack loaded: {kp_path_used} ({len(index.knowledge_pack)} top-level keys)")
            full_library         = _load_case_library()
            # Filter to user-selected jobs if any were specified at run start.
            if index.selected_jobs:
                index.case_library = [c for c in full_library if c.get("job_name") in index.selected_jobs]
            else:
                index.case_library = full_library
        except Exception as e:
            logger.error(f"[quick_proposal] failed to load knowledge base from {holdout_kp_path or _KP_PATH}: {e}")
            raise RuntimeError(f"Failed to load knowledge base: {e}")

        await _emit(queue, "index_loaded",
                    jobs=[{"id": c["job_name"], "name": c["job_name"]}
                          for c in index.case_library],
                    kp_path=kp_path_used)
        await _emit(queue, "phase_complete", phase="index")

        # Step 3 — Phase 1 per-page Gemini classification (or import from a previous run)
        if import_from_run_id:
            src_results_path = Path(RUNS_DIR) / import_from_run_id / "results.json"
            if not src_results_path.is_file():
                raise RuntimeError(f"Import run {import_from_run_id} has no saved classifications")
            src = json.loads(src_results_path.read_text(encoding="utf-8"))
            pages_dir = Path(RUNS_DIR) / run_id / "pages"
            bbox_map: dict[str, str] = {}  # old bbox_id → new bbox_id

            for bdata in src.get("bboxes", {}).values():
                old_id  = bdata["id"]
                new_id  = f"{bdata['page_idx']}_r{old_id.split('_r')[-1]}" if "_r" in old_id else old_id
                bbox_map[old_id] = new_id
                index.bboxes[new_id] = BboxRecord(
                    id=new_id,
                    page_idx=bdata["page_idx"],
                    x1=bdata["x1"], y1=bdata["y1"], x2=bdata["x2"], y2=bdata["y2"],
                    parent_id=bdata.get("parent_id"),
                    depth=bdata.get("depth", 0),
                    description=bdata.get("description", ""),
                    element_type=bdata.get("element_type"),
                    element_subtype=bdata.get("element_subtype"),
                    importance=bdata.get("importance"),
                )

            for pdata in src.get("pages", []):
                new_bbox_ids = [bbox_map.get(b, b) for b in pdata.get("bbox_ids", [])]
                matching = next((p for p in index.pages if p.idx == pdata["idx"]), None)
                if matching:
                    matching.classification = pdata.get("sheet_type", "")
                    matching.importance     = pdata.get("importance", "")
                    matching.description    = pdata.get("description", "")
                    matching.bbox_ids       = new_bbox_ids
                    regions_out = [
                        {"id": bid, "label": index.bboxes[bid].description,
                         "bbox": [index.bboxes[bid].x1, index.bboxes[bid].y1,
                                  index.bboxes[bid].x2, index.bboxes[bid].y2],
                         "extraction_hint": "", "importance": index.bboxes[bid].importance or "medium"}
                        for bid in new_bbox_ids if bid in index.bboxes
                    ]
                    await _emit(queue, "page_classified",
                                page_idx=matching.idx,
                                sheet_type=matching.classification,
                                importance=matching.importance,
                                description=matching.description,
                                regions=regions_out,
                                imported=True)

            await _emit(queue, "phase_start", phase="phase1", label="Importing classifications…", cached=False)
            await _emit(queue, "phase_complete", phase="phase1")
            logger.info(f"[quick_proposal] imported classifications from run {import_from_run_id}")
        else:
            await phase1_classify_pages(index, queue, model_override=gemini_model, retry_attempts=gemini_retry_attempts, fallback_models=gemini_fallback_models)
        _save_run_results(run_id, index)  # persist phase1 classifications + bboxes

        # Step 4 — Phase 2: build text index from Phase 1 results (pure Python, no LLM)
        await _emit(queue, "phase_start", phase="phase2", label="Building extraction index…")
        index.extracted_data["phase1_summary"] = _build_phase1_summary(index)
        await _emit(queue, "phase_complete", phase="phase2")

        # Step 5 — Phase 3: manager LLM + Gemini extraction tool loop
        await phase3_claude_gemini_loop(index, queue, manager_model=manager_model, gemini_model=gemini_model, retry_attempts=gemini_retry_attempts, gemini_fallback_models=gemini_fallback_models, holdout_kp_path=holdout_kp_path)
        _success = True

    except BaseException as e:
        is_cancel = isinstance(e, asyncio.CancelledError)
        logger.error(f"[quick_proposal] pipeline {'cancelled' if is_cancel else 'error'} run={run_id}: {e}", exc_info=not is_cancel)
        try:
            await _emit(queue, "error", message="Run was cancelled." if is_cancel else str(e), phase="unknown")
        except Exception:
            pass
        _save_run_meta(run_id, status="cancelled" if is_cancel else "error")
        if is_cancel:
            raise
    finally:
        if _success:
            _save_run_meta(run_id, status="complete")
            _save_run_results(run_id, index)
        queue.put_nowait(None)


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
            run_name=req.run_name,
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
            gemini_retry_attempts=req.gemini_retry_attempts,
            gemini_fallback_models=req.gemini_fallback_models or None,
            holdout_kp_path=req.holdout_kp_path,
            import_from_run_id=req.import_from_run_id,
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

    @router.get("/runs/{run_id}/status")
    async def get_run_status(run_id: str):
        meta_path = Path(RUNS_DIR) / run_id / "meta.json"
        if not meta_path.is_file():
            raise HTTPException(status_code=404, detail="Run not found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {
            "run_id":    run_id,
            "status":    meta.get("status", "unknown"),
            "filename":  meta.get("filename", ""),
            "timestamp": meta.get("timestamp", 0),
        }

    @router.get("/runs/{run_id}/classifications")
    async def get_classifications(run_id: str):
        """Return the page classifications + bboxes from a completed run (for import into a new run)."""
        results_path = Path(RUNS_DIR) / run_id / "results.json"
        meta_path    = Path(RUNS_DIR) / run_id / "meta.json"
        if not meta_path.is_file():
            raise HTTPException(status_code=404, detail="Run not found")
        if not results_path.is_file():
            raise HTTPException(status_code=400, detail="No classifications saved for this run")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        meta    = json.loads(meta_path.read_text(encoding="utf-8"))
        return {
            "run_id":   run_id,
            "filename": meta.get("filename", run_id),
            "pages":    results.get("pages", []),
            "bboxes":   results.get("bboxes", {}),
        }

    @router.post("/validate")
    async def validate_endpoints(req: ValidateRequest):
        """Smoke-test the manager and Gemini endpoints with a 1-token request before starting a run."""
        errors: dict[str, str] = {}

        def _test(url: str, headers: dict, model: str, label: str) -> str | None:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
            try:
                with httpx.Client(timeout=15.0) as client:
                    r = client.post(url, headers={**headers, "content-type": "application/json"}, json=payload)
                if r.status_code >= 400:
                    try:
                        detail = r.json().get("error", {}).get("message") or r.text[:120]
                    except Exception:
                        detail = r.text[:120]
                    return f"HTTP {r.status_code}: {detail}"
            except Exception as e:
                return str(e)[:120]
            return None

        # Resolve manager
        mgr_url = mgr_headers = mgr_model_id = None
        if req.manager_model:
            found = _get_endpoint_for_model(req.manager_model)
            if found:
                mgr_url, mgr_headers, _, mgr_model_id = found
                mgr_url = _to_openai_compat_url(mgr_url)
        if not mgr_url:
            try:
                db = SessionLocal()
                try:
                    ep = db.query(ModelEndpoint).filter(ModelEndpoint.base_url.ilike("%anthropic.com%")).first()
                    if ep:
                        base, api_key = resolve_endpoint_runtime(ep)
                        mgr_url = _to_openai_compat_url(build_chat_url(base))
                        mgr_headers = build_headers(api_key, base)
                        mgr_model_id = getattr(ep, "model", None) or _CLAUDE_MODEL
                finally:
                    db.close()
            except Exception:
                pass
        if not mgr_url:
            errors["manager"] = "No manager endpoint found — add an endpoint in Settings"
        else:
            err = await asyncio.to_thread(_test, mgr_url, mgr_headers or {}, mgr_model_id or _CLAUDE_MODEL, "manager")
            if err:
                errors["manager"] = err

        # Resolve Gemini
        if not errors.get("manager") or True:  # always test both
            gem_url = gem_headers = gem_model = None
            if req.gemini_model:
                found = _get_endpoint_for_model(req.gemini_model)
                if found:
                    gem_url, gem_headers, _, _ = found
                    gem_model = req.gemini_model
            if not gem_url:
                gem_url, gem_headers, _, gem_model = _get_gemini_endpoint()
            if not gem_url:
                errors["gemini"] = "No Gemini endpoint found — add a googleapis.com endpoint in Settings"
            else:
                err = await asyncio.to_thread(_test, gem_url, gem_headers or {}, gem_model or "", "gemini")
                if err:
                    errors["gemini"] = err

        return {"ok": len(errors) == 0, "errors": errors}

    @router.get("/jobs")
    async def list_jobs():
        """Return the case library job list so the UI can show it before a run starts."""
        holdout_base = _QP_DIR.parent.parent / "documentation" / "temp" / "knowledge_pack"
        # Pre-scan holdout dirs once so we can match by normalized job name.
        holdout_dirs: list[Path] = []
        if holdout_base.is_dir():
            holdout_dirs = [d for d in holdout_base.iterdir() if d.is_dir() and d.name.startswith("holdout_")]
        jobs = _load_case_library()
        result = []
        for c in jobs:
            job_name = c.get("job_name", "")
            # Normalize: lowercase, spaces → underscores, then check if any holdout dir contains it.
            slug = job_name.lower().replace(" ", "_").replace("-", "_")
            holdout_kp = None
            for d in holdout_dirs:
                if slug in d.name:
                    candidate = d / "knowledge_pack.json"
                    if candidate.is_file():
                        holdout_kp = str(candidate)
                        break
            result.append({
                "id":              job_name,
                "name":            job_name,
                "holdout_kp_path": holdout_kp,
            })
        return result

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

        task = asyncio.create_task(_run_phase3_only(index, queue, run_id, manager_model=req.manager_model, gemini_model=req.gemini_model, retry_attempts=req.gemini_retry_attempts, gemini_fallback_models=req.gemini_fallback_models or None))
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
            result = None
            last_err = None
            delay = 2.0
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
                try:
                    result = await _classify_one_page(str(img_path), prompt, url, headers, model, None)
                    last_err = None
                    break
                except httpx.HTTPStatusError as e:
                    last_err = e
                    if e.response.status_code not in _RETRYABLE_STATUS:
                        break
                    logger.warning(f"[quick_proposal] reclassify page={page_idx} HTTP {e.response.status_code} attempt={attempt + 1}/3")
                except (json.JSONDecodeError, httpx.TimeoutException) as e:
                    last_err = e
                    logger.warning(f"[quick_proposal] reclassify page={page_idx} {type(e).__name__} attempt={attempt + 1}/3: {e}")
                except Exception as e:
                    last_err = e
                    break
            if last_err is not None:
                raise last_err
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
            if max(bbox) > 100:
                bbox = [v / 10.0 for v in bbox]
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
                if max(bbox) > 100:
                    bbox = [v / 10.0 for v in bbox]
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