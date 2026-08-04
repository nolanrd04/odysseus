"""Regression tests for Anthropic prompt-cache breakpoints in _build_anthropic_payload (#791)."""
from src import llm_core


def _payload(system="sys", user="hi", tools=None, messages=None):
    if messages is None:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return llm_core._build_anthropic_payload("claude", messages, 0.0, 1000, stream=True, tools=tools)


def _cache_control(block_or_msg):
    content = block_or_msg.get("content")
    if isinstance(content, list):
        return content[-1].get("cache_control")
    return block_or_msg.get("cache_control")


def test_agentic_caches_system_tools_and_last_message():
    tools = [
        {"type": "function", "function": {"name": "a", "description": "x", "parameters": {}}},
        {"type": "function", "function": {"name": "b", "description": "y", "parameters": {}}},
    ]
    p = _payload(system="SYS PROMPT " * 50, tools=tools)
    assert isinstance(p["system"], list)
    assert p["system"][0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in p["tools"][0], "only the LAST tool is a breakpoint"
    assert p["tools"][-1].get("cache_control") == {"type": "ephemeral"}
    # Only one message survives (the user turn) — it gets the rolling breakpoint too.
    assert len(p["messages"]) == 1
    assert _cache_control(p["messages"][-1]) == {"type": "ephemeral"}


def test_tiny_tool_less_prompt_not_cached():
    p = _payload(system="hi", tools=None)
    assert isinstance(p["system"], list)
    assert "cache_control" not in p["system"][0]
    assert _cache_control(p["messages"][-1]) is None


def test_large_system_only_is_cached():
    p = _payload(system="z" * 5000, tools=None)
    assert p["system"][0].get("cache_control") == {"type": "ephemeral"}
    # A large stable system prompt implies reuse across rounds too, so the
    # message history gets the rolling breakpoint even without `tools`.
    assert _cache_control(p["messages"][-1]) == {"type": "ephemeral"}


def test_rolling_breakpoint_slides_across_last_two_messages():
    tools = [{"type": "function", "function": {"name": "a", "description": "x", "parameters": {}}}]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "turn 1 reply"},
        {"role": "user", "content": "turn 2"},
    ]
    p = _payload(tools=tools, messages=messages)
    assert len(p["messages"]) == 3
    # Last two messages carry the sliding pair of breakpoints...
    assert _cache_control(p["messages"][-1]) == {"type": "ephemeral"}
    assert _cache_control(p["messages"][-2]) == {"type": "ephemeral"}
    # ...but not the earlier, already-superseded prefix.
    assert _cache_control(p["messages"][0]) is None


def test_breakpoint_marking_does_not_mutate_input_messages():
    tools = [{"type": "function", "function": {"name": "a", "description": "x", "parameters": {}}}]
    original_content = [{"type": "text", "text": "hi"}]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": original_content},
    ]
    _payload(tools=tools, messages=messages)
    assert "cache_control" not in original_content[-1], "must not mutate caller's message content in place"
