"""Tenant governance hooks — functional core (unified design 2026-07-09 §1).

Pins the CC-parity matcher semantics, the declarative fast-lane evaluation,
the deny > ask > allow aggregation with the three deliberate Hive deltas
(fail-closed, sandbox-only, shrink-only allow), and the registration-row
parsing contract.
"""

from __future__ import annotations

from app.tools.hook_governance import (
    ArgRule,
    GovernanceHookSpec,
    HookVerdict,
    aggregate_verdicts,
    evaluate_declarative,
    matches_tool,
    spec_from_registration,
    spec_matches,
)


def _spec(**overrides) -> GovernanceHookSpec:
    base = dict(
        key="hook-1",
        layer="company",
        kind="declarative",
        matcher="write_file",
        decision="ask",
        reason="writes need review",
        arg_rules=(),
        command=None,
        timeout_seconds=10,
        enabled=True,
    )
    base.update(overrides)
    return GovernanceHookSpec(**base)


class TestMatcherSemantics:
    """CC matcher parity: empty/'*' match all; [A-Za-z0-9_|]+ exact or |-list;
    anything else is a regex."""

    def test_empty_and_star_match_everything(self):
        assert matches_tool("", "write_file") is True
        assert matches_tool("*", "run_command") is True

    def test_exact_and_pipe_list(self):
        assert matches_tool("write_file", "write_file") is True
        assert matches_tool("write_file", "read_file") is False
        assert matches_tool("write_file|delete_file", "delete_file") is True
        assert matches_tool("write_file|delete_file", "send_email") is False

    def test_regex_matcher(self):
        assert matches_tool("^send_.*", "send_email") is True
        assert matches_tool("^send_.*", "resend_email") is False

    def test_invalid_regex_never_matches(self):
        assert matches_tool("([unclosed", "write_file") is False


class TestSpecMatching:
    def test_arg_rules_all_must_hit(self):
        spec = _spec(
            matcher="write_file",
            arg_rules=(ArgRule(field="path", pattern=r"^/etc/"),),
        )
        assert spec_matches(spec, "write_file", {"path": "/etc/passwd"}) is True
        assert spec_matches(spec, "write_file", {"path": "workspace/notes.md"}) is False
        # Missing field: rule cannot hit.
        assert spec_matches(spec, "write_file", {}) is False

    def test_disabled_spec_never_matches(self):
        spec = _spec(enabled=False)
        assert spec_matches(spec, "write_file", {}) is False


class TestDeclarativeEvaluation:
    def test_matched_spec_yields_its_decision(self):
        verdict = evaluate_declarative(_spec(decision="deny", reason="no writes"), "write_file", {})
        assert verdict == HookVerdict(
            decision="deny", reason="no writes", hook_key="hook-1", layer="company", source="declarative"
        )

    def test_unmatched_spec_yields_none(self):
        assert evaluate_declarative(_spec(matcher="delete_file"), "write_file", {}) is None


class TestAggregation:
    """CC order deny > ask > allow, with the D3 shrink-only delta."""

    def test_deny_beats_everything(self):
        outcome = aggregate_verdicts(
            [
                HookVerdict("allow", "managed grant", "m", "managed", "declarative"),
                HookVerdict("ask", "review", "c", "company", "declarative"),
                HookVerdict("deny", "forbidden", "u", "user", "declarative"),
            ]
        )
        assert outcome.outcome == "deny"
        assert outcome.hook_key == "u"

    def test_ask_beats_allow_and_no_opinion(self):
        outcome = aggregate_verdicts(
            [
                HookVerdict("no_opinion", "", "a", "company", "command"),
                HookVerdict("ask", "review", "b", "company", "declarative"),
            ]
        )
        assert outcome.outcome == "ask"

    def test_tenant_layer_allow_degrades_to_no_opinion(self):
        """D3: a company/user-layer allow must NOT grant anything."""
        outcome = aggregate_verdicts([HookVerdict("allow", "looks fine", "c", "company", "declarative")])
        assert outcome.outcome == "no_opinion"

    def test_managed_allow_grants_within_the_hook_lane(self):
        """Decision 1.7-b: managed allow suppresses tenant-layer ask (not deny)."""
        outcome = aggregate_verdicts(
            [
                HookVerdict("ask", "company wants review", "c", "company", "declarative"),
                HookVerdict("allow", "enterprise pre-approved", "m", "managed", "declarative"),
            ]
        )
        assert outcome.outcome == "allow_grant"
        assert outcome.hook_key == "m"

    def test_managed_allow_does_not_suppress_deny(self):
        outcome = aggregate_verdicts(
            [
                HookVerdict("deny", "hard rule", "c", "company", "declarative"),
                HookVerdict("allow", "enterprise pre-approved", "m", "managed", "declarative"),
            ]
        )
        assert outcome.outcome == "deny"

    def test_empty_verdicts_are_no_opinion(self):
        assert aggregate_verdicts([]).outcome == "no_opinion"


class TestRegistrationParsing:
    def _row(self, **overrides):
        from types import SimpleNamespace

        base = dict(
            qualified_name="acme/compliance:pre_write",
            event="PreToolUse",
            handler="declarative:policy",
            mode="ask",
            status="approved",
            matcher_json={
                "layer": "company",
                "matcher": "write_file|delete_file",
                "decision": "ask",
                "reason": "compliance review",
                "arg_rules": [{"field": "path", "pattern": "^/etc/"}],
                "timeout_seconds": 15,
            },
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_declarative_row_parses(self):
        spec = spec_from_registration(self._row())
        assert spec is not None
        assert spec.kind == "declarative"
        assert spec.matcher == "write_file|delete_file"
        assert spec.decision == "ask"
        assert spec.layer == "company"
        assert spec.arg_rules == (ArgRule(field="path", pattern="^/etc/"),)
        assert spec.timeout_seconds == 15

    def test_command_row_parses(self):
        row = self._row(
            handler="command:compliance-check",
            matcher_json={
                "layer": "managed",
                "matcher": "run_command",
                "command": "python /opt/hooks/check.py",
                "reason": "managed compliance",
            },
        )
        spec = spec_from_registration(row)
        assert spec is not None
        assert spec.kind == "command"
        assert spec.command == "python /opt/hooks/check.py"
        assert spec.layer == "managed"

    def test_unapproved_row_is_skipped(self):
        assert spec_from_registration(self._row(status="pending_approval")) is None

    def test_unknown_layer_defaults_to_company(self):
        spec = spec_from_registration(self._row(matcher_json={"matcher": "*", "decision": "deny", "layer": "root"}))
        assert spec is not None
        assert spec.layer == "company"

    def test_malformed_governing_row_is_skipped_not_match_all(self):
        """Config-parse failures skip the row (Trust Gate approved it; breakage is a
        platform bug surfaced via audit) — runtime failures are what fail closed."""
        row = self._row(matcher_json={"decision": "warp", "matcher": "*"})
        assert spec_from_registration(row) is None

    def test_command_row_without_command_is_skipped(self):
        row = self._row(handler="command:broken", matcher_json={"matcher": "*", "layer": "company"})
        assert spec_from_registration(row) is None
