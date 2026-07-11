"""Persistent subagent definitions (cut ⑤): 定义.md load / parse / render / store.

A named subagent entity (e.g. "market-research-explorer") is frozen into a
Markdown file with YAML frontmatter (the SubagentSpec fields) plus a body (the
system prompt) — structurally the same as a Claude Code ``agents/<name>.md``.

Storage is tenant-scoped: the store is constructed with a base directory (one
per tenant), so cross-tenant isolation is just separate roots — no shared path,
matching the Memory Control Plane's RLS-style invariant.

Cut C1 (§12) adds the agent-level scope on the same store class — CC's
project-level ``.claude/agents/`` mapped onto the agent workspace — plus the
single resolution chain agent → tenant → (builtin list rows): same parser,
same renderer, same name guard, zero new formats.
"""

from __future__ import annotations

import logging
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from app.agents.subagent import (
    _TYPE_PRESETS,
    PUBLIC_BUILTIN_SUBAGENT_TYPES,
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    ForkLevel,
    SubagentSpec,
    builtin_type_description,
    canonical_subagent_type,
)

logger = logging.getLogger(__name__)

_FRONTMATTER_DELIM = "---"
_VALID_ISOLATION = ("none", "all", "worktree")
_VALID_MEMORY_SCOPES = ("user", "project", "local")
_SAFE_SUBAGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Definition scopes (§12.4): one resolution chain, agent-level wins on name
# collision, builtins appear as read-only template rows in list surfaces.
SCOPE_AGENT = "agent"
SCOPE_TENANT = "tenant"
SCOPE_BUILTIN = "builtin"
SCOPE_SESSION = "session"


def validate_subagent_name(name: str) -> str:
    """Return a tenant-local filename-safe subagent name.

    The stores are tenant-scoped by base directory; this guard prevents a
    definition or memory name from escaping that base via path traversal.
    """

    value = str(name or "").strip()
    if not value or not _SAFE_SUBAGENT_NAME_RE.fullmatch(value) or ".." in value or "/" in value or "\\" in value:
        raise ValueError(
            "invalid subagent name: use 1-128 ASCII letters, digits, '.', '_' or '-' without path traversal"
        )
    return value


def _tenant_subagent_root(tenant_id: object, *, kind: str, agent_data_dir: Path | str | None = None) -> Path:
    from app.config import get_settings

    root = Path(agent_data_dir or get_settings().AGENT_DATA_DIR)
    return root / "_tenants" / str(tenant_id) / "subagents" / kind


def _coerce_isolation(value: object) -> ForkLevel:
    """Validate an isolation value from frontmatter.

    Fork is binary (CC-alignment §5.2). The retired ``brief`` middle level coerces
    to ``all`` for already-stored definitions (it wanted parent context — give the
    full fork rather than dropping context to ``none``)."""

    raw = str(value or "none")
    if raw == "brief":
        return "all"
    if raw in _VALID_ISOLATION:
        return cast("ForkLevel", raw)
    raise ValueError(f"invalid isolation {raw!r}: expected one of {_VALID_ISOLATION}")


def _front_value(front: dict, field_name: str, *aliases: str) -> object:
    for key in (field_name, *aliases):
        if key in front:
            return front.get(key)
    return None


def _coerce_tool_list(front: dict, field_name: str, *aliases: str) -> tuple[str, ...]:
    value = _front_value(front, field_name, *aliases)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a YAML list of non-empty tool names")
    tools: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain only non-empty string tool names")
        tools.append(item.strip())
    return tuple(tools)


def _coerce_memory_scope(front: dict) -> str | None:
    value = front.get("memory")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("memory must be a string or null")
    raw = value.strip()
    if not raw:
        return None
    if raw not in _VALID_MEMORY_SCOPES:
        raise ValueError(f"memory must be one of {_VALID_MEMORY_SCOPES}, got {raw!r}")
    return raw


