"""Parse SKILL.md style documents into normalized metadata/body records."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from .types import ParsedSkill, SkillMetadata

logger = logging.getLogger(__name__)


class SkillParser:
    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

    def parse_file(self, path: Path, *, relative_path: str, default_name: str | None = None) -> ParsedSkill:
        content = path.read_text(encoding="utf-8", errors="replace")
        return self.parse_content(
            content,
            path=path,
            relative_path=relative_path,
            default_name=default_name,
        )

    def parse_content(
        self,
        content: str,
        *,
        path: Path,
        relative_path: str,
        default_name: str | None = None,
    ) -> ParsedSkill:
        stripped = content.strip()
        name = (default_name or path.stem).replace("_", " ").replace("-", " ")
        description = ""
        body = stripped
        frontmatter: dict[str, Any] = {}

        match = self.FRONTMATTER_PATTERN.match(stripped)
        if match:
            body = match.group(2).strip()
            try:
                loaded = yaml.safe_load(match.group(1)) or {}
                if isinstance(loaded, dict):
                    frontmatter = loaded
                else:
                    logger.warning("Skill %s frontmatter is not a mapping", relative_path)
            except yaml.YAMLError as exc:
                logger.warning("Skill %s has invalid YAML frontmatter: %s", relative_path, exc)

        if frontmatter:
            name = self._string_value(frontmatter.get("name")) or name
            description = self._string_value(frontmatter.get("description"))[:1024]

        declared_tools = self._string_tuple(frontmatter.get("tools"))
        declared_packs = self._string_tuple(frontmatter.get("packs"))
        allowed_tools = self._string_tuple(frontmatter.get("allowed-tools"))
        is_system = self._bool_value(frontmatter.get("is_system"), default=False)

        metadata = frontmatter.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        nested_hive = metadata.get("hive")
        nested_hive = nested_hive if isinstance(nested_hive, dict) else {}

        def hive_value(key: str, default: Any = None) -> Any:
            return metadata.get(f"hive.{key}", nested_hive.get(key, default))

        if not description:
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:1024]
                    break

        estimated_runtime = self._optional_int(hive_value("estimated_runtime_minutes"))

        return ParsedSkill(
            metadata=SkillMetadata(
                name=name,
                description=description,
                declared_tools=tuple(declared_tools),
                declared_packs=tuple(declared_packs),
                is_system=is_system,
                license=self._string_value(frontmatter.get("license")),
                compatibility=self._string_value(frontmatter.get("compatibility")),
                allowed_tools=allowed_tools,
                version=self._string_value(hive_value("version")) or "0.0.0",
                pack=self._string_value(hive_value("pack")),
                requires_skills=self._string_tuple(hive_value("requires_skills")),
                locale=self._string_value(hive_value("locale")) or "cloud",
                invocation=self._string_value(hive_value("invocation")) or "both",
                cost_tier=self._string_value(hive_value("cost_tier")),
                estimated_runtime_minutes=estimated_runtime,
                output_artifacts=self._string_tuple(hive_value("output_artifacts")),
                author=self._string_value(hive_value("author")),
                security_zone=self._string_value(hive_value("security_zone")),
                raw_metadata=dict(metadata),
            ),
            body=body,
            file_path=path,
            relative_path=relative_path,
        )

    @staticmethod
    def _string_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @classmethod
    def _string_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            separators = "," if "," in value else None
            items = value.split(separators) if separators else value.split()
            return tuple(item.strip() for item in items if item.strip())
        if isinstance(value, (list, tuple, set)):
            return tuple(item for item in (cls._string_value(item) for item in value) if item)
        return (cls._string_value(value),) if cls._string_value(value) else ()

    @staticmethod
    def _bool_value(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "on"}
        return bool(value)

    @classmethod
    def _optional_int(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(cls._string_value(value))
        except (TypeError, ValueError):
            return None
