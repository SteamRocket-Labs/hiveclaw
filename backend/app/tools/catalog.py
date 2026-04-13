"""Readable tool catalog for prompt assembly and debugging."""

from __future__ import annotations

from collections import OrderedDict

from .registry import ToolRegistry


class ToolCatalog:
    """Render a compact grouped tool catalog from a registry."""

    CATEGORY_ORDER = [
        "filesystem",
        "skills",
        "triggers",
        "communication",
        "search",
        "mcp",
        "email",
        "memory",
        "plaza",
        "feishu",
        "hr",
        "system",
    ]
    CATEGORY_LABELS = {
        "filesystem": "File System",
        "skills": "Skills",
        "triggers": "Scheduled",
        "communication": "IM Channel",
        "search": "Web Search",
        "mcp": "MCP",
        "email": "Email",
        "memory": "Memory",
        "plaza": "Plaza",
        "feishu": "Feishu",
        "hr": "HR",
        "system": "System",
    }

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    @classmethod
    def _label_for_category(cls, category: str) -> str:
        return cls.CATEGORY_LABELS.get(category, category.replace("_", " ").title())

    def render(self) -> str:
        categories: "OrderedDict[str, list[str]]" = OrderedDict()
        for category in self.CATEGORY_ORDER:
            categories[category] = []

        for tool in self.registry.values():
            categories.setdefault(tool.category, [])
            categories[tool.category].append(f"- `{tool.name}`: {tool.description}")

        parts = ["## Available Tools"]
        for category, entries in categories.items():
            if not entries:
                continue
            parts.append(f"\n### {self._label_for_category(category)}")
            parts.extend(entries)
        return "\n".join(parts)