def _coerce_optional_positive_int(front: dict, field_name: str, *aliases: str) -> int | None:
    value = _front_value(front, field_name, *aliases)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer or null")
    return value


def _coerce_optional_string(front: dict, field_name: str, *aliases: str) -> str | None:
    value = _front_value(front, field_name, *aliases)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    stripped = value.strip()
    return stripped or None


def _coerce_optional_bool(front: dict, field_name: str) -> bool:
    value = front.get(field_name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _coerce_string_tuple(front: dict, field_name: str, *aliases: str) -> tuple[str, ...]:
    value = _front_value(front, field_name, *aliases)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a YAML list of non-empty strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain only non-empty string values")
        items.append(item.strip())
    return tuple(items)


def _coerce_any_tuple(front: dict, field_name: str, *aliases: str) -> tuple[object, ...]:
    value = _front_value(front, field_name, *aliases)
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return (value,)
    raise ValueError(f"{field_name} must be a YAML list or mapping")


def _coerce_optional_dict(front: dict, field_name: str) -> dict | None:
    value = front.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a YAML mapping")
    return value


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

    raw_name = str(front.get("name") or "").strip()
    if not raw_name:
        raise ValueError("subagent definition missing required 'name'")
    name = validate_subagent_name(raw_name)

    # CC parity (parseAgentFromMarkdown): 'description' is required — it is the
    # whenToUse the parent model selects on; a definition without it is unusable.
    description = str(front.get("description") or "").strip()
    if not description:
        raise ValueError("subagent definition missing required 'description' (when should the parent use it?)")

    return SubagentSpec(
        name=name,
        description=description,
        type=canonical_subagent_type(front.get("type"), default=SUBAGENT_TYPE_GENERAL_PURPOSE),
        allowed_tools=_coerce_tool_list(front, "allowed_tools", "tools"),
        excluded_tools=_coerce_tool_list(front, "excluded_tools", "disallowedTools"),
        model=_coerce_optional_string(front, "model"),
        max_tool_rounds=_coerce_optional_positive_int(front, "max_tool_rounds", "maxTurns"),
        isolation=_coerce_isolation(front.get("isolation")),
        memory_scope=_coerce_memory_scope(front),
        system_prompt=body.strip(),
        background=_coerce_optional_bool(front, "background"),
        permission_mode=_coerce_optional_string(front, "permission_mode", "permissionMode"),
        skills=_coerce_string_tuple(front, "skills"),
        initial_prompt=_coerce_optional_string(front, "initial_prompt", "initialPrompt"),
        mcp_servers=_coerce_any_tuple(front, "mcp_servers", "mcpServers"),
        hooks=_coerce_optional_dict(front, "hooks"),
        color=_coerce_optional_string(front, "color"),
        effort=front.get("effort"),
    )


def render_subagent_definition(spec: SubagentSpec) -> str:
    """Render a SubagentSpec back to 定义.md text (round-trips ``parse``).

    Write-side mirror of the parse guard: anything rendered must read back, so
    an empty ``description`` is rejected here instead of producing a file that
    the next load refuses.
    """

    if not spec.description.strip():
        raise ValueError("subagent definition requires a non-empty 'description' (when should the parent use it?)")

    front = {
        "name": spec.name,
        "description": spec.description,
        "type": spec.type,
        "allowed_tools": list(spec.allowed_tools),
        "excluded_tools": list(spec.excluded_tools),
        "model": spec.model,
        "max_tool_rounds": spec.max_tool_rounds,
        "isolation": spec.isolation,
        "memory": spec.memory_scope,
        "background": spec.background,
        "permission_mode": spec.permission_mode,
        "skills": list(spec.skills),
        "initial_prompt": spec.initial_prompt,
        "mcp_servers": list(spec.mcp_servers),
        "hooks": spec.hooks,
        "color": spec.color,
        "effort": spec.effort,
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
        safe_name = validate_subagent_name(name)
        base = self.base_dir.resolve()
        path = (base / f"{safe_name}.md").resolve()
        if not path.is_relative_to(base):
            raise ValueError("invalid subagent name: path escapes tenant definition store")
        return path

    def save(self, spec: SubagentSpec) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(spec.name)
        path.write_text(render_subagent_definition(spec), encoding="utf-8")
        return path

    def load(self, name: str) -> SubagentSpec | None:
        safe_name = validate_subagent_name(name)
        path = self._path(name)
        if not path.exists():
            return None
        spec = parse_subagent_definition(path.read_text(encoding="utf-8"))
        if spec.name != safe_name:
            raise ValueError(f"frontmatter name {spec.name!r} mismatches file name {safe_name!r}")
        return spec

    def list_names(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return sorted(p.stem for p in self.base_dir.glob("*.md"))

    def delete(self, name: str) -> bool:
        """Delete a definition file; returns False when it does not exist."""

        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True


def definition_store_for_tenant(
    tenant_id: object, *, agent_data_dir: Path | str | None = None
) -> SubagentDefinitionStore:
    """Construct the tenant-scoped persistent subagent definition store."""

    return SubagentDefinitionStore(_tenant_subagent_root(tenant_id, kind="definitions", agent_data_dir=agent_data_dir))


def agent_subagent_root(agent_id: object, *, agent_data_dir: Path | str | None = None) -> Path:
    """Root of an agent's subagent scope: ``<AGENT_DATA_DIR>/<agent_id>/subagents``."""

    from app.config import get_settings

    root = Path(agent_data_dir or get_settings().AGENT_DATA_DIR)
    return root / str(agent_id) / "subagents"


def definition_store_for_agent(
    agent_id: object, *, agent_data_dir: Path | str | None = None
) -> SubagentDefinitionStore:
    """Construct the agent-scoped definition store (daily driver, §12.2).

    Same format, parser, renderer and name guard as the tenant store — the
    scope is just a different base directory inside the agent's workspace, so
    the agent itself can author these files via governed workspace writes.
    """

    return SubagentDefinitionStore(agent_subagent_root(agent_id, agent_data_dir=agent_data_dir))


@dataclass(slots=True)
class ResolvedSubagentDefinition:
    """A definition resolved through the scope chain, tagged with its origin."""

    spec: SubagentSpec
    scope: str  # SCOPE_AGENT | SCOPE_TENANT


def resolve_subagent_definition(
    name: str,
    *,
    agent_id: object | None,
    tenant_id: object | None,
    agent_data_dir: Path | str | None = None,
) -> ResolvedSubagentDefinition | None:
    """Resolve a named definition through the single chain (§12.4).

    agent-level ``<workspace>/subagents/<name>.md`` wins; tenant-level
    ``_tenants/<tid>/subagents/definitions/<name>.md`` is the fallback.
    Returns ``None`` when neither scope has the name (callers attach the
    merged available list to their error).
    """

    if agent_id is not None:
        spec = definition_store_for_agent(agent_id, agent_data_dir=agent_data_dir).load(name)
        if spec is not None:
            return ResolvedSubagentDefinition(spec=spec, scope=SCOPE_AGENT)
    if tenant_id is not None:
        spec = definition_store_for_tenant(tenant_id, agent_data_dir=agent_data_dir).load(name)
        if spec is not None:
            return ResolvedSubagentDefinition(spec=spec, scope=SCOPE_TENANT)
    return None


def resolve_runtime_subagent_definition(
    name: str,
    *,
    agent_id: object | None,
    tenant_id: object | None,
    workspace: Path | str,
    session_id: object | None,
    agent_data_dir: Path | str | None = None,
) -> ResolvedSubagentDefinition | None:
    """Resolve the current session overlay before durable Agent/Tenant scopes."""

    raw_session_id = str(session_id or "").strip()
    if raw_session_id:
        try:
            safe_session_id = str(uuid.UUID(raw_session_id))
        except ValueError:
            safe_session_id = ""
        if safe_session_id:
            root = Path(workspace).resolve()
            overlay = (root / "session_extensions" / safe_session_id / "subagents").resolve()
            if overlay.is_relative_to(root):
                spec = SubagentDefinitionStore(overlay).load(name)
                if spec is not None:
                    return ResolvedSubagentDefinition(spec=spec, scope=SCOPE_SESSION)
    return resolve_subagent_definition(
        name,
        agent_id=agent_id,
        tenant_id=tenant_id,
        agent_data_dir=agent_data_dir,
    )


def _definition_row(spec: SubagentSpec, scope: str) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "scope": scope,
        "type": spec.type,
        "model": spec.model,
        "isolation": spec.isolation,
        "memory": spec.memory_scope,
        "max_tool_rounds": spec.max_tool_rounds,
        "allowed_tools": list(spec.allowed_tools),
        "excluded_tools": list(spec.excluded_tools),
    }


def list_subagent_definitions(
    *,
    agent_id: object | None,
    tenant_id: object | None,
    agent_data_dir: Path | str | None = None,
) -> list[dict]:
    """Merged definition list across scopes (§12.4): agent wins on name collision,
    tenant fills the rest, builtin types close as read-only template rows.

    A definition file that fails to parse is skipped with a warning — one
    corrupt file must not blank the whole discovery surface.
    """

    rows: dict[str, dict] = {}

    def _collect(store: SubagentDefinitionStore, scope: str) -> None:
        for name in store.list_names():
            if name in rows:
                continue
            try:
                spec = store.load(name)
            except ValueError as exc:
                logger.warning("[Subagent] skipping unparseable definition %r in %s scope: %s", name, scope, exc)
                continue
            if spec is not None:
                rows[name] = _definition_row(spec, scope)

    if agent_id is not None:
        _collect(definition_store_for_agent(agent_id, agent_data_dir=agent_data_dir), SCOPE_AGENT)
    if tenant_id is not None:
        _collect(definition_store_for_tenant(tenant_id, agent_data_dir=agent_data_dir), SCOPE_TENANT)

    for builtin_type in PUBLIC_BUILTIN_SUBAGENT_TYPES:
        if builtin_type in rows:
            continue  # custom definition shadows the builtin row
        preset_tools = _TYPE_PRESETS[builtin_type]
        rows[builtin_type] = {
            "name": builtin_type,
            "description": builtin_type_description(builtin_type),
            "scope": SCOPE_BUILTIN,
            "type": builtin_type,
            "model": None,
            "isolation": "none",
            "max_tool_rounds": None,
            "allowed_tools": list(preset_tools),
            "excluded_tools": [],
        }

    return sorted(rows.values(), key=lambda row: row["name"])


def subagent_definition_signature(
    *,
    agent_id: object | None,
    tenant_id: object | None,
    agent_data_dir: Path | str | None = None,
) -> str:
    """Return a stable file-level signature for prompt-visible subagent definitions."""
    entries: list[dict[str, object]] = []

    def _collect(store: SubagentDefinitionStore, scope: str) -> None:
        if not store.base_dir.exists():
            return
        for path in sorted(store.base_dir.glob("*.md")):
            try:
                stat = path.stat()
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                logger.debug("[Subagent] failed to hash definition %s in %s scope: %s", path, scope, exc)
                continue
            entries.append(
                {
                    "scope": scope,
                    "name": path.stem,
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "content_hash": content_hash,
                }
            )

    if agent_id is not None:
        _collect(definition_store_for_agent(agent_id, agent_data_dir=agent_data_dir), SCOPE_AGENT)
    if tenant_id is not None:
        _collect(definition_store_for_tenant(tenant_id, agent_data_dir=agent_data_dir), SCOPE_TENANT)
    entries.append(
        {
            "scope": SCOPE_BUILTIN,
            "names": list(PUBLIC_BUILTIN_SUBAGENT_TYPES),
        }
    )
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
