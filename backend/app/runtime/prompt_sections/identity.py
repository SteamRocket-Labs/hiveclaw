"""§ Identity section — agent name, role, execution mode, personality."""

from __future__ import annotations

import json

from app.services.bootstrap_soul import project_bootstrap_soul_role


_IDENTITY_BY_MODE = {
    "coordinator": (
        "You are {agent_name}, operating in coordinator mode. "
        "Your role is to orchestrate work across worker agents — decompose, delegate, synthesize, and verify. "
        "You do NOT execute domain tools yourself, and you do NOT delegate a task back to yourself "
        "(self-delegation creates infinite loops). If no suitable worker exists, report the gap "
        "to the user instead of inventing a delegation path."
    ),
    "task": (
        "You are {agent_name}, executing an assigned task autonomously. "
        "Focus on completing the task thoroughly without asking follow-up questions."
    ),
    "heartbeat": (
        "You are {agent_name}, in self-evolution mode. "
        "Observe your performance, take one focused action, learn from the outcome."
    ),
}

_DEFAULT_IDENTITY = (
    "You are {agent_name}, an enterprise digital employee. You assist users through conversation, "
    "using tools to read/write files, search the web, communicate with colleagues, and execute code."
)


def _quoted_display_name(name: str) -> str:
    return json.dumps("".join(c for c in name if c.isprintable()).strip()[:200], ensure_ascii=False)


def build_identity_section(
    agent_name: str,
    role_description: str | None = None,
    invocation_scope: str = "conversation",
    soul_text: str = "",
    company_name: str | None = None,
    member_assistant: bool = False,
) -> str:
    """Build the identity & mission section.

    Args:
        agent_name: Agent display name.
        role_description: Agent's assigned role description.
        invocation_scope: conversation | coordinator | task | heartbeat.
        soul_text: Personality text from soul.md (already stripped of heading).
        company_name: Current tenant display name; not an authority source.
        member_assistant: This employee is bound as the requesting member's assistant.
    """
    template = _IDENTITY_BY_MODE.get(invocation_scope, _DEFAULT_IDENTITY)
    # Match the member's bound-assistant label without renaming stored identity.
    agent_name = _quoted_display_name(agent_name)
    identity = template.format(agent_name="the requesting member's own assistant" if member_assistant else agent_name)

    parts = ["## Identity & Mission", identity]

    if role_description:
        parts.append(f"### Role\n{role_description}")

    if soul_text and soul_text not in ("_描述你的角色和职责。_", "_Describe your role and responsibilities._"):
        if role_description is not None:
            soul_text = project_bootstrap_soul_role(soul_text, role_description)
        parts.append(f"### Personality\n{soul_text}")

    if member_assistant:
        parts.append('The member sees you as "我的助理" in the Chinese interface or "My Assistant" in the English interface, '
            'regardless of the stored employee name. In member replies, call yourself by the name that matches the '
            'member\'s language ("我的助理" when writing Chinese), introduce yourself naturally as their assistant '
            '(for example "我是你的助理"), and never quote the stored default name, an id, or a status code as your identity.')
    else:
        parts.append(f"Your current display name is {agent_name}. Use this display name when introducing yourself.")
    if company_name:
        parts.append(f"Your company is {_quoted_display_name(company_name)}. Use this name instead of any company name in Personality.")

    return "\n\n".join(parts)
