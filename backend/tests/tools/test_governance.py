from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_governance_blocks_unsafe_tool_in_public_zone():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    events = []

    async def resolve_security_zone(_agent_id):
        return "public"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        raise AssertionError("capability check should not run when security zone already blocks")

    async def write_audit(**kwargs):
        raise AssertionError("audit should not run for pure security-zone block")

    async def request_approval(*args, **kwargs):
        raise AssertionError("approval request should not run when security zone already blocks")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="write_file",
            arguments={"path": "focus.md", "content": "x"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert (
        message
        == "🔒 Tool 'write_file' is blocked — this agent is in the 'public' security zone and can only use safe read-only tools."
    )
    assert events == [
        {
            "type": "permission",
            "tool_name": "write_file",
            "status": "blocked",
            "message": "🔒 Tool 'write_file' is blocked — this agent is in the 'public' security zone and can only use safe read-only tools.",
            "security_zone": "public",
        }
    ]


@pytest.mark.asyncio
async def test_governance_allows_collected_safe_tool_without_registry_init():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    events = []

    async def resolve_security_zone(_agent_id):
        return "public"

    async def check_capability(*_args, **_kwargs):
        raise AssertionError("capability check should not run for safe tool in public zone")

    async def write_audit(**_kwargs):
        raise AssertionError("audit should not run for safe tool in public zone")

    async def request_approval(*_args, **_kwargs):
        raise AssertionError("approval should not run for safe tool in public zone")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=None,
            tool_name="discover_resources",
            arguments={"query": "send email"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is None
    assert events == []


@pytest.mark.asyncio
async def test_governance_emits_capability_denied_and_audit():
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    audit_calls = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        assert _tenant_id == tenant_id
        assert _agent_id == agent_id
        assert tool_name == "execute_code"
        return CapabilityCheckResult(
            allowed=False,
            denied=True,
            capability="workspace.code.execute",
            reason="Capability 'workspace.code.execute' is not allowed for this agent",
        )

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*args, **kwargs):
        raise AssertionError("approval request should not run after capability deny")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="execute_code",
            arguments={"code": "print(1)"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message == "🚫 Capability denied: Capability 'workspace.code.execute' is not allowed for this agent"
    assert audit_calls == [
        {
            "event_type": "capability.denied",
            "severity": "warn",
            "actor_type": "agent",
            "actor_id": agent_id,
            "tenant_id": tenant_id,
            "action": "capability_denied",
            "resource_type": "tool",
            "resource_id": None,
            "details": {"tool": "execute_code", "capability": "workspace.code.execute"},
        }
    ]
    assert events == [
        {
            "type": "permission",
            "tool_name": "execute_code",
            "status": "capability_denied",
            "message": "🚫 Capability denied: Capability 'workspace.code.execute' is not allowed for this agent",
            "capability": "workspace.code.execute",
        }
    ]


@pytest.mark.asyncio
async def test_governance_requests_approval_when_capability_requires_it():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(
            denied=False,
            escalate_to_l3=True,
            capability="channel.feishu.message",
            reason="Capability 'channel.feishu.message' requires approval",
        )

    async def write_audit(**kwargs):
        return None

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None):
        assert tool_name == "send_feishu_message"
        assert arguments["message"] == "hi"
        assert capability == "channel.feishu.message"
        assert reason is None
        return {"allowed": False, "approval_id": "approval-1"}

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="send_feishu_message",
            arguments={"member_name": "张三", "message": "hi"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message == (
        "⏳ This action requires approval. An approval request has been sent. "
        "Please wait for approval before retrying. (Approval ID: approval-1)"
    )
    assert events == [
        {
            "type": "permission",
            "tool_name": "send_feishu_message",
            "status": "approval_required",
            "message": (
                "⏳ This action requires approval. An approval request has been sent. "
                "Please wait for approval before retrying. (Approval ID: approval-1)"
            ),
            "approval_id": "approval-1",
            "capability": "channel.feishu.message",
        }
    ]


@pytest.mark.asyncio
async def test_restricted_zone_sensitive_tool_creates_real_approval_request():
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    approval_calls = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "restricted"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        assert tool_name == "write_file"
        return CapabilityCheckResult(
            allowed=True,
            denied=False,
            escalate_to_l3=False,
            capability="workspace.file.write",
            policy_found=True,
        )

    async def write_audit(**_kwargs):
        return None

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None):
        approval_calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "capability": capability,
                "reason": reason,
            }
        )
        return {"allowed": False, "approval_id": "approval-restricted"}

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="write_file",
            arguments={"path": "focus.md", "content": "x"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert "Approval ID: approval-restricted" in message
    assert approval_calls[0]["tool_name"] == "write_file"
    assert approval_calls[0]["capability"] == "workspace.file.write"
    assert approval_calls[0]["reason"] == "restricted security zone"
    assert events == [
        {
            "type": "permission",
            "tool_name": "write_file",
            "status": "approval_required",
            "message": message,
            "approval_id": "approval-restricted",
            "capability": "workspace.file.write",
        }
    ]


@pytest.mark.asyncio
async def test_governance_denies_feishu_doc_delete_in_standard_zone_when_policy_denies():
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    events = []
    audit_calls = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        assert _tenant_id == tenant_id
        assert _agent_id == agent_id
        assert tool_name == "feishu_doc_delete"
        return CapabilityCheckResult(
            allowed=False,
            denied=True,
            capability="channel.feishu.document",
            reason="Capability 'channel.feishu.document' is not allowed for this agent",
        )

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*args, **kwargs):
        raise AssertionError("approval should not be requested after capability deny")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="feishu_doc_delete",
            arguments={"document_token": "doc-token"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message == "🚫 Capability denied: Capability 'channel.feishu.document' is not allowed for this agent"
    assert audit_calls == [
        {
            "event_type": "capability.denied",
            "severity": "warn",
            "actor_type": "agent",
            "actor_id": agent_id,
            "tenant_id": tenant_id,
            "action": "capability_denied",
            "resource_type": "tool",
            "resource_id": None,
            "details": {"tool": "feishu_doc_delete", "capability": "channel.feishu.document"},
        }
    ]
    assert events == [
        {
            "type": "permission",
            "tool_name": "feishu_doc_delete",
            "status": "capability_denied",
            "message": "🚫 Capability denied: Capability 'channel.feishu.document' is not allowed for this agent",
            "capability": "channel.feishu.document",
        }
    ]


@pytest.mark.asyncio
async def test_governance_allows_secret_command_when_specific_policy_allows_without_approval():
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    checked_tools = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        checked_tools.append(tool_name)
        if tool_name == "workspace.command.secret_exfiltration":
            return CapabilityCheckResult(
                allowed=True,
                denied=False,
                escalate_to_l3=False,
                capability="workspace.command.secret_exfiltration",
                reason="",
            )
        return CapabilityCheckResult(
            allowed=True,
            denied=False,
            escalate_to_l3=False,
            capability="workspace.command.execute",
            reason="",
        )

    async def write_audit(**kwargs):
        raise AssertionError("audit should not run when policy allows")

    async def request_approval(*args, **kwargs):
        raise AssertionError("approval should not run when explicit secret policy allows auto")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "env | grep -E '^FEISHU_'"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
    )

    assert message is None
    assert checked_tools == ["run_command", "workspace.command.secret_exfiltration"]


