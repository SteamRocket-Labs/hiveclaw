"""§ Triggers section — active triggers configured for the agent."""

from __future__ import annotations


def build_triggers_section(triggers: list[dict], *, budget_chars: int = 3000) -> str:
    """Build the active triggers section.

    Args:
        triggers: List of trigger dicts with keys: name, type, config, reason, focus_ref.
        budget_chars: Max chars for the trigger list.
    """
    if not triggers:
        return ""

    lines = ["## Active Triggers", "", "You have the following active triggers:"]
    chars_used = 0
    for i, t in enumerate(triggers):
        name = t.get("name", "?")
        ttype = t.get("type", "?")
        config_str = str(t.get("config", ""))[:80]
        reason_str = (t.get("reason", "") or "")[:500]
        ref_str = f" (focus: {t.get('focus_ref')})" if t.get("focus_ref") else ""
        line = f"- **{name}** [{ttype}]{ref_str}\n  Config: `{config_str}`\n  Reason: {reason_str}"
        chars_used += len(line)
        if chars_used > budget_chars:
            lines.append(f"... and {len(triggers) - i} more triggers (truncated)")
            break
        lines.append(line)

    return "\n".join(lines)
