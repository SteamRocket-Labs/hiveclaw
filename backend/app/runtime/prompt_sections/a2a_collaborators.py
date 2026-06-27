"""§ A2A Collaborators section — governed agent-to-agent callable list."""

from __future__ import annotations

from typing import Any


def _agent_line(agent: dict[str, Any], *, relation_label: str) -> str:
    name = str(agent.get("name") or "Unnamed agent").strip()
    role = str(agent.get("role_description") or "").strip()
    status = str(agent.get("status") or "unknown").strip()
    agent_id = str(agent.get("id") or agent.get("agent_id") or "").strip()
    suffix = f" — {role}" if role else ""
    id_part = f" ({agent_id})" if agent_id else ""
    return f"- **{name}**{id_part}: {relation_label}, status={status}{suffix}"


def build_a2a_collaborators_section(read_model: dict[str, Any] | None, *, max_chars: int = 6000) -> str:
    """Build prompt-facing A2A collaborator context from the canonical read model."""

    if not read_model:
        return (
            "## A2A Collaborators\n\n"
            "No governed A2A collaborators are currently available for this session. "
            "Do not call `delegate_to_agent` or `send_message_to_agent` unless the user provides an explicit target "
            "that passes A2A policy."
        )

    parts: list[str] = []
    same_owner_agents = list(read_model.get("same_owner_agents") or [])
    public_agents = list(read_model.get("public_agents") or [])
    groups = list(read_model.get("collaboration_groups") or [])

    if same_owner_agents:
        lines = ["### Same-owner Agents", "These agents can be contacted directly through A2A."]
        lines.extend(_agent_line(agent, relation_label="same owner") for agent in same_owner_agents)
        parts.append("\n".join(lines))

    if public_agents:
        lines = ["### Public Agents", "These same-tenant public agents can be contacted directly through A2A."]
        lines.extend(_agent_line(agent, relation_label="public") for agent in public_agents)
        parts.append("\n".join(lines))

    group_blocks: list[str] = []
    for group in groups:
        members = list(group.get("members") or [])
        if not members:
            continue
        group_name = str(group.get("group_name") or "Unnamed group").strip()
        purpose = str(group.get("purpose") or "").strip()
        header = f"### A2A Collaboration Group: {group_name}"
        if purpose:
            header += f"\nPurpose: {purpose}"
        member_lines = [_agent_line(member, relation_label=f"group member/{member.get('role') or 'member'}") for member in members]
        group_blocks.append("\n".join([header, *member_lines]))
    parts.extend(group_blocks)

    if not parts:
        return (
            "## A2A Collaborators\n\n"
            "No governed A2A collaborators are currently available for this session. "
            "Do not call `delegate_to_agent` or `send_message_to_agent` unless the user provides an explicit target "
            "that passes A2A policy."
        )

    rendered = "## A2A Collaborators\n\n" + "\n\n".join(parts)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n...(A2A collaborator context truncated)"
    return rendered
