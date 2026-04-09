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
