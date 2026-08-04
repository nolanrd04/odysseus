"""The Anthropic `usage` SSE event must report the TRUE size of a round's
context (fresh + cache-read + cache-write), not just Anthropic's own
`input_tokens` field — which excludes cached tokens entirely.

A heavily-cached round can have `input_tokens: 2` against a 75k+ token actual
prompt (nearly all served from cache). Anything downstream that tracks
context-% off a bare `input_tokens` — src/agent_loop.py's context_percent,
and any live per-round tracking built on top of it — would badly under-report
real context size for exactly the long, deeply-cached agentic runs where
tracking it actually matters.
"""
import asyncio
import json

from src import llm_core


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _drive(monkeypatch, lines):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda *a, **k: False, raising=False)

    async def run():
        out = []
        async for chunk in llm_core.stream_llm(
            "https://api.anthropic.com/v1/messages", "claude-test",
            [{"role": "user", "content": "hi"}],
            headers={"Authorization": "Bearer k"},
        ):
            out.append(chunk)
        return out

    return asyncio.run(run())


def _usage_events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: "):
            try:
                j = json.loads(c[6:])
            except ValueError:
                continue
            if j.get("type") == "usage":
                out.append(j["data"])
    return out


def test_usage_input_tokens_includes_cache_read_and_write(monkeypatch):
    lines = [
        json.dumps({
            "type": "message_start",
            "message": {"usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 75553,
                "cache_creation_input_tokens": 904,
            }},
        }),
        json.dumps({"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text"}}),
        json.dumps({"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "hi"}}),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 30}}),
        json.dumps({"type": "message_stop"}),
    ]
    chunks = _drive(monkeypatch, [f"data: {ln}" for ln in lines])
    usage = _usage_events(chunks)
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 2 + 75553 + 904
    assert usage[0]["output_tokens"] == 30
    assert usage[0]["cache_read_tokens"] == 75553
    assert usage[0]["cache_write_tokens"] == 904


def test_usage_without_caching_still_reports_fresh_input(monkeypatch):
    lines = [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 500}}}),
        json.dumps({"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text"}}),
        json.dumps({"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "hi"}}),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 10}}),
        json.dumps({"type": "message_stop"}),
    ]
    chunks = _drive(monkeypatch, [f"data: {ln}" for ln in lines])
    usage = _usage_events(chunks)
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 500
    assert usage[0]["cache_read_tokens"] == 0
    assert usage[0]["cache_write_tokens"] == 0
