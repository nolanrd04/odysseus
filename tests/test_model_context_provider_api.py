"""Context windows come from the provider API, not a hand-maintained table.

`KNOWN_CONTEXT_WINDOWS` needs an edit on every model release, and a miss is not a
soft failure: `get_context_length_known` returns known=False, the agent refuses to
scale its input budget off an unproven window, and every turn is trimmed to the
unknown-model fallback. These pin the two defects that kept the table
authoritative for Anthropic: the /models probe sent no auth headers (so it 401'd
and never returned anything), and the entry parser didn't read `max_input_tokens`
(the only context field Anthropic's Models API actually reports).
"""

import sys
import types

import src.model_context as model_context
from src.model_context import _model_ctx_from_entry, _model_id_candidates


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return ("eq", self.name, value)


class _ModelEndpoint:
    is_enabled = _Column("is_enabled")


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *conditions):
        for condition in conditions:
            if isinstance(condition, tuple) and condition[0] == "eq":
                _, field, value = condition
                self.rows = [row for row in self.rows if getattr(row, field) == value]
        return self

    def all(self):
        return list(self.rows)


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def query(self, model):
        return _Query(self.rows)

    def close(self):
        pass


def _install_endpoint_db(monkeypatch, rows):
    mod = types.ModuleType("core.database")
    mod.ModelEndpoint = _ModelEndpoint
    mod.SessionLocal = lambda: _Db(rows)
    monkeypatch.setitem(sys.modules, "core.database", mod)


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


ANTHROPIC_BASE = "https://api.anthropic.com/v1"


def _anthropic_db(monkeypatch, api_key="sk-ant-test"):
    _install_endpoint_db(monkeypatch, [
        types.SimpleNamespace(
            base_url=ANTHROPIC_BASE,
            endpoint_kind="api",
            api_key=api_key,
            is_enabled=True,
        )
    ])


class TestModelCtxFromEntry:
    def test_reads_anthropic_max_input_tokens(self):
        # The Models API reports the context window as max_input_tokens and the
        # output cap separately as max_tokens — there is no context_window field.
        entry = {"id": "claude-opus-5", "max_input_tokens": 1000000, "max_tokens": 128000}
        assert _model_ctx_from_entry(entry) == 1000000

    def test_max_tokens_alone_is_not_a_context_window(self):
        # max_tokens is the OUTPUT cap. Reading it as the window would report a
        # 1M-context model as a 128K one.
        assert _model_ctx_from_entry({"id": "m", "max_tokens": 128000}) is None

    def test_openai_style_context_length_still_wins(self):
        assert _model_ctx_from_entry({"id": "m", "context_length": 8192}) == 8192


class TestModelIdCandidates:
    def test_strips_variant_suffix(self):
        assert _model_id_candidates("claude-opus-5[1m]") == ["claude-opus-5[1m]", "claude-opus-5"]

    def test_strips_routing_prefix(self):
        assert _model_id_candidates("anthropic/claude-opus-5") == [
            "anthropic/claude-opus-5", "claude-opus-5",
        ]

    def test_plain_id_yields_one_candidate(self):
        assert _model_id_candidates("claude-opus-5") == ["claude-opus-5"]


class TestAnthropicProviderLookup:
    def setup_method(self):
        model_context._context_cache.clear()
        model_context._catalog_ctx_cache.clear()

    def test_provider_value_beats_a_stale_table_entry(self, monkeypatch):
        # The whole point: the table said 200000 for sonnet-4-6 while the real
        # window was 1M, and a hand-edit was the only fix. The API now wins.
        _anthropic_db(monkeypatch)
        monkeypatch.setitem(model_context.KNOWN_CONTEXT_WINDOWS, "claude-sonnet-4-6", 200000)

        def fake_get(url, *args, **kwargs):
            assert url.endswith("/v1/models/claude-sonnet-4-6"), url
            return _FakeResp({"id": "claude-sonnet-4-6", "max_input_tokens": 1000000})

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        ctx, known = model_context.get_context_length_known(
            f"{ANTHROPIC_BASE}/messages", "claude-sonnet-4-6"
        )
        assert (ctx, known) == (1000000, True)

    def test_model_absent_from_the_table_resolves_from_the_provider(self, monkeypatch):
        # A model released after the last table edit must not collapse to the
        # unknown-model fallback.
        _anthropic_db(monkeypatch)

        def fake_get(url, *args, **kwargs):
            return _FakeResp({"id": "claude-brandnew-9", "max_input_tokens": 2000000})

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        ctx, known = model_context.get_context_length_known(
            f"{ANTHROPIC_BASE}/messages", "claude-brandnew-9"
        )
        assert (ctx, known) == (2000000, True)

    def test_probe_sends_the_api_key(self, monkeypatch):
        # An unauthenticated probe 401s, which is why the provider path was dead.
        _anthropic_db(monkeypatch)
        seen = {}

        def fake_get(url, *args, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return _FakeResp({"id": "claude-opus-5", "max_input_tokens": 1000000})

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        model_context.get_context_length(f"{ANTHROPIC_BASE}/messages", "claude-opus-5")

        assert seen.get("x-api-key") == "sk-ant-test"
        assert seen.get("anthropic-version")

    def test_unauthenticated_probe_falls_back_to_the_table(self, monkeypatch):
        # No key configured: the table is still a usable offline answer, and the
        # result must stay known=True so the budget still scales.
        _anthropic_db(monkeypatch, api_key=None)
        monkeypatch.setitem(model_context.KNOWN_CONTEXT_WINDOWS, "claude-opus-5", 1000000)

        monkeypatch.setattr(
            model_context.httpx, "get",
            lambda url, *a, **k: _FakeResp({"error": "unauthorized"}, status_code=401),
        )

        ctx, known = model_context.get_context_length_known(
            f"{ANTHROPIC_BASE}/messages", "claude-opus-5"
        )
        assert (ctx, known) == (1000000, True)

    def test_variant_suffix_retries_the_bare_id(self, monkeypatch):
        # "claude-opus-5[1m]" is an app-side variant label; the provider only
        # knows the bare id, so a 404 must retry rather than give up.
        _anthropic_db(monkeypatch)
        tried = []

        def fake_get(url, *args, **kwargs):
            tried.append(url)
            if url.endswith("%5B1m%5D"):
                return _FakeResp({"type": "not_found_error"}, status_code=404)
            return _FakeResp({"id": "claude-opus-5", "max_input_tokens": 1000000})

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        ctx, known = model_context.get_context_length_known(
            f"{ANTHROPIC_BASE}/messages", "claude-opus-5[1m]"
        )
        assert (ctx, known) == (1000000, True)
        assert len(tried) == 2

    def test_network_failure_falls_back_and_never_raises(self, monkeypatch):
        _anthropic_db(monkeypatch)

        def fake_get(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        ctx, known = model_context.get_context_length_known(
            f"{ANTHROPIC_BASE}/messages", "totally-unknown-model"
        )
        assert (ctx, known) == (model_context.DEFAULT_CONTEXT, False)
