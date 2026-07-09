"""Tenant-programmable governance hooks — functional core (2026-07-09 §1).

Two swim lanes evaluated at the tail of ``run_tool_governance``:

- **Fast lane** — declarative policy hooks: in-process rule evaluation, zero
  code execution. Covers the ~90% of enterprise control that is declarative
  ("write_file to /etc/** must be approved").
- **Slow lane** — executable command hooks: sandboxed through the
  code-execution provider with a stdin JSON payload and an exit-code/JSON
  verdict protocol. Default off; triggered only for tools an approved matcher
  names, so unmatched tools pay zero sandbox cost (decision 1.7-d).

Three deliberate deltas from the CC local-trust baseline (design §1.3):

- **D1 fail-closed** — a governing hook that crashes, times out, or emits a
  malformed verdict denies the tool call (CC fails open). Config-parse
  failures on registration rows are *skipped with audit* instead: rows passed
  the Trust Gate, so breakage there is a platform bug, not a runtime signal.
- **D2 sandbox-only** — executable hooks never run raw subprocesses; the
  runner is the governed code-execution provider with an env allowlist.
- **D3 shrink-only** — a company/user-layer ``allow`` degrades to
  "no opinion"; only the managed (enterprise policy) layer keeps allow-grant
  (decision 1.7-b), and even that grant applies within the hook lane — the
  platform gates that already ran stay the floor.

Aggregation follows the CC order deny > ask > allow (hooks.ts:2820-2847).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

HOOK_LAYERS: tuple[str, ...] = ("managed", "company", "user")
GOVERNING_DECISIONS: frozenset[str] = frozenset({"deny", "ask", "allow"})

_EXACT_OR_LIST_MATCHER = re.compile(r"^[a-zA-Z0-9_|]+$")

DECLARATIVE_HANDLER_PREFIX = "declarative"
COMMAND_HANDLER_PREFIX = "command"

PRE_TOOL_USE_EVENTS: frozenset[str] = frozenset({"PreToolUse", "pre_tool_use"})


@dataclass(frozen=True, slots=True)
class ArgRule:
    """Declarative condition on one tool argument (regex search, IGNORECASE)."""

    field: str
    pattern: str


@dataclass(frozen=True, slots=True)
class GovernanceHookSpec:
    key: str
    layer: str  # managed | company | user
    kind: str  # declarative | command
    matcher: str
    decision: str | None  # declarative lane: deny | ask | allow
    reason: str
    arg_rules: tuple[ArgRule, ...]
    command: str | None
    timeout_seconds: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class HookVerdict:
    decision: str  # deny | ask | allow | no_opinion
    reason: str
    hook_key: str
    layer: str
    source: str  # declarative | command | failure


@dataclass(frozen=True, slots=True)
class AggregateDecision:
    outcome: str  # deny | ask | allow_grant | no_opinion
    reason: str
    hook_key: str | None
    layer: str | None


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def matches_tool(matcher: str, tool_name: str) -> bool:
    """CC matcher semantics: empty/'*' match all; ``[A-Za-z0-9_|]+`` is an exact
    name or a ``|`` list; anything else is a regex (invalid regex never matches)."""
    cleaned = (matcher or "").strip()
    if not cleaned or cleaned == "*":
        return True
    if _EXACT_OR_LIST_MATCHER.match(cleaned):
        return tool_name in cleaned.split("|")
    compiled = _compile(cleaned)
    if compiled is None:
        return False
    return bool(compiled.search(tool_name))


def _arg_rule_hits(rule: ArgRule, arguments: dict[str, Any]) -> bool:
    if rule.field not in arguments:
        return False
    compiled = _compile(rule.pattern)
    if compiled is None:
        return False
    return bool(compiled.search(str(arguments.get(rule.field) or "")))


def spec_matches(spec: GovernanceHookSpec, tool_name: str, arguments: dict[str, Any]) -> bool:
    """A spec triggers when it is enabled, its matcher names the tool, and every
    arg rule hits (no arg rules = matcher alone decides)."""
    if not spec.enabled:
        return False
    if not matches_tool(spec.matcher, tool_name):
        return False
    return all(_arg_rule_hits(rule, arguments) for rule in spec.arg_rules)


def evaluate_declarative(spec: GovernanceHookSpec, tool_name: str, arguments: dict[str, Any]) -> HookVerdict | None:
    if spec.kind != "declarative":
        return None
    if not spec_matches(spec, tool_name, arguments):
        return None
    return HookVerdict(
        decision=spec.decision or "ask",
        reason=spec.reason,
        hook_key=spec.key,
        layer=spec.layer,
        source="declarative",
    )


def aggregate_verdicts(verdicts: list[HookVerdict]) -> AggregateDecision:
    """CC order deny > ask > allow with the D3 shrink-only delta.

    - Any ``deny`` wins outright (managed allow cannot suppress it).
    - ``managed`` allow is a grant *within the hook lane*: it suppresses
      tenant-layer ``ask`` verdicts (decision 1.7-b).
    - company/user ``allow`` degrades to no-opinion — it never widens anything.
    """
    denies = [v for v in verdicts if v.decision == "deny"]
    if denies:
        winner = denies[0]
        return AggregateDecision("deny", winner.reason, winner.hook_key, winner.layer)

    managed_allows = [v for v in verdicts if v.decision == "allow" and v.layer == "managed"]
    asks = [v for v in verdicts if v.decision == "ask"]
    if asks:
        if managed_allows:
            winner = managed_allows[0]
            return AggregateDecision("allow_grant", winner.reason, winner.hook_key, winner.layer)
        winner = asks[0]
        return AggregateDecision("ask", winner.reason, winner.hook_key, winner.layer)

    if managed_allows:
        winner = managed_allows[0]
        return AggregateDecision("allow_grant", winner.reason, winner.hook_key, winner.layer)

    return AggregateDecision("no_opinion", "", None, None)


def spec_from_registration(row: Any) -> GovernanceHookSpec | None:
    """Parse an approved ``ExternalExtensionHookRegistration`` row into a spec.

    Returns ``None`` (skip) for rows that are not approved, not a PreToolUse
    governance hook, or malformed — config-parse failures are a management-plane
    problem surfaced through audit, not a runtime fail-closed signal (§1.3 D1).
    """
    if str(getattr(row, "status", "") or "") != "approved":
        return None
    if str(getattr(row, "event", "") or "") not in PRE_TOOL_USE_EVENTS:
        return None
    handler = str(getattr(row, "handler", "") or "")
    kind = handler.split(":", 1)[0].strip().lower()
    if kind not in {DECLARATIVE_HANDLER_PREFIX, COMMAND_HANDLER_PREFIX}:
        return None
    config = getattr(row, "matcher_json", None)
    if not isinstance(config, dict):
        return None

    layer = str(config.get("layer") or "company").strip().lower()
    if layer not in HOOK_LAYERS:
        layer = "company"
    matcher = str(config.get("matcher") or "").strip()
    reason = str(config.get("reason") or f"governance hook {getattr(row, 'qualified_name', handler)}")
    try:
        timeout_seconds = max(1, int(config.get("timeout_seconds") or 30))
    except (TypeError, ValueError):
        timeout_seconds = 30

    arg_rules: list[ArgRule] = []
    raw_rules = config.get("arg_rules")
    if isinstance(raw_rules, list):
        for raw in raw_rules:
            if not isinstance(raw, dict):
                return None
            field = str(raw.get("field") or "").strip()
            pattern = str(raw.get("pattern") or "").strip()
            if not field or not pattern or _compile(pattern) is None:
                return None
            arg_rules.append(ArgRule(field=field, pattern=pattern))

    if kind == DECLARATIVE_HANDLER_PREFIX:
        decision = str(config.get("decision") or getattr(row, "mode", "") or "").strip().lower()
        if decision not in GOVERNING_DECISIONS:
            return None
        return GovernanceHookSpec(
            key=str(getattr(row, "qualified_name", handler)),
            layer=layer,
            kind="declarative",
            matcher=matcher,
            decision=decision,
            reason=reason,
            arg_rules=tuple(arg_rules),
            command=None,
            timeout_seconds=timeout_seconds,
            enabled=True,
        )

    command = str(config.get("command") or "").strip()
    if not command:
        return None
    return GovernanceHookSpec(
        key=str(getattr(row, "qualified_name", handler)),
        layer=layer,
        kind="command",
        matcher=matcher,
        decision=None,
        reason=reason,
        arg_rules=tuple(arg_rules),
        command=command,
        timeout_seconds=timeout_seconds,
        enabled=True,
    )
