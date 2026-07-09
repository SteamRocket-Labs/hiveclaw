"""Declarative command execution policy engine (Codex execpolicy-inspired).

This module replaces the hand-written dangerous-command regex table that used to
live inline in ``app.tools.governance`` with a declarative rule set. Each rule is
*data* — a match pattern, its capability, a decision, a human-readable reason,
and self-validating ``match``/``not_match`` examples. Adding a new command policy
is a data change (append a ``CommandPolicyRule``), not a code change.

Codex parity (``codex-rs/execpolicy``):

* ``Decision {Allow, Prompt, Forbidden}`` maps to
  ``PolicyDecision {ALLOW, REQUIRE_APPROVAL, DENY}``.
* Rules carry a human-readable ``reason`` (codex ``justification``) surfaced in
  approval/denial messages.
* ``match``/``not_match`` examples act as per-rule unit tests validated by
  :func:`validate_rule_examples` (codex validates them at policy load time).

Hive-specific, deliberately preserved so downstream governance semantics do not
change: evaluation is **first-match-wins** over ``candidates`` × ``rules`` (the
exact order the old inline loop used), the matched rule's ``capability`` still
feeds the capability gate, and candidates are case-folded before matching (the
old loop lowercased each candidate, which is why the SQL rules below must keep
``re.IGNORECASE``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

_DANGEROUS_CAPABILITY = "workspace.command.dangerous"
_SECRET_EXFILTRATION_CAPABILITY = "workspace.command.secret_exfiltration"


class PolicyDecision(str, Enum):
    """Declarative action for a matched command policy rule.

    Mirrors codex ``Decision`` (``allow``/``prompt``/``forbidden``). Hive names
    them for the governance vocabulary the team uses: a ``REQUIRE_APPROVAL``
    match escalates through the capability gate / session-permission flow, while
    a ``DENY`` match is a hard block.
    """

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"  # codex: prompt
    DENY = "deny"  # codex: forbidden


@dataclass(frozen=True, slots=True)
class CommandPolicyRule:
    """One declarative command-policy rule.

    ``pattern`` is a regular-expression source matched with ``flags`` against a
    case-folded command (or shell sub-command) candidate. ``capability`` is the
    governance capability the match maps to (fed to the capability gate).
    ``match_examples``/``not_match_examples`` are validated by
    :func:`validate_rule_examples`.
    """

    pattern: str
    capability: str
    reason: str
    decision: PolicyDecision = PolicyDecision.REQUIRE_APPROVAL
    flags: int = 0
    match_examples: tuple[str, ...] = ()
    not_match_examples: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandPolicyMatch:
    """Result of evaluating a command against the policy rules."""

    capability: str
    reason: str
    decision: PolicyDecision
    matched_candidate: str
    rule: CommandPolicyRule


@lru_cache(maxsize=256)
def _compile(pattern: str, flags: int) -> re.Pattern[str]:
    return re.compile(pattern, flags)


# Declarative replacement for the former ``_DANGEROUS_COMMAND_PATTERNS`` tuple in
# governance.py. Each entry keeps the original regex source, flags, capability,
# and reason verbatim so downstream governance routing is byte-for-byte
# unchanged. ``decision`` documents the governance intent (all current rules
# escalate to approval — none are unconditional hard-denies; path-syntax /
# managed-credential hard-blocks remain their own token-based detectors in
# governance.py and are intentionally out of this table).
DANGEROUS_COMMAND_RULES: tuple[CommandPolicyRule, ...] = (
    CommandPolicyRule(
        pattern=r"\brm\s+-[^\s]*r",
        capability=_DANGEROUS_CAPABILITY,
        reason="recursive delete",
        match_examples=("rm -rf /tmp/build-cache", "rm -R dir"),
        not_match_examples=("remove old notes", "rm report.md"),
    ),
    CommandPolicyRule(
        pattern=r"\brm\s+--recursive\b",
        capability=_DANGEROUS_CAPABILITY,
        reason="recursive delete",
        match_examples=("rm --recursive build",),
        not_match_examples=("rm report.md",),
    ),
    CommandPolicyRule(
        pattern=r"\bgit\s+clean\s+-[a-z]*f[a-z]*x[a-z]*\b",
        capability=_DANGEROUS_CAPABILITY,
        reason="git clean -fx",
        match_examples=("git clean -fdx", "git clean -fx"),
        not_match_examples=("git clean -n", "git status"),
    ),
    CommandPolicyRule(
        pattern=r"\bfind\b.+\s-delete\b",
        capability=_DANGEROUS_CAPABILITY,
        reason="find -delete",
        match_examples=("find . -name '*.tmp' -delete",),
        not_match_examples=("find . -name '*.tmp'",),
    ),
    CommandPolicyRule(
        pattern=r"\bDROP\s+(TABLE|DATABASE)\b",
        capability=_DANGEROUS_CAPABILITY,
        reason="SQL DROP",
        flags=re.IGNORECASE,
        match_examples=("drop table users", "DROP DATABASE prod"),
        not_match_examples=("select * from users",),
    ),
    CommandPolicyRule(
        pattern=r"\bTRUNCATE\s+(TABLE)?\s*\w",
        capability=_DANGEROUS_CAPABILITY,
        reason="SQL TRUNCATE",
        flags=re.IGNORECASE,
        match_examples=("truncate table logs", "TRUNCATE logs"),
        not_match_examples=("update logs set x = 1",),
    ),
    CommandPolicyRule(
        pattern=r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)",
        capability=_DANGEROUS_CAPABILITY,
        reason="SQL DELETE without WHERE",
        flags=re.IGNORECASE | re.DOTALL,
        match_examples=("delete from users",),
        not_match_examples=("delete from users where id = 1",),
    ),
    CommandPolicyRule(
        pattern=r"\bchmod\s+(-[^\s]*\s+)*(777|666)\b",
        capability=_DANGEROUS_CAPABILITY,
        reason="world-writable permissions",
        match_examples=("chmod 777 secret", "chmod -R 666 dir"),
        not_match_examples=("chmod 644 file",),
    ),
    CommandPolicyRule(
        pattern=r"\b(chown|sudo)\b",
        capability=_DANGEROUS_CAPABILITY,
        reason="privileged ownership or sudo operation",
        match_examples=("sudo rm x", "chown root:root file"),
        not_match_examples=("echo done",),
    ),
    CommandPolicyRule(
        pattern=r"(\bcat\s+\.env\b|\bprintenv\b|\benv\s*\|\s*grep\b|\bSECRET[_A-Z0-9]*\b|\bTOKEN[_A-Z0-9]*\b)",
        capability=_SECRET_EXFILTRATION_CAPABILITY,
        reason="secret exfiltration",
        match_examples=("cat .env", "printenv CUSTOM_TOKEN", "env | grep -E '^FEISHU_'"),
        not_match_examples=("echo done",),
    ),
)


def evaluate_command(
    candidates: Iterable[str],
    *,
    rules: tuple[CommandPolicyRule, ...] = DANGEROUS_COMMAND_RULES,
) -> CommandPolicyMatch | None:
    """Return the first policy rule that matches any candidate, else ``None``.

    ``candidates`` are matched in order (the caller passes the full command
    followed by its shell sub-commands); each candidate is case-folded and tried
    against ``rules`` in declaration order — first match wins, preserving the
    exact precedence of the former inline governance loop.
    """
    for candidate in candidates:
        lowered = candidate.lower()
        for rule in rules:
            if _compile(rule.pattern, rule.flags).search(lowered):
                return CommandPolicyMatch(
                    capability=rule.capability,
                    reason=rule.reason,
                    decision=rule.decision,
                    matched_candidate=candidate,
                    rule=rule,
                )
    return None


def validate_rule_examples(rules: tuple[CommandPolicyRule, ...] = DANGEROUS_COMMAND_RULES) -> None:
    """Validate every rule's ``match``/``not_match`` examples against its pattern.

    Ports codex execpolicy's load-time example validation: each ``match_examples``
    entry MUST match the rule's own pattern and each ``not_match_examples`` entry
    MUST NOT. Examples are case-folded to mirror :func:`evaluate_command`. Raises
    ``AssertionError`` on the first violation.
    """
    for rule in rules:
        compiled = _compile(rule.pattern, rule.flags)
        for example in rule.match_examples:
            if not compiled.search(example.lower()):
                raise AssertionError(f"rule {rule.reason!r} pattern {rule.pattern!r} must match example {example!r}")
        for example in rule.not_match_examples:
            if compiled.search(example.lower()):
                raise AssertionError(
                    f"rule {rule.reason!r} pattern {rule.pattern!r} must NOT match example {example!r}"
                )
