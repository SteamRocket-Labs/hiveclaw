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
            arguments={"path": "workspace/notes.md", "content": "x"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    # B1 teaching denial: tool name + why (zone) + what to do next
    assert message is not None
    assert "write_file" in message
    assert "public" in message and "read-only" in message
    assert "What you can do instead" in message  # teaching element
    assert events == [
        {
            "type": "permission",
            "tool_name": "write_file",
            "status": "blocked",
            "message": message,
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
async def test_governance_allows_mcp_metadata_tools_in_public_zone():
    """Closure A2 review-fix: approval gates execution, not discovery.

    MCP metadata tools only read local imported-tool records. They must be
    treated like other safe discovery tools so public-zone agents can see that
    an approval-gated MCP call exists without executing it.
    """
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    async def resolve_security_zone(_agent_id):
        return "public"

    async def check_capability(*_args, **_kwargs):
        raise AssertionError("safe MCP metadata should not hit capability check in public zone")

    async def write_audit(**_kwargs):
        raise AssertionError("safe MCP metadata should not write audit in public zone")

    async def request_approval(*_args, **_kwargs):
        raise AssertionError("safe MCP metadata should not request approval")

    for tool_name, arguments in (
        ("list_mcp_resources", {}),
        ("read_mcp_resource", {"tool_name": "notion_search"}),
    ):
        message = await run_tool_governance(
            ToolGovernanceContext(
                agent_id=uuid4(),
                user_id=uuid4(),
                tenant_id=None,
                tool_name=tool_name,
                arguments=arguments,
            ),
            GovernanceDependencies(
                resolve_security_zone=resolve_security_zone,
                check_capability=check_capability,
                write_audit_event=write_audit,
                request_approval=request_approval,
            ),
        )

        assert message is None


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

    # B1 teaching denial: tool + capability + original reason + next steps
    assert message is not None
    assert "execute_code" in message
    assert "workspace.code.execute" in message  # the capability the model should learn
    assert "not allowed for this agent" in message  # original policy reason preserved
    assert "What you can do instead" in message
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
            "message": message,
            "capability": "workspace.code.execute",
        }
    ]


@pytest.mark.asyncio
async def test_governance_creates_enterprise_approval_when_capability_policy_requires_approval():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    events = []
    approval_calls = []
    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(
            denied=False,
            escalate_to_l3=True,
            capability="channel.feishu.message",
            reason="Capability 'channel.feishu.message' requires approval",
            policy_found=True,
        )

    async def write_audit(**kwargs):
        return None

    async def request_approval(
        *, agent_id, user_id, tool_name, arguments, capability, reason=None, session_id=None, approval_origin_type=None
    ):
        approval_calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "capability": capability,
                "reason": reason,
                "session_id": session_id,
                "approval_origin_type": approval_origin_type,
            }
        )
        return {"allowed": False, "approval_id": "approval-company-1", "message": "Approval requested from admin"}

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            tool_name="send_feishu_message",
            arguments={"member_name": "张三", "message": "hi"},
            session_id="session-1",
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
    assert "send_feishu_message" in message
    assert "channel.feishu.message" in message
    assert "approval_required" in message
    assert approval_calls == [
        {
            "agent_id": agent_id,
            "user_id": user_id,
            "tool_name": "send_feishu_message",
            "arguments": {"member_name": "张三", "message": "hi"},
            "capability": "channel.feishu.message",
            "reason": "Capability 'channel.feishu.message' requires approval",
            "session_id": "session-1",
            "approval_origin_type": "company_tool_policy",
        }
    ]
    assert events and events[-1]["status"] == "approval_required"
    assert events[-1]["tool_name"] == "send_feishu_message"
    assert events[-1]["capability"] == "channel.feishu.message"
    assert events[-1]["approval_id"] == "approval-company-1"
    assert events[-1]["approval_required"] is True
    assert "permission_request" not in events[-1]


