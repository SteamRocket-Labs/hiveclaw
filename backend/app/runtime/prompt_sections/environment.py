"""§ Environment section — runtime context, user, channel, timestamp."""

from __future__ import annotations

from datetime import datetime, timezone


def build_environment_section(
    user_name: str = "",
    channel: str = "",
    agent_name: str = "",
) -> str:
    """Build the environment info section with runtime context."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["## Environment"]

    if agent_name:
        lines.append(f"- Agent: {agent_name}")
    if user_name:
        lines.append(f"- Current user: {user_name}")
    if channel:
        lines.append(f"- Channel: {channel}")
    lines.append(f"- Current time: {now}")

    return "\n".join(lines)
