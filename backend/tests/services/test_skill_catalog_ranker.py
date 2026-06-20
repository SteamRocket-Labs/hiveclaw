from __future__ import annotations

import json
from pathlib import Path

from app.skills.types import ParsedSkill, SkillMetadata


def _skill(name: str, *, description: str = "desc", is_system: bool = False) -> ParsedSkill:
    return ParsedSkill(
        metadata=SkillMetadata(name=name, description=description, is_system=is_system),
        body=f"# {name}",
        file_path=Path(f"skills/{name}/SKILL.md"),
        relative_path=f"skills/{name}/SKILL.md",
    )


def test_skill_catalog_ranker_orders_hot_active_skills_before_cold_storage(tmp_path: Path) -> None:
    from app.services.skill_catalog_ranker import rank_skills_for_prompt

    usage_path = tmp_path / "evolution" / "skill_usage.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(
        json.dumps(
            {
                "cold-skill": {"state": "stale", "use_count": 0, "last_used_at": "2026-01-01T00:00:00Z"},
                "hot-skill": {"state": "active", "use_count": 20, "last_used_at": "2026-06-20T10:00:00Z"},
                "system-search": {"state": "active", "use_count": 2, "last_used_at": "2026-06-01T00:00:00Z"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ranked = rank_skills_for_prompt(
        tmp_path,
        [_skill("cold-skill"), _skill("system-search", is_system=True), _skill("hot-skill")],
    )

    assert [skill.metadata.name for skill in ranked] == ["hot-skill", "system-search", "cold-skill"]


def test_skill_catalog_ranker_can_force_relevant_cold_skill_for_scenario(tmp_path: Path) -> None:
    from app.services.skill_catalog_ranker import rank_skills_for_prompt

    (tmp_path / "evolution").mkdir()
    (tmp_path / "evolution" / "skill_usage.json").write_text(
        json.dumps(
            {
                "cold-skill": {"state": "stale", "use_count": 0},
                "hot-skill": {"state": "active", "use_count": 10},
            }
        ),
        encoding="utf-8",
    )

    ranked = rank_skills_for_prompt(
        tmp_path,
        [_skill("hot-skill", description="Generic work"), _skill("cold-skill", description="Quarterly board deck")],
        scenario_text="Need the quarterly board deck workflow",
    )

    assert [skill.metadata.name for skill in ranked] == ["cold-skill", "hot-skill"]