@pytest.mark.asyncio
async def test_governance_escalates_dangerous_run_command_even_when_capability_allows():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    events = []
    approval_calls = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(
            denied=False,
            escalate_to_l3=False,
            capability="workspace.command.execute",
            reason="",
        )

    async def write_audit(**kwargs):
        return None

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None):
        approval_calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "capability": capability,
                "reason": reason,
            }
        )
        return {"allowed": False, "approval_id": "approval-danger"}

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "rm -rf /tmp/build-cache"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert "requires approval" in message
    assert approval_calls[0]["tool_name"] == "run_command"
    assert approval_calls[0]["capability"] == "workspace.command.dangerous"
    assert "recursive delete" in approval_calls[0]["reason"]
    assert events == [
        {
            "type": "permission",
            "tool_name": "run_command",
            "status": "approval_required",
            "message": message,
            "approval_id": "approval-danger",
            "capability": "workspace.command.dangerous",
        }
    ]


# ── P0-1a: tenant_id=None fail-closed for non-safe tools ──────────────
# Closes the bypass where invoker._resolve_runtime_config fallbacks (agent_id
# missing / agent not found / DB exception) returned tenant_id=None and
# governance silently skipped capability checks. Now: non-safe tools blocked,
# safe tools (read-only) still permitted to support bootstrap/discovery paths.


