"""Dynamic skill ordering for progressive-disclosure catalog injection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from app.services.skill_curator import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, load_skill_usage
from app.skills.types import ParsedSkill

_STATE_RANK = {STATE_ACTIVE: 0, STATE_STALE: 1, STATE_ARCHIVED: 2}


def _normalize_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,}", str(value or "").lower())}


def _usage_for_skill(usage: dict[str, dict], skill: ParsedSkill) -> dict:
    candidates = {
        _normalize_slug(skill.metadata.name),
        _normalize_slug(skill.relative_path.rsplit("/", 2)[-2] if "/" in skill.relative_path else skill.relative_path),
        _normalize_slug(skill.file_path.parent.name),
        _normalize_slug(skill.file_path.stem),
    }
    for candidate in candidates:
        record = usage.get(candidate)
        if isinstance(record, dict):
            return record
    return {}


def _scenario_score(skill: ParsedSkill, scenario_tokens: set[str]) -> int:
    if not scenario_tokens:
        return 0
    searchable = _tokens(f"{skill.metadata.name} {skill.metadata.description}")
    if not searchable:
        return 0
    overlap = scenario_tokens & searchable
    return len(overlap) * 100


def rank_skills_for_prompt(
    workspace: Path,
    skills: Iterable[ParsedSkill],
    *,
    scenario_text: str | None = None,
) -> list[ParsedSkill]:
    """Order skills by relevance, heat, and lifecycle state.

    Hot active skills naturally move forward. Stale/cold skills remain
    loadable, but they fall behind unless the current scenario text explicitly
    matches their name or description.
    """

    usage = load_skill_usage(workspace)
    scenario_tokens = _tokens(scenario_text or "")

    def key(skill: ParsedSkill) -> tuple[int, int, int, str]:
        record = _usage_for_skill(usage, skill)
        state = str(record.get("state") or STATE_ACTIVE)
        use_count = int(record.get("use_count") or 0)
        score = _scenario_score(skill, scenario_tokens)
        return (-score, _STATE_RANK.get(state, 9), -use_count, skill.metadata.name.lower())

    return sorted(list(skills), key=key)
