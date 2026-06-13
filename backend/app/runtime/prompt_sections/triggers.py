"""§ Triggers section — active wake policies configured for the agent."""

from __future__ import annotations


def build_triggers_section(triggers: list[dict], *, budget_chars: int = 3000) -> str:
    """Build the active trigger/wake-policy section.

    Args:
        triggers: List of trigger dicts with keys: name, type, config, reason.
        budget_chars: Max chars for the trigger list.
    """
    if not triggers:
        return ""

    lines = [
        "## Active Triggers",
        "",
        "Active triggers are wake policies, not goals. A trigger fires you on a schedule or "
        "event; what you do once awake is up to you.",
    ]
    chars_used = 0
    for i, t in enumerate(triggers):
        name = t.get("name", "?")
        ttype = t.get("type", "?")
        config = t.get("config", {})
        config_dict = config if isinstance(config, dict) else {}
        config_str = str(config)[:120]
        reason_str = (t.get("reason", "") or "")[:500]
        trigger_class = str(config_dict.get("trigger_class") or t.get("trigger_class") or "").strip()
        binding_parts = []
        if trigger_class:
            binding_parts.append(f"trigger_class: {trigger_class}")
        binding = ", ".join(binding_parts) if binding_parts else "unclassified wake policy"
        line = f"- **{name}** [{ttype}]\n  Binding: {binding}\n  Config: `{config_str}`\n  Reason: {reason_str}"
        chars_used += len(line)
        if chars_used > budget_chars:
            lines.append(f"... and {len(triggers) - i} more triggers (truncated)")
            break
        lines.append(line)

    return "\n".join(lines)
