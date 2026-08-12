"""Detached agent-run manager.

Keeps an agent/chat stream running server-side after the SSE client disconnects
(tab close, navigate away, refresh). The streaming generator is drained by a
background asyncio task into a per-session replay buffer; SSE clients SUBSCRIBE
to that buffer (replay everything so far, then live). Closing the SSE only drops
the subscriber — the drain task keeps going.

The wrapped generator already persists the assistant message to the session on
completion, so reopening the session shows the finished result even if nobody
was connected when it finished. Reconnecting mid-run replays the buffer + streams
live (pick up where it is).

Durability scope: in-memory, survives as long as the server process runs (tab
close / navigation / refresh). It does NOT survive a server restart.
"""
import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, Optional

logger = logging.getLogger(__name__)


class _Run:
    __slots__ = ("buffer", "subscribers", "status", "task", "evict_task")

    def __init__(self) -> None:
        self.buffer: list = []          # ordered SSE event strings (replay log)
        self.subscribers: set = set()   # one asyncio.Queue per connected client
        self.status: str = "running"    # running | done | error | stopped
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None


_RUNS: Dict[str, _Run] = {}

# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180

# Stop requests that arrived BEFORE the run existed: {session_id: monotonic ts}.
#
# start() is the last line of the chat_stream handler — everything before it
# (upload, file conversion, indexing, job provisioning) runs inline first, and
# for a heavy attachment that is seconds to minutes. A Stop clicked in that window
# found nothing in _RUNS, returned "stopped": false, and the handler then
# launched the full run anyway. Observed live: Stop at 21:01:04, session DELETEd
# at 21:01:12, run started regardless at 21:01:15 and billed 20 rounds against
# a session that no longer existed.
#
# So a stop with no run to cancel is remembered instead of dropped, and start()
# refuses to launch a run the user already cancelled. Compared by timestamp
# against when the request began, so a stale tombstone can never kill a
# legitimate LATER run for the same session.
_STOP_REQUESTS: Dict[str, float] = {}

# Tombstones are consumed by the matching start(); this only bounds the leak
# from stops whose request died before ever reaching start().
_STOP_TOMBSTONE_TTL_S = 900


def _publish(run: _Run, ev: str) -> None:
    """Append one SSE event and fan it out to every live subscriber."""
    run.buffer.append(ev)
    seq = len(run.buffer) - 1
    for q in list(run.subscribers):
        try:
            q.put_nowait((seq, ev))
        except Exception:
            pass


def _schedule_evict(session_id: str) -> None:
    """(Re)arm a grace-period eviction for a terminal run with no subscribers.
    Identity-checked so a run that gets replaced/reused is never evicted by a
    stale timer."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _RUNS.get(session_id)
        if cur is run_ref and cur.status != "running" and not cur.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    r = _RUNS.get(session_id)
    return bool(r and r.status == "running")


def get_status(session_id: str) -> Optional[str]:
    r = _RUNS.get(session_id)
    return r.status if r else None


async def _drain(session_id: str, agen: AsyncGenerator[str, None],
                 prev_task: Optional[asyncio.Task] = None) -> None:
    """Pull every event from the wrapped generator into the run buffer, fanning
    each out to live subscribers. Runs to completion regardless of subscribers."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    # If this run replaced an in-flight one (rapid double-send), wait for that
    # one to fully finish first. Its CancelledError handler calls aclose(), which
    # persists its partial response — letting it complete before we start writing
    # keeps the two runs' session saves sequential instead of interleaved.
    if prev_task is not None and not prev_task.done():
        try:
            await asyncio.wait({prev_task})
        except asyncio.CancelledError:
            raise            # our own cancellation — propagate
        except Exception:
            pass
    try:
        async for ev in agen:
            _publish(run, ev)
        if run.status == "running":
            run.status = "done"
    except asyncio.CancelledError:
        run.status = "stopped"
        # Let the wrapped generator's own CancelledError handler run (it saves
        # the partial response to the session).
        try:
            await agen.aclose()
        except Exception:
            pass
    except Exception as e:
        logger.error("[agent-run] %s failed: %s", session_id, e, exc_info=True)
        run.status = "error"
        _publish(
            run,
            "event: error\n"
            f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
        )
        _publish(run, "data: [DONE]\n\n")
    finally:
        # Wake every subscriber with the end sentinel so their SSE closes.
        for q in list(run.subscribers):
            try:
                q.put_nowait((None, None))
            except Exception:
                pass
        # Run is terminal — arm the grace timer so it (and its buffer) is
        # eventually freed even if nobody ever reconnects. subscribe() cancels
        # this on connect and re-arms on disconnect.
        _schedule_evict(session_id)


