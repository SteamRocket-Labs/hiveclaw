"""Parse SKILL.md style documents into normalized metadata/body records."""

from __future__ import annotations

import re
from pathlib import Path

from .types import ParsedSkill, SkillMetadata


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
        declared_tools: list[str] = []
        declared_packs: list[str] = []
        is_system = False
        body = stripped

        match = self.FRONTMATTER_PATTERN.match(stripped)
        if match:
            frontmatter = match.group(1)
            body = match.group(2).strip()
            lines = frontmatter.splitlines()
            i = 0
            while i < len(lines):
                raw_line = lines[i]
                line = raw_line.strip()
                if line.lower().startswith("name:"):
                    value = line[5:].strip().strip('"').strip("'")
                    if value:
                        name = value
                elif line.lower().startswith("description:"):
                    value = line[12:].strip().strip('"').strip("'")
                    if value:
                        description = value[:200]
                elif line.lower().startswith("tools:"):
                    inline_value = line[6:].strip()
                    if inline_value:
                        declared_tools.extend(
                            item.strip().strip('"').strip("'")
                            for item in inline_value.strip("[]").split(",")
                            if item.strip()
                        )
                    else:
                        i += 1
                        while i < len(lines):
                            tool_line = lines[i]
                            stripped_tool_line = tool_line.strip()
                            if stripped_tool_line.startswith("- "):
                                value = stripped_tool_line[2:].strip().strip('"').strip("'")
                                if value:
                                    declared_tools.append(value)
                                i += 1
                                continue
                            if tool_line.startswith((" ", "\t")) and not stripped_tool_line:
                                i += 1
                                continue
                            i -= 1
                            break
                elif line.lower().startswith("is_system:"):
                    value = line[10:].strip().strip('"').strip("'").lower()
                    is_system = value in ("true", "yes", "1")
                elif line.lower().startswith("packs:"):
                    inline_value = line[6:].strip()
                    if inline_value:
                        declared_packs.extend(
                            item.strip().strip('"').strip("'")
                            for item in inline_value.strip("[]").split(",")
                            if item.strip()
                        )
                    else:
                        i += 1
                        while i < len(lines):
                            pack_line = lines[i]
                            stripped_pack_line = pack_line.strip()
                            if stripped_pack_line.startswith("- "):
                                value = stripped_pack_line[2:].strip().strip('"').strip("'")
                                if value:
                                    declared_packs.append(value)
                                i += 1
                                continue
                            if pack_line.startswith((" ", "\t")) and not stripped_pack_line:
                                i += 1
                                continue
                            i -= 1
                            break
                i += 1

        if not description:
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:200]
                    break

        return ParsedSkill(
            metadata=SkillMetadata(
                name=name,
                description=description,
                declared_tools=tuple(declared_tools),
                declared_packs=tuple(declared_packs),
                is_system=is_system,
            ),
            body=body,
            file_path=path,
            relative_path=relative_path,
        )
