"""Untagged-reasoning split for local models that mash the reply onto the thinking.

Some models emit their reasoning as plain prose inside ``content`` — no
``<think>`` markup and no separate reasoning channel to route it by — and
frequently run the final sentence of that reasoning straight into the reply
with no separating whitespace. ``_normalize_thinking`` has to guess the
boundary; these cover the guess going wrong in a way that leaked reasoning
into the saved reply.
"""

from routes.chat_helpers import _normalize_thinking


def _split(text):
    """Return (thinking, reply) from _normalize_thinking's <think>-wrapped output."""
    import re
    out = _normalize_thinking(text)
    m = re.match(r"^<think>([\s\S]*?)</think>\n?([\s\S]*)", out)
    return (m.group(1), m.group(2)) if m else (None, out)


def test_unspaced_mash_boundary_is_found():
    """`summary.Since` — reply opener absent from the greetings allowlist.

    Regression: the last-resort scan skipped the true reply line (it begins
    "I will ", a reasoning prefix) and cut inside the reasoning's own bullet
    summary, so the tail of the thinking was persisted as the reply.
    """
    text = (
        "The user asked what I know.\n"
        "Summary of info:\n"
        "- User works for an engineering firm.\n"
        "- Current date is August 5, 2026.\n"
        "\n"
        "I will provide a concise summary.Since this is not the only message "
        "in our conversation, I have access to several pieces of information."
    )
    thinking, reply = _split(text)
    assert reply.startswith("Since this is not the only message")
    assert "Summary of info" in thinking
    assert "- Current date is August 5, 2026." not in reply


def test_greeting_allowlist_still_wins():
    """The pre-existing greeting split must keep working."""
    _, reply = _split(
        "The user is saying hi. I should greet them back warmly.\n"
        "Hey there! How can I help?"
    )
    assert reply.strip().startswith("Hey there!")


def test_no_split_inside_fenced_code():
    """`obj.Method()` is syntax, not a sentence boundary."""
    _, reply = _split(
        "The user wants code.\n"
        "```python\n"
        "foo = obj.Method()\n"
        "bar = obj.Other()\n"
        "```\n"
        "Here is the snippet you asked for."
    )
    assert reply.strip().startswith("Here is the snippet")


def test_tagged_thinking_is_untouched():
    _, reply = _split("<think>reasoning here</think>\nThe answer is 42.")
    assert reply.strip() == "The answer is 42."


def test_plain_answer_passes_through():
    text = "Here is a plain answer with no reasoning whatsoever."
    assert _normalize_thinking(text) == text
