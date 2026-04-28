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

    assert message == "🔒 Tool 'write_file' is blocked — this agent is in the 'public' security zone and can only use safe read-only tools."
    assert events == [{
        "type": "permission",
        "tool_name": "write_file",
        "status": "blocked",
        "message": "🔒 Tool 'write_file' is blocked — this agent is in the 'public' security zone and can only use safe read-only tools.",
        "security_zone": "public",
    }]


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
    assert audit_calls == [{
        "event_type": "capability.denied",
        "severity": "warn",
        "actor_type": "agent",
        "actor_id": agent_id,
        "tenant_id": tenant_id,
        "action": "capability_denied",
        "resource_type": "tool",
        "resource_id": None,
        "details": {"tool": "execute_code", "capability": "workspace.code.execute"},
    }]
    assert events == [{
        "type": "permission",
        "tool_name": "execute_code",
        "status": "capability_denied",
        "message": "🚫 Capability denied: Capability 'workspace.code.execute' is not allowed for this agent",
        "capability": "workspace.code.execute",
    }]


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
    assert events == [{
        "type": "permission",
        "tool_name": "send_feishu_message",
        "status": "approval_required",
        "message": (
            "⏳ This action requires approval. An approval request has been sent. "
            "Please wait for approval before retrying. (Approval ID: approval-1)"
        ),
        "approval_id": "approval-1",
        "capability": "channel.feishu.message",
    }]


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
    assert audit_calls == [{
        "event_type": "capability.denied",
        "severity": "warn",
        "actor_type": "agent",
        "actor_id": agent_id,
        "tenant_id": tenant_id,
        "action": "capability_denied",
        "resource_type": "tool",
        "resource_id": None,
        "details": {"tool": "feishu_doc_delete", "capability": "channel.feishu.document"},
    }]
    assert events == [{
        "type": "permission",
        "tool_name": "feishu_doc_delete",
        "status": "capability_denied",
        "message": "🚫 Capability denied: Capability 'channel.feishu.document' is not allowed for this agent",
        "capability": "channel.feishu.document",
    }]


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
    assert events == [{
        "type": "permission",
        "tool_name": "run_command",
        "status": "approval_required",
        "message": message,
        "approval_id": "approval-danger",
        "capability": "workspace.command.dangerous",
    }]
