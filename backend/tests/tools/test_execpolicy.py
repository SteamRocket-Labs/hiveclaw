"""Tests for the declarative command execution policy engine (D3).

Locks the behavior the former inline ``_DANGEROUS_COMMAND_PATTERNS`` regex table
in governance.py provided, and proves the new engine is declaratively extensible
(a new rule is data, not code) and self-validating (codex ``match``/``not_match``).
"""

from __future__ import annotations

import re

import pytest

from app.tools.execpolicy import (
    DANGEROUS_COMMAND_RULES,
    CommandPolicyRule,
    PolicyDecision,
    evaluate_command,
    validate_rule_examples,
)


def _candidates(command: str) -> tuple[str, ...]:
    # Mirror governance's candidate expansion for a single (already-split) command.
    return (command,)


@pytest.mark.parametrize(
    "command,expected_capability,expected_reason",
    [
        ("rm -rf /tmp/build-cache", "workspace.command.dangerous", "recursive delete"),
        ("rm --recursive build", "workspace.command.dangerous", "recursive delete"),
        ("git clean -fdx", "workspace.command.dangerous", "git clean -fx"),
        ("find . -name '*.tmp' -delete", "workspace.command.dangerous", "find -delete"),
        ("drop table users", "workspace.command.dangerous", "SQL DROP"),
        ("DROP DATABASE prod", "workspace.command.dangerous", "SQL DROP"),
        ("truncate table logs", "workspace.command.dangerous", "SQL TRUNCATE"),
        ("delete from users", "workspace.command.dangerous", "SQL DELETE without WHERE"),
        ("chmod 777 secret", "workspace.command.dangerous", "world-writable permissions"),
        ("chmod -R 666 dir", "workspace.command.dangerous", "world-writable permissions"),
        ("sudo rm x", "workspace.command.dangerous", "privileged ownership or sudo operation"),
        ("chown root:root file", "workspace.command.dangerous", "privileged ownership or sudo operation"),
        ("printenv CUSTOM_TOKEN", "workspace.command.secret_exfiltration", "secret exfiltration"),
        ("cat .env", "workspace.command.secret_exfiltration", "secret exfiltration"),
        ("env | grep -E '^FEISHU_'", "workspace.command.secret_exfiltration", "secret exfiltration"),
    ],
)
def test_dangerous_commands_map_to_capability_and_reason(command, expected_capability, expected_reason):
    match = evaluate_command(_candidates(command))
    assert match is not None
    assert match.capability == expected_capability
    assert match.reason == expected_reason
    # All current rules escalate to approval (none are unconditional hard-denies).
    assert match.decision is PolicyDecision.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "command",
    [
        "echo done",
        "ls -la",
        "rm report.md",  # non-recursive single delete is NOT flagged here
        "git status",
        "select * from users",
        "delete from users where id = 1",  # WHERE clause exempts it
        "chmod 644 file",
        "update logs set x = 1",
    ],
)
def test_non_dangerous_commands_return_none(command):
    assert evaluate_command(_candidates(command)) is None


def test_sql_rules_are_case_insensitive_against_lowercased_candidates():
    # The governance loop lowercases each candidate before matching, so the SQL
    # rules (uppercase source) MUST keep re.IGNORECASE to fire. Guard the flag.
    assert evaluate_command(("DROP TABLE Accounts",)) is not None
    assert evaluate_command(("Drop Database Prod",)) is not None
    drop_rule = next(r for r in DANGEROUS_COMMAND_RULES if r.reason == "SQL DROP")
    assert drop_rule.flags & re.IGNORECASE
    delete_rule = next(r for r in DANGEROUS_COMMAND_RULES if r.reason == "SQL DELETE without WHERE")
    assert delete_rule.flags & re.IGNORECASE
    assert delete_rule.flags & re.DOTALL


def test_delete_without_where_across_newlines_is_flagged():
    # DOTALL lets the negative-lookahead see a WHERE on a later line.
    assert evaluate_command(("delete from users\nlimit 5",)) is not None
    assert evaluate_command(("delete from users\nwhere id = 1",)) is None


def test_first_match_wins_over_candidate_order():
    # Full command is tried before sub-commands; the first matching rule wins.
    candidates = ("npm install && rm -rf node_modules", "npm install", "rm -rf node_modules")
    match = evaluate_command(candidates)
    assert match is not None
    assert match.capability == "workspace.command.dangerous"
    # The whole-command candidate matches first.
    assert match.matched_candidate == "npm install && rm -rf node_modules"


def test_rule_examples_self_validate():
    # Codex parity: every rule's match/not_match examples must hold. This is the
    # guard that keeps new declarative rules honest without extra test code.
    validate_rule_examples()


def test_validate_rule_examples_rejects_a_bad_rule():
    bad_rule = CommandPolicyRule(
        pattern=r"\bfoo\b",
        capability="workspace.command.dangerous",
        reason="bogus",
        match_examples=("bar",),  # does not contain "foo"
    )
    with pytest.raises(AssertionError):
        validate_rule_examples((bad_rule,))


def test_engine_is_declaratively_extensible_with_new_decisions():
    # Proves acceptance: a new command policy is DATA, not code. A caller can
    # supply an alternate rule set expressing DENY/ALLOW and the same engine
    # honors it — no engine changes required.
    custom_rules = (
        CommandPolicyRule(
            pattern=r"\bmkfs\b",
            capability="workspace.command.destructive_format",
            reason="filesystem format",
            decision=PolicyDecision.DENY,
            match_examples=("mkfs.ext4 /dev/sda1",),
            not_match_examples=("echo hello",),
        ),
    )
    validate_rule_examples(custom_rules)
    match = evaluate_command(("mkfs.ext4 /dev/sda1",), rules=custom_rules)
    assert match is not None
    assert match.decision is PolicyDecision.DENY
    assert match.capability == "workspace.command.destructive_format"
    # The default rule set does not know this rule.
    assert evaluate_command(("mkfs.ext4 /dev/sda1",)) is None


def test_empty_candidates_return_none():
    assert evaluate_command(()) is None
    assert evaluate_command(("",)) is None
