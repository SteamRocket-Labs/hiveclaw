"""Dynamic skill ordering for progressive-disclosure catalog injection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.services.skill_curator import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, load_skill_usage
from app.skills.types import ParsedSkill

_STATE_RANK = {STATE_ACTIVE: 0, STATE_STALE: 1, STATE_ARCHIVED: 2}
_PATH_TRIGGER_SCORE = 1000
_ACTIVE_SKILL_SCORE = 500
_SCENARIO_OVERLAP_SCORE = 100


@dataclass(frozen=True, slots=True)
class SkillRankingDecision:
    skill: ParsedSkill
    score: int
    reasons: tuple[str, ...]
    state: str
    use_count: int


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


def _scenario_overlap_tokens(skill: ParsedSkill, scenario_tokens: set[str]) -> tuple[str, ...]:
    if not scenario_tokens:
        return ()
    searchable = _tokens(f"{skill.metadata.name} {skill.metadata.description}")
    return tuple(sorted(scenario_tokens & searchable))


def _skill_identity_tokens(skill: ParsedSkill) -> set[str]:
    return {
        _normalize_slug(skill.metadata.name),
        _normalize_slug(skill.relative_path.rsplit("/", 2)[-2] if "/" in skill.relative_path else skill.relative_path),
        _normalize_slug(skill.file_path.parent.name),
        _normalize_slug(skill.file_path.stem),
    }


def _matches_skill_name(skill: ParsedSkill, names: set[str]) -> bool:
    if not names:
        return False
    return bool(_skill_identity_tokens(skill) & names)


def rank_skills_for_prompt_with_reasons(
    workspace: Path,
    skills: Iterable[ParsedSkill],
    *,
    scenario_text: str | None = None,
    active_skill_names: Iterable[str] | None = None,
    path_triggered_skill_names: Iterable[str] | None = None,
) -> list[SkillRankingDecision]:
    usage = load_skill_usage(workspace)
    scenario_tokens = _tokens(scenario_text or "")
    active_names = {_normalize_slug(name) for name in active_skill_names or () if _normalize_slug(name)}
    path_triggered_names = {_normalize_slug(name) for name in path_triggered_skill_names or () if _normalize_slug(name)}

    decisions: list[SkillRankingDecision] = []
    for skill in skills:
        record = _usage_for_skill(usage, skill)
        state = str(record.get("state") or STATE_ACTIVE)
        use_count = int(record.get("use_count") or 0)
        score = 0
        reasons: list[str] = []

        if _matches_skill_name(skill, path_triggered_names):
            score += _PATH_TRIGGER_SCORE
            reasons.append("path_triggered")
        if _matches_skill_name(skill, active_names):
            score += _ACTIVE_SKILL_SCORE
            reasons.append("active_in_session")
        overlap = _scenario_overlap_tokens(skill, scenario_tokens)
        if overlap:
            score += len(overlap) * _SCENARIO_OVERLAP_SCORE
            reasons.append(f"scenario_overlap:{','.join(overlap)}")
        if use_count:
            reasons.append(f"usage_count:{use_count}")
        if state != STATE_ACTIVE:
            reasons.append(f"lifecycle_state:{state}")

        decisions.append(
            SkillRankingDecision(
                skill=skill,
                score=score,
                reasons=tuple(reasons or ("default_order",)),
                state=state,
                use_count=use_count,
            )
        )

    return sorted(
        decisions,
        key=lambda decision: (
            -decision.score,
            _STATE_RANK.get(decision.state, 9),
            -decision.use_count,
            decision.skill.metadata.name.lower(),
        ),
    )


def rank_skills_for_prompt(
    workspace: Path,
    skills: Iterable[ParsedSkill],
    *,
    scenario_text: str | None = None,
    active_skill_names: Iterable[str] | None = None,
    path_triggered_skill_names: Iterable[str] | None = None,
) -> list[ParsedSkill]:
    """Order skills by relevance, heat, and lifecycle state.

    Hot active skills naturally move forward. Stale/cold skills remain
    loadable, but they fall behind unless the current scenario text explicitly
    matches their name or description.
    """

    return [
        decision.skill
        for decision in rank_skills_for_prompt_with_reasons(
            workspace,
            skills,
            scenario_text=scenario_text,
            active_skill_names=active_skill_names,
            path_triggered_skill_names=path_triggered_skill_names,
        )
    ]
