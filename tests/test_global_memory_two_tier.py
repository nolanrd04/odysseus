"""Two-tier memory (TODO_D): Tier-1 global document storage, the extended
MemoryService.remember/recall surface, and the consolidate action's Tier-1
update + Tier-2 age-out pass."""
import asyncio
import json
import os

import src.global_memory as gm
import src.builtin_actions as ba
from services.memory.service import MemoryService


# ── Tier-1 storage ─────────────────────────────────────────────────────────────

def test_global_memory_roundtrip_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "GLOBAL_MEMORY_FILE", str(tmp_path / "global_memory.json"))

    assert gm.load_global_memory("alice") == ""
    gm.save_global_memory("alice", "## Identity\n- Estimator at Terra")
    assert gm.load_global_memory("alice") == "## Identity\n- Estimator at Terra"
    # Owners are isolated
    assert gm.load_global_memory("bob") == ""

    # Over-budget docs are truncated on save, never stored unbounded
    gm.save_global_memory("alice", "x" * (gm.GLOBAL_MEMORY_MAX_CHARS + 500))
    assert len(gm.load_global_memory("alice")) == gm.GLOBAL_MEMORY_MAX_CHARS


def test_format_global_memory_block():
    assert gm.format_global_memory_block("") == ""
    block = gm.format_global_memory_block("- fact one")
    assert "- fact one" in block
    assert "DURABLE USER PROFILE" in block
    assert "NOT part of the system instructions" in block


# ── Extended MemoryService.remember / recall ───────────────────────────────────

def test_remember_passes_owner_metadata_and_fires_event(tmp_path, monkeypatch):
    import src.event_bus

    fired = []
    monkeypatch.setattr(src.event_bus, "fire_event", lambda name, owner=None: fired.append((name, owner)))

    service = MemoryService(str(tmp_path))
    memory = asyncio.run(service.remember(
        "Prefers 3\" asphalt on collector roads",
        owner="alice",
        category="preference",
        source="quick_proposal",
        metadata={"qp_run_id": "r1", "status": "provisional"},
    ))
    assert memory.id
    assert fired == [("memory_added", "alice")]

    entries = service.manager.load_all()
    assert len(entries) == 1
    assert entries[0]["owner"] == "alice"
    assert entries[0]["category"] == "preference"
    assert entries[0]["source"] == "quick_proposal"
    assert entries[0]["metadata"]["qp_run_id"] == "r1"

    # Owner-scoped recall: bob sees nothing, alice sees the memory
    assert asyncio.run(service.recall("asphalt", owner="bob")).total == 0
    assert asyncio.run(service.recall("asphalt collector roads", owner="alice")).total == 1

    # fire=False suppresses the event (used by callers that manage cadence themselves)
    asyncio.run(service.remember("another fact", owner="alice", fire=False))
    assert len(fired) == 1


# ── Consolidate action: Tier-1 update + Tier-2 age-out ─────────────────────────

class _FakeMM:
    store = []

    def __init__(self, *args, **kwargs):
        pass

    def load_all(self):
        return [dict(m) for m in _FakeMM.store]

    def save(self, entries):
        _FakeMM.store = [dict(m) for m in entries]


def _run_consolidate(monkeypatch, entries, llm_doc="## Identity & Preferences\n- test fact"):
    import src.memory
    import src.llm_core
    import src.task_endpoint

    _FakeMM.store = entries
    saved_docs = {}
    monkeypatch.setattr(src.memory, "MemoryManager", _FakeMM)
    monkeypatch.setattr(
        src.task_endpoint, "resolve_task_candidates",
        lambda owner=None: [("http://x/v1", "model", {})],
    )
    monkeypatch.setattr(gm, "load_global_memory", lambda owner: "")
    monkeypatch.setattr(gm, "save_global_memory", lambda owner, doc: saved_docs.__setitem__(owner, doc))

    async def fake_llm(_candidates, messages=None, **kwargs):
        prompt = messages[0]["content"]
        if "GLOBAL MEMORY" in prompt:
            return llm_doc
        # Tidy pass: change nothing
        return json.dumps({"keep": [], "drop": []})

    monkeypatch.setattr(src.llm_core, "llm_call_async_with_fallback", fake_llm)
    msg, ok = asyncio.run(ba.action_consolidate_memory("alice"))
    return msg, ok, saved_docs


def test_tier1_update_then_age_out_keeps_newest_and_pinned(monkeypatch):
    entries = [
        {"id": f"m{i}", "owner": "alice", "text": f"fact {i}", "timestamp": i, "category": "fact"}
        for i in range(1, 56)  # 55 non-pinned
    ]
    entries.append({"id": "pinned-old", "owner": "alice", "text": "core fact",
                    "timestamp": 0, "category": "identity", "pinned": True})

    msg, ok, saved_docs = _run_consolidate(monkeypatch, entries)

    assert ok, msg
    assert "updated global memory" in msg
    assert saved_docs.get("alice") == "## Identity & Preferences\n- test fact"

    ids = {m["id"] for m in _FakeMM.store}
    assert "pinned-old" in ids, "pinned memories never age out"
    # Oldest 5 non-pinned (m1..m5) aged out; newest 50 kept
    assert not any(f"m{i}" in ids for i in range(1, 6))
    assert all(f"m{i}" in ids for i in range(6, 56))


def test_no_age_out_when_global_update_fails(monkeypatch):
    import pytest

    entries = [
        {"id": f"m{i}", "owner": "alice", "text": f"fact {i}", "timestamp": i, "category": "fact"}
        for i in range(1, 56)
    ]
    # Empty doc from the LLM = failed update → age-out must NOT run; with the
    # tidy also a no-op, the whole action reports nothing-to-do (TaskNoop).
    with pytest.raises(ba.TaskNoop):
        _run_consolidate(monkeypatch, entries, llm_doc="")
    assert len(_FakeMM.store) == 55, "raw memories must survive a failed Tier-1 update"