@pytest.mark.asyncio
async def test_consumed_approval_suppresses_only_the_exact_live_approval_gate() -> None:
    from app.services.approval_ticket import ApprovalDecisionSet, hash_tool_input
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    approver_id = uuid4()
    approval_id = uuid4()
    arguments = {"member_name": "张三", "message": "approved"}
    capability = "channel.feishu.message"
    decision_id = "decision:exact-approved-call"
    approval_decision = ApprovalDecisionSet(
        approval_id=approval_id,
        tenant_id=tenant_id,
        action_type=capability,
        tool_name="send_feishu_message",
        input_hash=hash_tool_input("send_feishu_message", arguments),
        policy_snapshot_hash="policy-hash",
        envelope_hash="envelope-hash",
        decision_id=decision_id,
        requested_by_user_id=requester_id,
        approved_by_user_id=approver_id,
    )
    approval_requests: list[dict] = []
    events: list[dict] = []

    async def check_capability(*_args):
        return SimpleNamespace(
            denied=False,
            escalate_to_l3=True,
            capability=capability,
            reason="company policy requires approval",
            policy_found=True,
        )

    async def request_approval(**kwargs):
        approval_requests.append(kwargs)
        raise AssertionError("a consumed exact approval must not open another approval")

    result = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=requester_id,
            tenant_id=str(tenant_id),
            tool_name="send_feishu_message",
            arguments=arguments,
            tool_call_id="approved-call",
            decision_id=decision_id,
            approval_decision=approval_decision,
        ),
        GovernanceDependencies(
            resolve_security_zone=lambda _agent_id: "standard",
            check_capability=check_capability,
            write_audit_event=lambda **_kwargs: None,
            request_approval=request_approval,
        ),
        event_callback=events.append,
    )

    assert result is None
    assert approval_requests == []
    assert any(event.get("status") == "permission_resolved" for event in events)


@pytest.mark.asyncio
async def test_consumed_approval_cannot_override_a_live_capability_deny() -> None:
    from app.services.approval_ticket import ApprovalDecisionSet, hash_tool_input
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    arguments = {"member_name": "张三", "message": "approved"}
    approval_decision = ApprovalDecisionSet(
        approval_id=uuid4(),
        tenant_id=tenant_id,
        action_type="channel.feishu.message",
        tool_name="send_feishu_message",
        input_hash=hash_tool_input("send_feishu_message", arguments),
        policy_snapshot_hash="policy-hash",
        envelope_hash="envelope-hash",
        decision_id="decision:denied-after-approval",
        requested_by_user_id=requester_id,
        approved_by_user_id=uuid4(),
    )

    async def check_capability(*_args):
        return SimpleNamespace(
            denied=True,
            escalate_to_l3=False,
            capability="channel.feishu.message",
            reason="company policy now denies this action",
            policy_found=True,
        )

    result = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=requester_id,
            tenant_id=str(tenant_id),
            tool_name="send_feishu_message",
            arguments=arguments,
            decision_id=approval_decision.decision_id,
            approval_decision=approval_decision,
        ),
        GovernanceDependencies(
            resolve_security_zone=lambda _agent_id: "standard",
            check_capability=check_capability,
            write_audit_event=lambda **_kwargs: None,
            request_approval=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not request approval")),
        ),
    )

    assert result is not None
    assert "company policy now denies" in result


@pytest.mark.asyncio
async def test_restricted_zone_sensitive_tool_uses_session_policy_not_enterprise_approval():
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
        raise AssertionError("restricted zone must not create an enterprise approval")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="write_file",
            arguments={"path": "workspace/notes.md", "content": "x"},
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
    assert approval_calls == []
    assert events == []


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

    assert message is not None
    assert "feishu_doc_delete" in message
    assert "channel.feishu.document" in message
    assert "What you can do instead" in message
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
            "message": message,
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
            arguments={"command": "printenv CUSTOM_TOKEN"},
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
async def test_governance_requires_session_confirmation_for_simple_delete_command_even_when_run_allowed():
    from app.services.capability_gate import CapabilityCheckResult
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    checked_tools = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        checked_tools.append(tool_name)
        return CapabilityCheckResult(
            allowed=True,
            denied=False,
            escalate_to_l3=False,
            capability=tool_name if tool_name.startswith("workspace.") else "workspace.command.execute",
            reason="",
            policy_found=True,
        )

    async def write_audit(**kwargs):
        raise AssertionError("delete confirmation should remain session-local")

    async def request_approval(*args, **kwargs):
        raise AssertionError("delete confirmation should not create enterprise approval")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "rm workspace/report.md"},
            permission_profile=PermissionProfileV1(mode=PermissionMode.BYPASS_PERMISSIONS),
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
    assert "requires session permission" in message
    assert checked_tools == ["run_command", "workspace.command.destructive_delete"]
    assert events[-1]["capability"] == "workspace.command.destructive_delete"
    assert events[-1]["permission_request"]["allow_session_allowed"] is False


