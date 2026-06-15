"""Shared skill types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillMetadata:
    # Step 9: trimmed to the fields the runtime actually consumes. The parser
    # still tolerates (and ignores) any other frontmatter keys, so legacy skill
    # files carrying license/version/cost_tier/etc. parse without error — those
    # 11 dead fields were parsed-but-never-read and are gone (zero consumers
    # verified across the skill data flow).
    name: str
    description: str
    declared_tools: tuple[str, ...] = ()
    declared_packs: tuple[str, ...] = ()
    is_system: bool = False
    allowed_tools: tuple[str, ...] = ()  # Step 9: surfaced as load_skill scope guidance
    pack: str | None = None
    requires_skills: tuple[str, ...] = ()


@dataclass(slots=True)
class ParsedSkill:
    metadata: SkillMetadata
    body: str
    file_path: Path
    relative_path: str
