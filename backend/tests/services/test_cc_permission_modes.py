"""CCPlus session permission modes must be distinct from enterprise approvals."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1, build_permission_profile
from app.runtime.hooks import HookContext, HookEvent, HookResult, hook_registry
from app.services.capability_gate import CapabilityCheckResult
from app.tools.governance import ToolGovernanceContext, run_tool_governance


def _deps(*, capability_result: CapabilityCheckResult | None = None, approval_calls=None, audit_calls=None):
    approval_calls = approval_calls if approval_calls is not None else []
    audit_calls = audit_calls if audit_calls is not None else []

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, tool_name):
        if capability_result is not None:
            return capability_result
        return CapabilityCheckResult(
            allowed=False,
            escalate_to_l3=True,
            capability=f"capability.{tool_name}",
            reason="missing policy",
            policy_found=False,
        )

    async def write_audit_event(**kwargs):
        audit_calls.append(kwargs)

    async def request_approval(**kwargs):
        approval_calls.append(kwargs)
        return {"allowed": False, "approval_id": "approval-should-not-exist"}

    return SimpleNamespace(
        resolve_security_zone=resolve_security_zone,
        check_capability=check_capability,
        write_audit_event=write_audit_event,
        request_approval=request_approval,
        resolve_mcp_tool_mode=None,
    )


async def _govern(
    *,
    tool_name: str,
    mode: PermissionMode | str = PermissionMode.DEFAULT,
    arguments: dict | None = None,
    capability_result: CapabilityCheckResult | None = None,
):
    approval_calls: list[dict] = []
    audit_calls: list[dict] = []
    events: list[dict] = []
    message = await run_tool_governance(
        ToolGovernanceContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            tool_name=tool_name,
            arguments=arguments or {},
            permission_profile=PermissionProfileV1(mode=mode),
        ),
        _deps(capability_result=capability_result, approval_calls=approval_calls, audit_calls=audit_calls),
        event_callback=events.append,
    )
    return message, approval_calls, audit_calls, events


def test_permission_profile_normalizes_cc_mode_names_and_legacy_aliases() -> None:
    assert PermissionProfileV1().mode == PermissionMode.AUTO
    assert build_permission_profile().mode == PermissionMode.AUTO
    assert build_permission_profile({"mode": "acceptEdits"}).mode == PermissionMode.ACCEPT_EDITS
    assert build_permission_profile({"mode": "accept_edits"}).mode == PermissionMode.ACCEPT_EDITS
    assert build_permission_profile({"mode": "dontAsk"}).mode == PermissionMode.DONT_ASK
    assert build_permission_profile({"mode": "dont_ask_low_risk"}).mode == PermissionMode.DONT_ASK
    assert build_permission_profile({"mode": "auto_review"}).mode == PermissionMode.AUTO
    assert build_permission_profile({"mode": "break_glass"}).mode == PermissionMode.BYPASS_PERMISSIONS
    assert build_permission_profile({"mode": "bogus"}).mode == PermissionMode.DEFAULT


@pytest.mark.asyncio
async def test_default_mode_allows_core_read_discovery_without_backend_approval() -> None:
    for tool_name, arguments in (
        ("web_search", {"query": "github trending"}),
        ("web_fetch", {"url": "https://github.com/trending"}),
        ("tool_search", {"query": "web search"}),
        ("load_skill", {"name": "research"}),
        ("get_current_time", {"timezone": "Asia/Shanghai"}),
    ):
        message, approval_calls, _audit_calls, events = await _govern(tool_name=tool_name, arguments=arguments)
        assert message is None, tool_name
        assert approval_calls == []
        assert events == []


@pytest.mark.asyncio
async def test_default_mode_asks_session_locally_for_sensitive_missing_policy() -> None:
    message, approval_calls, audit_calls, events = await _govern(
        tool_name="send_email",
        arguments={"to": "a@example.com", "subject": "x", "body": "x"},
    )

    assert message is not None
    assert "requires session permission" in message
    payload = json.loads(message)
    assert payload["status"] == "session_permission_required"
    assert payload["permission_request"]["permission_request_id"]
    assert payload["permission_request"]["tool_name"] == "send_email"
    assert approval_calls == []
    assert audit_calls == []
    assert events[-1]["status"] == "session_permission_required"
    assert events[-1]["permission_request_id"] == payload["permission_request"]["permission_request_id"]
    assert "approval_id" not in events[-1]


@pytest.mark.asyncio
async def test_permission_request_hook_can_approve_and_rewrite_session_tool_call() -> None:
    arguments = {"to": "a@example.com", "subject": "draft", "body": "x"}
    seen_contexts: list[HookContext] = []

    async def permission_approver(ctx: HookContext) -> HookResult:
        seen_contexts.append(ctx)
        return HookResult(
            permission_request_result={
                "behavior": "allow",
                "updatedInput": {"to": "a@example.com", "subject": "approved", "body": "x"},
                "updatedPermissions": [{"tool": "send_email", "behavior": "allow", "scope": "once"}],
            }
        )

    hook_registry.register(HookEvent.PERMISSION_REQUEST, permission_approver, key="test:permission-approve")
    try:
        message, approval_calls, audit_calls, events = await _govern(tool_name="send_email", arguments=arguments)
    finally:
        hook_registry.unregister_key_prefix("test:")

    assert message is None
    assert arguments == {"to": "a@example.com", "subject": "approved", "body": "x"}
    assert approval_calls == []
    assert audit_calls == []
    assert seen_contexts
    assert seen_contexts[-1].event == HookEvent.PERMISSION_REQUEST
    assert seen_contexts[-1].metadata["permission_request"]["tool_name"] == "send_email"
    assert events[-1]["status"] == "permission_resolved"
    assert events[-1]["decision"] == "allow"
    assert events[-1]["permission_request_id"]


@pytest.mark.asyncio
async def test_dont_ask_mode_denies_sensitive_missing_policy_without_backend_approval() -> None:
    message, approval_calls, audit_calls, events = await _govern(
        tool_name="send_email",
        mode=PermissionMode.DONT_ASK,
        arguments={"to": "a@example.com", "subject": "x", "body": "x"},
    )

    assert message is not None
    assert "permission mode 'dontAsk' denies" in message
    assert approval_calls == []
    assert audit_calls == []
    assert events[-1]["status"] == "permission_denied"


@pytest.mark.asyncio
async def test_plan_mode_denies_mutation_missing_policy_without_backend_approval() -> None:
    message, approval_calls, audit_calls, events = await _govern(
        tool_name="write_file",
        mode=PermissionMode.PLAN,
        arguments={"path": "workspace/report.md", "content": "x"},
    )

    assert message is not None
    assert "permission mode 'plan' denies" in message
    assert approval_calls == []
    assert audit_calls == []
    assert events[-1]["status"] == "permission_denied"


@pytest.mark.asyncio
async def test_accept_edits_allows_workspace_edit_but_asks_external_side_effect() -> None:
    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="write_file",
        mode=PermissionMode.ACCEPT_EDITS,
        arguments={"path": "workspace/report.md", "content": "x"},
    )
    assert message is None
    assert approval_calls == []
    assert events == []

    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="send_email",
        mode=PermissionMode.ACCEPT_EDITS,
        arguments={"to": "a@example.com", "subject": "x", "body": "x"},
    )
    assert message is not None
    assert "requires session permission" in message
    assert approval_calls == []
    assert events[-1]["status"] == "session_permission_required"


@pytest.mark.asyncio
async def test_auto_mode_allows_low_risk_workspace_edit_but_asks_dangerous_command() -> None:
    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="edit_file",
        mode=PermissionMode.AUTO,
        arguments={"path": "workspace/report.md", "old_text": "a", "new_text": "b"},
    )
    assert message is None
    assert approval_calls == []
    assert events == []

    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="run_command",
        mode=PermissionMode.AUTO,
        arguments={"command": "rm -rf workspace/tmp"},
    )
    assert message is not None
    assert "requires session permission" in message
    assert approval_calls == []
    assert events[-1]["status"] == "session_permission_required"


@pytest.mark.asyncio
async def test_auto_mode_requires_one_time_confirmation_for_delete_writes() -> None:
    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="fs_write",
        mode=PermissionMode.AUTO,
        arguments={"path": "workspace/report.md", "mode": "delete"},
    )

    assert message is not None
    payload = json.loads(message)
    request = payload["permission_request"]
    assert payload["status"] == "session_permission_required"
    assert request["risk_class"] == "destructive_delete"
    assert request["confirmation_kind"] == "destructive_once"
    assert request["allow_session_allowed"] is False
    assert approval_calls == []
    assert events[-1]["status"] == "session_permission_required"
    assert events[-1]["permission_request"]["allow_session_allowed"] is False


@pytest.mark.asyncio
async def test_bypass_permissions_still_requires_one_time_confirmation_for_delete_commands() -> None:
    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="run_command",
        mode=PermissionMode.BYPASS_PERMISSIONS,
        arguments={"command": "rm workspace/tmp.txt"},
    )

    assert message is not None
    payload = json.loads(message)
    request = payload["permission_request"]
    assert payload["status"] == "session_permission_required"
    assert request["risk_class"] == "destructive_delete"
    assert request["confirmation_kind"] == "destructive_once"
    assert request["allow_session_allowed"] is False
    assert request["capability"] == "workspace.command.destructive_delete"
    assert approval_calls == []
    assert events[-1]["status"] == "session_permission_required"
    assert events[-1]["permission_request"]["allow_session_allowed"] is False


@pytest.mark.asyncio
async def test_explicit_approval_policy_asks_session_without_backend_approval() -> None:
    message, approval_calls, _audit_calls, events = await _govern(
        tool_name="web_search",
        arguments={"query": "github trending"},
        capability_result=CapabilityCheckResult(
            allowed=False,
            escalate_to_l3=True,
            capability="external.web.search",
            reason="explicit approval policy",
            policy_found=True,
        ),
    )

    assert message is not None
    assert "requires session permission" in message
    assert approval_calls == []
    assert events[-1]["status"] == "session_permission_required"
    assert events[-1]["capability"] == "external.web.search"
    assert "approval_id" not in events[-1]
