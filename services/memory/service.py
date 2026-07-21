# services/memory/service.py
"""Memory service — persistent memory storage and retrieval."""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import os

from .memory import MemoryManager
from .memory_vector import MemoryVectorStore
from src.memory_provider import MemoryRecord, NativeMemoryProvider
from src.constants import DATA_DIR


@dataclass
class Memory:
    """A stored memory."""
    id: str
    text: str
    timestamp: int
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemorySearchResult:
    """Result of memory search."""
    memories: List[Memory]
    query: str
    total: int


class MemoryService:
    """
    Memory storage and retrieval service.

    Usage:
        service = MemoryService()
        await service.remember("User prefers dark mode")
        results = await service.recall("preferences")
    """

    def __init__(self, data_dir: str = DATA_DIR):
        self.manager = MemoryManager(data_dir)
        self.vector_store = MemoryVectorStore(data_dir) if os.path.exists(
            os.path.join(data_dir, "memory_vectors")
        ) else None
        self.provider = NativeMemoryProvider(self.manager, self.vector_store)

    def _sync_provider(self) -> None:
        self.provider.memory_vector = self.vector_store

    @staticmethod
    def _to_memory(entry: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Memory:
        return Memory(
            id=entry.get("id", ""),
            text=entry.get("text", ""),
            timestamp=entry.get("timestamp", 0),
            session_id=entry.get("session_id"),
            metadata=metadata or {},
        )

    @staticmethod
    def _record_to_memory(record: MemoryRecord, metadata: Optional[Dict[str, Any]] = None) -> Memory:
        merged_metadata = dict(record.metadata)
        if metadata:
            merged_metadata.update(metadata)
        return Memory(
            id=record.id,
            text=record.text,
            timestamp=record.timestamp,
            session_id=record.session_id,
            metadata=merged_metadata,
        )

    async def remember(
        self,
        text: str,
        session_id: Optional[str] = None,
        *,
        owner: Optional[str] = None,
        category: str = "fact",
        source: str = "user",
        metadata: Optional[Dict[str, Any]] = None,
        fire: bool = True,
    ) -> Memory:
        """
        Store a new memory.

        Args:
            text: Memory content
            session_id: Optional session association
            owner: Owning username (per-user scoping)
            category: Memory category (fact | preference | identity | ...)
            source: What produced this memory (user | ai_agent | quick_proposal | ...)
            metadata: Arbitrary metadata dict, round-tripped by the provider
            fire: Fire the memory_added event (drives the Memory Tidy /
                Tier-1 global-memory update cadence). The chat tool path
                (do_manage_memory) writes via MemoryManager directly and fires
                its own event, so there is no double-fire.

        Returns:
            Created Memory object
        """
        self._sync_provider()
        record = await self.provider.remember(
            text,
            owner=owner,
            session_id=session_id,
            category=category,
            source=source,
            metadata=metadata,
        )
        if fire:
            try:
                from src.event_bus import fire_event
                fire_event("memory_added", owner)
            except Exception:
                pass
        return self._record_to_memory(record)

    async def recall(
        self,
        query: str,
        top_k: int = 5,
        *,
        owner: Optional[str] = None,
    ) -> MemorySearchResult:
        """
        Search memories.

        Args:
            query: Search query
            top_k: Max results
            owner: Restrict results to this owner's memories

        Returns:
            MemorySearchResult with matching memories
        """
        self._sync_provider()
        results = await self.provider.recall(query, top_k=top_k, owner=owner)
        memories = [
            self._record_to_memory(hit.memory, metadata={"score": hit.score})
            if hit.score is not None
            else self._record_to_memory(hit.memory)
            for hit in results
        ]
        return MemorySearchResult(memories=memories, query=query, total=len(memories))

    def get_all(self, limit: int = 100) -> List[Memory]:
        """Get all memories."""
        records = self.manager.load_all()[:limit]
        return [self._to_memory(m) for m in records]

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID."""
        memories = self.manager.load_all()
        remaining = [m for m in memories if m.get("id") != memory_id]
        if len(remaining) == len(memories):
            return False

        self.manager.save(remaining)
        if self.vector_store and self.vector_store.healthy:
            self.vector_store.remove(memory_id)
        return True

    # ── Tier-1 global memory (per-owner living document) ──────────────────

    def get_global(self, owner: Optional[str] = None) -> str:
        """Return the owner's Tier-1 global memory document ("" if none)."""
        from src.global_memory import load_global_memory
        return load_global_memory(owner)

    def update_global(self, owner: Optional[str], doc: str) -> None:
        """Replace the owner's Tier-1 global memory document."""
        from src.global_memory import save_global_memory
        save_global_memory(owner, doc)

    # ── Tier-2 age-out ─────────────────────────────────────────────────────

    def age_out(self, owner: Optional[str], keep: int = 50) -> int:
        """Drop the owner's oldest non-pinned memories beyond the rolling cap.

        This is what bounds Tier-2 storage. Only call AFTER a successful
        Tier-1 global-memory update has absorbed the owner's specifics —
        aging out first would lose facts that were never condensed.
        Pinned memories never age out. Returns the number removed.
        """
        owner_key = (owner or "").strip() or None
        memories = self.manager.load_all()

        def _is_owned(m: Dict[str, Any]) -> bool:
            return ((m.get("owner") or "").strip() or None) == owner_key

        eligible = [m for m in memories if _is_owned(m) and not m.get("pinned")]
        if len(eligible) <= keep:
            return 0

        eligible.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
        drop_ids = {m.get("id") for m in eligible[keep:] if m.get("id")}
        if not drop_ids:
            return 0

        remaining = [m for m in memories if m.get("id") not in drop_ids]
        self.manager.save(remaining)
        if self.vector_store and self.vector_store.healthy:
            for mem_id in drop_ids:
                try:
                    self.vector_store.remove(mem_id)
                except Exception:
                    pass
        return len(drop_ids)
