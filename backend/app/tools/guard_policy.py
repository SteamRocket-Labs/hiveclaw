"""Pure cloud-runtime evaluation for the tenant GuardPolicy tool-rule subset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_DECISION_PRIORITY = {"allow": 0, "require_approval": 1, "deny": 2}


@dataclass(frozen=True, slots=True)
class GuardPolicyVerdict:
    decision: str
    reason: str | None
    policy_version: int
    matched_rules: tuple[str, ...]


def validate_guard_policy_snapshot(snapshot: dict[str, Any] | None) -> None:
    """Validate the cloud-enforced subset without interpreting opaque Desktop keys."""
    policy = dict(snapshot or {})
    for lane in ("zone_guard", "egress_guard"):
        config = policy.get(lane) or {}
        if not isinstance(config, dict):
            raise ValueError(f"GuardPolicy {lane} must be an object")
        rules = config.get("tool_rules", [])
        if not isinstance(rules, list):
            raise ValueError(f"GuardPolicy {lane}.tool_rules must be a list")
        for index, raw_rule in enumerate(rules):
            if not isinstance(raw_rule, dict):
                raise ValueError(f"GuardPolicy {lane}.tool_rules[{index}] must be an object")
            decision = str(raw_rule.get("decision") or "").strip()
            if decision not in _DECISION_PRIORITY:
                raise ValueError(f"GuardPolicy rule has invalid decision: {decision or '<empty>'}")
            _rule_matches(raw_rule, tool_name="", arguments={})


def _rule_matches(rule: dict[str, Any], *, tool_name: str, arguments: dict[str, Any]) -> bool:
    tools = rule.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(value, str) for value in tools):
        raise ValueError("GuardPolicy tool rule requires a non-empty string tools list")
    if "*" not in tools and tool_name not in tools:
        return False
    required_arguments = rule.get("argument_equals", {})
    if not isinstance(required_arguments, dict):
        raise ValueError("GuardPolicy argument_equals must be an object")
    return all(arguments.get(key) == value for key, value in required_arguments.items())


def evaluate_guard_policy(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    external_visible: bool,
    snapshot: dict[str, Any] | None,
) -> GuardPolicyVerdict:
    """Combine shrink-only GuardPolicy rules without replacing capability auth.

    ``zone_guard.tool_rules`` applies to every tool.  The egress lane applies
    only to ToolMeta.external_visible tools.  Explicit allow is advisory; it
    cannot override any later CapabilityPolicy, ResourcePermission, session,
    preflight, or hook denial.
    """
    policy = dict(snapshot or {})
    version = int(policy.get("version") or 0)
    matches: list[tuple[str, str, str | None]] = []
    lanes = (("zone_guard", True), ("egress_guard", external_visible))
    try:
        validate_guard_policy_snapshot(policy)
        for lane, enabled in lanes:
            if not enabled:
                continue
            config = policy.get(lane) or {}
            rules = config.get("tool_rules", [])
            for index, raw_rule in enumerate(rules):
                decision = str(raw_rule.get("decision") or "").strip()
                if _rule_matches(raw_rule, tool_name=tool_name, arguments=arguments):
                    reason = str(raw_rule.get("reason") or "").strip() or None
                    matches.append((f"{lane}:{index}", decision, reason))
    except (TypeError, ValueError) as exc:
        return GuardPolicyVerdict(
            decision="deny",
            reason=f"malformed GuardPolicy: {exc}",
            policy_version=version,
            matched_rules=("guard_policy:malformed",),
        )

    if not matches:
        return GuardPolicyVerdict("allow", None, version, ())
    strongest = max(matches, key=lambda item: _DECISION_PRIORITY[item[1]])
    return GuardPolicyVerdict(
        decision=strongest[1],
        reason=strongest[2],
        policy_version=version,
        matched_rules=tuple(item[0] for item in matches),
    )
