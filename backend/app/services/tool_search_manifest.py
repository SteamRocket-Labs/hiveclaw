"""Structured discovery manifest for tool_search."""

from __future__ import annotations

from typing import Any, Iterable


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _matches_query(query: str, *values: Any) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return True
    return any(normalized in _as_text(value).lower() for value in values)


def _loaded_tool_schema_entries(tool_names: Iterable[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_name in tool_names:
        name = _as_text(raw_name)
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "callable": True})
    return entries


def _mcp_candidate_entries(tool_names: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {"name": entry["name"], "source": "mcp_deferred_tool"}
        for entry in _loaded_tool_schema_entries(tool_names)
        if entry["name"].startswith(("mcp_", "mcp__"))
    ]


def _skill_candidate_entries(skills: Iterable[Any], query: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for skill in skills:
        metadata = getattr(skill, "metadata", None)
        name = _as_text(getattr(metadata, "name", ""))
        description = _as_text(getattr(metadata, "description", ""))
        declared_tools = list(getattr(metadata, "declared_tools", ()) or ())
        if not _matches_query(query, name, description, " ".join(declared_tools)):
            continue
        entries.append(
            {
                "name": name,
                "description": description,
                "declared_tools": declared_tools,
                "source": _as_text(getattr(skill, "relative_path", "")),
                "callable_after_search": False,
                "activation": "load_skill_for_guidance_only",
            }
        )
    return entries


def _subagent_candidate_entries(rows: Iterable[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        name = _as_text(row.get("name"))
        description = _as_text(row.get("description"))
        worker_type = _as_text(row.get("type"))
        if not _matches_query(query, name, description, worker_type):
            continue
        entries.append(
            {
                "name": name,
                "description": description,
                "type": worker_type,
                "scope": _as_text(row.get("scope")),
                "callable_after_search": False,
                "activation": "spawn_subagent_or_delegate_to_agent",
            }
        )
    return entries


def build_tool_search_manifest(
    *,
    query: str,
    loaded_tool_names: Iterable[str] = (),
    skills: Iterable[Any] = (),
    subagents: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    loaded_tool_schemas = _loaded_tool_schema_entries(loaded_tool_names)
    return {
        "schema": "hive.tool_search.discovery_manifest.v1",
        "query": _as_text(query),
        "loaded_tool_schemas": loaded_tool_schemas,
        "skill_candidates": _skill_candidate_entries(skills, query),
        "subagent_candidates": _subagent_candidate_entries(subagents, query),
        "mcp_candidates": _mcp_candidate_entries(entry["name"] for entry in loaded_tool_schemas),
    }


def render_tool_search_manifest_sections(manifest: dict[str, Any]) -> str:
    lines = ["Structured discovery manifest:"]
    sections = (
        ("loaded_tool_schemas", "name"),
        ("skill_candidates", "name"),
        ("subagent_candidates", "name"),
        ("mcp_candidates", "name"),
    )
    for section_name, label_key in sections:
        lines.append(f"{section_name}:")
        rows = manifest.get(section_name)
        if not isinstance(rows, list) or not rows:
            lines.append("- none")
            continue
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            label = _as_text(row.get(label_key))
            detail = _as_text(row.get("description") or row.get("source") or row.get("activation"))
            suffix = f" | {detail}" if detail else ""
            lines.append(f"- {label}{suffix}")
    return "\n".join(lines)
