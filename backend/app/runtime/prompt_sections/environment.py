"""§ Environment section — runtime context, user, channel, timestamp."""

from __future__ import annotations

from datetime import datetime, timezone


def build_environment_section(
    user_name: str = "",
    channel: str = "",
    agent_name: str = "",
    include_time: bool = True,
) -> str:
    """Build the environment info section with runtime context."""
    lines = ["## Environment"]

    if agent_name:
        lines.append(f"- Agent: {agent_name}")
    if user_name:
        lines.append(f"- Current user: {user_name}")
    if channel:
        lines.append(f"- Channel: {channel}")
    # Recency reminder: MEMBER_LANGUAGE_RULE sits in the frozen prefix far above;
    # models follow it reliably only when it is restated near the end of the system
    # prompt. This section is below the prompt-cache boundary, so the cache-stable
    # prefix bytes are unaffected.
    lines.append("- Language: write every reply to the member, including progress notes between tool calls, in the member's language (Chinese if unclear).")
    if include_time:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"- Current time: {now}")

    return "\n".join(lines)