@pytest.mark.asyncio
async def test_governance_blocks_managed_channel_env_probe_without_approval_request():
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    approval_calls = []
    audit_calls = []
    events = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        return CapabilityCheckResult(
            allowed=True,
            denied=False,
            escalate_to_l3=False,
            capability=tool_name if tool_name.startswith("workspace.") else "workspace.command.execute",
            reason="",
        )

    async def write_audit(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(*args, **kwargs):
        approval_calls.append(kwargs)
        return {"allowed": False, "approval_id": "should-not-exist"}

    agent_id = uuid4()
    tenant_id = uuid4()

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(tenant_id),
            tool_name="run_command",
            arguments={"command": "env | grep -E '^FEISHU_'"},
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
    assert "managed channel credentials" in message
    assert "dedicated tools" in message
    assert approval_calls == []
    assert audit_calls == [
        {
            "event_type": "capability.denied",
            "severity": "warn",
            "actor_type": "agent",
            "actor_id": agent_id,
            "tenant_id": tenant_id,
            "action": "managed_credential_env_blocked",
            "resource_type": "tool",
            "resource_id": None,
            "details": {
                "tool": "run_command",
                "capability": "workspace.command.secret_exfiltration",
                "credential_family": "feishu",
            },
        }
    ]
    assert events == [
        {
            "type": "permission",
            "tool_name": "run_command",
            "status": "blocked",
            "message": message,
            "capability": "workspace.command.secret_exfiltration",
            "credential_family": "feishu",
        }
    ]


@pytest.mark.asyncio
async def test_governance_asks_session_for_dangerous_run_command_even_when_base_capability_allows():
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
        raise AssertionError("dangerous command approval must stay inside the session")

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

    assert "requires session permission" in message
    assert approval_calls == []
    assert events and events[-1]["status"] == "session_permission_required"
    assert events[-1]["capability"] == "workspace.command.destructive_delete"
    assert events[-1]["permission_request"]["allow_session_allowed"] is False
    assert "approval_id" not in events[-1]


@pytest.mark.asyncio
async def test_governance_asks_session_for_dangerous_run_command_subcommand():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    checked_tools: list[str] = []
    approval_calls: list[dict] = []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        checked_tools.append(tool_name)
        return SimpleNamespace(
            denied=False,
            escalate_to_l3=False,
            capability="workspace.command.execute",
            reason="",
            policy_found=True,
        )

    async def write_audit(**_kwargs):
        return None

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None):
        raise AssertionError("dangerous subcommand approval must stay inside the session")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "npm install && git clean -fdx"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
    )

    assert "requires session permission" in message
    assert checked_tools == ["run_command", "workspace.command.destructive_delete"]
    assert approval_calls == []


@pytest.mark.asyncio
async def test_governance_allows_shell_expansion_and_globs_inside_governed_sandbox():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext, run_tool_governance

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        return SimpleNamespace(
            denied=False,
            escalate_to_l3=False,
            capability=tool_name if tool_name.startswith("workspace.") else "workspace.command.execute",
            reason="",
            policy_found=tool_name.startswith("workspace."),
        )

    async def write_audit(**_kwargs):
        return None

    async def request_approval(**_kwargs):
        raise AssertionError("ordinary shell syntax must not create a synthetic approval gate")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "printf '%s\\n' \"$HOME\" && printf '%s\\n' workspace/*.txt"},
        ),
        GovernanceDependencies(
            resolve_security_zone=resolve_security_zone,
            check_capability=check_capability,
            write_audit_event=write_audit,
            request_approval=request_approval,
        ),
    )

    assert message is None


