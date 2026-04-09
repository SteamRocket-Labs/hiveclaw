"""Shared tool definition types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolDefinition:
    """Normalized tool definition used by the internal registry."""

    name: str
    description: str
    parameters: dict[str, Any]
    category: str
    raw_schema: dict[str, Any]
    read_only: bool = False
    parallel_safe: bool = False

    @classmethod
    def from_openai_tool(cls, tool: dict[str, Any], category: str) -> "ToolDefinition":
        fn = tool.get("function", {})
        return cls(
            name=fn.get("name", ""),
            description=fn.get("description", ""),
            parameters=fn.get("parameters", {"type": "object", "properties": {}}),
            category=category,
            raw_schema=tool,
        )

    def to_openai_tool(self) -> dict[str, Any]:
        return self.raw_schema