async def _discard(agen: AsyncGenerator[str, None]) -> None:
    """Close a generator that will never be drained, so its own cleanup runs."""
    try:
        await agen.aclose()
    except Exception:
        pass


def _prune_stop_requests(now: float) -> None:
    for sid, ts in list(_STOP_REQUESTS.items()):
        if now - ts > _STOP_TOMBSTONE_TTL_S:
            _STOP_REQUESTS.pop(sid, None)


def was_stopped_during_setup(session_id: str, requested_at: Optional[float]) -> bool:
    """True if a Stop landed for this session after its request began.

    Consumes the tombstone. A tombstone older than ``requested_at`` belongs to a
    previous request and is discarded rather than applied to this one.
    """
    ts = _STOP_REQUESTS.pop(session_id, None)
    if ts is None:
        return False
    return requested_at is None or ts >= requested_at


def start(session_id: str, agen: AsyncGenerator[str, None],
          requested_at: Optional[float] = None) -> _Run:
    """Start a detached run draining `agen` for a session. If a run is already in
    flight for this session (e.g. a rapid double-send), it's cancelled first.

    ``requested_at`` is the monotonic time the originating request began. Pass it
    so a Stop clicked during a long pre-flight (file conversion/indexing) is honored
    instead of silently lost — see _STOP_REQUESTS.
    """
    if was_stopped_during_setup(session_id, requested_at):
        logger.info(
            "[agent-run] %s cancelled during setup — not starting (Stop arrived "
            "before the run was registered)", session_id,
        )
        run = _Run()
        run.status = "stopped"
        _RUNS[session_id] = run
        _publish(run, "data: [DONE]\n\n")
        # The generator was never iterated; close it so its own cleanup runs.
        try:
            run.task = asyncio.create_task(_discard(agen))
        except RuntimeError:
            pass
        _schedule_evict(session_id)
        return run

    prev = _RUNS.get(session_id)
    prev_task: Optional[asyncio.Task] = None
    if prev:
        if prev.task and not prev.task.done():
            prev.task.cancel()
            prev_task = prev.task   # new run awaits this before it starts writing
        if prev.evict_task and not prev.evict_task.done():
            prev.evict_task.cancel()
    run = _Run()
    _RUNS[session_id] = run
    run.task = asyncio.create_task(_drain(session_id, agen, prev_task))
    return run


async def subscribe(session_id: str) -> AsyncGenerator[str, None]:
    """Replay the run's buffer from the start, then stream live until it ends.
    Safe to call repeatedly (reconnect) and from multiple clients at once."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)            # register BEFORE replaying so nothing is missed
    # A live subscriber is connected — don't let a pending grace timer evict
    # the run out from under it mid-replay.
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        next_seq = 0
        while next_seq < len(run.buffer):
            yield run.buffer[next_seq]
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_idx = 0
        while True:
            try:
                seq, ev = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep slow local models/proxies alive while they prefill before
                # the first token. SSE comments are ignored by the UI but reset
                # browser/proxy idle timers, which prevents "empty response"
                # disconnects on llama.cpp first-token latencies of 30s+.
                if run.status == "running":
                    heartbeat_idx += 1
                    yield f": heartbeat {heartbeat_idx}\n\n"
                    continue
                seq, ev = (None, None)
            if seq is None:            # end sentinel
                while next_seq < len(run.buffer):   # flush any tail the sentinel raced
                    yield run.buffer[next_seq]
                    next_seq += 1
                break
            if seq >= next_seq:        # skip events already replayed from the buffer
                yield ev
                next_seq = seq + 1
    finally:
        run.subscribers.discard(q)
        # Last subscriber gone on a finished run — (re)arm eviction so the
        # buffer doesn't linger indefinitely.
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id)


def stop(session_id: str) -> bool:
    """Cancel an in-flight run (the wrapped generator saves its partial).

    Returns True when a live run was cancelled. When there is nothing to cancel
    the request is REMEMBERED (see _STOP_REQUESTS) rather than dropped, because
    the common case is a Stop clicked while the turn is still in its pre-flight
    and has not registered a run yet. Callers must not read False as "the user's
    Stop did nothing" — it means "nothing running yet; recorded".
    """
    now = time.monotonic()
    _prune_stop_requests(now)
    # Recorded unconditionally: a run can be registered moments after a
    # successful cancel (rapid re-send), and the timestamp check in start()
    # makes an unused tombstone harmless.
    _STOP_REQUESTS[session_id] = now
    run = _RUNS.get(session_id)
    if run and run.task and not run.task.done():
        run.task.cancel()
        return True
    logger.info("[agent-run] stop for %s with no live run — recorded so a run "
                "still in pre-flight won't start", session_id)
    return False
