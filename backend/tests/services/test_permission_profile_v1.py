"""D-12 — PermissionProfileV1.default_decision governs the live governance decision.

`check_capability` returns ``escalate_to_l3=True`` (with ``policy_found=False``)
for the *mapped-capability-no-policy* case. Historically governance routed that
into an L3 approval request via a hardcoded "escalate". These tests exercise the
real ``run_tool_governance`` path and pin that the per-turn ``PermissionProfileV1``
now governs that branch:

  * ``default_decision="escalate"`` (the contract default) → approval requested
    (identical to the previous hardcoded behavior — baseline unchanged);
  * ``default_decision="deny"``                         → tool is DENIED outright.

Revert-sensitivity: if the governance wiring is reverted to the hardcoded
escalate, the ``deny`` case below would request approval instead of blocking,
so ``test_permission_profile_deny_blocks_mapped_no_policy_capability`` fails.

Patterns mirror tests/services/test_capability_gate_strict_mapping.py (fake DB
returning ``scalar_one_or_none() -> None`` to reach the no-policy branch) and
tests/tools/test_governance.py (governance dependency stubs).
"""

from __future__ import annotations

import uuid

import pytest

from app.runtime.ccplus_contracts import PermissionProfileV1
from app.services import capability_gate
from app.services.capability_gate import check_capability
from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance


class _NoPolicyDB:
    """Async DB stub whose policy lookups always miss → no-policy escalate branch."""

    async def execute(self, _stmt):
        class _Result:
            def scalar_one_or_none(self):
                return None

        return _Result()


def _governance_deps(*, check_capability_fn, approval_calls, audit_calls):
    async def resolve_security_zone(_agent_id):
        return "standard"

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None, **_kw):
        approval_calls.append({"tool_name": tool_name, "capability": capability})
        return {"allowed": False, "approval_id": "approval-no-policy"}

    return GovernanceDependencies(
        resolve_security_zone=resolve_security_zone,
        check_capability=check_capability_fn,
        write_audit_event=write_audit,
        request_approval=request_approval,
    )


def _live_no_policy_check_capability():
    """Build a deps.check_capability that runs the REAL check_capability against a
    no-policy DB, so the result is the live ``escalate_to_l3=True``/
    ``policy_found=False`` shape — the exact hardcode D-12 routes through the profile."""

    async def _check(_tenant_id, agent_id, tool_name):
        return await check_capability(
            db=_NoPolicyDB(),
            tenant_id=_tenant_id,
            agent_id=agent_id,
            tool_name=tool_name,
        )

    return _check


@pytest.mark.asyncio
async def test_permission_profile_escalate_default_keeps_no_policy_approval() -> None:
    """default_decision="escalate" (contract default) must preserve the historical
    hardcoded behavior: a mapped-no-policy capability escalates to L3 approval."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            tool_name="write_file",  # maps to workspace.file.write, no policy → escalate
            arguments={"path": "workspace/notes.md", "content": "x"},
            permission_profile=PermissionProfileV1(default_decision="escalate"),
        ),
        _governance_deps(
            check_capability_fn=_live_no_policy_check_capability(),
            approval_calls=approval_calls,
            audit_calls=audit_calls,
        ),
    )

    assert message is not None
    assert "requires approval" in message
    assert "Approval ID: approval-no-policy" in message
    # Approval was requested for the mapped capability — the escalate path ran.
    assert approval_calls == [{"tool_name": "write_file", "capability": "workspace.file.write"}]
    # And it was audited as an escalation, not a denial.
    assert any(c["event_type"] == "capability.escalated" for c in audit_calls)
    assert not any(c.get("action") == "no_policy_default_denied" for c in audit_calls)


@pytest.mark.asyncio
async def test_permission_profile_deny_blocks_mapped_no_policy_capability() -> None:
    """default_decision="deny" must DENY the mapped-no-policy capability outright —
    no approval request. Reverting the governance wiring to the hardcoded escalate
    makes this fail (approval would be requested and ``message`` would say
    "requires approval" instead of being a teaching denial)."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []

    deps = _governance_deps(
        check_capability_fn=_live_no_policy_check_capability(),
        approval_calls=approval_calls,
        audit_calls=audit_calls,
    )

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            tool_name="write_file",
            arguments={"path": "workspace/notes.md", "content": "x"},
            permission_profile=PermissionProfileV1(default_decision="deny"),
        ),
        deps,
        event_callback=events.append,
    )

    assert message is not None
    # Denied, not escalated: no approval request was made.
    assert approval_calls == []
    assert "requires approval" not in message
    assert "Approval ID" not in message
    # Teaching denial surfaces the tool + capability.
    assert "write_file" in message
    assert "workspace.file.write" in message
    assert "What you can do instead" in message
    # Audit + event reflect the profile-driven denial, distinct from a plain
    # capability policy deny.
    assert any(
        c["event_type"] == "capability.denied" and c.get("action") == "no_policy_default_denied" for c in audit_calls
    )
    assert events and events[-1]["status"] == "capability_denied"
    assert events[-1]["tool_name"] == "write_file"


@pytest.mark.asyncio
async def test_permission_profile_none_falls_back_to_escalate() -> None:
    """No profile threaded onto the turn (the live default, since the resolver
    leaves it None) → fail-closed escalate, identical to the pre-D-12 baseline."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            tool_name="write_file",
            arguments={"path": "workspace/notes.md", "content": "x"},
            # permission_profile defaults to None
        ),
        _governance_deps(
            check_capability_fn=_live_no_policy_check_capability(),
            approval_calls=approval_calls,
            audit_calls=audit_calls,
        ),
    )

    assert message is not None
    assert "requires approval" in message
    assert approval_calls == [{"tool_name": "write_file", "capability": "workspace.file.write"}]


def test_permission_profile_resolve_no_policy_decision_normalizes() -> None:
    """The pure resolver maps the profile's default_decision to a governance
    action and fail-closes unknown / None values to "escalate"."""
    assert capability_gate.resolve_no_policy_decision(None) == "escalate"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1()) == "escalate"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="deny")) == "deny"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="allow")) == "allow"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="ESCALATE")) == "escalate"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="bogus")) == "escalate"
