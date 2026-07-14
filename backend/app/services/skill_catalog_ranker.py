"""Dynamic skill ordering for progressive-disclosure catalog injection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.runtime.activation_candidates import ActivationCandidate, ActivationScore, ActivationSurface
from app.runtime.context_candidates import build_metadata_activation_keys
from app.services.skill_curator import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, load_skill_usage
from app.services.skill_evolution_registry import (
    LOADABLE_STATES,
    STATE_PROVISIONAL,
    get_skill_evolution_entry,
)
from app.skills.types import ParsedSkill

_STATE_RANK = {STATE_ACTIVE: 0, STATE_PROVISIONAL: 1, STATE_STALE: 2, STATE_ARCHIVED: 3}
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

    @property
    def activation_keys(self) -> dict[str, Any]:
        metadata = self.skill.metadata
        name = metadata.name
        source_ref = self.skill.relative_path or str(self.skill.file_path)
        terms = sorted(_tokens(f"{name} {metadata.description} {metadata.when_to_use} {metadata.context}"))
        return build_metadata_activation_keys(
            candidate_kind="skill",
            item_id=name,
            source_type="skill_catalog",
            key_features={
                "name": [name],
                "description_terms": terms,
                "declared_tools": list(metadata.declared_tools),
                "declared_packs": list(metadata.declared_packs),
                "allowed_tools": list(metadata.allowed_tools),
                "paths": list(metadata.paths),
                "state": [self.state],
                "use_count": self.use_count,
                "reasons": list(self.reasons),
            },
            value_pointer={
                "loader": "load_skill",
                "name": name,
                "source": source_ref,
            },
            source_refs=[source_ref],
            ref_kind="skill",
            payload={
                "name": name,
                "description": metadata.description,
                "source": source_ref,
                "state": self.state,
            },
        )


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
        evolution_entry = get_skill_evolution_entry(workspace, skill.metadata.name) or get_skill_evolution_entry(
            workspace, skill.relative_path
        )
        state = str((evolution_entry or {}).get("state") or record.get("state") or STATE_ACTIVE)
        if evolution_entry is not None and state not in LOADABLE_STATES:
            continue
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


def gather_skill_candidates_for_prompt(
    workspace: Path,
    skills: Iterable[ParsedSkill],
    *,
    scenario_text: str | None = None,
    active_skill_names: Iterable[str] | None = None,
    path_triggered_skill_names: Iterable[str] | None = None,
    limit: int = 20,
) -> list[ActivationCandidate]:
    decisions = rank_skills_for_prompt_with_reasons(
        workspace,
        skills,
        scenario_text=scenario_text,
        active_skill_names=active_skill_names,
        path_triggered_skill_names=path_triggered_skill_names,
    )
    candidates: list[ActivationCandidate] = []
    # ``limit`` is retained for call-site compatibility only. Candidate
    # gathering is an evidence phase; selection belongs to the model/runtime
    # activation policy with the complete authorized candidate set visible.
    _ = limit
    for decision in decisions:
        keys = decision.activation_keys
        preview = f"{decision.skill.metadata.name}: {decision.skill.metadata.description}".strip()
        rank_score = min(1.0, max(0.1, decision.score / 1000 if decision.score else 0.1))
        source_refs = tuple(str(ref) for ref in keys.get("source_refs") or () if str(ref).strip())
        candidates.append(
            ActivationCandidate(
                candidate_kind="skill",
                candidate_ref=dict(keys["candidate_ref"]),
                key_features=dict(keys["key_features"]),
                value_pointer=dict(keys["value_pointer"]),
                surface=ActivationSurface(
                    surface_kind="skill_catalog",
                    preview=preview,
                    token_estimate=max(1, len(preview) // 4),
                    source_refs=source_refs,
                ),
                source_refs=source_refs,
                score=ActivationScore(
                    head_scores={"skill_rank": rank_score},
                    total_score=rank_score,
                    reasons=decision.reasons,
                    scorer="skill_catalog_gatherer",
                ),
                metadata={
                    "state": decision.state,
                    "use_count": decision.use_count,
                    "score": decision.score,
                },
            )
        )
    return candidates
