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
) -> str:
    """Build the identity & mission section.

    Args:
        agent_name: Agent display name.
        role_description: Agent's assigned role description.
        invocation_scope: conversation | coordinator | task | heartbeat.
        soul_text: Personality text from soul.md (already stripped of heading).
        company_name: Current tenant display name; not an authority source.
    """
    template = _IDENTITY_BY_MODE.get(invocation_scope, _DEFAULT_IDENTITY)
    # Match assistantNameForDisplay in the member UI without renaming stored identity.
    default_assistant = agent_name == "My Assistant"
    agent_name = _quoted_display_name("我的助理" if default_assistant else agent_name)
    identity = template.format(agent_name=agent_name)

    parts = ["## Identity & Mission", identity]

    if role_description:
        parts.append(f"### Role\n{role_description}")

    if soul_text and soul_text not in ("_描述你的角色和职责。_", "_Describe your role and responsibilities._"):
        if role_description is not None:
            soul_text = project_bootstrap_soul_role(soul_text, role_description)
        parts.append(f"### Personality\n{soul_text}")

    parts.append(f"Your current display name is {agent_name}. Use this display name when introducing yourself.")
    if default_assistant:
        parts.append('In English conversations, the same display name is "My Assistant".')
    if company_name:
        parts.append(f"Your company is {_quoted_display_name(company_name)}. Use this name instead of any company name in Personality.")

    return "\n\n".join(parts)
