"""Parse SKILL.md style documents into normalized metadata/body records."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from .types import ParsedSkill, SkillMetadata

logger = logging.getLogger(__name__)
_WARNED_FRONTMATTER_ISSUES: set[tuple[str, str, str]] = set()
_WARNED_FRONTMATTER_LOCK = threading.Lock()


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
                    self._warn_frontmatter_once(relative_path, "not_a_mapping", "frontmatter is not a mapping")
            except yaml.YAMLError as exc:
                self._warn_frontmatter_once(relative_path, "invalid_yaml", str(exc))

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

        def frontmatter_value(*keys: str, default: Any = None) -> Any:
            for key in keys:
                if key in frontmatter:
                    return frontmatter[key]
            for key in keys:
                value = hive_value(key, None)
                if value is not None:
                    return value
            return default

        if not description:
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:1024]
                    break

        # Step 9: only the consumed fields are mapped into SkillMetadata. Any
        # other frontmatter keys (license/version/cost_tier/security_zone/…) are
        # parsed by yaml above and silently ignored — legacy files stay valid.
        return ParsedSkill(
            metadata=SkillMetadata(
                name=name,
                description=description,
                declared_tools=tuple(declared_tools),
                declared_packs=tuple(declared_packs),
                is_system=is_system,
                allowed_tools=allowed_tools,
                pack=self._string_value(hive_value("pack")),
                requires_skills=self._string_tuple(hive_value("requires_skills")),
                disable_model_invocation=self._bool_value(
                    frontmatter_value(
                        "disable-model-invocation",
                        "disable_model_invocation",
                        default=False,
                    ),
                    default=False,
                )
                or not self._bool_value(frontmatter_value("model-invocable", "model_invocable", default=True), default=True),
                user_invocable=self._bool_value(
                    frontmatter_value("user-invocable", "user_invocable", default=True),
                    default=True,
                ),
                hidden=self._bool_value(frontmatter_value("hidden", "isHidden", "is_hidden", default=False), default=False),
                when_to_use=self._string_value(frontmatter_value("when_to_use", "when-to-use")),
                context=self._string_value(frontmatter_value("context")),
                agent=self._string_value(frontmatter_value("agent")),
                hooks=self._string_tuple(frontmatter_value("hooks")),
                paths=self._string_tuple(frontmatter_value("paths", "path")),
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

    @staticmethod
    def _warn_frontmatter_once(relative_path: str, issue_type: str, detail: str) -> None:
        signature = (relative_path, issue_type, detail)
        with _WARNED_FRONTMATTER_LOCK:
            if signature in _WARNED_FRONTMATTER_ISSUES:
                return
            _WARNED_FRONTMATTER_ISSUES.add(signature)

        if issue_type == "not_a_mapping":
            logger.warning("Skill %s frontmatter is not a mapping", relative_path)
            return
        logger.warning("Skill %s has invalid YAML frontmatter: %s", relative_path, detail)
