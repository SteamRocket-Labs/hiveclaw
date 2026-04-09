"""Readable tool catalog for prompt assembly and debugging."""

from __future__ import annotations

from collections import OrderedDict

from .registry import ToolRegistry


class ToolCatalog:
    """Render a compact grouped tool catalog from a registry."""

    CATEGORY_ORDER = [
        "File System",
        "Skills",
        "Scheduled",
        "IM Channel",
        "Web Search",
        "System",
    ]

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

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
            parts.append(f"\n### {category}")
            parts.extend(entries)
        return "\n".join(parts)
