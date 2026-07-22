"""TODO_CCC: per-token cost accounting for the Quick Proposal manager (Claude) and
extractor (Gemini) roles. Covers the pricing-rate lookup and the asymmetric cost
formula (Claude's cache fields are additive on top of input_tokens; Gemini's
cached_tokens is a subset of prompt_tokens and must not be double-billed), plus
_add_cumulative_usage's persisted, incrementally-accumulated cost_usd.

_qp_estimate_cost takes ONE call's usage, not aggregated cumulative_usage totals — a
live run showed cumulative cache_read exceeding cumulative input_tokens after enough
calls (meaning at least one individual Gemini call reported cached_tokens exceeding
that same call's prompt_tokens), and clamping the AGGREGATE difference at zero wiped
out "regular input" billing for the whole run, not just the one anomalous call."""
import json

import pytest

import routes.quick_proposal_routes as qpr


def test_pricing_rates_fall_back_to_defaults(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    rates = qpr._qp_pricing_rates("claude")
    assert rates == qpr._QP_DEFAULT_PRICING["claude"]


def test_pricing_rates_read_settings_override(monkeypatch):
    overrides = {"qp_pricing_claude_input_per_million": 9.0}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: overrides.get(key, default))
    rates = qpr._qp_pricing_rates("claude")
    assert rates["input"] == 9.0
    assert rates["output"] == qpr._QP_DEFAULT_PRICING["claude"]["output"]


def test_claude_cost_is_additive_across_input_and_cache_fields(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    # 1M uncached input + 1M cache-write + 1M cache-read + 1M output, at default Claude rates.
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
             "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    rates = qpr._QP_DEFAULT_PRICING["claude"]
    expected = rates["input"] + rates["output"] + rates["cache_write"] + rates["cache_read"]
    assert qpr._qp_estimate_cost("claude", usage) == pytest.approx(expected)


def test_gemini_cache_read_is_not_double_billed(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    # Gemini's cached_tokens is a SUBSET of prompt_tokens for a single call, not additive —
    # 1M prompt_tokens of which 400k were cache reads should bill 600k at full price + 400k
    # at cache-read price, never the full 1M at full price on top of the cache-read charge.
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 400_000}
    rates = qpr._QP_DEFAULT_PRICING["gemini"]
    expected = 600_000 / 1_000_000 * rates["input"] + 400_000 / 1_000_000 * rates["cache_read"]
    assert qpr._qp_estimate_cost("gemini", usage) == pytest.approx(expected)


def test_gemini_cache_read_never_exceeds_input_tokens(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    # Defensive: cache_read_input_tokens should never exceed prompt_tokens for a single call,
    # but if a single call somehow reported it that way, billable input must floor at 0
    # rather than go negative (this is the per-call clamp; see the aggregate-level
    # regression test below for why clamping at the AGGREGATE level instead is the bug).
    usage = {"prompt_tokens": 100, "completion_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500}
    assert qpr._qp_estimate_cost("gemini", usage) >= 0


def test_add_cumulative_usage_persists_cost_usd(tmp_path, monkeypatch):
    monkeypatch.setattr(qpr, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    run_id = "test-run-cost"
    (tmp_path / run_id).mkdir()
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    totals = qpr._add_cumulative_usage(run_id, "claude", usage)
    assert totals["cost_usd"] == pytest.approx(qpr._QP_DEFAULT_PRICING["claude"]["input"])
    persisted = json.loads((tmp_path / run_id / "results.json").read_text())
    assert persisted["cumulative_usage"]["claude"]["cost_usd"] == totals["cost_usd"]


def test_one_anomalous_call_does_not_zero_out_other_calls_cost(tmp_path, monkeypatch):
    """Regression for the bug found live: a single Gemini call whose reported
    cached_tokens exceeds that call's own prompt_tokens (a real, observed API anomaly)
    must not wipe out billing for every other normal call accumulated in the same run."""
    monkeypatch.setattr(qpr, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    run_id = "test-run-anomaly"
    (tmp_path / run_id).mkdir()
    rates = qpr._QP_DEFAULT_PRICING["gemini"]

    normal_call = {"prompt_tokens": 20_000, "completion_tokens": 100,
                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 15_000}
    anomalous_call = {"prompt_tokens": 100, "completion_tokens": 10,
                       "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50_000}

    qpr._add_cumulative_usage(run_id, "gemini", normal_call)
    qpr._add_cumulative_usage(run_id, "gemini", anomalous_call)
    totals = qpr._add_cumulative_usage(run_id, "gemini", normal_call)

    # If cost were recomputed from aggregated totals (the old, buggy approach), cumulative
    # cache_read (80,000) would exceed cumulative input_tokens (40,100), clamping billable
    # input to 0 for the ENTIRE run and losing ~$0.06 of legitimate per-call input billing.
    expected = (
        2 * ((20_000 - 15_000) / 1_000_000 * rates["input"] + 100 / 1_000_000 * rates["output"]
             + 15_000 / 1_000_000 * rates["cache_read"])
        + (0 / 1_000_000 * rates["input"] + 10 / 1_000_000 * rates["output"]
           + 50_000 / 1_000_000 * rates["cache_read"])
    )
    assert totals["cost_usd"] == pytest.approx(expected)
    assert totals["cost_usd"] > 0

