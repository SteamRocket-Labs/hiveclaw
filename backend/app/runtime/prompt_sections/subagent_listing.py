"""Prompt-facing listing for session-local subagent worker types."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.subagent import (
    PUBLIC_BUILTIN_SUBAGENT_TYPES,
    builtin_type_description,
)
from app.agents.subagent_definition import SCOPE_BUILTIN, list_subagent_definitions
from app.runtime.activation_candidates import ActivationCandidate, ActivationScore, ActivationSurface
from app.runtime.context_candidates import build_metadata_activation_keys

_BUILTIN_ORDER = PUBLIC_BUILTIN_SUBAGENT_TYPES


def _render_custom_definition_rows(
    *,
    agent_id: Any | None,
    tenant_id: Any | None,
    agent_data_dir: Path | str | None,
) -> list[str]:
    if agent_id is None and tenant_id is None:
        return []
    rows = [
        row
        for row in list_subagent_definitions(agent_id=agent_id, tenant_id=tenant_id, agent_data_dir=agent_data_dir)
        if row.get("scope") != SCOPE_BUILTIN
    ]
    if not rows:
        return []
    lines = [
        "",
        "### Custom Session Worker Definitions",
        "Use these with the same `spawn_subagent` tool by setting `definition_name`; do not use A2A delegation for them.",
        "",
    ]
    for row in rows:
        name = str(row.get("name") or "")
        scope = str(row.get("scope") or "custom")
        worker_type = str(row.get("type") or "general-purpose")
        description = str(row.get("description") or "")
        extra: list[str] = []
        max_rounds = row.get("max_tool_rounds")
        if max_rounds:
            extra.append(f"max_tool_rounds={max_rounds}")
        allowed_tools = row.get("allowed_tools")
        if isinstance(allowed_tools, list) and allowed_tools:
            extra.append("tools=" + ",".join(str(tool) for tool in allowed_tools))
        suffix = f" [{'; '.join(extra)}]" if extra else ""
        lines.append(f"- `{name}` ({scope}, type=`{worker_type}`): {description}{suffix}")
    return lines


def _subagent_activation_keys_for_row(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("name") or "").strip()
    scope = str(row.get("scope") or "custom").strip()
    worker_type = str(row.get("type") or "general-purpose").strip()
    source_type = "subagent_builtin" if scope == SCOPE_BUILTIN else "subagent_definition"
    allowed_tools = [str(tool) for tool in row.get("allowed_tools") or () if str(tool).strip()]
    key_features = {
        "name": [name],
        "scope": [scope],
        "type": [worker_type],
        "description_terms": _subagent_terms(str(row.get("description") or "")),
        "allowed_tools": allowed_tools,
    }
    value_pointer = {
        "loader": "spawn_subagent",
        "subagent_type": worker_type,
    }
    if scope != SCOPE_BUILTIN:
        value_pointer["definition_name"] = name
    return build_metadata_activation_keys(
        candidate_kind="subagent",
        item_id=name,
        source_type=source_type,
        key_features=key_features,
        value_pointer=value_pointer,
        source_refs=[f"subagent:{scope}:{name}"],
        ref_kind="subagent",
        payload=row,
    )


def _subagent_terms(value: str) -> list[str]:
    import re

    return sorted({token for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,}", value.lower())})


def build_subagent_activation_key_manifest(
    *,
    agent_id: Any | None = None,
    tenant_id: Any | None = None,
    agent_data_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    rows = list_subagent_definitions(agent_id=agent_id, tenant_id=tenant_id, agent_data_dir=agent_data_dir)
    return [_subagent_activation_keys_for_row(row) for row in rows]


def gather_subagent_candidates(
    *,
    agent_id: Any | None = None,
    tenant_id: Any | None = None,
    agent_data_dir: Path | str | None = None,
    limit: int | None = None,
) -> list[ActivationCandidate]:
    manifests = build_subagent_activation_key_manifest(
        agent_id=agent_id,
        tenant_id=tenant_id,
        agent_data_dir=agent_data_dir,
    )
    candidates: list[ActivationCandidate] = []
    del limit
    for index, manifest in enumerate(manifests):
        key_features = dict(manifest.get("key_features") or {})
        value_pointer = dict(manifest.get("value_pointer") or {})
        name = next(iter(key_features.get("name") or ()), "")
        worker_type = next(iter(key_features.get("type") or ()), "")
        scope = next(iter(key_features.get("scope") or ()), "")
        preview = f"{name} ({scope}, type={worker_type})".strip()
        source_refs = tuple(str(ref) for ref in manifest.get("source_refs") or () if str(ref).strip())
        score = max(0.1, 1.0 - (index * 0.01))
        candidates.append(
            ActivationCandidate(
                candidate_kind="subagent",
                candidate_ref=dict(manifest["candidate_ref"]),
                key_features=key_features,
                value_pointer=value_pointer,
                surface=ActivationSurface(
                    surface_kind="subagent_listing",
                    preview=preview,
                    token_estimate=max(1, len(preview) // 4),
                    source_refs=source_refs,
                ),
                source_refs=source_refs,
                score=ActivationScore(
                    head_scores={"subagent_rank": score},
                    total_score=score,
                    reasons=("available_session_worker",),
                    scorer="subagent_listing_gatherer",
                ),
                metadata={
                    "name": name,
                    "scope": scope,
                    "type": worker_type,
                },
            )
        )
    return candidates


def build_subagent_listing_section(
    *,
    agent_id: Any | None = None,
    tenant_id: Any | None = None,
    agent_data_dir: Path | str | None = None,
    activation_key_manifest: list[dict[str, Any]] | None = None,
) -> str:
    """Render the always-visible session worker type list.

    This mirrors CC's persistent agent-type routing signal without adding a
    second execution path: every entry routes to the same ``spawn_subagent``
    tool, while real digital-employee collaboration remains on A2A tools.
    """
    if activation_key_manifest is not None:
        activation_key_manifest[:] = build_subagent_activation_key_manifest(
            agent_id=agent_id,
            tenant_id=tenant_id,
            agent_data_dir=agent_data_dir,
        )

    lines = [
        "## Session Worker Types",
        "",
        "These are To Session Worker types for `spawn_subagent`; they are not A2A employees and do not require A2A Collaborators.",
        "Use `prompt` for the worker instruction and `subagent_type` to choose the type.",
        "",
    ]
    for name in _BUILTIN_ORDER:
        description = builtin_type_description(name)
        lines.append(f"- `{name}`: {description}")
    lines.extend(
        [
            "",
            "## Agent Team vs Session Workers",
            "",
            "If the user explicitly asks for Agent Team, team, swarm, named teammates, or a multi-role team: Do not silently downgrade to plain one-shot Session Workers.",
            "Agent Team creation follows deferred-tool discovery: if `team_create` is not currently callable, call `tool_search` with `select:team_create` first, then call `team_create` to create the Team container.",
            "After the Team container exists, spawn each teammate with `spawn_subagent` using both `team_name` and `name`; that creates enterable Team member sessions.",
            "Use ordinary `spawn_subagent` without `team_name` only for lightweight one-shot work that does not need persistent named teammate sessions.",
            "Use Dynamic Workflow instead of Agent Team when the requirement is fixed step order, gate/wait/resume, budgeted fan-out, or deterministic orchestration rather than named teammates.",
        ]
    )
    lines.extend(
        _render_custom_definition_rows(
            agent_id=agent_id,
            tenant_id=tenant_id,
            agent_data_dir=agent_data_dir,
        )
    )
    return "\n".join(lines)
