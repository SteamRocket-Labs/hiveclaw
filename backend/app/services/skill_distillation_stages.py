"""Pure selection stages for the single skill-distillation cycle."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.skill_lifecycle import SkillCandidateRecord


@dataclass(slots=True, frozen=True)
class RankedSkillCandidates:
    patchable: tuple[SkillCandidateRecord, ...]
    promotable: tuple[SkillCandidateRecord, ...]


def rank_skill_candidates(
    records: Iterable[SkillCandidateRecord],
    *,
    patch_threshold: int,
    promote_threshold: int,
) -> RankedSkillCandidates:
    candidates = tuple(records)
    patchable = sorted(
        (record for record in candidates if not record.blocker and len(record.patch_candidates) >= patch_threshold),
        key=lambda record: (len(record.patch_candidates), record.last_updated_at),
        reverse=True,
    )
    promotable = sorted(
        (
            record
            for record in candidates
            if record.last_status == "success"
            and not record.blocker
            and len(record.promote_candidates) >= promote_threshold
            and not record.patch_candidates
        ),
        key=lambda record: (len(record.promote_candidates), record.last_updated_at),
        reverse=True,
    )
    return RankedSkillCandidates(tuple(patchable), tuple(promotable))


def _cursor_part(item: Any, key: str) -> str:
    if isinstance(item, Mapping):
        return str(item.get(key) or "")
    return str(getattr(item, key, "") or "")


def advance_distiller_cursor(current: tuple[str, str], evidence: Iterable[Any]) -> tuple[str, str]:
    return max(
        (current, *((_cursor_part(item, "occurred_at"), _cursor_part(item, "session_id")) for item in evidence)),
    )