@pytest.mark.asyncio
async def test_governance_fail_closed_when_tenant_missing_for_non_safe_tool():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    audit_calls = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"  # not public, not restricted — would normally pass to capability gate

    async def check_capability(*_args, **_kwargs):
        raise AssertionError("capability check must NOT run when tenant_id is missing")

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*_args, **_kwargs):
        raise AssertionError("approval must NOT run on tenant-missing fail-closed path")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=None,  # ← simulates invoker fallback
            tool_name="edit_file",  # non-safe, has capability mapping
            arguments={"path": "x.md", "content": "y"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "no tenant context" in message.lower()
    assert "edit_file" in message
    assert len(audit_calls) == 1
    assert audit_calls[0]["event_type"] == "capability.tenant_missing"
    assert audit_calls[0]["action"] == "tenant_missing_blocked"
    assert audit_calls[0]["tenant_id"] is None
    assert audit_calls[0]["details"] == {"tool": "edit_file"}
    assert events == [
        {
            "type": "permission",
            "tool_name": "edit_file",
            "status": "blocked",
            "message": message,
        }
    ]


@pytest.mark.asyncio
async def test_governance_fail_closed_does_not_break_safe_tool_bootstrap():
    """Bootstrap/discovery paths still need read-only tools without tenant context."""
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(*_args, **_kwargs):
        raise AssertionError("capability check must not run when tenant_id is missing")

    async def write_audit(**_kwargs):
        raise AssertionError("audit must not run for safe tool on bootstrap path")

    async def request_approval(*_args, **_kwargs):
        raise AssertionError("approval must not run for safe tool on bootstrap path")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=None,
            tool_name="read_file",  # in SAFE_TOOLS
            arguments={"path": "soul.md"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is None  # safe tool allowed
    assert events == []


@pytest.mark.asyncio
async def test_governance_fail_closed_blocks_dangerous_command_without_tenant():
    """tenant_id=None + dangerous run_command — must still block (run_command is non-safe)."""
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    audit_calls = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(*_args, **_kwargs):
        raise AssertionError("capability check must not run on tenant-missing fail-closed")

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*_args, **_kwargs):
        raise AssertionError("approval must not run on tenant-missing fail-closed")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=None,
            tool_name="run_command",  # non-safe; would have triggered dangerous detection downstream
            arguments={"command": "rm -rf /"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "no tenant context" in message.lower()
    # Fail-closed kicks in BEFORE dangerous command detection — that's intentional:
    # without tenant we cannot resolve the policy that would have approved/denied it.
    assert audit_calls[0]["event_type"] == "capability.tenant_missing"
    assert events[0]["status"] == "blocked"


# ── P1-W3-3 — delegation token enforcement in governance ──────


@pytest.mark.asyncio
async def test_governance_denies_when_delegation_token_expired():
    from app.agents.delegation_token import issue_delegation_token
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    audit_calls: list[dict] = []
    events: list[dict] = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_t, _a, _tool):
        return CapabilityCheckResult(allowed=True, capability="workspace.file.read")

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*_a, **_kw):
        raise AssertionError("approval should not run after token denial")

    expired_token = issue_delegation_token(
        parent_agent_id=uuid4(),
        child_agent_id=agent_id,
        granted_capabilities=frozenset({"workspace.file.read"}),
        ttl_seconds=10.0,
        now=0.0,
    )
    # Pretend "now" is past expiry by passing a token whose expires_at is
    # already < real time.

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="read_file",
            arguments={},
            delegation_token=expired_token,
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "Delegation token rejected" in message
    assert "expired" in message
    assert any(c["event_type"] == "delegation.token_denied" for c in audit_calls)
    assert any(e["status"] == "delegation_token_denied" for e in events)


