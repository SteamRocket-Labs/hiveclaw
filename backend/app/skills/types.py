"""Shared skill types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillMetadata:
    name: str
    description: str
    declared_tools: tuple[str, ...] = ()
    declared_packs: tuple[str, ...] = ()
    is_system: bool = False
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: tuple[str, ...] = ()
    version: str = "0.0.0"
    pack: str | None = None
    requires_skills: tuple[str, ...] = ()
    locale: str = "cloud"
    invocation: str = "both"
    cost_tier: str | None = None
    estimated_runtime_minutes: int | None = None
    output_artifacts: tuple[str, ...] = ()
    author: str | None = None
    security_zone: str | None = None
    raw_metadata: dict | None = None


@dataclass(slots=True)
class ParsedSkill:
    metadata: SkillMetadata
    body: str
    file_path: Path
    relative_path: str
