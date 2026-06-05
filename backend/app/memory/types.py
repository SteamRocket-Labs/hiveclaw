"""Lightweight memory layer types used by the compatibility store."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_logger = logging.getLogger(__name__)


def parse_utc_timestamp(value: str | None) -> datetime | None:
    """Parse timestamp string to UTC datetime. Shared across memory subsystem."""
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        _logger.debug("Unparseable timestamp: %s", value)
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    EXTERNAL = "external"


# Valid memory categories (Claude Code aligned + evolution types + general default).
MEMORY_CATEGORIES = frozenset(
    {
        "user",
        "feedback",
        "project",
        "reference",
        "general",
        "constraint",
        "strategy",
        "blocked_pattern",
    }
)

# Container-candidate vocabulary (docs/agent-memory-md-first-spec.md §6).
# Shared between the Extractor prompt contract, T2 entry metadata, and the
# PromotionRouter. A hint is advisory routing evidence — never a final write.
CONTAINER_CANDIDATES = frozenset(
    {
        "memory_append",
        "soul_candidate",
        "skill_candidate",
        "workflow_candidate",
        "artifact_only",
    }
)


@dataclass(slots=True)
class MemoryItem:
    """Unified memory item returned by the retrieval pipeline."""

    kind: MemoryKind
    content: str
    score: float = 0.0
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.score = max(0.0, min(self.score, 1.0))


@dataclass(slots=True)
class WorkingMemory:
    content: str = ""
    source: str = "focus"


@dataclass(slots=True)
class EpisodicMemory:
    summary: str = ""
    session_id: str | None = None


@dataclass(slots=True)
class SemanticMemory:
    subject: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExternalMemoryRef:
    source: str
    content: str
