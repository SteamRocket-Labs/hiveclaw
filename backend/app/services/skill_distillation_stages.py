"""Pure selection stages for the single skill-distillation cycle."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.skill_lifecycle import SkillCandidateRecord


@dataclass(slots=True, frozen=True)
class RankedSkillCandidates:
    reviewable: tuple[SkillCandidateRecord, ...]


def rank_skill_candidates(
    records: Iterable[SkillCandidateRecord],
) -> RankedSkillCandidates:
    """Expose every authorized candidate; the model assigns semantic lanes."""
    return RankedSkillCandidates(tuple(records))


def _cursor_part(item: Any, key: str) -> str:
    if isinstance(item, Mapping):
        return str(item.get(key) or "")
    return str(getattr(item, key, "") or "")


def advance_distiller_cursor(current: tuple[str, str], evidence: Iterable[Any]) -> tuple[str, str]:
    return max(
        (current, *((_cursor_part(item, "occurred_at"), _cursor_part(item, "session_id")) for item in evidence)),
    )
