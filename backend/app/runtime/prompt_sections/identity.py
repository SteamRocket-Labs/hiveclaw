"""§ Identity section — agent name, role, execution mode, personality."""

from __future__ import annotations


_IDENTITY_BY_MODE = {
    "coordinator": (
        "You are {agent_name}, operating in coordinator mode. "
        "Your role is to orchestrate work across worker agents — decompose, delegate, synthesize, and verify."
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


def build_identity_section(
    agent_name: str,
    role_description: str = "",
    execution_mode: str = "conversation",
    soul_text: str = "",
) -> str:
    """Build the identity & mission section.

    Args:
        agent_name: Agent display name.
        role_description: Agent's assigned role description.
        execution_mode: conversation | coordinator | task | heartbeat.
        soul_text: Personality text from soul.md (already stripped of heading).
    """
    template = _IDENTITY_BY_MODE.get(execution_mode, _DEFAULT_IDENTITY)
    identity = template.format(agent_name=agent_name)

    parts = ["## Identity & Mission", identity]

    if role_description:
        parts.append(f"### Role\n{role_description}")

    if soul_text and soul_text not in ("_描述你的角色和职责。_", "_Describe your role and responsibilities._"):
        parts.append(f"### Personality\n{soul_text}")

    return "\n\n".join(parts)
