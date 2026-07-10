"""Shared parsing boundary for external capability Markdown frontmatter."""

from __future__ import annotations

from typing import Any

import yaml


def split_yaml_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    stripped = content.strip()
    if not stripped.startswith("---\n"):
        return {}, stripped
    remainder = stripped[4:]
    if "\n---\n" not in remainder:
        return {}, stripped
    frontmatter_text, body = remainder.split("\n---\n", 1)
    loaded = yaml.safe_load(frontmatter_text) or {}
    return (loaded if isinstance(loaded, dict) else {}), body.strip()
