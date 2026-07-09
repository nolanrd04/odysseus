import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.constants import DATA_DIR, UPLOAD_DIR
from core.database import SessionLocal, ModelEndpoint, Session as DbSession
from src.endpoint_resolver import resolve_endpoint_runtime, build_chat_url, build_headers

logger = logging.getLogger(__name__)

_session_manager = None  # set by setup_quick_proposal_routes; used by _save_chat_message

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


class _RunBroadcaster:
    """Fans an SSE event out to every currently-connected client for a run.

    A plain asyncio.Queue only supports one effective consumer: Queue.get() hands each
    item to a single waiter, so if two browser tabs both open /stream/{run_id} for the
    same run, each event goes to whichever tab's get() happened to be waiting — the run's
    event stream gets silently split between tabs instead of shown to both (confirmed via
    two-tab testing: each tab rendered a different, incomplete subset of the same run's
    tool calls). This wraps the same put()-based interface pipeline code already calls via
    _emit(), so no pipeline function needs to change — only stream_run/cancel_run below,
    which now subscribe()/unsubscribe() per connection instead of sharing one queue.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def put(self, item) -> None:
        for q in list(self._subscribers):
            await q.put(item)


# run_id → _RunBroadcaster fanning out SSE event dicts (None = end-of-stream sentinel)
_active_runs: dict[str, _RunBroadcaster] = {}
# run_id → asyncio.Task (so we can cancel in-flight pipelines)
_active_tasks: dict[str, asyncio.Task] = {}
# run_id → asyncio.Task for the in-flight phase-6 chat continuation (a proposal
# follow-up question). Previously qp_chat_continuation fired an untracked
# asyncio.create_task with no way to stop or supersede it, so clicking Stop only
# ever cleared the client-side bubble — the manager kept generating server-side
# and, if the user sent another follow-up in the meantime, two turns ran
# concurrently against the same session and landed out of order (see the
# "response vanished, then a stale reply appeared after my real question"
# incident). New requests for the same run now cancel whatever's still running
# first.
_active_continuations: dict[str, asyncio.Task] = {}


async def _cancel_continuation(run_id: str) -> bool:
    """Cancel the in-flight chat-continuation task for `run_id`, if any, and wait
    for it to actually unwind before returning. Safe to call even if nothing is
    running. Each save inside _qp_continuation_task is an atomic, independent DB
    write (tool results as they happen, the final reply at the end), so
    cancelling mid-flight just stops it from producing further messages — no
    partial-write cleanup is needed."""
    task = _active_continuations.get(run_id)
    if not task or task.done():
        return False
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return True


def is_continuation_active(run_id: str) -> bool:
    """Whether a phase-6 chat-continuation task is still running for `run_id`.

    Used by /api/chat/stream_status so a dropped client connection (network
    blip, tab backgrounding, etc.) degrades to the existing spinner+poll
    fallback instead of a silent void — qp_chat_continuation's task keeps
    running and saving messages regardless of whether anyone is still
    listening on its SSE stream, but without this check the frontend had no
    way to know that and see it (see TODO_GG)."""
    task = _active_continuations.get(run_id)
    return bool(task and not task.done())


# ── Request model ──────────────────────────────────────────────────────────────

class Phase3OnlyRequest(BaseModel):
    manager_model: str = ""
    gemini_model: str = ""
    gemini_retry_attempts: int = 3
    gemini_fallback_models: List[str] = []
    holdout_kp_path: str = ""
    resume: bool = False
    completeness_only: bool = False
    reuse_notes: bool = False


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
    import_notes_from_run_id: str = ""
    import_scope_from_run_id: str = ""
    project_type: str = ""  # "" = auto-detect via Phase 1; else: residential_subdivision | commercial_development | rural_access | mixed


class AdvancePhaseRequest(BaseModel):
    run_id: str
    phase: str = ""


class ValidateRequest(BaseModel):
    gemini_model: str = ""
    manager_model: str = ""


class QPChatRequest(BaseModel):
    messages: List[dict]
    manager_model: str = ""


class QPContinuationRequest(BaseModel):
    message: str
    manager_model: str = ""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _save_chat_message(session_id: str, role: str, content, meta: dict | None = None) -> None:
    """Persist a single manager-conversation turn to the session's ChatMessage table.

    Uses session_manager.add_message when available so the in-memory session
    cache stays current — otherwise a hard refresh (server still up) returns
    stale history from the cache and the extraction log disappears.
    """
    if not session_id:
        return
    content_str = content if isinstance(content, str) else json.dumps(content)
    if _session_manager is not None:
        from core.models import ChatMessage as CoreChatMessage
        try:
            _session_manager.add_message(
                session_id,
                CoreChatMessage(role=role, content=content_str, metadata=meta or {}),
            )
            return  # success — in-memory cache + DB both updated
        except Exception as e:
            logger.warning(f"[quick_proposal] ChatMessage save via session_manager failed (falling back to direct DB write): {e}")
    # Fallback: direct DB write (session_manager not wired up)
    from core.database import ChatMessage as DbChatMessage, SessionLocal as _SL
    db = _SL()
    try:
        msg = DbChatMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role=role,
            content=content_str,
            meta_data=json.dumps(meta) if meta else None,
        )
        db.add(msg)
        db.commit()
    except Exception as e:
        logger.warning(f"[quick_proposal] ChatMessage save failed: {e}")
        db.rollback()
    finally:
        db.close()


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


def _repair_unescaped_quotes(text: str) -> str:
    """Best-effort fix for literal, unescaped " characters inside JSON string values
    (e.g. Gemini writing 12" instead of 12in for an inch mark). Walks the text tracking
    JSON string state; a " encountered mid-string is treated as a real closing quote only
    if the next non-whitespace character is a JSON structural character, otherwise it's
    escaped as \\"."""
    out = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string and ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
                i += 1
                continue
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            nxt = text[j] if j < n else ""
            if nxt in ",:}]" or nxt == "":
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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


def _save_notes_text(run_id: str, notes_text: dict) -> None:
    """Patch extracted_data.notes_text into results.json without touching other keys."""
    results_path = Path(RUNS_DIR) / run_id / "results.json"
    try:
        existing: dict = {}
        if results_path.is_file():
            existing = json.loads(results_path.read_text(encoding="utf-8"))
        existing.setdefault("extracted_data", {})["notes_text"] = notes_text
        results_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] notes_text save failed run={run_id}: {e}")


def _save_scope_analysis(run_id: str, scope_analysis: str) -> None:
    """Patch extracted_data.scope_analysis into results.json without touching other keys."""
    results_path = Path(RUNS_DIR) / run_id / "results.json"
    try:
        existing: dict = {}
        if results_path.is_file():
            existing = json.loads(results_path.read_text(encoding="utf-8"))
        existing.setdefault("extracted_data", {})["scope_analysis"] = scope_analysis
        results_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] scope_analysis save failed run={run_id}: {e}")


def _save_context_usage(run_id: str, role: str, payload: dict) -> None:
    """Patch the latest per-role context-usage snapshot into results.json without touching other keys.

    This is the persisted counterpart to the context_usage SSE event — the event alone only
    reaches clients connected at the moment it fires, so a role's usage was otherwise lost on
    reconnect/hard-refresh (see TODO_L/TODO_S).
    """
    results_path = Path(RUNS_DIR) / run_id / "results.json"
    try:
        existing: dict = {}
        if results_path.is_file():
            existing = json.loads(results_path.read_text(encoding="utf-8"))
        existing.setdefault("context_usage", {})[role] = payload
        results_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] context_usage save failed run={run_id}: {e}")


async def _run_phase5_only(index, queue: asyncio.Queue, run_id: str, manager_model: str = "", gemini_model: str = "", retry_attempts: int = 3, gemini_fallback_models: list | None = None, holdout_kp_path: str = "", resume: bool = False, completeness_only: bool = False, session_id: str = "") -> None:
    """Re-run phase2 index build + phase3 extraction loop using saved page classifications."""
    try:
        await _emit(queue, "phase_start", phase="phase4", label="Building extraction index…")
        index.extracted_data["phase1_summary"] = _build_phase1_summary(index)
        await _emit(queue, "phase_complete", phase="phase4")

        await phase_notes_extraction(
            index, queue,
            gemini_model=gemini_model,
            retry_attempts=retry_attempts,
            gemini_fallback_models=gemini_fallback_models,
        )

        await phase_scope_analysis(
            index, queue,
            gemini_model=gemini_model,
            retry_attempts=retry_attempts,
            gemini_fallback_models=gemini_fallback_models,
        )

        if completeness_only or "plan_completeness" not in index.extracted_values:
            await phase3_completeness_score(index, queue, gemini_model=gemini_model, retry_attempts=retry_attempts, gemini_fallback_models=gemini_fallback_models)

        if not completeness_only:
            await phase5_extraction_loop(index, queue, manager_model=manager_model, gemini_model=gemini_model, retry_attempts=retry_attempts, gemini_fallback_models=gemini_fallback_models, holdout_kp_path=holdout_kp_path, resume=resume, session_id=session_id)
        _save_run_results(run_id, index)
        if not completeness_only:
            _save_generation_snapshot(run_id, session_id, manager_model, gemini_model, holdout_kp_path)
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
        # Preserve context_usage — it's patched in incrementally by _save_context_usage during
        # the run, not tracked on `index`, so a naive overwrite here would erase it at completion.
        existing_context_usage: dict = {}
        if results_path.is_file():
            existing_context_usage = json.loads(results_path.read_text(encoding="utf-8")).get("context_usage", {})
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
            "context_usage":    existing_context_usage,
        }
        results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[quick_proposal] results save failed run={run_id}: {e}")


def _save_generation_snapshot(run_id: str, session_id: str, manager_model: str, gemini_model: str, holdout_kp_path: str) -> None:
    """Persist an immutable QpGeneration row for a just-completed extraction+proposal attempt.

    Called once per completed phase5 run (fresh or rerun) — this is what survives a future
    rerun's in-place overwrite of results.json / wipe of the session's chat history, so every
    attempt is a distinct data point instead of only the latest one.
    """
    from core.database import ChatMessage as _DbMsg, SessionLocal as _SL, QpGeneration as _QpGen

    results_path = Path(RUNS_DIR) / run_id / "results.json"
    results_snapshot: dict = {}
    if results_path.is_file():
        try:
            results_snapshot = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    log_path = Path(RUNS_DIR) / run_id / "phase5_log.jsonl"
    grand_total = None
    resolved_manager_model = manager_model or None
    if log_path.is_file():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("type") == "grand_total":
                    grand_total = entry.get("amount")
                if entry.get("model"):
                    resolved_manager_model = entry["model"]
        except Exception:
            pass

    db = _SL()
    try:
        messages_snapshot: list = []
        if session_id:
            rows = (
                db.query(_DbMsg)
                .filter(_DbMsg.session_id == session_id)
                .order_by(_DbMsg.timestamp)
                .all()
            )
            messages_snapshot = [
                {
                    "role":      r.role,
                    "content":   r.content,
                    "metadata":  r.meta_data,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]

        generation_index = db.query(_QpGen).filter(_QpGen.run_id == run_id).count() + 1

        db.add(_QpGen(
            id=str(uuid.uuid4()),
            run_id=run_id,
            session_id=session_id or None,
            generation_index=generation_index,
            manager_model=resolved_manager_model,
            gemini_model=gemini_model or None,
            holdout_kp_path=holdout_kp_path or None,
            grand_total=grand_total,
            results_snapshot=results_snapshot,
            messages_snapshot=messages_snapshot,
        ))
        db.commit()
    except Exception as e:
        logger.warning(f"[quick_proposal] generation snapshot save failed run={run_id}: {e}")
        db.rollback()
    finally:
        db.close()


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
        stripped = _strip_fences(text)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as e:
            try:
                repaired = json.loads(_repair_unescaped_quotes(stripped))
                logger.info(f"[quick_proposal] JSON parse failed ({e}), repaired via quote-escaping, no retry needed")
                return repaired
            except json.JSONDecodeError:
                logger.warning(f"[quick_proposal] JSON parse failed, raw response: {text[:500]}")
                raise e


async def phase2_classify_pages(index, queue: asyncio.Queue, model_override: str = "", retry_attempts: int = 3, fallback_models: list | None = None) -> None:
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
                await _emit(queue, "error", message="No Gemini API key found — add a googleapis.com endpoint in Settings", phase="phase2")
                return
    else:
        url, headers, api_key, model = _get_gemini_endpoint()
        if not api_key:
            await _emit(queue, "error", message="No Gemini API key found — add a googleapis.com endpoint in Settings", phase="phase2")
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

    await _emit(queue, "phase_start", phase="phase2",
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

    await _emit(queue, "phase_complete", phase="phase2")


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
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "Writes the COMPLETE VERBATIM text of a notes/list region to the shared notes store. Do not summarize, paraphrase, or omit anything — copy every line exactly, including numbers, units, and callout labels. This is separate from index_write and is only used during the notes-extraction phase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bbox_id": {"type": "string", "description": "Region identifier this text was read from, e.g. '1_r2'"},
                    "text":    {"type": "string", "description": "The complete verbatim text visible in the region"},
                },
                "required": ["bbox_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_scope_analysis",
            "description": "Writes your complete scope-boundary analysis to the shared scope store. This is separate from index_write and is only used during the scope-analysis phase — see that phase's prompt for what to cover.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The complete scope analysis write-up"},
                },
                "required": ["text"],
            },
        },
    },
]


