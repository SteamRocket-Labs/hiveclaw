from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.tools.guard_policy import evaluate_guard_policy


def test_guard_policy_hard_deny_has_priority_over_approval_and_allow() -> None:
    verdict = evaluate_guard_policy(
        tool_name="send_email",
        arguments={"to": "outside@example.com"},
        external_visible=True,
        snapshot={
            "version": 7,
            "zone_guard": {
                "tool_rules": [{"tools": ["send_email"], "decision": "require_approval", "reason": "review outbound"}]
            },
            "egress_guard": {"tool_rules": [{"tools": ["send_email"], "decision": "deny", "reason": "mail disabled"}]},
        },
    )

    assert verdict.decision == "deny"
    assert verdict.reason == "mail disabled"
    assert verdict.policy_version == 7
    assert verdict.matched_rules == ("zone_guard:0", "egress_guard:0")


def test_guard_policy_egress_rules_do_not_apply_to_internal_tools() -> None:
    verdict = evaluate_guard_policy(
        tool_name="read_file",
        arguments={"path": "notes.md"},
        external_visible=False,
        snapshot={
            "version": 3,
            "zone_guard": {},
            "egress_guard": {"tool_rules": [{"tools": ["*"], "decision": "deny", "reason": "no egress"}]},
        },
    )

    assert verdict.decision == "allow"
    assert verdict.matched_rules == ()


@pytest.mark.asyncio
async def test_guard_policy_approval_uses_company_ticket_path() -> None:
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    approval_calls = []

    async def _approval(**kwargs):
        approval_calls.append(kwargs)
        return {"allowed": False, "approval_id": "approval-guard-1"}

    deps = GovernanceDependencies(
        resolve_security_zone=lambda _agent_id: "restricted",
        check_capability=lambda *_args: SimpleNamespace(
            denied=False,
            escalate_to_l3=False,
            capability="communication.email.send",
            policy_found=True,
        ),
        write_audit_event=lambda **_kwargs: None,
        request_approval=_approval,
        load_guard_policy=lambda *_args: {
            "version": 8,
            "zone_guard": {
                "tool_rules": [{"tools": ["send_email"], "decision": "require_approval", "reason": "four eyes"}]
            },
            "egress_guard": {},
        },
    )
    context = ToolGovernanceContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        tool_name="send_email",
        arguments={"to": "person@example.com", "subject": "hello"},
        tool_call_id="tool-call-guard-1",
    )

    result = await run_tool_governance(context, deps)

    assert "approval_required" in str(result)
    assert approval_calls[0]["approval_origin_type"] == "guard_policy"
    assert approval_calls[0]["decision_id"] == "decision:tool-call-guard-1"
    assert context.approval_id == "approval-guard-1"
    assert context.guard_policy_snapshot["version"] == 8


@pytest.mark.asyncio
async def test_guard_policy_allow_cannot_override_capability_deny() -> None:
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    deps = GovernanceDependencies(
        resolve_security_zone=lambda _agent_id: "restricted",
        check_capability=lambda *_args: SimpleNamespace(
            denied=True,
            escalate_to_l3=False,
            capability="workspace.write",
            reason="company capability denies writes",
            policy_found=True,
        ),
        write_audit_event=lambda **_kwargs: None,
        request_approval=lambda **_kwargs: {"allowed": False},
        load_guard_policy=lambda *_args: {
            "version": 1,
            "zone_guard": {"tool_rules": [{"tools": ["write_file"], "decision": "allow", "reason": "advice only"}]},
            "egress_guard": {},
        },
    )

    result = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="write_file",
            arguments={"path": "notes.md", "content": "x"},
        ),
        deps,
    )

    assert result is not None
    assert "denied" in result.lower() or "blocked" in result.lower()