@pytest.mark.asyncio
async def test_governance_timeout_is_typed_unavailable_not_policy_denied(monkeypatch):
    import asyncio

    from app.tools import governance

    events: list[dict] = []

    async def unavailable_pipeline(*_args, **_kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(governance, "_GOVERNANCE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(governance, "_run_governance_inner", unavailable_pipeline)
    message = await governance.run_tool_governance(
        governance.ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="run_command",
            arguments={"command": "pwd"},
        ),
        governance.GovernanceDependencies(
            resolve_security_zone=lambda _agent_id: "standard",
            check_capability=lambda *_args: None,
            write_audit_event=lambda **_kwargs: None,
            request_approval=lambda **_kwargs: None,
        ),
        event_callback=events.append,
    )

    assert "<tool_error>" in message
    assert '"error_class": "governance_dependency_unavailable"' in message
    assert '"outcome": "unavailable"' in message
    assert "policy denied" not in message.lower()
    assert events[-1]["status"] == "unavailable"


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
    assert "delegation token does not cover it" in message
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
    assert "delegation token does not cover it" in message
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


# ── MCP server-policy gate (closure A2: approval gates execution) ──────────
#
# The MCP tool mode (auto / approval / deny) was resolved at execution time
# but only deny was enforced — approval behaved exactly like auto, a silent
# governance promise. The gate now lives in governance preflight so the
# post-approval replay path (execute_approved skips preflight) cannot loop.


def _mcp_gate_deps(resolve_mcp_tool_mode, request_approval=None):
    from app.tools.governance import GovernanceDependencies

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(denied=False, escalate_to_l3=False, capability=None, reason=None)

    async def write_audit(**kwargs):
        return None

    async def _default_request_approval(**kwargs):
        raise AssertionError("request_approval must not be called")

    return GovernanceDependencies(
        resolve_security_zone=resolve_security_zone,
        check_capability=check_capability,
        write_audit_event=write_audit,
        request_approval=request_approval or _default_request_approval,
        resolve_mcp_tool_mode=resolve_mcp_tool_mode,
    )


@pytest.mark.asyncio
async def test_governance_mcp_approval_mode_asks_session_permission():
    from app.tools.governance import ToolGovernanceContext, run_tool_governance

    events = []
    approvals = []

    async def resolve_mcp_tool_mode(_agent_id, tool_name, arguments):
        assert tool_name == "call_mcp_tool"
        assert arguments["tool_name"] == "notion_search"
        return "approval"

    async def request_approval(*, agent_id, user_id, tool_name, arguments, capability, reason=None):
        raise AssertionError("MCP approval mode must stay inside the session")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="call_mcp_tool",
            arguments={"tool_name": "notion_search", "arguments": {"q": "x"}},
        ),
        _mcp_gate_deps(resolve_mcp_tool_mode, request_approval),
        event_callback=events.append,
    )

    assert message is not None
    assert "requires session permission" in message
    assert approvals == []
    assert events and events[0]["status"] == "session_permission_required"
    assert events[0]["tool_name"] == "call_mcp_tool"
    assert events[0]["capability"] == "mcp_tool_call"


@pytest.mark.asyncio
async def test_governance_mcp_deny_mode_blocks_without_approval():
    from app.tools.governance import ToolGovernanceContext, run_tool_governance

    events = []

    async def resolve_mcp_tool_mode(_agent_id, _tool_name, _arguments):
        return "deny"

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="notion_search",
            arguments={"q": "x"},
        ),
        _mcp_gate_deps(resolve_mcp_tool_mode),
        event_callback=events.append,
    )

    assert message is not None
    assert "denied" in message.lower() or "blocked" in message.lower()
    assert events and events[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_governance_mcp_auto_and_none_fall_through():
    from app.tools.governance import ToolGovernanceContext, run_tool_governance

    for mode in ("auto", None):

        async def resolve_mcp_tool_mode(_agent_id, _tool_name, _arguments, _mode=mode):
            return _mode

        message = await run_tool_governance(
            ToolGovernanceContext(
                agent_id=uuid4(),
                user_id=uuid4(),
                tenant_id=str(uuid4()),
                tool_name="read_file",
                arguments={"path": "notes.md"},
            ),
            _mcp_gate_deps(resolve_mcp_tool_mode),
        )

        assert message is None  # falls through to the rest of governance


@pytest.mark.asyncio
async def test_governance_mcp_resolve_failure_fails_closed():
    from app.tools.governance import ToolGovernanceContext, run_tool_governance

    async def resolve_mcp_tool_mode(_agent_id, _tool_name, _arguments):
        raise RuntimeError("db down")

    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            tool_name="call_mcp_tool",
            arguments={"tool_name": "notion_search"},
        ),
        _mcp_gate_deps(resolve_mcp_tool_mode),
    )

    assert message is not None
    assert "blocked" in message.lower()
