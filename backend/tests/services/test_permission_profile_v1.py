"""CCPlus permission profile routes no-policy tools to session permission.

`check_capability` returns ``escalate_to_l3=True`` (with ``policy_found=False``)
for the *mapped-capability-no-policy* case. That is not an enterprise approval
request by itself. The real ``run_tool_governance`` path must route it through
CC session permission modes:

  * default / legacy ``default_decision="escalate"`` → local session ask;
  * ``mode="dontAsk"`` or ``mode="plan"``             → local deny;
  * explicit company approval policy                  → enterprise approval.

Revert-sensitivity: if the governance wiring is reverted to the hardcoded
escalate, the no-policy cases below would create backend approvals instead of
session-local asks.

Patterns mirror tests/services/test_capability_gate_strict_mapping.py (fake DB
returning ``scalar_one_or_none() -> None`` to reach the no-policy branch) and
tests/tools/test_governance.py (governance dependency stubs).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

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


class _PolicyDB:
    """Async DB stub returning the same explicit capability policy for lookups."""

    def __init__(self, *, allowed: bool, requires_approval: bool) -> None:
        self.policy = SimpleNamespace(allowed=allowed, requires_approval=requires_approval)

    async def execute(self, _stmt):
        policy = self.policy

        class _Result:
            def scalar_one_or_none(self):
                return policy

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


def _live_policy_check_capability(*, allowed: bool, requires_approval: bool):
    async def _check(_tenant_id, agent_id, tool_name):
        return await check_capability(
            db=_PolicyDB(allowed=allowed, requires_approval=requires_approval),
            tenant_id=_tenant_id,
            agent_id=agent_id,
            tool_name=tool_name,
        )

    return _check


@pytest.mark.asyncio
async def test_core_read_and_discovery_tools_allow_missing_policy_without_approval() -> None:
    """Core read/discovery tools must not require approval just because a tenant
    has not created explicit CapabilityPolicy rows yet."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []
    deps = _governance_deps(
        check_capability_fn=_live_no_policy_check_capability(),
        approval_calls=approval_calls,
        audit_calls=audit_calls,
    )

    for tool_name, arguments in (
        ("web_search", {"query": "github trending"}),
        ("web_fetch", {"url": "https://github.com/trending"}),
        ("tool_search", {"query": "web search"}),
        ("load_skill", {"name": "research"}),
        ("get_current_time", {"timezone": "Asia/Shanghai"}),
    ):
        message = await run_tool_governance(
            ToolGovernanceContext(
                agent_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                tenant_id=str(uuid.uuid4()),
                tool_name=tool_name,
                arguments=arguments,
                permission_profile=PermissionProfileV1(default_decision="escalate"),
            ),
            deps,
            event_callback=events.append,
        )
        assert message is None, tool_name

    assert approval_calls == []
    assert audit_calls == []
    assert events == []


@pytest.mark.asyncio
async def test_core_read_tool_explicit_approval_policy_creates_enterprise_approval() -> None:
    """The missing-policy default is open for core read/discovery tools, but an
    explicit tenant/admin approval policy must be honored as company approval,
    not bypassed by Session permission mode."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            tool_name="web_search",
            arguments={"query": "github trending"},
            session_id="session-company-approval",
            permission_profile=PermissionProfileV1(mode="bypassPermissions", default_decision="escalate"),
        ),
        _governance_deps(
            check_capability_fn=_live_policy_check_capability(allowed=True, requires_approval=True),
            approval_calls=approval_calls,
            audit_calls=audit_calls,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "approval_required" in message
    assert approval_calls == [{"tool_name": "web_search", "capability": "external.web.search"}]
    assert events and events[-1]["status"] == "approval_required"
    assert events[-1]["tool_name"] == "web_search"
    assert events[-1]["capability"] == "external.web.search"
    assert events[-1]["approval_id"] == "approval-no-policy"


@pytest.mark.asyncio
async def test_permission_profile_legacy_escalate_default_asks_session_locally() -> None:
    """Legacy default_decision="escalate" maps to CC local session permission,
    not Hive backend approval."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            session_id="session-1",
            tool_call_id="call_missing_policy",
            tool_name="write_file",  # maps to workspace.file.write, no policy → escalate
            arguments={"path": "workspace/notes.md", "content": "x"},
            permission_profile=PermissionProfileV1(mode="default", default_decision="escalate"),
        ),
        _governance_deps(
            check_capability_fn=_live_no_policy_check_capability(),
            approval_calls=approval_calls,
            audit_calls=audit_calls,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "requires session permission" in message
    assert "Approval ID" not in message
    assert approval_calls == []
    assert audit_calls == []
    assert events and events[-1]["status"] == "session_permission_required"
    pending_frame = events[-1]["permission_request"]["pending_tool_frame"]
    assert pending_frame["permission_request_id"] == events[-1]["permission_request_id"]
    assert pending_frame["tool_call_id"] == "call_missing_policy"
    assert pending_frame["tool_name"] == "write_file"
    assert pending_frame["session_id"] == "session-1"
    assert pending_frame["permission_profile"]["mode"] == "default"


@pytest.mark.asyncio
async def test_session_permission_pending_frame_carries_runtime_turn_frame() -> None:
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            session_id="session-1",
            tool_call_id="tool-call-1",
            tool_name="write_file",
            arguments={"path": "workspace/notes.md", "content": "x"},
            permission_profile=PermissionProfileV1(mode="default", default_decision="escalate"),
            turn_id="turn-1",
            runtime_task_id="runtime-1",
            origin_channel="feishu",
            round_state={"round": 3, "tool_call_index": 1},
            t0_refs=("t0://sessions/session-1/segments/seg-1/events.jsonl#42",),
        ),
        _governance_deps(
            check_capability_fn=_live_no_policy_check_capability(),
            approval_calls=approval_calls,
            audit_calls=audit_calls,
        ),
        event_callback=events.append,
    )

    assert message is not None
    pending_frame = events[-1]["permission_request"]["pending_tool_frame"]
    assert pending_frame["turn_id"] == "turn-1"
    assert pending_frame["runtime_task_id"] == "runtime-1"
    assert pending_frame["origin_channel"] == "feishu"
    assert pending_frame["round_state"] == {"round": 3, "tool_call_index": 1}
    assert pending_frame["t0_refs"] == ("t0://sessions/session-1/segments/seg-1/events.jsonl#42",)


@pytest.mark.asyncio
async def test_permission_profile_dont_ask_blocks_mapped_no_policy_capability() -> None:
    """dontAsk mode must DENY the mapped-no-policy capability outright — no
    backend approval request."""
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
            permission_profile=PermissionProfileV1(mode="dontAsk"),
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
    assert audit_calls == []
    assert events and events[-1]["status"] == "permission_denied"
    assert events[-1]["tool_name"] == "write_file"


@pytest.mark.asyncio
async def test_permission_profile_none_falls_back_to_full_access_default() -> None:
    """No profile threaded onto the turn uses Hive's current session default:
    full access, while explicit hard-deny policies still win elsewhere."""
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []

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
        event_callback=events.append,
    )

    assert message is None
    assert approval_calls == []
    assert audit_calls == []
    assert events == []


def test_permission_profile_resolve_no_policy_decision_normalizes() -> None:
    """The legacy pure resolver maps default_decision to a session-level action."""
    assert capability_gate.resolve_no_policy_decision(None) == "ask"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1()) == "ask"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="deny")) == "deny"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="allow")) == "allow"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="ESCALATE")) == "ask"
    assert capability_gate.resolve_no_policy_decision(PermissionProfileV1(default_decision="bogus")) == "ask"