@pytest.mark.asyncio
async def test_governance_denies_when_capability_outside_token_grant():
    from app.agents.delegation_token import issue_delegation_token
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    audit_calls: list[dict] = []
    events: list[dict] = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_t, _a, _tool):
        return CapabilityCheckResult(
            allowed=True,
            capability="workspace.file.write",  # not in token grant
        )

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*_a, **_kw):
        raise AssertionError("approval should not run")

    fresh_token = issue_delegation_token(
        parent_agent_id=uuid4(),
        child_agent_id=agent_id,
        granted_capabilities=frozenset({"workspace.file.read"}),
        ttl_seconds=300.0,  # not expired
    )

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="write_file",
            arguments={},
            delegation_token=fresh_token,
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "Delegation token rejected" in message
    assert "not in delegation grant" in message


@pytest.mark.asyncio
async def test_governance_denies_when_delegation_token_belongs_to_other_child():
    from app.agents.delegation_token import issue_delegation_token
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    audit_calls: list[dict] = []
    events: list[dict] = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_t, _a, _tool):
        return CapabilityCheckResult(allowed=True, capability="workspace.file.read")

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*_a, **_kw):
        raise AssertionError("approval should not run")

    token_for_other_child = issue_delegation_token(
        parent_agent_id=uuid4(),
        child_agent_id=uuid4(),
        granted_capabilities=frozenset({"workspace.file.read"}),
        ttl_seconds=300.0,
    )

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="read_file",
            arguments={},
            delegation_token=token_for_other_child,
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert message is not None
    assert "child agent mismatch" in message
    assert any(c["event_type"] == "delegation.token_denied" for c in audit_calls)
    assert any(e["status"] == "delegation_token_denied" for e in events)


@pytest.mark.asyncio
async def test_governance_allows_when_token_grants_capability():
    """A fresh, in-scope token must not block — falls through to normal flow."""
    from app.agents.delegation_token import issue_delegation_token
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_t, _a, _tool):
        return CapabilityCheckResult(allowed=True, capability="workspace.file.read")

    async def write_audit(**_kw):
        pass

    async def request_approval(*_a, **_kw):
        return {"granted": True}

    fresh_token = issue_delegation_token(
        parent_agent_id=uuid4(),
        child_agent_id=agent_id,
        granted_capabilities=frozenset({"workspace.file.read"}),
        ttl_seconds=300.0,
    )

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="read_file",
            arguments={},
            delegation_token=fresh_token,
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
    )

    # No blocking message — None means "proceed with execution".
    assert message is None


@pytest.mark.asyncio
async def test_governance_skips_token_check_when_no_token():
    """Web chat / trigger / heartbeat have no delegation token; the
    enforcement branch must short-circuit cleanly."""
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()

    async def resolve_security_zone(_a):
        return "standard"

    async def check_capability(_t, _a, _tool):
        return CapabilityCheckResult(allowed=True, capability="workspace.file.read")

    async def write_audit(**_kw):
        pass

    async def request_approval(*_a, **_kw):
        return {"granted": True}

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="read_file",
            arguments={},
            # delegation_token defaults to None
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
    )

    assert message is None  # passes through
