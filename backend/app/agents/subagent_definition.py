"""Persistent subagent definitions (cut ⑤): 定义.md load / parse / render / store.

A named subagent entity (e.g. "market-research-explorer") is frozen into a
Markdown file with YAML frontmatter (the SubagentSpec fields) plus a body (the
system prompt) — structurally the same as a Claude Code ``agents/<name>.md``.

Storage is tenant-scoped: the store is constructed with a base directory (one
per tenant), so cross-tenant isolation is just separate roots — no shared path,
matching the Memory Control Plane's RLS-style invariant.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import yaml

from app.agents.subagent import SUBAGENT_TYPE_EXPLORER, ForkLevel, SubagentSpec

logger = logging.getLogger(__name__)

_FRONTMATTER_DELIM = "---"
_VALID_ISOLATION = ("none", "brief", "all")


def _coerce_isolation(value: object) -> ForkLevel:
    """Validate an isolation value from frontmatter; fall back to 'none'."""

    raw = str(value or "none")
    if raw in _VALID_ISOLATION:
        return cast("ForkLevel", raw)
    return "none"


def parse_subagent_definition(text: str) -> SubagentSpec:
    """Parse a 定义.md (YAML frontmatter + Markdown body) into a SubagentSpec.

    Everything after the frontmatter block becomes ``system_prompt``.
    """

    front: dict = {}
    body = text
    stripped = text.lstrip()
    if stripped.startswith(_FRONTMATTER_DELIM):
        after = stripped[len(_FRONTMATTER_DELIM) :]
        end = after.find("\n" + _FRONTMATTER_DELIM)
        if end != -1:
            yaml_block = after[:end]
            body = after[end + len("\n" + _FRONTMATTER_DELIM) :].lstrip("\n")
            loaded = yaml.safe_load(yaml_block) or {}
            if isinstance(loaded, dict):
                front = loaded

    name = str(front.get("name") or "").strip()
    if not name:
        raise ValueError("subagent definition missing required 'name'")

    return SubagentSpec(
        name=name,
        type=str(front.get("type") or SUBAGENT_TYPE_EXPLORER),
        allowed_tools=tuple(front.get("allowed_tools") or ()),
        excluded_tools=tuple(front.get("excluded_tools") or ()),
        model=front.get("model"),
        max_tool_rounds=front.get("max_tool_rounds"),
        isolation=_coerce_isolation(front.get("isolation")),
        system_prompt=body.strip(),
    )


def render_subagent_definition(spec: SubagentSpec) -> str:
    """Render a SubagentSpec back to 定义.md text (round-trips ``parse``)."""

    front = {
        "name": spec.name,
        "type": spec.type,
        "allowed_tools": list(spec.allowed_tools),
        "excluded_tools": list(spec.excluded_tools),
        "model": spec.model,
        "max_tool_rounds": spec.max_tool_rounds,
        "isolation": spec.isolation,
    }
    yaml_block = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
    return f"{_FRONTMATTER_DELIM}\n{yaml_block}\n{_FRONTMATTER_DELIM}\n\n{spec.system_prompt}\n"


class SubagentDefinitionStore:
    """Tenant-scoped store for persistent subagent 定义.md files.

    Construct with the tenant's base directory; cross-tenant isolation is just a
    different ``base_dir`` (no shared path). Files live at ``<base_dir>/<name>.md``.
    """

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)

    def _path(self, name: str) -> Path:
        return self.base_dir / f"{name}.md"

    def save(self, spec: SubagentSpec) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(spec.name)
        path.write_text(render_subagent_definition(spec), encoding="utf-8")
        return path

    def load(self, name: str) -> SubagentSpec | None:
        path = self._path(name)
        if not path.exists():
            return None
        return parse_subagent_definition(path.read_text(encoding="utf-8"))

    def list_names(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return sorted(p.stem for p in self.base_dir.glob("*.md"))
