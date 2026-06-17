"""Optional memory enhancement adapter boundary.

Native T3 Markdown is the only durable semantic memory store. This module is a
single no-op integration point for future rebuildable read accelerators or
analysis programs. No concrete external memory program is configured here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MemoryEnhancementSyncResult:
    synced: int = 0
    reason: str = "no_memory_enhancement_program_configured"


async def sync_t3_to_memory_enhancement(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    *,
    data_root: Path | None = None,
) -> MemoryEnhancementSyncResult:
    """No-op hook for future optional memory enhancement adapters."""
    del agent_id, tenant_id, data_root
    return MemoryEnhancementSyncResult()