_GEMINI_UTILITY_TOOLS = [
    t for t in _GEMINI_PHASE3_TOOLS
    if t["function"]["name"] in {"get_image", "list_images", "index_write"}
]

_GEMINI_COMPLETENESS_TOOLS = [
    t for t in _GEMINI_PHASE3_TOOLS
    if t["function"]["name"] in {"get_image", "list_images", "index_read", "index_write"}
]

_GEMINI_NOTES_TOOLS = [
    t for t in _GEMINI_PHASE3_TOOLS
    if t["function"]["name"] in {"enhance_region", "crop_page", "get_image", "list_images", "write_note", "index_write"}
]

_GEMINI_SCOPE_TOOLS = [
    t for t in _GEMINI_PHASE3_TOOLS
    if t["function"]["name"] in {"enhance_region", "crop_page", "enhance_subregion", "get_image", "list_images", "write_scope_analysis", "index_write"}
]

# run_id → asyncio.Event set by /advance-phase to unblock a waiting pipeline gate
_active_gates: dict[str, asyncio.Event] = {}


async def _wait_for_gate(run_id: str, queue: asyncio.Queue, gate_phase: str, next_label: str) -> None:
    """Emit a phase_gate event and block until /advance-phase is called for this run."""
    event = asyncio.Event()
    _active_gates[run_id] = event
    await _emit(queue, "phase_gate", phase=gate_phase, next_phase_label=next_label)
    await event.wait()
    _active_gates.pop(run_id, None)


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


async def _render_and_cache(
    image_id: str,
    page_idx: int,
    x1: float, y1: float, x2: float, y2: float,
    dpi: int,
    index,
    image_store: dict,
    desc: str,
) -> tuple[bytes, str]:
    """Render a page crop and cache the result. Returns (jpeg_bytes, cached_note).
    Raises ValueError if PIL crop coords are degenerate."""
    if image_id in image_store:
        return image_store[image_id]["bytes"], " (from cache)"
    if index.source_is_pdf:
        jpeg_bytes = await asyncio.to_thread(
            _render_pdf_crop, index.source_path, page_idx, x1, y1, x2, y2, dpi,
        )
    else:
        from PIL import Image as PilImage
        img = await asyncio.to_thread(PilImage.open, index.pages[page_idx].image_path)
        w, h = img.size
        px1 = max(0, int(x1 / 100 * w))
        py1 = max(0, int(y1 / 100 * h))
        px2 = min(w, int(x2 / 100 * w))
        py2 = min(h, int(y2 / 100 * h))
        if px2 <= px1 or py2 <= py1:
            raise ValueError(f"invalid crop coords ({px1},{py1},{px2},{py2})")
        buf = io.BytesIO()
        img.crop((px1, py1, px2, py2)).save(buf, "JPEG", quality=90)
        jpeg_bytes = buf.getvalue()
    image_store[image_id] = {"bytes": jpeg_bytes, "desc": desc}
    return jpeg_bytes, ""


