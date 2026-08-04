"""Regression test for the agent_stream_timeout_seconds shadowing bug.

settings.py's save path materializes every DEFAULT_SETTINGS value into
settings.json, so a persisted `agent_stream_timeout_seconds: 300` is
byte-identical to "user never touched this setting" (same shape as
agent_input_token_budget, see context_budget.budget_is_explicit). The old
code always honoured whatever get_setting() returned, so local endpoints
never got their longer auto-scaled timeout — a colibri/local-model run
would have its stream killed client-side after 300s of silent prefill,
which the server then logs as an aborted connection.

resolve_stream_timeout() must only treat a configured value that DIFFERS
from the default as a deliberate override.
"""
from src.agent_loop import (
    resolve_stream_timeout,
    STREAM_TIMEOUT_DEFAULT,
    LOCAL_STREAM_TIMEOUT_DEFAULT,
)


def test_materialized_default_still_auto_scales_for_local_endpoints():
    # This is the exact regression: settings.json has the baked-in default
    # (300) written to disk, not a deliberate user choice.
    assert resolve_stream_timeout(STREAM_TIMEOUT_DEFAULT, True) == LOCAL_STREAM_TIMEOUT_DEFAULT


def test_materialized_default_stays_default_for_cloud_endpoints():
    assert resolve_stream_timeout(STREAM_TIMEOUT_DEFAULT, False) == STREAM_TIMEOUT_DEFAULT


def test_explicit_non_default_override_wins_for_local_endpoint():
    assert resolve_stream_timeout(600, True) == 600


def test_explicit_non_default_override_wins_for_cloud_endpoint():
    assert resolve_stream_timeout(120, False) == 120


def test_missing_or_falsy_configured_value_treated_as_default():
    assert resolve_stream_timeout(0, True) == LOCAL_STREAM_TIMEOUT_DEFAULT
    assert resolve_stream_timeout(None, True) == LOCAL_STREAM_TIMEOUT_DEFAULT
    assert resolve_stream_timeout(0, False) == STREAM_TIMEOUT_DEFAULT
