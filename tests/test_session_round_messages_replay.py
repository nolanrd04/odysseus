"""Regression: an agent-mode turn's real tool_calls/tool_results must survive
into the next turn's LLM context.

A completed agent turn is saved as a single collapsed assistant ChatMessage
(its display text — "Done." when the turn was cut off before producing real
prose). Without replaying the actual exchange, a later "continue" sees only
that placeholder and restarts the task from scratch. `metadata.round_messages`
(set in src/agent_loop.py) carries the real native assistant/tool sequence for
the turn; `get_context_messages()` must splice it in instead of the collapsed
bubble whenever present.
"""

from core.models import Session, ChatMessage


def test_round_messages_replayed_instead_of_collapsed_bubble():
    s = Session(id="s1", name="t", endpoint_url="http://x/v1", model="m")
    s.add_message(ChatMessage("user", "Extract a quantity takeoff sheet."))
    round_messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "census.json (4500 B)"},
    ]
    s.add_message(ChatMessage(
        "assistant", "Done.",
        metadata={"stopped": True, "stream_error": True, "round_messages": round_messages},
    ))

    ctx = s.get_context_messages()

    assert ctx[0] == {"role": "user", "content": "Extract a quantity takeoff sheet."}
    # The collapsed "Done." bubble is replaced by the real exchange, not appended alongside it.
    assert ctx[1:] == round_messages
    assert not any(m.get("content") == "Done." for m in ctx)


def test_turn_without_round_messages_keeps_collapsed_bubble():
    s = Session(id="s2", name="t", endpoint_url="http://x/v1", model="m")
    s.add_message(ChatMessage("user", "hi"))
    s.add_message(ChatMessage("assistant", "hello back", metadata={"model": "m"}))

    ctx = s.get_context_messages()

    assert ctx == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello back", "metadata": {"model": "m"}},
    ]


def test_round_messages_and_slash_filter_compose():
    s = Session(id="s3", name="t", endpoint_url="http://x/v1", model="m")
    s.add_message(ChatMessage("user", "/setup copilot", metadata={"source": "slash"}))
    s.add_message(ChatMessage("assistant", "Starting sign-in...", metadata={"source": "slash"}))
    s.add_message(ChatMessage("user", "run the extraction"))
    round_messages = [{"role": "assistant", "content": "did the work"}]
    s.add_message(ChatMessage("assistant", "Done.", metadata={"round_messages": round_messages}))

    ctx = s.get_context_messages()

    assert ctx == [
        {"role": "user", "content": "run the extraction"},
        {"role": "assistant", "content": "did the work"},
    ]