async def _tool_enhance_region(args: dict, index, image_store: dict) -> list:
    bbox_id = args.get("bbox_id", "")
    dpi     = int(args.get("dpi", 200))
    rec     = index.bboxes.get(bbox_id)
    if not rec:
        if bbox_id in image_store:
            jpeg_bytes = image_store[bbox_id]["bytes"]
            b64 = base64.b64encode(jpeg_bytes).decode()
            return [
                {"type": "text", "text": f"Image {bbox_id} (from cache) [image_id: {bbox_id}]:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        available = list(image_store.keys())
        hint = f" Available image_ids: {available}" if available else ""
        return [{"type": "text", "text": f"Error: bbox_id '{bbox_id}' not found in index or image store.{hint}"}]
    try:
        jpeg_bytes, cached_note = await _render_and_cache(
            bbox_id, rec.page_idx, rec.x1, rec.y1, rec.x2, rec.y2, dpi,
            index, image_store, f"Region {bbox_id} at {dpi} DPI",
        )
        b64 = base64.b64encode(jpeg_bytes).decode()
        return [
            {"type": "text", "text": f"Region {bbox_id} at {dpi} DPI{cached_note} [image_id: {bbox_id}]:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    except Exception as e:
        logger.warning(f"[quick_proposal] enhance_region error: {e}", exc_info=True)
        return [{"type": "text", "text": f"Error rendering region: {e}"}]


async def _tool_crop_page(args: dict, index, image_store: dict) -> list:
    page_idx = int(args.get("page_idx", 0))
    x1       = float(args.get("x1", 0))
    y1       = float(args.get("y1", 0))
    x2       = float(args.get("x2", 100))
    y2       = float(args.get("y2", 100))
    dpi      = int(args.get("dpi", 200))
    if page_idx >= len(index.pages):
        return [{"type": "text", "text": f"Error: page_idx {page_idx} out of range ({len(index.pages)} pages)"}]
    if x1 == 0 and y1 == 0 and x2 == 100 and y2 == 100:
        image_id = f"p{page_idx}_full"
    else:
        image_id = f"p{page_idx}_{x1:.0f}_{y1:.0f}_{x2:.0f}_{y2:.0f}"
    desc = f"Page {page_idx} crop [{x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f}]% at {dpi} DPI"
    try:
        jpeg_bytes, cached_note = await _render_and_cache(
            image_id, page_idx, x1, y1, x2, y2, dpi, index, image_store, desc,
        )
        b64 = base64.b64encode(jpeg_bytes).decode()
        return [
            {"type": "text", "text": f"{desc}{cached_note} [image_id: {image_id}]:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    except Exception as e:
        logger.warning(f"[quick_proposal] crop_page error: {e}", exc_info=True)
        return [{"type": "text", "text": f"Error rendering crop: {e}"}]


async def _tool_enhance_subregion(args: dict, index, image_store: dict) -> list:
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
    pw      = parent.x2 - parent.x1
    ph      = parent.y2 - parent.y1
    x1_page = parent.x1 + x1_rel / 100 * pw
    y1_page = parent.y1 + y1_rel / 100 * ph
    x2_page = parent.x1 + x2_rel / 100 * pw
    y2_page = parent.y1 + y2_rel / 100 * ph
    desc = f"Sub-region of {parent_id} [{x1_rel:.0f},{y1_rel:.0f},{x2_rel:.0f},{y2_rel:.0f}]% at {dpi} DPI"
    try:
        jpeg_bytes, cached_note = await _render_and_cache(
            image_id, parent.page_idx, x1_page, y1_page, x2_page, y2_page, dpi,
            index, image_store, desc,
        )
        b64 = base64.b64encode(jpeg_bytes).decode()
        return [
            {"type": "text", "text": f"Sub-region of {parent_id} [{x1_rel:.0f},{y1_rel:.0f},{x2_rel:.0f},{y2_rel:.0f}]% → page [{x1_page:.1f},{y1_page:.1f},{x2_page:.1f},{y2_page:.1f}]% at {dpi} DPI{cached_note} [image_id: {image_id}]:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    except Exception as e:
        logger.warning(f"[quick_proposal] enhance_subregion error: {e}", exc_info=True)
        return [{"type": "text", "text": f"Error rendering subregion: {e}"}]


async def _tool_index_read(args: dict, index) -> list:
    return [{"type": "text", "text": json.dumps(index.extracted_values) if index.extracted_values else "{}"}]


async def _tool_list_images(args: dict, image_store: dict) -> list:
    entries = [{"image_id": k, "desc": v["desc"]} for k, v in image_store.items()]
    return [{"type": "text", "text": json.dumps(entries)}]


async def _tool_get_image(args: dict, image_store: dict) -> list:
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


async def _tool_index_write(args: dict, index, queue: asyncio.Queue) -> list:
    key            = args.get("key", "")
    value          = args.get("value")
    if isinstance(value, str) and value[:1] in "[{":
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    source_bbox_id = args.get("source_bbox_id")
    confidence     = args.get("confidence", "medium")
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


async def _tool_write_note(args: dict, index, queue: asyncio.Queue) -> list:
    """Writes verbatim notes text to a store separate from extracted_values, so the
    manager's default read_index() doesn't balloon with it — see read_index(section='notes')."""
    bbox_id = args.get("bbox_id", "")
    text    = args.get("text", "")
    notes   = index.extracted_data.setdefault("notes_text", {})
    notes[bbox_id] = text
    await _emit(queue, "notes_update", bbox_id=bbox_id, chars=len(text))
    _save_notes_text(index.run_id, notes)
    return [{"type": "text", "text": f"Wrote note for {bbox_id} ({len(text)} chars)"}]


async def _tool_write_scope_analysis(args: dict, index, queue: asyncio.Queue) -> list:
    """Writes the scope-boundary analysis to a store separate from extracted_values, so the
    manager's default read_index() doesn't balloon with it — see read_index(section='scope')."""
    text = args.get("text", "")
    index.extracted_data["scope_analysis"] = text
    await _emit(queue, "scope_update", chars=len(text))
    _save_scope_analysis(index.run_id, text)
    return [{"type": "text", "text": f"Wrote scope analysis ({len(text)} chars)"}]


async def _execute_gemini_tool(
    name: str, args: dict, index, queue: asyncio.Queue, image_store: dict
) -> list:
    """Execute a Gemini Phase 3 tool. Returns a list of OpenAI content blocks."""
    if name == "enhance_region":
        return await _tool_enhance_region(args, index, image_store)
    elif name == "crop_page":
        return await _tool_crop_page(args, index, image_store)
    elif name == "enhance_subregion":
        return await _tool_enhance_subregion(args, index, image_store)
    elif name == "index_read":
        return await _tool_index_read(args, index)
    elif name == "list_images":
        return await _tool_list_images(args, image_store)
    elif name == "get_image":
        return await _tool_get_image(args, image_store)
    elif name == "index_write":
        return await _tool_index_write(args, index, queue)
    elif name == "write_note":
        return await _tool_write_note(args, index, queue)
    elif name == "write_scope_analysis":
        return await _tool_write_scope_analysis(args, index, queue)
    else:
        return [{"type": "text", "text": f"Unknown tool: {name}"}]


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

def _is_retryable_manager_error(err_msg: str) -> bool:
    """True for transient manager-call failures worth retrying (Anthropic's mid-stream
    `overloaded_error`, rate limits, 5xx, and connection drops) — as opposed to fatal
    errors (bad request, auth, context length) that will fail identically on retry."""
    msg = (err_msg or "").lower()
    if "overloaded" in msg or "rate_limit" in msg or "rate limit" in msg:
        return True
    if "connection to model failed" in msg:
        return True
    for code in (429, 500, 502, 503, 504, 529):
        if f"model returned {code}" in msg:
            return True
    return False


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
    tools: list | None = None,
    stop_keys: set | None = None,
    session_id: str = "",
) -> str:
    """Append user_message to Gemini history, call Gemini, handle tool loops, return final text."""
    image_store = gemini_state.setdefault("image_store", {})
    gemini_state["messages"].append({"role": "user", "content": user_message})

    active_tools = tools if tools is not None else _GEMINI_PHASE3_TOOLS
    tool_call_counts: dict[str, int] = {}

    for _ in range(200):
        payload: dict = {
            "model":    gemini_state["model"],
            "messages": gemini_state["messages"],
            "tools":    active_tools,
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
            _ctx_payload = dict(role="gemini",
                                 model=gemini_state.get("model", ""),
                                 input_tokens=g_usage.get("prompt_tokens", 0),
                                 output_tokens=g_usage.get("completion_tokens", 0),
                                 context_window=1048576)
            await _emit(queue, "context_usage", **_ctx_payload)
            _save_context_usage(index.run_id, "gemini", _ctx_payload)
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
            # Intermediate text that precedes tool calls is Gemini's reasoning —
            # show it collapsed. Text in a final (no tool_calls) turn is the summary.
            gemini_role = "gemini_thinking" if tool_calls else "gemini"
            await _emit(queue, "extraction_message", role=gemini_role, text=text_out)
            if log_path := gemini_state.get("log_path"):
                _log_phase3_event(log_path, {"type": "extraction_message", "role": gemini_role, "text": text_out})

        if not tool_calls:
            if not (text_out and text_out.strip()):
                if tool_call_counts:
                    # Gemini did real work this turn (tool calls executed) but returned no
                    # closing text. Returning "" here would let the caller mistake silence
                    # for inactivity — surface what actually happened instead.
                    activity = ", ".join(f"{name} x{n}" for name, n in tool_call_counts.items())
                    text_out = (
                        f"(Gemini made {sum(tool_call_counts.values())} tool call(s) this turn "
                        f"but returned no closing summary text: {activity}. Call read_index() "
                        f"to see what was written before deciding whether to redirect further.)"
                    )
                else:
                    text_out = "(Gemini returned no text and made no tool calls this turn.)"
                await _emit(queue, "extraction_message", role="gemini", text=text_out)
                if log_path := gemini_state.get("log_path"):
                    _log_phase3_event(log_path, {"type": "extraction_message", "role": "gemini", "text": text_out})
            return text_out

        for tc in tool_calls:
            fn        = tc.get("function", {})
            tool_name = fn.get("name", "")
            raw_args  = fn.get("arguments", "{}")
            tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
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
            _save_chat_message(session_id, "tool", result_text, {"tool_name": tool_name, "source": "gemini_subtool"})
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
        # Exit early if a terminal key has been written to the index (e.g. completeness_complete).
        if stop_keys and any(
            index.extracted_values.get(k, {}).get("value") for k in stop_keys
        ):
            return text_out

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

    logger.warning("[quick_proposal] Gemini tool loop hit 200-round limit")
    return "(extraction loop limit reached)"


def _build_gemini_state_for_phase(
    prompt_file: str,
    gemini_model: str,
    gemini_fallback_models: list | None,
    index,
) -> tuple[dict, list]:
    """Resolve the Gemini endpoint and build a fresh state dict for a utility phase.
    Returns (gemini_state, fallback_models_info)."""
    if gemini_model:
        found = _get_endpoint_for_model(gemini_model)
        if found:
            url, headers, _, _ = found
        else:
            url, headers, _, gemini_model = _get_gemini_endpoint()
    else:
        url, headers, _, gemini_model = _get_gemini_endpoint()

    fallback_models_info: list = []
    for fb_model in (gemini_fallback_models or []):
        found = _get_endpoint_for_model(fb_model)
        if found:
            fallback_models_info.append((found[0], found[1], fb_model))
        else:
            fallback_models_info.append((url, headers, fb_model))

    prompt_text = (_PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")

    image_store: dict = {}
    for page in index.pages:
        try:
            image_store[f"p{page.idx}_thumbnail"] = {
                "bytes": Path(page.image_path).read_bytes(),
                "desc":  f"Page {page.idx} thumbnail",
            }
        except Exception:
            pass

    state: dict = {
        "messages":    [{"role": "system", "content": prompt_text}],
        "url":         url,
        "headers":     headers,
        "model":       gemini_model,
        "cache_name":  None,
        "image_store": image_store,
    }
    return state, fallback_models_info


async def phase1_detect_job_type(
    index,
    queue: asyncio.Queue,
    gemini_model: str = "",
    retry_attempts: int = 3,
    gemini_fallback_models: list | None = None,
) -> None:
    """Detect project type via Gemini. Only called when project_type not already set."""
    await _emit(queue, "phase_start", phase="phase1", label="Job Type Detection…")
    state, fallback_models_info = _build_gemini_state_for_phase(
        "gemini_phase0_5.txt", gemini_model, gemini_fallback_models, index
    )
    await _run_gemini_with_tools(
        "Please identify the project type for this plan set. "
        "Start with list_images() to see available thumbnails, then examine the cover or first plan pages.",
        state, index, queue,
        retry_attempts=retry_attempts,
        fallback_models_info=fallback_models_info,
        tools=_GEMINI_UTILITY_TOOLS,
    )
    detected = index.extracted_values.get("project_type", {}).get("value", "unknown")
    logger.info(f"[quick_proposal] phase1 complete — project_type={detected}")
    await _emit(queue, "phase_complete", phase="phase1")


async def phase3_completeness_score(
    index,
    queue: asyncio.Queue,
    gemini_model: str = "",
    retry_attempts: int = 3,
    gemini_fallback_models: list | None = None,
) -> None:
    """Score plan completeness across categories. Always runs after Phase 1."""
    await _emit(queue, "phase_start", phase="phase3", label="Completeness Scoring…")
    state, fallback_models_info = _build_gemini_state_for_phase(
        "gemini_completeness.txt", gemini_model, gemini_fallback_models, index
    )
    project_type   = index.extracted_values.get("project_type", {}).get("value", "residential_subdivision")
    phase1_summary = index.extracted_data.get("phase1_summary", "(no Phase 1 summary available)")
    initial_ctx = (
        f"Project type: {project_type}\n\n"
        "Phase 1 classification summary:\n\n"
        + phase1_summary
        + "\n\nPlease score the plan completeness for each relevant category."
    )
    await _run_gemini_with_tools(
        initial_ctx,
        state, index, queue,
        retry_attempts=retry_attempts,
        fallback_models_info=fallback_models_info,
        tools=_GEMINI_COMPLETENESS_TOOLS,
        stop_keys={"completeness_complete"},
    )
    logger.info(f"[quick_proposal] phase3 complete — project_type={project_type}")
    await _emit(queue, "phase_complete", phase="phase3")


def _collect_notes_bboxes(index) -> dict[int, list]:
    """Group every notes/list-type region by page index for the notes-extraction phase."""
    by_page: dict[int, list] = {}
    for bbox_id, rec in index.bboxes.items():
        if rec.element_type in ("notes", "list"):
            by_page.setdefault(rec.page_idx, []).append(rec)
    return by_page


async def phase_notes_extraction(
    index,
    queue: asyncio.Queue,
    gemini_model: str = "",
    retry_attempts: int = 3,
    gemini_fallback_models: list | None = None,
) -> None:
    """Bulk, one-pass verbatim transcription of every general-notes/spec/list region.

    Runs once, before the extraction loop, so Phase 5's manager can pull the full text via
    read_index(section='notes') instead of repeatedly sending Gemini back to re-read the same
    notes page for one keyword at a time (earthwork balance, ballast, stripping depth, etc.).
    """
    await _emit(queue, "phase_start", phase="notes", label="Extracting plan notes…")

    if index.extracted_data.get("notes_text"):
        # Already populated (e.g. resuming an interrupted run) — don't re-spend API calls.
        await _emit(queue, "phase_complete", phase="notes")
        return

    by_page = _collect_notes_bboxes(index)
    if not by_page:
        logger.info("[quick_proposal] notes phase — no notes/list regions found, skipping")
        await _emit(queue, "phase_complete", phase="notes")
        return

    state, fallback_models_info = _build_gemini_state_for_phase(
        "gemini_notes.txt", gemini_model, gemini_fallback_models, index
    )

    lines = []
    for page_idx in sorted(by_page):
        for rec in by_page[page_idx]:
            lines.append(f"  {rec.id} (page {page_idx}): {rec.description}")
    region_list = "\n".join(lines)

    initial_ctx = (
        f"Here are all {sum(len(v) for v in by_page.values())} notes/list regions found across "
        f"the plan set:\n\n{region_list}\n\n"
        "For each one: call enhance_region(bbox_id), then call write_note(bbox_id, text) with "
        "the complete verbatim text you see. When every region above has been written, call "
        "index_write(\"notes_extraction_complete\", true, null, \"high\") to finish."
    )
    await _run_gemini_with_tools(
        initial_ctx,
        state, index, queue,
        retry_attempts=retry_attempts,
        fallback_models_info=fallback_models_info,
        tools=_GEMINI_NOTES_TOOLS,
        stop_keys={"notes_extraction_complete"},
    )
    logger.info(f"[quick_proposal] notes phase complete — {len(index.extracted_data.get('notes_text', {}))} regions transcribed")
    await _emit(queue, "phase_complete", phase="notes")


async def phase_scope_analysis(
    index,
    queue: asyncio.Queue,
    gemini_model: str = "",
    retry_attempts: int = 3,
    gemini_fallback_models: list | None = None,
) -> None:
    """Determine the project's actual contracted scope — which roads/areas/lots are actually
    being built vs. shown for context only, phasing boundaries, expansions of existing
    infrastructure, etc. — via a dedicated Gemini pass, run once before extraction begins.

    Runs in its own isolated context (fresh Gemini call, own prompt file), the same way Phase 2
    (classification) and phase_notes_extraction do. Does NOT build off the notes-extraction
    phase's context, and its result is NOT force-fed into Phase 5's initial prompt — it is simply
    available for the manager to pull via read_index(section='scope') on demand, exactly like
    notes_text already is. This exists so a project's scope boundary is established up front
    instead of the manager inferring it ad hoc mid-extraction (see TODO_PP).
    """
    await _emit(queue, "phase_start", phase="scope", label="Analyzing project scope…")

    if index.extracted_data.get("scope_analysis"):
        # Already populated (e.g. resuming an interrupted run) — don't re-spend API calls.
        await _emit(queue, "phase_complete", phase="scope")
        return

    state, fallback_models_info = _build_gemini_state_for_phase(
        "gemini_scope.txt", gemini_model, gemini_fallback_models, index
    )

    project_type = index.extracted_values.get("project_type", {}).get("value", "unknown")
    initial_ctx = (
        f"Project type: {project_type}\n\n"
        "Call list_images() to see all page thumbnails, then examine the cover sheet, "
        "overall/key sheet, and any phase-limit, project-limit, or \"not in contract\" callouts "
        "to determine the actual contracted scope of this project. When you have a clear "
        "picture, call write_scope_analysis(text) with your findings, then call "
        "index_write(\"scope_analysis_complete\", true, null, \"high\") to finish."
    )
    await _run_gemini_with_tools(
        initial_ctx,
        state, index, queue,
        retry_attempts=retry_attempts,
        fallback_models_info=fallback_models_info,
        tools=_GEMINI_SCOPE_TOOLS,
        stop_keys={"scope_analysis_complete"},
    )
    logger.info(f"[quick_proposal] scope phase complete — {len(index.extracted_data.get('scope_analysis', ''))} chars written")
    await _emit(queue, "phase_complete", phase="scope")


def _kp_lookup(knowledge_pack: dict, query: str) -> str:
    """Fuzzy lookup for unit price distributions, price trends, item pair detail, or named KP sections."""
    import difflib

    distributions = knowledge_pack.get("unit_price_distributions", {})
    price_trends  = knowledge_pack.get("price_trends", {})
    pairs         = knowledge_pack.get("item_pairs", {}).get("pairs", [])

    q = query.strip()
    # Tool-call query strings containing an inch-mark (e.g. `4" SIDEWALKS`) sometimes
    # round-trip through the manager's JSON tool-call arguments with a stray backslash
    # before the quote (`4\" SIDEWALKS`). The fuzzy distributions match below tolerates
    # this by luck (difflib similarity), but the item_pairs exact-substring match does
    # not — it silently returns "no pairs found" even when the pair exists. Normalize
    # any run of backslashes immediately before a quote down to a plain quote so every
    # branch below sees the same clean string.
    q = re.sub(r'\\+"', '"', q)

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


_THINK_TAG_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL)


def _anthropic_native_url(url: str) -> str:
    """Force the native /v1/messages endpoint regardless of which URL form (native
    or OpenAI-compat) the caller resolved."""
    if url and url.endswith("/v1/chat/completions"):
        return url[: -len("/v1/chat/completions")] + "/v1/messages"
    return url


def _openai_tools_to_anthropic(tools: list) -> list:
    out = []
    for t in (tools or []):
        fn = t.get("function", t)
        out.append({
            "name":        fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _openai_messages_to_anthropic(messages: list) -> tuple[str, list]:
    """Convert the shared OpenAI-style mgr_messages list to Anthropic's native
    {system, messages} shape.

    Assistant turns tagged with `_anthropic_native_content` (the verbatim content
    blocks Anthropic returned for that turn, including its thinking block) are
    replayed as-is. This is required — Anthropic rejects a tool_use turn that's
    immediately followed by tool_result if the original thinking block for that
    turn is missing. Older/untagged assistant turns (e.g. resumed from DB, or
    generated by a non-Anthropic model) are reconstructed from text + tool_calls;
    Anthropic accepts that and trims any thinking requirement for non-immediate
    turns automatically.
    """
    system = ""
    out: list = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")
        if role == "system":
            system = m.get("content") or ""
            i += 1
        elif role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
            i += 1
        elif role == "assistant":
            native_blocks = m.get("_anthropic_native_content")
            if native_blocks:
                out.append({"role": "assistant", "content": native_blocks})
            else:
                blocks = []
                text = m.get("content")
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in (m.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    try:
                        tool_input = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tool_input = {}
                    blocks.append({
                        "type": "tool_use", "id": tc.get("id", ""),
                        "name": fn.get("name", ""), "input": tool_input,
                    })
                if not blocks:
                    blocks.append({"type": "text", "text": "(continuing)"})
                out.append({"role": "assistant", "content": blocks})
            i += 1
        elif role == "tool":
            # Anthropic requires every tool_result for a given assistant turn to
            # land in a single user message, not one user message per result.
            tool_blocks = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                tool_blocks.append({
                    "type":        "tool_result",
                    "tool_use_id": tm.get("tool_call_id", ""),
                    "content":     tm.get("content") or "",
                })
                i += 1
            out.append({"role": "user", "content": tool_blocks})
        else:
            i += 1
    return system, out


async def _stream_anthropic_native(
    url: str,
    headers: dict,
    payload: dict,
    queue: asyncio.Queue,
    mgr_model_id: str,
    log_path: str,
) -> dict:
    """Stream a native Anthropic /v1/messages call with extended thinking enabled.

    Anthropic's OpenAI-compat endpoint (/v1/chat/completions) does not surface
    extended thinking as a separate field, which is why Claude manager thinking
    chains were rendering as regular text bubbles instead of thinking bubbles
    (Ollama/vLLM models populate `reasoning_content` natively; Claude needs the
    native Messages API plus an explicit `thinking` request param). This emits
    the same claude_thinking_start/delta QP SSE events Ollama runs already use,
    so no frontend changes are needed.
    """
    url = _anthropic_native_url(url)
    system, anth_messages = _openai_messages_to_anthropic(payload.get("messages") or [])
    max_tokens    = payload.get("max_tokens", 8000)
    budget_tokens = max(1024, min(8000, max_tokens - 2000))

    body = {
        "model":      mgr_model_id,
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   anth_messages,
        "thinking":   {"type": "enabled", "budget_tokens": budget_tokens},
        "stream":     True,
    }
    anth_tools = _openai_tools_to_anthropic(payload.get("tools") or [])
    if anth_tools:
        body["tools"] = anth_tools

    req_headers = {**headers, "content-type": "application/json"}
    req_headers.setdefault("anthropic-version", "2023-06-01")
    if "x-api-key" not in req_headers and "authorization" not in {k.lower() for k in req_headers}:
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            req_headers["x-api-key"] = env_key

    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
    thinking_id         = uuid.uuid4().hex[:8]
    thinking_streaming  = False
    text_streaming      = False
    content_blocks: dict[int, dict] = {}
    raw_blocks: list    = []
    tool_calls_map: dict[int, dict] = {}
    usage: dict         = {}
    stop_reason: str | None = None
    thinking_buf = ""
    content_buf  = ""

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=req_headers, json=body) as resp:
                if not resp.is_success:
                    body_text = await resp.aread()
                    body_str  = body_text.decode("utf-8", errors="ignore")
                    logger.error(f"[quick_proposal] anthropic native manager HTTP {resp.status_code}: {body_str}")
                    return {"error": {"message": f"Model returned {resp.status_code}: {body_str}"}}
                _line_iter = resp.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await asyncio.wait_for(_line_iter.__anext__(), timeout=120.0)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        logger.warning("[quick_proposal] anthropic native stream idle >120s — model may have crashed")
                        return {"error": {"message": "Model stopped responding (no tokens for 120 seconds). The model may have crashed or run out of memory."}}
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    etype = data.get("type")

                    if etype == "message_start":
                        u = (data.get("message") or {}).get("usage") or {}
                        if u:
                            usage["prompt_tokens"] = u.get("input_tokens", 0)

                    elif etype == "content_block_start":
                        idx   = data.get("index", 0)
                        block = data.get("content_block") or {}
                        btype = block.get("type")
                        if btype == "tool_use":
                            content_blocks[idx] = {"type": "tool_use", "id": block.get("id", ""),
                                                    "name": block.get("name", ""), "json_buf": ""}
                        elif btype == "redacted_thinking":
                            content_blocks[idx] = {"type": "redacted_thinking", "data": block.get("data", "")}
                        else:
                            content_blocks[idx] = {"type": btype or "text", "text": "", "thinking": "", "signature": ""}

                    elif etype == "content_block_delta":
                        idx   = data.get("index", 0)
                        delta = data.get("delta") or {}
                        dtype = delta.get("type")
                        block = content_blocks.setdefault(idx, {"type": "text", "text": ""})
                        if dtype == "thinking_delta":
                            chunk = delta.get("thinking", "")
                            block["thinking"] = block.get("thinking", "") + chunk
                            if not thinking_streaming:
                                thinking_streaming = True
                                await _emit(queue, "extraction_message",
                                            role="claude_thinking_start", thinking_id=thinking_id, model=mgr_model_id)
                            await _emit(queue, "extraction_message",
                                        role="claude_thinking_delta", thinking_id=thinking_id,
                                        text=chunk, model=mgr_model_id)
                            thinking_buf += chunk
                        elif dtype == "signature_delta":
                            block["signature"] = block.get("signature", "") + delta.get("signature", "")
                        elif dtype == "text_delta":
                            chunk = delta.get("text", "")
                            block["text"] = block.get("text", "") + chunk
                            content_buf += chunk
                            if not text_streaming:
                                text_streaming = True
                                await _emit(queue, "extraction_message", role="claude_text_start", model=mgr_model_id)
                            await _emit(queue, "extraction_message",
                                        role="claude_text_delta", text=chunk, model=mgr_model_id)
                        elif dtype == "input_json_delta":
                            block["json_buf"] = block.get("json_buf", "") + delta.get("partial_json", "")

                    elif etype == "content_block_stop":
                        idx   = data.get("index", 0)
                        block = content_blocks.get(idx)
                        if not block:
                            continue
                        if block["type"] == "thinking":
                            raw_blocks.append({"type": "thinking", "thinking": block.get("thinking", ""),
                                                "signature": block.get("signature", "")})
                        elif block["type"] == "redacted_thinking":
                            raw_blocks.append({"type": "redacted_thinking", "data": block.get("data", "")})
                        elif block["type"] == "tool_use":
                            try:
                                tool_input = json.loads(block.get("json_buf") or "{}")
                            except json.JSONDecodeError:
                                tool_input = {}
                            raw_blocks.append({"type": "tool_use", "id": block.get("id", ""),
                                                "name": block.get("name", ""), "input": tool_input})
                            tool_calls_map[idx] = {
                                "id": block.get("id", ""), "type": "function",
                                "function": {"name": block.get("name", ""),
                                             "arguments": block.get("json_buf") or "{}"},
                            }
                        else:
                            raw_blocks.append({"type": "text", "text": block.get("text", "")})

                    elif etype == "message_delta":
                        d = data.get("delta") or {}
                        if d.get("stop_reason"):
                            stop_reason = d["stop_reason"]
                        u = data.get("usage") or {}
                        if u.get("output_tokens") is not None:
                            usage["completion_tokens"] = u.get("output_tokens", 0)

                    elif etype == "message_stop":
                        break

                    elif etype == "error":
                        err = data.get("error") or {}
                        return {"error": {"message": err.get("message", "Anthropic stream error")}}

    except httpx.HTTPStatusError as _http_err:
        logger.error(f"[quick_proposal] Unexpected HTTPStatusError (anthropic native): {_http_err}")
        return {"error": {"message": f"Model returned {_http_err.response.status_code}: (unexpected error)"}}
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as _conn_err:
        logger.error(f"[quick_proposal] anthropic native stream connection error: {_conn_err}")
        return {"error": {"message": f"Connection to model failed: {_conn_err}"}}

    if thinking_buf.strip() and log_path:
        _log_phase3_event(log_path, {
            "type": "extraction_message", "role": "claude_thinking",
            "text": thinking_buf.strip(), "model": mgr_model_id,
        })

    finish_reason   = {"max_tokens": "length", "tool_use": "tool_calls"}.get(stop_reason, "stop")
    tool_calls_list = [tool_calls_map[i] for i in sorted(tool_calls_map)] if tool_calls_map else None

    return {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {
                "content":    content_buf.strip() or None,
                "tool_calls": tool_calls_list,
            },
        }],
        "usage": usage,
        "_native_blocks": raw_blocks or None,
    }


async def _stream_manager_call(
    url: str,
    headers: dict,
    payload: dict,
    queue: asyncio.Queue,
    mgr_model_id: str,
    log_path: str,
) -> dict:
    """Stream an OpenAI-compat manager call, emitting thinking as a QP SSE event.

    Returns a dict with the same {choices, usage} shape as a non-streaming response
    so the caller loop requires no restructuring.

    Using read=None means no per-chunk timeout — as long as thinking tokens keep
    arriving the connection stays alive, which is the whole point.

    Anthropic models are routed to `_stream_anthropic_native` instead — its
    OpenAI-compat endpoint doesn't surface extended thinking as a separate field.
    """
    if "anthropic.com" in (url or "") or "anthropic-version" in (headers or {}):
        return await _stream_anthropic_native(url, headers, payload, queue, mgr_model_id, log_path)

    stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    thinking_id  = uuid.uuid4().hex[:8]
    thinking_buf = ""
    thinking_streaming = False   # True once we've sent the start event
    text_streaming = False        # True once we've sent the text_start event
    content_buf = ""
    tool_calls_map: dict[int, dict] = {}
    usage: dict = {}
    finish_reason: str | None = None

    req_headers = {**headers, "content-type": "application/json"}
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

    try:
      # Pre-flight: log payload details before sending
      try:
        payload_json = json.dumps(stream_payload)
        payload_size = len(payload_json.encode('utf-8'))
        logger.info(f"[quick_proposal] Sending manager request: {payload_size} bytes, {len(stream_payload.get('messages', []))} messages, {len(stream_payload.get('tools', []))} tools")
      except Exception as e:
        logger.error(f"[quick_proposal] Could not serialize payload to JSON: {e}")
        return {"error": {"message": f"Failed to serialize request payload: {e}"}}

      async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=req_headers, json=stream_payload) as resp:
            if not resp.is_success:
                # Read full body before raising so we have the error message
                body_text = await resp.aread()
                body_str = body_text.decode('utf-8', errors='ignore')
                logger.error(f"[quick_proposal] manager HTTP {resp.status_code}")
                logger.error(f"  Response body: {body_str}")
                logger.error(f"  Response headers: {dict(resp.headers)}")
                logger.error(f"  Request model: {payload.get('model')}")
                logger.error(f"  Request messages count: {len(payload.get('messages', []))}")
                logger.error(f"  Request tools count: {len(payload.get('tools', []))}")
                if payload.get('messages'):
                    total_chars = sum(len(str(m.get('content', ''))) for m in payload.get('messages', []))
                    logger.error(f"  Total message content chars: {total_chars}")
                return {"error": {"message": f"Model returned {resp.status_code}: {body_str}"}}
            _line_iter = resp.aiter_lines().__aiter__()
            while True:
                try:
                    line = await asyncio.wait_for(_line_iter.__anext__(), timeout=120.0)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("[quick_proposal] manager stream idle >120s — model may have crashed")
                    return {"error": {"message": "Model stopped responding (no tokens for 120 seconds). The model may have crashed or run out of memory."}}
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                if data.get("usage"):
                    usage = data["usage"]

                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}

                # Thinking tokens — Ollama/vLLM emit these as a separate field.
                # Stream them live: send a start event on the first chunk, then
                # delta events for each subsequent chunk so the UI fills in real-time.
                reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thinking")
                    or ""
                )
                if reasoning:
                    if not thinking_streaming:
                        thinking_streaming = True
                        await _emit(queue, "extraction_message",
                                    role="claude_thinking_start",
                                    thinking_id=thinking_id,
                                    model=mgr_model_id)
                    await _emit(queue, "extraction_message",
                                role="claude_thinking_delta",
                                thinking_id=thinking_id,
                                text=reasoning,
                                model=mgr_model_id)
                    thinking_buf += reasoning

                # Content tokens — may contain <think> tags for models that
                # embed thinking inline rather than in a separate field.
                content = delta.get("content") or ""
                if content:
                    content_buf += content
                    if not text_streaming:
                        text_streaming = True
                        await _emit(queue, "extraction_message",
                                    role="claude_text_start", model=mgr_model_id)
                    await _emit(queue, "extraction_message",
                                role="claude_text_delta", text=content, model=mgr_model_id)

                # Tool call argument chunks — accumulate by index.
                for tc_delta in (delta.get("tool_calls") or []):
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc_delta.get("id"):
                        tool_calls_map[idx]["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        tool_calls_map[idx]["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tool_calls_map[idx]["function"]["arguments"] += fn["arguments"]

    except httpx.HTTPStatusError as _http_err:
        # Fallback if somehow an HTTPStatusError still gets raised (shouldn't happen now)
        logger.error(f"[quick_proposal] Unexpected HTTPStatusError: {_http_err}")
        return {"error": {"message": f"Model returned {_http_err.response.status_code}: (unexpected error)"}}
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as _conn_err:
        logger.error(f"[quick_proposal] manager stream connection error: {_conn_err}")
        return {"error": {"message": f"Connection to model failed: {_conn_err}"}}

    # Inline <think> fallback — models that embed thinking in content rather than
    # a separate field. Emit as a single claude_thinking event (not streamed).
    inline_thinks = _THINK_TAG_RE.findall(content_buf)
    if inline_thinks and not thinking_buf.strip():
        thinking_buf = "\n\n".join(inline_thinks)
        await _emit(queue, "extraction_message",
                    role="claude_thinking", text=thinking_buf.strip(), model=mgr_model_id)
    clean_content = _THINK_TAG_RE.sub("", content_buf).strip()

    # Log the full accumulated thinking (both streaming and inline cases).
    if thinking_buf.strip() and log_path:
        _log_phase3_event(log_path, {
            "type": "extraction_message", "role": "claude_thinking",
            "text": thinking_buf.strip(), "model": mgr_model_id,
        })

    tool_calls_list = (
        [tool_calls_map[i] for i in sorted(tool_calls_map)] if tool_calls_map else None
    )

    return {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {
                "content": clean_content or None,
                "tool_calls": tool_calls_list,
            },
        }],
        "usage": usage,
    }


async def _fetch_context_window_async(chat_url: str, model_id: str) -> int:
    """Try to get context window from Ollama /api/show. Returns 0 if unsupported or failed."""
    base = re.sub(r'/v1(?:/.*)?$', '', chat_url)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(f"{base}/api/show", json={"model": model_id})
            if r.is_success:
                ctx = (r.json().get("model_info") or {}).get("llama.context_length")
                if ctx:
                    return int(ctx)
    except Exception:
        pass
    return 0


async def phase5_extraction_loop(index, queue: asyncio.Queue, manager_model: str = "", gemini_model: str = "", retry_attempts: int = 3, gemini_fallback_models: list | None = None, holdout_kp_path: str = "", resume: bool = False, session_id: str = "") -> None:
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
        raise RuntimeError("No manager endpoint found — add an endpoint in Settings")

    mgr_context_window = await _fetch_context_window_async(mgr_url, mgr_model_id) or 200000

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

    kp_label = holdout_kp_path if holdout_kp_path else str(_KP_PATH)
    kp_key_count = len(index.knowledge_pack) if index.knowledge_pack else 0
    logger.info(f"[quick_proposal] phase3 starting — KP: {kp_label} ({kp_key_count} top-level keys)")
    await _emit(queue, "manager_thinking", text=f"[Phase 3] Knowledge pack: {kp_label} ({kp_key_count} keys)")

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
        "log_path":    str(Path(RUNS_DIR) / index.run_id / "phase5_log.jsonl"),
        "image_store": image_store,
    }

    if not gemini_cache_name:
        gemini_state["messages"].append({
            "role":    "system",
            "content": gemini_phase3_prompt,
        })

    # Seed Gemini's history with Phase 1 index + project type + completeness as initial context.
    phase1_summary   = index.extracted_data.get("phase1_summary", "")
    project_type_val = index.extracted_values.get("project_type", {}).get("value", "")
    plan_completeness_val = index.extracted_values.get("plan_completeness", {}).get("value")
    completeness_notes_val = index.extracted_values.get("completeness_notes", {}).get("value", "")

    ctx_parts = ["Here is the Phase 1 classification index for this plan set:\n\n" + phase1_summary]
    if project_type_val:
        ctx_parts.append(f"\nProject type (from Phase 1 / UI picker): {project_type_val}")
    if plan_completeness_val:
        ctx_parts.append(
            "\nPlan completeness scores (from Phase 3):\n"
            + json.dumps(plan_completeness_val, indent=2)
        )
    if completeness_notes_val:
        ctx_parts.append(f"\nCompleteness notes: {completeness_notes_val}")
    ctx_parts.append("\n\nStand by for extraction instructions.")

    gemini_state["messages"].extend([
        {
            "role":    "user",
            "content": "\n".join(ctx_parts),
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
                "Returns shared index state as JSON. With no arguments (or section='values'), "
                "returns all extracted values — call this before asking Gemini to re-read "
                "something, the value may already be extracted. With section='notes', returns "
                "the full verbatim text of every general-notes/spec/list region, already "
                "transcribed by Gemini in a one-time pass before extraction started — call this "
                "ONCE near the start of Phase A and read it into context; you should rarely need "
                "to call it again this run. With section='scope', returns a dedicated scope-"
                "boundary analysis (which roads/areas/lots are actually in contract, phasing, "
                "expansions of existing infrastructure, etc.), also written in a one-time pass "
                "before extraction started — call this ONCE alongside section='notes'."
            ),
            "parameters": {
                "type":       "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["values", "notes", "scope"],
                        "description": "Which store to read. Defaults to 'values'.",
                    },
                },
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



    if resume and index.extracted_values:
        already_done = json.dumps(index.extracted_values, indent=2)
        # Append to the last user message — inserting a new user message here would
        # create two consecutive user turns, which Anthropic's API rejects.
        mgr_messages[-1]["content"] += (
            "\n\nRESUME: This run was interrupted and is being resumed. "
            "The following values were already extracted before the interruption:\n\n"
            f"```json\n{already_done}\n```\n\n"
            "Call read_index() to confirm the current index state, then continue "
            "extraction from where it left off. Skip any fields that are already present."
        )
        await _emit(queue, "manager_thinking",
                    text=f"[Resume] Restoring {len(index.extracted_values)} previously extracted values — picking up where we left off.")

    for i, m in enumerate(mgr_messages):
        logger.info(f"[quick_proposal] init mgr_messages[{i}] role={m['role']} chars={len(str(m.get('content') or ''))}")

    all_tools = [send_to_gemini_tool, read_index_tool, kp_lookup_tool, end_generation_tool]

    await _emit(queue, "phase_start", phase="phase5", label="Extracting values from plans…")

    phase_b_complete = False
    phase_b_nudges   = 0
    log_path         = gemini_state.get("log_path", "")

    try:
        for _ in range(60):
            oai_messages = [{"role": "system", "content": mgr_system}] + mgr_messages
            payload = {
                "model":      mgr_model_id,
                "max_tokens": 32000,
                "messages":   oai_messages,
                "tools":      all_tools,
            }
            resp = await _stream_manager_call(
                mgr_url, mgr_headers or {}, payload, queue, mgr_model_id, log_path
            )

            if "error" in resp:
                err_msg = resp.get("error", {}).get("message", str(resp))
                for attempt in range(1, 4):
                    if not _is_retryable_manager_error(err_msg):
                        break
                    wait_s = 2 ** (attempt + 1)  # 4, 8, 16
                    notice = f"Manager overloaded — retrying in {wait_s}s (attempt {attempt}/3)…"
                    logger.warning(f"[quick_proposal] {notice} err={err_msg}")
                    await _emit(queue, "extraction_message", role="retry_notice", text=notice)
                    await asyncio.sleep(wait_s)
                    resp = await _stream_manager_call(
                        mgr_url, mgr_headers or {}, payload, queue, mgr_model_id, log_path
                    )
                    if "error" not in resp:
                        break
                    err_msg = resp.get("error", {}).get("message", str(resp))

            if "error" in resp:
                err_msg = resp.get("error", {}).get("message", str(resp))
                logger.error(f"[quick_proposal] manager error in phase3: {err_msg}")
                raise RuntimeError(f"Manager error: {err_msg}")

            # ── Parse response ─────────────────────────────────────────────────
            usage         = resp.get("usage", {})
            choice        = resp.get("choices", [{}])[0]
            finish_reason = choice.get("finish_reason")
            msg           = choice.get("message", {})
            text_content  = msg.get("content") or ""
            tool_calls    = msg.get("tool_calls") or []

            _msg_metrics: dict = {}
            if usage:
                _ctx_payload = dict(role="claude", model=mgr_model_id,
                                     input_tokens=usage.get("prompt_tokens", 0),
                                     output_tokens=usage.get("completion_tokens", 0),
                                     context_window=mgr_context_window)
                await _emit(queue, "context_usage", **_ctx_payload)
                _save_context_usage(index.run_id, "claude", _ctx_payload)
                _ctx_pct = (round(_ctx_payload["input_tokens"] / mgr_context_window * 100, 1)
                            if mgr_context_window else None)
                # Persisted alongside the message (not just the run-level context_usage
                # store) so the per-message stats footer survives a hard refresh instead
                # of only showing live during the original stream.
                _msg_metrics = {
                    "input_tokens": _ctx_payload["input_tokens"],
                    "output_tokens": _ctx_payload["output_tokens"],
                    "context_percent": _ctx_pct,
                    "context_length": mgr_context_window,
                    "usage_source": "real",
                }

            if text_content.strip():
                await _emit(queue, "extraction_message", role="claude", text=text_content, model=mgr_model_id)
                if log_path:
                    _log_phase3_event(log_path, {"type": "extraction_message", "role": "claude",
                                                  "text": text_content, "model": mgr_model_id})
            else:
                # No visible content (all tokens were in <think>/<thinking> blocks or whitespace).
                # Signal the frontend to remove the live streaming bubble so it doesn't become stale.
                await _emit(queue, "extraction_message", role="claude_text_end", model=mgr_model_id)

            assistant_msg: dict = {"role": "assistant"}
            # Only include content if non-empty; Ollama's OpenAI-compat rejects null content
            if text_content.strip():
                assistant_msg["content"] = text_content
            elif not tool_calls:
                # If no tool calls and no text, still need some content (use empty string)
                assistant_msg["content"] = ""
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            _native_blocks = resp.get("_native_blocks")
            if _native_blocks:
                assistant_msg["_anthropic_native_content"] = _native_blocks
            mgr_messages.append(assistant_msg)
            _save_chat_message(session_id, "assistant", text_content or "",
                               {"model": mgr_model_id, **_msg_metrics,
                                **({"tool_calls": tool_calls} if tool_calls else {})})

            logger.info(f"[quick_proposal] manager finish_reason={finish_reason} output_tokens={usage.get('completion_tokens', '?')}")

            if finish_reason == "length":
                logger.warning("[quick_proposal] manager output truncated (finish_reason=length) — sending continuation")
                await _emit(queue, "extraction_message", role="claude",
                            text="*(output truncated — continuing…)*", model=mgr_model_id)
                _extraction_done = bool(index.extracted_values.get("extraction_complete"))
                if _extraction_done:
                    _nudge = (
                        "Your proposal output was cut off mid-generation. "
                        "Continue writing the proposal exactly where you left off — do not repeat anything already written. "
                        "Once the full proposal is complete, call end_generation(grand_total) with the final Grand Total to finish the pipeline."
                    )
                else:
                    _nudge = "Continue exactly where you left off. Do not repeat anything already written."
                mgr_messages.append({"role": "user", "content": _nudge})
                _save_chat_message(session_id, "user", _nudge, {"source": "pipeline_nudge"})
                continue

            if not tool_calls:
                # Text-only stop with no tool calls.
                if phase_b_nudges < 2:
                    phase_b_nudges += 1
                    extraction_done = bool(index.extracted_values.get("extraction_complete"))
                    if not extraction_done:
                        _nudge_text = (
                            "Gemini has not yet called extraction_complete. "
                            "Use send_to_gemini to send your QC feedback or redirect to Gemini — "
                            "do not write instructions for Gemini as plain text. "
                            "All communication with Gemini must go through the send_to_gemini tool."
                        )
                    else:
                        _nudge_text = (
                            "Generate the complete Phase B proposal if not already done, "
                            "then call end_generation(grand_total) with the final Grand Total "
                            "to complete the pipeline."
                        )
                    mgr_messages.append({"role": "user", "content": _nudge_text})
                    _save_chat_message(session_id, "user", _nudge_text, {"source": "pipeline_nudge"})
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
                    await _emit(queue, "extraction_message",
                                role="tool_call", tool_id=tool_id, tool="send_to_gemini", model=mgr_model_id,
                                args=json.dumps({"message": msg_text[:300]}))
                    gemini_resp = await _run_gemini_with_tools(
                        msg_text, gemini_state, index, queue,
                        retry_attempts=retry_attempts,
                        fallback_models_info=fallback_models_info or None,
                        session_id=session_id,
                    )
                    await _emit(queue, "extraction_message",
                                role="tool_result", tool_id=tool_id, tool="send_to_gemini", model=mgr_model_id,
                                result=gemini_resp or "(no response from Gemini)")
                    if gemini_resp.strip():
                        await _emit(queue, "extraction_message", role="gemini", text=gemini_resp)
                        if log_path:
                            _log_phase3_event(log_path, {"type": "extraction_message", "role": "gemini", "text": gemini_resp})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id,
                                         "content": gemini_resp or "(no response from Gemini)"})
                    _save_chat_message(session_id, "tool", gemini_resp or "(no response from Gemini)",
                                       {"tool_call_id": tool_id, "tool_name": "send_to_gemini"})
                elif tool_name == "read_index":
                    section = tool_input.get("section", "values")
                    if section == "notes":
                        index_json = json.dumps(index.extracted_data.get("notes_text", {}), indent=2)
                    elif section == "scope":
                        index_json = json.dumps(index.extracted_data.get("scope_analysis", ""), indent=2)
                    else:
                        index_json = json.dumps(index.extracted_values, indent=2)
                    await _emit(queue, "extraction_message",
                                role="tool_call", tool_id=tool_id, tool="read_index", model=mgr_model_id,
                                args=json.dumps({"section": section}))
                    await _emit(queue, "extraction_message",
                                role="tool_result", tool_id=tool_id, tool="read_index", model=mgr_model_id, result=index_json)
                    if log_path:
                        _log_phase3_event(log_path, {"type": "extraction_message", "role": "tool_result",
                                                      "tool_id": tool_id, "tool": "read_index", "result": index_json})
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": index_json})
                    _save_chat_message(session_id, "tool", index_json,
                                       {"tool_call_id": tool_id, "tool_name": "read_index"})
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
                    _save_chat_message(session_id, "tool", lookup_result,
                                       {"tool_call_id": tool_id, "tool_name": "kp_lookup"})
                elif tool_name == "end_generation":
                    grand_total = tool_input.get("grand_total")
                    await _emit(queue, "grand_total", amount=grand_total, model=mgr_model_id)
                    if log_path:
                        _log_phase3_event(log_path, {"type": "grand_total", "amount": grand_total})
                    _end_content = f"Pipeline complete. Grand Total recorded: ${grand_total:,.2f}" if grand_total is not None else "Pipeline complete."
                    mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": _end_content})
                    _save_chat_message(session_id, "tool", _end_content,
                                       {"tool_call_id": tool_id, "tool_name": "end_generation"})
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

    await _emit(queue, "phase_complete", phase="phase5")


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
    import_notes_from_run_id: str = "",
    import_scope_from_run_id: str = "",
    project_type: str = "",
    session_id: str = "",
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

        # Step 2.5 — Write project_type if provided by UI picker; else run Phase 1 detection
        if project_type:
            index.extracted_values["project_type"] = {
                "value": project_type, "source_bbox_id": None, "confidence": "high",
            }
            await _emit(queue, "index_update", key="project_type", value=project_type,
                        source_bbox_id=None, confidence="high")
            _save_extracted_values(run_id, index.extracted_values)
            logger.info(f"[quick_proposal] project_type set from UI picker: {project_type}")
        else:
            await phase1_detect_job_type(
                index, queue,
                gemini_model=gemini_model,
                retry_attempts=gemini_retry_attempts,
                gemini_fallback_models=gemini_fallback_models,
            )

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

            await _emit(queue, "phase_start", phase="phase2", label="Importing classifications…", cached=False)
            await _emit(queue, "phase_complete", phase="phase2")
            logger.info(f"[quick_proposal] imported classifications from run {import_from_run_id}")
        else:
            await _wait_for_gate(run_id, queue, "classify", "Classify Pages")
            await phase2_classify_pages(index, queue, model_override=gemini_model, retry_attempts=gemini_retry_attempts, fallback_models=gemini_fallback_models)
        _save_run_results(run_id, index)  # persist phase1 classifications + bboxes

        # Step 3.5 — Build phase1_summary (needed by Phase 3), then gate → Phase 3
        index.extracted_data["phase1_summary"] = _build_phase1_summary(index)

        await _wait_for_gate(run_id, queue, "phase2", "Completeness Scoring")

        await phase3_completeness_score(
            index, queue,
            gemini_model=gemini_model,
            retry_attempts=gemini_retry_attempts,
            gemini_fallback_models=gemini_fallback_models,
        )

        # Step 4 — Phase 2: (phase1_summary already built above; just emit events)
        await _emit(queue, "phase_start", phase="phase4", label="Building extraction index…")
        await _emit(queue, "phase_complete", phase="phase4")

        # Step 4.5 — one-pass verbatim notes transcription (or import from a previous run)
        if import_notes_from_run_id:
            src_notes_path = Path(RUNS_DIR) / import_notes_from_run_id / "results.json"
            if not src_notes_path.is_file():
                raise RuntimeError(f"Import run {import_notes_from_run_id} has no saved results")
            src_notes = json.loads(src_notes_path.read_text(encoding="utf-8"))
            notes_text = (src_notes.get("extracted_data") or {}).get("notes_text") or {}
            index.extracted_data["notes_text"] = notes_text
            _save_notes_text(run_id, notes_text)
            await _emit(queue, "phase_start", phase="notes", label="Importing notes…", cached=False)
            await _emit(queue, "phase_complete", phase="notes")
            logger.info(f"[quick_proposal] imported {len(notes_text)} notes from run {import_notes_from_run_id}")
        else:
            await phase_notes_extraction(
                index, queue,
                gemini_model=gemini_model,
                retry_attempts=gemini_retry_attempts,
                gemini_fallback_models=gemini_fallback_models,
            )

        # Step 4.6 — scope analysis (or import from a previous run)
        if import_scope_from_run_id:
            src_scope_path = Path(RUNS_DIR) / import_scope_from_run_id / "results.json"
            if not src_scope_path.is_file():
                raise RuntimeError(f"Import run {import_scope_from_run_id} has no saved results")
            src_scope = json.loads(src_scope_path.read_text(encoding="utf-8"))
            scope_text = (src_scope.get("extracted_data") or {}).get("scope_analysis") or ""
            index.extracted_data["scope_analysis"] = scope_text
            _save_scope_analysis(run_id, scope_text)
            await _emit(queue, "phase_start", phase="scope", label="Importing scope analysis…", cached=False)
            await _emit(queue, "phase_complete", phase="scope")
            logger.info(f"[quick_proposal] imported scope analysis ({len(scope_text)} chars) from run {import_scope_from_run_id}")
        else:
            await phase_scope_analysis(
                index, queue,
                gemini_model=gemini_model,
                retry_attempts=gemini_retry_attempts,
                gemini_fallback_models=gemini_fallback_models,
            )

        # Gate before Phase 3
        await _wait_for_gate(run_id, queue, "phase3", "Extraction")

        # Step 5 — Phase 3: manager LLM + Gemini extraction tool loop
        await phase5_extraction_loop(index, queue, manager_model=manager_model, gemini_model=gemini_model, retry_attempts=gemini_retry_attempts, gemini_fallback_models=gemini_fallback_models, holdout_kp_path=holdout_kp_path, session_id=session_id)
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
            _save_generation_snapshot(run_id, session_id, manager_model, gemini_model, holdout_kp_path)
        queue.put_nowait(None)


# ── Routes ─────────────────────────────────────────────────────────────────────

def _load_index_from_run(run_id: str, meta: dict):
    """Reconstruct a ChatIndex from a completed run's results.json for phase-6 chat."""
    from src.quick_proposal.index import ChatIndex, PageRecord, BboxRecord
    results_path = Path(RUNS_DIR) / run_id / "results.json"
    if not results_path.is_file():
        return None
    data = json.loads(results_path.read_text(encoding="utf-8"))

    pages_dir = Path(RUNS_DIR) / run_id / "pages"
    pages = [
        PageRecord(
            idx=p["idx"],
            image_path=str(pages_dir / f"page_{p['idx']:04d}.jpg"),
            classification=p.get("sheet_type", ""),
            importance=p.get("importance", ""),
            description=p.get("description", ""),
            bbox_ids=p.get("bbox_ids", []),
        )
        for p in data.get("pages", [])
    ]

    bboxes = {
        bid: BboxRecord(
            id=rec["id"],
            page_idx=rec["page_idx"],
            x1=rec["x1"], y1=rec["y1"],
            x2=rec["x2"], y2=rec["y2"],
            parent_id=rec.get("parent_id"),
            depth=rec.get("depth", 0),
            description=rec.get("description", ""),
            element_type=rec.get("element_type"),
            element_subtype=rec.get("element_subtype"),
            importance=rec.get("importance"),
        )
        for bid, rec in data.get("bboxes", {}).items()
    }

    index = ChatIndex(run_id=run_id)
    index.pages = pages
    index.bboxes = bboxes
    index.extracted_values = data.get("extracted_values", {})
    index.extracted_data = data.get("extracted_data", {})
    index.knowledge_pack = _load_knowledge_pack(meta.get("holdout_kp_path") or None)
    upload_id = meta.get("upload_id", "")
    if upload_id:
        try:
            index.source_path = _resolve_upload_path(upload_id)
            index.source_is_pdf = index.source_path.lower().endswith(".pdf")
        except FileNotFoundError:
            logger.warning(f"[qp_chat] source upload '{upload_id}' not found for run={run_id}; image tools will fail")
    return index


async def _qp_continuation_task(
    run_id: str,
    session_id: str,
    meta: dict,
    user_message: str,
    manager_model: str,
    queue: asyncio.Queue,
) -> None:
    """Drive one user turn of phase-6 chat: load history, call manager, handle tools."""
    from core.database import ChatMessage as DbChatMessage, SessionLocal as _SL

    # Resolve manager endpoint (prefer override, fall back to meta, then Anthropic default).
    mgr_url = mgr_headers = mgr_api_key = mgr_model_id = None
    effective_model = manager_model or meta.get("manager_model", "")
    if effective_model:
        found = _get_endpoint_for_model(effective_model)
        if found:
            mgr_url, mgr_headers, mgr_api_key, mgr_model_id = found
            mgr_url = _to_openai_compat_url(mgr_url)

    if not mgr_url:
        try:
            db = _SL()
            try:
                ep = db.query(ModelEndpoint).filter(
                    ModelEndpoint.base_url.ilike("%anthropic.com%")
                ).first()
                if ep:
                    from src.endpoint_resolver import resolve_endpoint_runtime, build_chat_url, build_headers as _build_headers
                    base, mgr_api_key = resolve_endpoint_runtime(ep)
                    mgr_url      = build_chat_url(base)
                    mgr_headers  = _build_headers(mgr_api_key, base)
                    mgr_model_id = getattr(ep, "model", None) or _CLAUDE_MODEL
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[qp_chat] could not load manager endpoint: {e}")
    mgr_url      = mgr_url      or "https://api.anthropic.com/v1/chat/completions"
    mgr_model_id = mgr_model_id or _CLAUDE_MODEL
    mgr_context_window = await _fetch_context_window_async(mgr_url, mgr_model_id) or 200000

    # Load chat history from DB.
    db = _SL()
    try:
        rows = db.query(DbChatMessage).filter(
            DbChatMessage.session_id == session_id
        ).order_by(DbChatMessage.timestamp).all()
    finally:
        db.close()

    mgr_system = ""
    mgr_messages: list = []
    for row in rows:
        meta_d = json.loads(row.meta_data) if row.meta_data else {}
        if row.role == "system":
            mgr_system = row.content
            continue
        if row.role == "tool" and not meta_d.get("tool_call_id"):
            # Gemini's own internal sub-tool calls (enhance_region, crop_page, etc.)
            # get persisted as role="tool" rows with source=gemini_subtool for
            # display/history purposes, but they're not part of the manager's own
            # tool_use/tool_result pairing — replaying them with an empty
            # tool_call_id/tool_use_id fails Anthropic's ID validation.
            continue
        msg: dict = {"role": row.role, "content": row.content or ""}
        if row.role == "assistant":
            raw_tc = meta_d.get("tool_calls")
            if raw_tc:
                msg["tool_calls"] = raw_tc
                msg["content"] = row.content or ""
        elif row.role == "tool":
            msg["tool_call_id"] = meta_d.get("tool_call_id", "")
        mgr_messages.append(msg)

    # Append the new human turn.
    mgr_messages.append({"role": "user", "content": user_message, "name": "user"})
    _save_chat_message(session_id, "user", user_message, {"name": "user"})

    # Reconstruct index + gemini_state for tool execution.
    index = _load_index_from_run(run_id, meta)
    if index is None:
        await _emit(queue, "error", message="Run results not found — cannot continue chat.", phase="phase6")
        return

    gemini_model = meta.get("gemini_model", "")
    if gemini_model:
        found = _get_endpoint_for_model(gemini_model)
        if found:
            gemini_url, gemini_headers, gemini_api_key, _unused = found
        else:
            gemini_url, gemini_headers, gemini_api_key, _unused = _get_gemini_endpoint()
    else:
        gemini_url, gemini_headers, gemini_api_key, gemini_model = _get_gemini_endpoint()

    gemini_phase3_prompt = (_PROMPTS_DIR / "gemini_phase3.txt").read_text(encoding="utf-8")
    image_store: dict = {}
    for page in index.pages:
        try:
            image_store[f"p{page.idx}_thumbnail"] = {
                "bytes": Path(page.image_path).read_bytes(),
                "desc":  f"Page {page.idx} thumbnail",
            }
        except Exception:
            pass

    log_path = str(Path(RUNS_DIR) / run_id / "phase5_log.jsonl")
    gemini_state: dict = {
        "messages":    [{"role": "system", "content": gemini_phase3_prompt}],
        "url":         gemini_url,
        "headers":     gemini_headers,
        "model":       gemini_model,
        "cache_name":  None,
        "log_path":    log_path,
        "image_store": image_store,
    }

    all_tools = [
        {
            "type": "function",
            "function": {
                "name": "send_to_gemini",
                "description": "Sends a message to the Gemini extraction sub-agent and returns its response.",
                "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_index",
                "description": (
                    "Returns shared index state as JSON. With no arguments (or section='values'), "
                    "returns all extracted values. With section='notes', returns the full verbatim "
                    "text of every general-notes/spec/list region transcribed during extraction. "
                    "With section='scope', returns the scope-boundary analysis written before "
                    "extraction started."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "enum": ["values", "notes", "scope"]},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "kp_lookup",
                "description": "Look up knowledge pack data on demand.",
                "parameters": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]},
            },
        },
    ]

    # Drive manager loop (up to 10 iterations for multi-step tool calls).
    for _ in range(10):
        oai_messages = [{"role": "system", "content": mgr_system}] + mgr_messages
        payload = {"model": mgr_model_id, "max_tokens": 16000, "messages": oai_messages, "tools": all_tools}
        resp = await _stream_manager_call(mgr_url, mgr_headers or {}, payload, queue, mgr_model_id, log_path)

        if "error" in resp:
            await _emit(queue, "error", message=resp.get("error", {}).get("message", str(resp)), phase="phase6")
            return

        choice        = resp.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason")
        msg           = choice.get("message", {})
        text_content  = msg.get("content") or ""
        tool_calls    = msg.get("tool_calls") or []
        usage         = resp.get("usage", {})

        # Emit + persist token/context usage the same way phase5_extraction_loop does,
        # so the message footer shows live stats AND they survive a hard refresh
        # (previously phase-6 continuation chat never tracked this at all — the
        # per-message metadata only ever had "model").
        _msg_metrics: dict = {}
        if usage:
            _ctx_payload = dict(role="claude", model=mgr_model_id,
                                 input_tokens=usage.get("prompt_tokens", 0),
                                 output_tokens=usage.get("completion_tokens", 0),
                                 context_window=mgr_context_window)
            await _emit(queue, "context_usage", **_ctx_payload)
            _save_context_usage(run_id, "claude", _ctx_payload)
            _ctx_pct = (round(_ctx_payload["input_tokens"] / mgr_context_window * 100, 1)
                        if mgr_context_window else None)
            _msg_metrics = {
                "input_tokens": _ctx_payload["input_tokens"],
                "output_tokens": _ctx_payload["output_tokens"],
                "context_percent": _ctx_pct,
                "context_length": mgr_context_window,
                "usage_source": "real",
            }

        if text_content.strip():
            await _emit(queue, "extraction_message", role="claude", text=text_content, model=mgr_model_id)

        assistant_msg: dict = {"role": "assistant", "content": text_content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        _native_blocks = resp.get("_native_blocks")
        if _native_blocks:
            assistant_msg["_anthropic_native_content"] = _native_blocks
        mgr_messages.append(assistant_msg)
        _save_chat_message(session_id, "assistant", text_content or "",
                           {"model": mgr_model_id, **_msg_metrics,
                            **({"tool_calls": tool_calls} if tool_calls else {})})

        # A long answer (e.g. a full actuals-vs-predicted reconciliation) can hit the
        # 16000-token output cap mid-sentence with no tool call requested — `not tool_calls`
        # below would treat that as "the manager is done" and silently truncate the reply.
        # Mirrors phase5_extraction_loop's identical handling of finish_reason == "length".
        if finish_reason == "length":
            logger.warning("[qp_chat] manager output truncated (finish_reason=length) — sending continuation")
            await _emit(queue, "extraction_message", role="claude",
                        text="*(output truncated — continuing…)*", model=mgr_model_id)
            _nudge = ("Your reply was cut off mid-generation. Continue exactly where you left off — "
                      "do not repeat anything already written.")
            mgr_messages.append({"role": "user", "content": _nudge})
            _save_chat_message(session_id, "user", _nudge, {"source": "pipeline_nudge"})
            continue

        if not tool_calls:
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
                await _emit(queue, "extraction_message",
                            role="tool_call", tool_id=tool_id, tool="send_to_gemini", model=mgr_model_id,
                            args=json.dumps({"message": msg_text[:300]}))
                gemini_resp = await _run_gemini_with_tools(msg_text, gemini_state, index, queue, session_id=session_id)
                await _emit(queue, "extraction_message",
                            role="tool_result", tool_id=tool_id, tool="send_to_gemini", model=mgr_model_id,
                            result=gemini_resp or "(no response)")
                if gemini_resp.strip():
                    await _emit(queue, "extraction_message", role="gemini", text=gemini_resp)
                result = gemini_resp or "(no response)"
                _save_chat_message(session_id, "tool", result, {"tool_call_id": tool_id, "tool_name": "send_to_gemini"})
            elif tool_name == "read_index":
                section = tool_input.get("section", "values")
                if section == "notes":
                    result = json.dumps(index.extracted_data.get("notes_text", {}), indent=2)
                elif section == "scope":
                    result = json.dumps(index.extracted_data.get("scope_analysis", ""), indent=2)
                else:
                    result = json.dumps(index.extracted_values, indent=2)
                _save_chat_message(session_id, "tool", result, {"tool_call_id": tool_id, "tool_name": "read_index"})
            elif tool_name == "kp_lookup":
                item_query = tool_input.get("item", "").strip()
                result = _kp_lookup(index.knowledge_pack or {}, item_query)
                _save_chat_message(session_id, "tool", result, {"tool_call_id": tool_id, "tool_name": "kp_lookup"})
            else:
                result = f"Unknown tool: {tool_name}"
            mgr_messages.append({"role": "tool", "tool_call_id": tool_id, "content": result})

    await _emit(queue, "phase_complete", phase="phase6")


def setup_quick_proposal_routes(session_manager=None):
    global _session_manager
    _session_manager = session_manager
    router = APIRouter(prefix="/api/quick_proposal", tags=["quick_proposal"])

    @router.post("/start-proposal-session")
    async def start_proposal_session(req: RunRequest, request: Request):
        """Create a real chat Session linked to a QP run, then start the pipeline.

        Returns {session_id, run_id}. The frontend navigates to #<session_id>
        and opens the SSE stream on /stream/<run_id> as usual.
        """
        from src.auth_helpers import effective_user
        if session_manager is None:
            raise HTTPException(status_code=500, detail="session_manager not available")

        run_id     = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        owner      = effective_user(request)

        os.makedirs(os.path.join(RUNS_DIR, run_id, "pages"), exist_ok=True)
        _save_run_meta(
            run_id,
            id=run_id,
            session_id=session_id,
            upload_id=req.upload_id,
            filename=req.filename or req.upload_id,
            run_name=req.run_name,
            notes=req.notes,
            timestamp=int(time.time()),
            status="running",
            holdout_kp_path=req.holdout_kp_path or "",
            gemini_model=req.gemini_model or "",
            manager_model=req.manager_model or "",
        )

        # Resolve manager endpoint for session model/url metadata.
        mgr_url = mgr_model_id = None
        if req.manager_model:
            found = _get_endpoint_for_model(req.manager_model)
            if found:
                mgr_url, _, _, mgr_model_id = found
                mgr_url = _to_openai_compat_url(mgr_url)
        if not mgr_url:
            try:
                db = SessionLocal()
                try:
                    ep = db.query(ModelEndpoint).filter(
                        ModelEndpoint.base_url.ilike("%anthropic.com%")
                    ).first()
                    if ep:
                        from src.endpoint_resolver import resolve_endpoint_runtime, build_chat_url
                        base, _ = resolve_endpoint_runtime(ep)
                        mgr_url      = build_chat_url(base)
                        mgr_model_id = getattr(ep, "model", None) or _CLAUDE_MODEL
                finally:
                    db.close()
            except Exception:
                pass
        mgr_url      = mgr_url      or "https://api.anthropic.com/v1/chat/completions"
        mgr_model_id = mgr_model_id or _CLAUDE_MODEL

        session_manager.create_session(
            session_id,
            name=req.run_name or req.filename or req.upload_id,
            endpoint_url=mgr_url,
            model=mgr_model_id,
            owner=owner,
        )

        # Stamp proposal_run_id on the DB row so the frontend can detect proposal sessions.
        db = SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row:
                row.proposal_run_id = run_id
                db.commit()
        finally:
            db.close()

        queue = _RunBroadcaster()
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
            import_notes_from_run_id=req.import_notes_from_run_id,
            import_scope_from_run_id=req.import_scope_from_run_id,
            project_type=req.project_type,
            session_id=session_id,
        ))
        _active_tasks[run_id] = task
        return {"session_id": session_id, "run_id": run_id}

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
            holdout_kp_path=req.holdout_kp_path or "",
        )

        queue = _RunBroadcaster()
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
            import_notes_from_run_id=req.import_notes_from_run_id,
            import_scope_from_run_id=req.import_scope_from_run_id,
            project_type=req.project_type,
        ))
        _active_tasks[run_id] = task
        return {"run_id": run_id}

    @router.post("/advance-phase")
    async def advance_phase(req: AdvancePhaseRequest):
        """Called by the frontend to unblock a pipeline waiting at a phase gate.
        When auto-mode is ON, the frontend calls this immediately on phase_gate.
        When auto-mode is OFF, the user clicks a gate button which calls this."""
        event = _active_gates.get(req.run_id)
        if event:
            event.set()
            return {"status": "ok", "run_id": req.run_id}
        return {"status": "no_gate", "run_id": req.run_id}

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
        status = meta.get("status", "unknown")
        # If meta says running but no active queue exists (server restart), auto-correct.
        if status == "running" and run_id not in _active_runs:
            results_path = Path(RUNS_DIR) / run_id / "results.json"
            if results_path.is_file():
                results = json.loads(results_path.read_text(encoding="utf-8"))
                ev = results.get("extracted_values") or {}
                corrected = "complete" if any(k not in ("project_type", "plan_completeness", "completeness_notes", "extraction_complete") for k in ev) else "cancelled"
            else:
                corrected = "cancelled"
            _save_run_meta(run_id, status=corrected)
            status = corrected
        return {
            "run_id":    run_id,
            "status":    status,
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
                with httpx.Client(timeout=60.0) as client:
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
            # Normalize: lowercase, spaces/hyphens → underscores.
            slug = job_name.lower().replace(" ", "_").replace("-", "_")
            holdout_kp = None
            for d in holdout_dirs:
                # Strip the job-number prefix ("holdout_26013-2_") to get the short
                # name ("solara"), then match bidirectionally: short name in slug OR
                # slug in full dir name. This handles truncated dir names (e.g.
                # "boyds" matching "boyds_landing") and PH suffixes (e.g. "solara"
                # matching "solara_ph1") without requiring exact slug equality.
                dir_short = d.name.split("_", 2)[-1]  # everything after "holdout_NNNNN-N_"
                if dir_short in slug or slug in d.name:
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

    @router.post("/runs/{run_id}/phase5")
    async def start_phase5_only(run_id: str, req: Phase3OnlyRequest):
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
        # Resolve holdout KP: request wins; fall back to what was saved at run start.
        resolved_holdout = req.holdout_kp_path or meta.get("holdout_kp_path", "") or ""
        index.knowledge_pack = _load_knowledge_pack(resolved_holdout or None)
        index.case_library   = _load_case_library()
        _save_run_meta(run_id, holdout_kp_path=resolved_holdout)

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

        saved_ev = results.get("extracted_values") or {}
        if req.resume:
            if saved_ev:
                index.extracted_values = saved_ev
        else:
            for key in ("project_type", "plan_completeness", "completeness_notes"):
                if key in saved_ev:
                    index.extracted_values[key] = saved_ev[key]

        # reuse_notes: carry over the prior run's verbatim notes transcription so
        # phase_notes_extraction's own "already populated" check (see its docstring)
        # skips re-transcribing every notes/list region — the plans haven't changed,
        # so this is a pure API-call/time savings independent of `resume`.
        if req.reuse_notes:
            saved_notes = (results.get("extracted_data") or {}).get("notes_text")
            if saved_notes:
                index.extracted_data["notes_text"] = saved_notes

        # Scope analysis describes a static property of this same plan set (unlike notes,
        # it doesn't need an opt-in toggle) — always carry it forward so phase_scope_analysis's
        # "already populated" check skips re-running Gemini on every phase5-only rerun.
        saved_scope = (results.get("extracted_data") or {}).get("scope_analysis")
        if saved_scope:
            index.extracted_data["scope_analysis"] = saved_scope

        # Write the starting extracted_values to disk immediately so the frontend
        # seed fetch (which runs right after this request returns) sees the correct
        # state rather than stale values from the previous run.
        results["extracted_values"] = dict(index.extracted_values)
        results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

        session_id = meta.get("session_id", "")

        # On a fresh rerun (not resume), truncate phase5_log.jsonl too — it's opened
        # in append mode on every write, so without this a rerun's entries pile up
        # on top of every prior attempt's (including ones made with a different
        # manager_model), making the log look like one run mixed multiple models.
        if not req.resume:
            try:
                (Path(RUNS_DIR) / run_id / "phase5_log.jsonl").write_text("", encoding="utf-8")
            except Exception as _e:
                logger.warning(f"[quick_proposal] failed to truncate phase5_log before rerun: {_e}")

        # On a fresh rerun (not resume), wipe the session's chat history so a
        # hard refresh shows only the new run's messages — not all prior runs'.
        if not req.resume and session_id:
            from core.database import ChatMessage as _DbMsg, SessionLocal as _SL2
            _db2 = _SL2()
            try:
                _db2.query(_DbMsg).filter(_DbMsg.session_id == session_id).delete()
                _db2.commit()
            except Exception as _e:
                logger.warning(f"[quick_proposal] failed to clear session messages before rerun: {_e}")
                _db2.rollback()
            finally:
                _db2.close()
            if _session_manager is not None:
                try:
                    sess = _session_manager.get_session(session_id)
                    if sess:
                        sess.history = []
                except Exception:
                    pass

        queue = _RunBroadcaster()
        _active_runs[run_id] = queue
        _save_run_meta(run_id, status="running")

        task = asyncio.create_task(_run_phase5_only(index, queue, run_id, manager_model=req.manager_model, gemini_model=req.gemini_model, retry_attempts=req.gemini_retry_attempts, gemini_fallback_models=req.gemini_fallback_models or None, holdout_kp_path=req.holdout_kp_path, resume=req.resume, completeness_only=req.completeness_only, session_id=session_id))
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

    @router.delete("/runs/{run_id}")
    async def delete_run(run_id: str):
        run_dir = Path(RUNS_DIR) / run_id
        if not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="Run not found")
        task = _active_tasks.pop(run_id, None)
        if task and not task.done():
            task.cancel()
        _active_runs.pop(run_id, None)
        await asyncio.to_thread(shutil.rmtree, run_dir, ignore_errors=True)
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

    @router.get("/runs/{run_id}/phase5_log")
    async def get_phase3_log(run_id: str):
        log_path = Path(RUNS_DIR) / run_id / "phase5_log.jsonl"
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
        broadcaster = _active_runs.get(run_id)
        if broadcaster is None:
            raise HTTPException(404, f"Run {run_id} not found")

        # Each connection gets its own queue subscribed to the run's broadcaster, so
        # multiple tabs watching the same run each see the full event stream instead of
        # splitting it (see _RunBroadcaster docstring).
        my_queue = broadcaster.subscribe()

        async def sse_generator():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(my_queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield ":\n\n"
                        continue
                    if item is None:
                        break
                    yield f"event: {item['type']}\ndata: {json.dumps(item)}\n\n"
            finally:
                broadcaster.unsubscribe(my_queue)

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/runs/{run_id}/status")
    async def get_run_status(run_id: str):
        meta_path = Path(RUNS_DIR) / run_id / "meta.json"
        if not meta_path.is_file():
            raise HTTPException(404, f"Run {run_id} not found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {"run_id": run_id, "status": meta.get("status", "unknown"), "session_id": meta.get("session_id", "")}

    @router.get("/runs/{run_id}/results")
    async def get_run_results(run_id: str):
        results_path = Path(RUNS_DIR) / run_id / "results.json"
        if not results_path.is_file():
            raise HTTPException(404, f"Results not found for run {run_id}")
        return json.loads(results_path.read_text(encoding="utf-8"))

    @router.post("/runs/{run_id}/chat-stream")
    async def qp_chat_continuation(run_id: str, req: QPContinuationRequest, request: Request):
        """Phase-6 chat: continue conversing with the manager after extraction completes."""
        meta_path = Path(RUNS_DIR) / run_id / "meta.json"
        if not meta_path.is_file():
            raise HTTPException(404, f"Run {run_id} not found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        session_id = meta.get("session_id", "")
        if not session_id:
            raise HTTPException(400, "Run has no linked session — use /start-proposal-session")

        # A prior follow-up on this run may still be generating (Stop only ever
        # cancelled the client-side stream, never this task) — supersede it so
        # the two turns can't interleave writes into the same session.
        await _cancel_continuation(run_id)

        cont_queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            try:
                await _qp_continuation_task(
                    run_id=run_id,
                    session_id=session_id,
                    meta=meta,
                    user_message=req.message,
                    manager_model=req.manager_model,
                    queue=cont_queue,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[qp_chat] continuation error run={run_id}: {e}", exc_info=True)
                await _emit(cont_queue, "error", message=str(e), phase="phase6")
            finally:
                await cont_queue.put(None)
                if _active_continuations.get(run_id) is task:
                    _active_continuations.pop(run_id, None)

        task = asyncio.create_task(_run())
        _active_continuations[run_id] = task

        async def sse_generator():
            while True:
                try:
                    item = await asyncio.wait_for(cont_queue.get(), timeout=60.0)
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