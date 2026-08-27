from __future__ import annotations

import asyncio
from pathlib import Path
import json
from types import SimpleNamespace
import uuid
from uuid import uuid4

import pytest


def _approved_ticket_runtime_fields(context, *, tool_name: str, arguments: dict) -> dict:
    from app.services.approval_ticket import (
        build_approval_execution_envelope,
        hash_approval_execution_envelope,
        hash_tool_input,
    )

    envelope = build_approval_execution_envelope(
        context=context,
        tool_call_id=f"approved-tool-call:{uuid4()}",
        emit_runtime_hooks=True,
    )
    return {
        "tenant_id": uuid.UUID(str(context.tenant_id)),
        "tool_name": tool_name,
        "arguments": arguments,
        "input_hash": hash_tool_input(tool_name, arguments),
        "action_type": f"test.{tool_name}",
        "execution_envelope": envelope,
        "execution_envelope_hash": hash_approval_execution_envelope(envelope),
    }


class _FakeRuntimeResolver:
    def __init__(self, context):
        self.context = context
        self.calls = []

    async def resolve(self, *, agent_id, user_id, session_id=None):
        self.calls.append((agent_id, user_id, session_id))
        return self.context


class _FakeGovernanceResolver:
    def __init__(self, governance_context, governance_dependencies):
        self.governance_context = governance_context
        self.governance_dependencies = governance_dependencies
        self.context_calls = []
        self.deps_calls = 0

    async def build_context(self, *, runtime_context, tool_name, arguments, delegation_token=None):
        self.context_calls.append((runtime_context, tool_name, arguments, delegation_token))
        return self.governance_context

    def build_dependencies(self):
        self.deps_calls += 1
        return self.governance_dependencies


class _FakeRegistry:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def try_execute(self, request):
        self.calls.append(request)
        return self.result


@pytest.mark.asyncio
async def test_successful_skill_load_emits_instructions_loaded_boundary(monkeypatch, tmp_path):
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=tmp_path,
        session_id="session-1",
        runtime_task_id=str(uuid4()),
    )
    captured = []

    async def fake_emit(event, **kwargs):
        captured.append((event.value, kwargs))
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    await ToolRuntimeService._emit_loaded_instruction_hook(
        tool_name="load_skill",
        arguments={"name": "market-research"},
        context=context,
        tool_call_id="call-1",
        result="# Market Research\nFollow the evidence protocol.",
    )

    assert captured[0][0] == "instructions_loaded"
    metadata = captured[0][1]["metadata"]
    assert metadata["instruction_uri"] == f"agent://{context.agent_id}/skills/market-research"
    assert metadata["load_reason"] == "load_skill"
    assert metadata["content_sha256"]


@pytest.mark.asyncio
async def test_failed_legacy_skill_result_emits_no_instruction_or_asset_usage(monkeypatch, tmp_path):
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=tmp_path, session_id="session-1"
    )
    context.resolved_asset_refs = (
        ResolvedAssetRefV1(
            asset_id=str(uuid4()),
            asset_type="skill",
            native_key="skill:agent:a:missing",
            revision_id=str(uuid4()),
            revision_version=1,
            content_hash="hash",
        ),
    )
    hooks = []
    usage = []

    async def fake_emit(*args, **kwargs):
        hooks.append((args, kwargs))

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    service = object.__new__(ToolRuntimeService)
    service.asset_usage_recorder = lambda **kwargs: usage.append(kwargs)
    from app.tools.result_envelope import render_tool_error

    result = render_tool_error(
        tool_name="load_skill",
        error_class="not_found",
        message="Skill not found: missing",
        retryable=False,
    )

    await service._emit_loaded_instruction_hook(
        tool_name="load_skill", arguments={"name": "missing"}, context=context, tool_call_id="call-1", result=result
    )
    await service._record_resolved_asset_usage_for_tool(
        tool_name="load_skill", context=context, tool_call_id="call-1", result=result
    )

    assert hooks == []
    assert usage == []


def test_workspace_mutation_evidence_captures_hash_and_deletion_without_live_rehash(tmp_path):
    from app.tools.service import _capture_workspace_mutation_evidence

    agent_root = tmp_path / "agent"
    workspace = agent_root / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("owned version", encoding="utf-8")

    states, errors, lineage = _capture_workspace_mutation_evidence(
        tool_name="write_file",
        arguments={"path": "workspace/report.md"},
        result="OK",
        workspace=agent_root,
        before_states={
            "workspace/report.md": {
                "path": "workspace/report.md",
                "exists": False,
                "sha256": None,
                "size": 0,
            }
        },
    )

    assert errors == {}
    assert states["workspace/report.md"]["exists"] is True
    assert len(states["workspace/report.md"]["sha256"]) == 64
    assert lineage[0]["before_state"]["exists"] is False

    report.unlink()
    states, errors, lineage = _capture_workspace_mutation_evidence(
        tool_name="delete_file",
        arguments={"path": "workspace/report.md"},
        result="Deleted",
        workspace=agent_root,
        before_states={"workspace/report.md": states["workspace/report.md"]},
    )
    assert errors == {}
    assert states["workspace/report.md"]["exists"] is False
    assert lineage[0]["after_state"]["exists"] is False

    states, errors, lineage = _capture_workspace_mutation_evidence(
        tool_name="write_file",
        arguments={"path": "workspace/report.md"},
        result='❌ denied\n<tool_error>{"error_class":"denied"}</tool_error>',
        workspace=agent_root,
    )
    assert states == {}
    assert errors == {}
    assert lineage == []

    states, errors, lineage = _capture_workspace_mutation_evidence(
        tool_name="office_document_create",
        arguments={"path": "workspace/report.docx"},
        result='{"ok": false, "error": "operation_failed"}',
        workspace=agent_root,
    )
    assert states == {}
    assert errors == {}
    assert lineage == []


@pytest.mark.asyncio
async def test_tool_runtime_service_executes_through_registry_and_logs(monkeypatch):
    from app.core.execution_context import (
        A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        A2AToolAuthorityFrame,
        ExecutionPrincipal,
    )
    from app.runtime.ccplus_contracts import (
        PermissionMode,
        PermissionProfileV1,
        permission_profile_snapshot,
        permission_profile_snapshot_hash,
    )
    from app.services.execution_receipts import canonical_payload_hash
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    permission_profile = PermissionProfileV1(
        mode=PermissionMode.DONT_ASK,
        allowed_tools=("write_file",),
    )
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
        permission_profile=permission_profile,
        session_id="child-session-a2a",
        runtime_task_id="child-task-a2a",
        budget_run_id="budget-a2a",
    )
    execution_principal = ExecutionPrincipal(
        tenant_id=context.tenant_id,
        source_agent_id=context.agent_id,
        requester_user_id=context.user_id,
        root_session_id="root-session-a2a",
        root_runtime_task_id="root-task-a2a",
        delegation_chain=("agent:parent", f"agent:{context.agent_id}"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    parent_agent_id = uuid4()
    delegation_token = SimpleNamespace(
        delegation_id="delegation-a2a",
        parent_agent_id=parent_agent_id,
        child_agent_id=context.agent_id,
    )
    governance_context.delegation_token = delegation_token
    profile_snapshot = permission_profile_snapshot(permission_profile)
    authority_snapshot = {
        "schema": "hive.a2a_authority_snapshot.v1",
        "tenant_id": str(context.tenant_id),
        "owner_id": str(context.user_id),
        "source_agent_id": str(parent_agent_id),
        "target_agent_id": str(context.agent_id),
        "session_id": "child-session-a2a",
        "parent_session_id": "root-session-a2a",
        "trace_id": "trace-a2a",
        "runtime_task_id": "child-task-a2a",
        "root_runtime_task_id": "root-task-a2a",
        "budget_run_id": "budget-a2a",
        "interaction_type": "delegation",
        "depth": 1,
        "tool_profile": "worker_safe",
        "permission_profile": profile_snapshot,
        "execution_identity": None,
        "execution_principal": execution_principal.to_evidence(),
    }
    runtime_resolver = _FakeRuntimeResolver(context)
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("OK")
    logged = []
    ensured = []
    lease_renewals = []
    trace_metadata = {}

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        _context.guard_policy_snapshot = {"version": 11, "zone_guard": {}, "egress_guard": {}}
        _context.guard_policy_verdict = {
            "decision": "allow",
            "reason": None,
            "policy_version": 11,
            "matched_rules": (),
        }
        _context.capability_snapshot = {
            "allowed": True,
            "denied": False,
            "name": "workspace.file.write",
            "policy_found": True,
        }
        return None

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    async def fake_renew_current_runtime_task_lease(*, lease_seconds):
        lease_renewals.append(lease_seconds)
        return None

    monkeypatch.setattr(
        "app.services.runtime_task_fence.renew_current_runtime_task_lease",
        fake_renew_current_runtime_task_lease,
    )

    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: ensured.append(True),
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "write_file",
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        authority_frame=A2AToolAuthorityFrame(
            schema=A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
            principal=execution_principal,
            capability_snapshot=authority_snapshot,
            capability_snapshot_hash=canonical_payload_hash(authority_snapshot),
            policy_snapshot_hash=permission_profile_snapshot_hash(permission_profile),
            permission_profile=permission_profile,
            delegation_token=delegation_token,
            session_id="child-session-a2a",
            parent_session_id="root-session-a2a",
            runtime_task_id="child-task-a2a",
            root_runtime_task_id="root-task-a2a",
            budget_run_id="budget-a2a",
            trace_id="trace-a2a",
            delegation_id="delegation-a2a",
            sandbox_profile=str(profile_snapshot["sandbox"]),
            approval_policy=str(profile_snapshot["approval_policy"]),
        ),
        session_id="child-session-a2a",
        runtime_task_id="child-task-a2a",
        budget_run_id="budget-a2a",
        permission_profile=permission_profile,
        delegation_token=delegation_token,
        trace_metadata_sink=trace_metadata,
    )

    assert result == "OK"
    assert runtime_resolver.calls == [(context.agent_id, context.user_id, "child-session-a2a")]
    assert governance_resolver.deps_calls == 1
    assert governance_resolver.context_calls[0][3] is delegation_token
    assert ensured == [True]
    assert registry.calls[0].tool_name == "write_file"
    assert len(lease_renewals) == 1
    assert lease_renewals[0] > 0
    assert logged and logged[0][0][0] == context.agent_id
    states = [record["lifecycle_state"] for record in context.tool_lifecycle_records]
    assert states == ["created", "validated", "governed", "preflight", "executing", "completed"]
    tool_call_ids = {record["tool_call_id"] for record in context.tool_lifecycle_records}
    assert len(tool_call_ids) == 1
    assert context.tool_execution_frames[0]["status"] == "executing"
    assert context.tool_execution_frames[-1]["status"] == "completed"
    assert context.tool_execution_frames[-1]["tool_call_id"] in tool_call_ids
    assert logged[0][1]["detail"]["tool_call_lifecycle"]["tool_call_id"] in tool_call_ids
    assert logged[0][1]["detail"]["tool_execution_frame"]["status"] == "completed"
    assert trace_metadata["tool_decision"]["outcome"] == "allow"
    assert trace_metadata["decision_id"] == trace_metadata["tool_decision"]["decision_id"]
    assert trace_metadata["input_hash"] == trace_metadata["tool_decision"]["input_hash"]
    assert trace_metadata["tool_decision"]["delegated_by"] == str(parent_agent_id)
    assert trace_metadata["authority_policy_snapshot"]["guard_policy"]["version"] == 11
    assert trace_metadata["authority_policy_snapshot"]["guard_policy_verdict"]["decision"] == "allow"
    assert trace_metadata["authority_capability_snapshot"]["live_policy"]["name"] == "workspace.file.write"
    assert trace_metadata["execution_principal"] == execution_principal.to_evidence()
    assert trace_metadata["authority_snapshot_hash"] == canonical_payload_hash(authority_snapshot)
    assert trace_metadata["authority_frame_schema"] == A2A_TOOL_AUTHORITY_FRAME_SCHEMA
    assert trace_metadata["authority_policy_hash"] == permission_profile_snapshot_hash(permission_profile)
    assert trace_metadata["authority_frame_receipt"] == {
        "schema": A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        "capability_snapshot_hash": canonical_payload_hash(authority_snapshot),
        "policy_snapshot_hash": permission_profile_snapshot_hash(permission_profile),
        "trace_id": "trace-a2a",
        "session_id": "child-session-a2a",
        "parent_session_id": "root-session-a2a",
        "runtime_task_id": "child-task-a2a",
        "root_runtime_task_id": "root-task-a2a",
        "budget_run_id": "budget-a2a",
        "delegation_id": "delegation-a2a",
        "sandbox_profile": "workspace_write",
        "approval_policy": "granular",
    }
    assert trace_metadata["tool_decision"]["policy_snapshot_hash"] == trace_metadata["policy_snapshot_hash"]
    assert trace_metadata["workspace_mutation_evidence_captured"] is True
    assert trace_metadata["workspace_mutation_states"]["workspace/notes.md"]["exists"] is False
    assert trace_metadata["workspace_mutation_lineage"][0]["path"] == "workspace/notes.md"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame_overrides", "reason_code"),
    [
        ({"schema": None}, "a2a_authority_frame_version_invalid"),
        ({"principal": None}, "a2a_execution_principal_missing"),
        ({"capability_snapshot_hash": "not-a-hash"}, "a2a_authority_snapshot_invalid"),
        ({"policy_snapshot_hash": "f" * 64}, "a2a_authority_policy_drift"),
        ({"required": False}, "a2a_authority_frame_required_invalid"),
    ],
)
async def test_a2a_authority_frame_fails_before_effect(frame_overrides, reason_code, tmp_path):
    from app.core.execution_context import (
        A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        A2AToolAuthorityFrame,
        ExecutionPrincipal,
    )
    from app.runtime.ccplus_contracts import (
        PermissionMode,
        PermissionProfileV1,
        permission_profile_snapshot,
        permission_profile_snapshot_hash,
    )
    from app.services.execution_receipts import canonical_payload_hash
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    profile = PermissionProfileV1(mode=PermissionMode.DONT_ASK, allowed_tools=("write_file",))
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=tmp_path,
        permission_profile=profile,
    )
    principal = ExecutionPrincipal(
        tenant_id=context.tenant_id,
        source_agent_id=context.agent_id,
        requester_user_id=context.user_id,
        root_session_id="root-session",
    )
    profile_snapshot = permission_profile_snapshot(profile)
    authority_snapshot = {
        "schema": "hive.a2a_authority_snapshot.v1",
        "tenant_id": str(context.tenant_id),
        "owner_id": str(context.user_id),
        "source_agent_id": "source-agent",
        "target_agent_id": str(context.agent_id),
        "session_id": "child-session",
        "parent_session_id": "root-session",
        "trace_id": "trace-a2a",
        "runtime_task_id": "",
        "root_runtime_task_id": "",
        "budget_run_id": "",
        "interaction_type": "agent_message",
        "depth": 1,
        "tool_profile": "agent_message",
        "permission_profile": profile_snapshot,
        "execution_identity": None,
        "execution_principal": principal.to_evidence(),
    }
    runtime_resolver = _FakeRuntimeResolver(context)
    registry = _FakeRegistry("EFFECT_RAN")
    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=SimpleNamespace(),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
    )
    frame = {
        "principal": principal,
        "capability_snapshot": authority_snapshot,
        "capability_snapshot_hash": canonical_payload_hash(authority_snapshot),
        "policy_snapshot_hash": permission_profile_snapshot_hash(profile),
        "schema": A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        "permission_profile": profile,
        "session_id": "child-session",
        "parent_session_id": "root-session",
        "trace_id": "trace-a2a",
        "sandbox_profile": str(profile_snapshot["sandbox"]),
        "approval_policy": str(profile_snapshot["approval_policy"]),
    }
    frame.update(frame_overrides)

    result = await service.execute(
        "write_file",
        {"path": "workspace/a.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id="child-session",
        permission_profile=profile,
        authority_frame=A2AToolAuthorityFrame(**frame),
    )

    assert "<tool_error>" in str(result)
    assert reason_code in str(result)
    assert runtime_resolver.calls == []
    assert registry.calls == []


@pytest.mark.asyncio
async def test_a2a_authority_frame_rejects_capability_snapshot_drift_before_effect(tmp_path):
    from app.core.execution_context import (
        A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        A2AToolAuthorityFrame,
        ExecutionPrincipal,
    )
    from app.runtime.ccplus_contracts import (
        PermissionMode,
        PermissionProfileV1,
        permission_profile_snapshot,
        permission_profile_snapshot_hash,
    )
    from app.services.execution_receipts import canonical_payload_hash
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    profile = PermissionProfileV1(mode=PermissionMode.DONT_ASK, allowed_tools=("write_file",))
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=tmp_path,
        permission_profile=profile,
    )
    principal = ExecutionPrincipal(
        tenant_id=context.tenant_id,
        source_agent_id=context.agent_id,
        requester_user_id=context.user_id,
        root_session_id="root-session",
        root_runtime_task_id="root-task",
    )
    snapshot = {
        "schema": "hive.a2a_authority_snapshot.v1",
        "tenant_id": context.tenant_id,
        "owner_id": str(context.user_id),
        "source_agent_id": "",
        "target_agent_id": str(context.agent_id),
        "session_id": "child-session",
        "parent_session_id": "",
        "trace_id": "trace-a2a",
        "runtime_task_id": "",
        "root_runtime_task_id": "root-task",
        "budget_run_id": "",
        "interaction_type": "agent_message",
        "depth": 1,
        "tool_profile": "agent_message",
        "permission_profile": permission_profile_snapshot(profile),
        "execution_identity": None,
        "execution_principal": principal.to_evidence(),
    }
    trusted_hash = canonical_payload_hash(snapshot)
    snapshot["target_agent_id"] = str(uuid4())
    runtime_resolver = _FakeRuntimeResolver(context)
    registry = _FakeRegistry("EFFECT_RAN")
    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=SimpleNamespace(),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
    )

    result = await service.execute(
        "write_file",
        {"path": "workspace/a.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id="child-session",
        permission_profile=profile,
        authority_frame=A2AToolAuthorityFrame(
            schema=A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
            principal=principal,
            capability_snapshot=snapshot,
            capability_snapshot_hash=trusted_hash,
            policy_snapshot_hash=permission_profile_snapshot_hash(profile),
            permission_profile=profile,
            session_id="child-session",
            root_runtime_task_id="root-task",
            trace_id="trace-a2a",
            sandbox_profile="workspace_write",
            approval_policy="granular",
        ),
    )

    assert "a2a_authority_snapshot_drift" in str(result)
    assert runtime_resolver.calls == []
    assert registry.calls == []


@pytest.mark.asyncio
async def test_a2a_parent_profile_exact_effect_deny_overrides_global_allow(tmp_path):
    from app.core.execution_context import (
        A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
        A2AToolAuthorityFrame,
        ExecutionPrincipal,
    )
    from app.runtime.ccplus_contracts import (
        PermissionMode,
        PermissionProfileV1,
        permission_profile_snapshot,
        permission_profile_snapshot_hash,
    )
    from app.services.execution_receipts import canonical_payload_hash
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    profile = PermissionProfileV1(
        mode=PermissionMode.BYPASS_PERMISSIONS,
        allowed_tools=("read_file", "tool_search", "delegate_to_agent"),
        denied_actions=("write_file",),
    )
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=tmp_path,
        permission_profile=profile,
    )
    principal = ExecutionPrincipal(
        tenant_id=context.tenant_id,
        source_agent_id=context.agent_id,
        requester_user_id=context.user_id,
        root_session_id="root-session",
    )
    profile_snapshot = permission_profile_snapshot(profile)
    snapshot = {
        "schema": "hive.a2a_authority_snapshot.v1",
        "tenant_id": context.tenant_id,
        "owner_id": str(context.user_id),
        "source_agent_id": "",
        "target_agent_id": str(context.agent_id),
        "session_id": "child-session",
        "parent_session_id": "root-session",
        "trace_id": "trace-a2a-deny",
        "runtime_task_id": "",
        "root_runtime_task_id": "",
        "budget_run_id": "",
        "interaction_type": "agent_message",
        "depth": 1,
        "tool_profile": "agent_message",
        "permission_profile": profile_snapshot,
        "execution_identity": None,
        "execution_principal": principal.to_evidence(),
    }
    registry = _FakeRegistry("EFFECT_RAN")
    governance_context = SimpleNamespace(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/a.md", "content": "x"},
        delegation_token=None,
        permission_profile=profile,
    )
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
    )

    result = await service.execute(
        "write_file",
        {"path": "workspace/a.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id="child-session",
        permission_profile=profile,
        emit_runtime_hooks=False,
        authority_frame=A2AToolAuthorityFrame(
            schema=A2A_TOOL_AUTHORITY_FRAME_SCHEMA,
            principal=principal,
            capability_snapshot=snapshot,
            capability_snapshot_hash=canonical_payload_hash(snapshot),
            policy_snapshot_hash=permission_profile_snapshot_hash(profile),
            permission_profile=profile,
            session_id="child-session",
            parent_session_id="root-session",
            trace_id="trace-a2a-deny",
            sandbox_profile=str(profile_snapshot["sandbox"]),
            approval_policy=str(profile_snapshot["approval_policy"]),
        ),
    )

    assert "a2a_parent_effect_denied" in str(result)
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_binds_resolved_asset_revision_to_frame_and_usage_event() -> None:
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
        session_id="session-asset",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="load_skill",
        arguments={"name": "Report Display Name"},
    )
    asset_ref = ResolvedAssetRefV1(
        asset_id=str(uuid4()),
        asset_type="skill",
        native_key=f"skill:agent:{context.agent_id}:report-folder",
        revision_id=str(uuid4()),
        revision_version=7,
        content_hash="hash-v7",
        source_ref=f"agent:{context.agent_id}/skills/report-folder",
    )
    resolution_calls = []
    usage_calls = []

    async def resolve_refs(**kwargs):
        resolution_calls.append(kwargs)
        return (asset_ref,)

    async def record_usage(**kwargs):
        usage_calls.append(kwargs)
        return True

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("# Report\nResolved from report-folder."),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        asset_ref_resolver=resolve_refs,
        asset_usage_recorder=record_usage,
    )
    trace_metadata = {}

    result = await service.execute(
        "load_skill",
        {"name": "Report Display Name"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        tool_call_id="call-asset-1",
        emit_runtime_hooks=False,
        trace_metadata_sink=trace_metadata,
    )

    assert result.startswith("# Report")
    assert resolution_calls[0]["arguments"] == {"name": "Report Display Name"}
    completed_frame = context.tool_execution_frames[-1]
    assert completed_frame["resolved_asset_refs"][0]["native_key"].endswith(":report-folder")
    assert completed_frame["resolved_asset_refs"][0]["revision_version"] == 7
    assert usage_calls[0]["asset_refs"] == (asset_ref,)
    assert usage_calls[0]["tool_call_id"] == "call-asset-1"
    assert trace_metadata["tool_execution_frame"]["resolved_asset_refs"][0]["revision_id"] == asset_ref.revision_id
    assert trace_metadata["authority_capability_snapshot"]["resolved_asset_refs"][0]["content_hash"] == "hash-v7"


@pytest.mark.asyncio
async def test_approved_asset_tool_fails_closed_when_revision_changed() -> None:
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=Path("/tmp/ws"))
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="load_skill",
        arguments={"name": "Report"},
    )
    common = {
        "asset_id": str(uuid4()),
        "asset_type": "skill",
        "native_key": f"skill:agent:{context.agent_id}:report",
        "source_ref": f"agent:{context.agent_id}/skills/report",
    }
    approved = ResolvedAssetRefV1(**common, revision_id=str(uuid4()), revision_version=1, content_hash="hash-v1")
    current = ResolvedAssetRefV1(**common, revision_id=str(uuid4()), revision_version=2, content_hash="hash-v2")
    registry = _FakeRegistry("MUST_NOT_EXECUTE")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        asset_ref_resolver=lambda **_kwargs: (current,),
        asset_usage_recorder=lambda **_kwargs: True,
    )

    result = await service.execute(
        "load_skill",
        {"name": "Report"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        emit_runtime_hooks=False,
        _expected_asset_refs=(approved,),
    )

    assert "approval_asset_revision_drift" in str(result)
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_does_not_prefetch_company_knowledge_for_preflight():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("OK"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
    )
    trace_metadata_sink: dict[str, object] = {}

    result = await service.execute(
        "write_file",
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace_metadata_sink,
    )

    assert result == "OK"
    assert "evidence_refs" not in trace_metadata_sink
    assert "truth_evidence" not in trace_metadata_sink


@pytest.mark.asyncio
async def test_tool_decision_links_the_durable_approval_ticket(monkeypatch):
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_email",
        arguments={"to": "person@example.com", "subject": "hello", "body": "body"},
    )

    async def require_approval(inner_context, _dependencies, **_kwargs):
        inner_context.decision_id = "decision-approval-1"
        inner_context.approval_id = "approval-1"
        return '{"status":"approval_required","approval_id":"approval-1"}'

    def forbidden_database_access(*_args, **_kwargs):
        raise AssertionError("unit test must inject capability policy instead of opening PostgreSQL")

    monkeypatch.setattr("app.database.tenant_scoped_session", forbidden_database_access)

    trace_metadata = {}
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("MUST_NOT_EXECUTE"),
        ensure_registry=lambda: None,
        governance_runner=require_approval,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        capability_group_policy_loader=lambda _context: {"email_pack": True},
    )

    result = await service.execute(
        "send_email",
        {"to": "person@example.com", "subject": "hello", "body": "body"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace_metadata,
    )

    assert "approval_required" in str(result)
    assert trace_metadata["tool_decision"]["outcome"] == "require_approval"
    assert trace_metadata["tool_decision"]["decision_id"] == "decision-approval-1"
    assert trace_metadata["tool_decision"]["approval_id"] == "approval-1"


@pytest.mark.asyncio
async def test_tool_runtime_service_threads_origin_channel_to_runtime_resolver():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )

    class RuntimeResolverWithOriginChannel:
        def __init__(self):
            self.calls = []

        async def resolve(self, *, agent_id, user_id, origin_channel=None):
            self.calls.append((agent_id, user_id, origin_channel))
            context.origin_channel = origin_channel
            return context

    runtime_resolver = RuntimeResolverWithOriginChannel()
    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("OK"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "write_file",
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        origin_channel="feishu",
    )

    assert result == "OK"
    assert runtime_resolver.calls == [(context.agent_id, context.user_id, "feishu")]


@pytest.mark.asyncio
async def test_tool_runtime_service_blocks_disabled_l2_pack_at_call_time(monkeypatch):
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="exa_search",
        arguments={"query": "openai sdk"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    monkeypatch.setattr("app.tools.service.is_l2_tool", lambda tool_name: tool_name == "exa_search", raising=False)
    monkeypatch.setattr("app.tools.service.policy_pack_names_for_tool", lambda tool_name: ("web_pack",), raising=False)
    monkeypatch.setattr(
        "app.tools.service.is_pack_enabled", lambda policies, pack_name: bool(policies.get(pack_name)), raising=False
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        pack_policy_loader=lambda runtime_context: {"web_pack": False},
    )

    result = await service.execute(
        "exa_search",
        {"query": "openai sdk"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert "<tool_error>" in result
    assert "extension_disabled" in result
    assert "web_pack" in result
    assert registry.calls == []
    assert governance_resolver.context_calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_blocks_disabled_l2_pack_in_execute_with_context(monkeypatch):
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    monkeypatch.setattr("app.tools.service.is_l2_tool", lambda tool_name: tool_name == "exa_search", raising=False)
    monkeypatch.setattr("app.tools.service.policy_pack_names_for_tool", lambda tool_name: ("web_pack",), raising=False)
    monkeypatch.setattr(
        "app.tools.service.is_pack_enabled", lambda policies, pack_name: bool(policies.get(pack_name)), raising=False
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        pack_policy_loader=lambda runtime_context: {"web_pack": False},
    )

    result = await service.execute_with_context(
        "exa_search",
        {"query": "openai sdk"},
        context,
    )

    assert "<tool_error>" in result
    assert "extension_disabled" in result
    assert "web_pack" in result
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_does_not_l2_block_core_command_wrappers():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="task_list",
        arguments={},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("OK")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        pack_policy_loader=lambda runtime_context: {"command_pack": False},
    )

    result = await service.execute(
        "task_list",
        {},
        agent_id=context.agent_id,
        user_id=context.user_id,
        emit_runtime_hooks=False,
    )

    assert result == "OK"
    assert registry.calls
    assert governance_resolver.context_calls


@pytest.mark.asyncio
async def test_tool_runtime_service_emits_hooks_and_revalidates_modified_args():
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("OK")
    hook_events: list[tuple[str, dict | None]] = []

    def pre_hook(ctx):
        hook_events.append((ctx.event.value, dict(ctx.tool_args or {})))
        return HookResult(modified_args={"path": "workspace/safe.md", "content": ctx.tool_args["content"]})

    def post_hook(ctx):
        hook_events.append((ctx.event.value, dict(ctx.tool_args or {})))
        return None

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    hook_registry.clear()
    hook_registry.register(HookEvent.PRE_TOOL_USE, pre_hook)
    hook_registry.register(HookEvent.POST_TOOL_USE, post_hook)
    try:
        service = ToolRuntimeService(
            runtime_resolver=_FakeRuntimeResolver(context),
            governance_resolver=governance_resolver,
            registry=registry,
            ensure_registry=lambda: None,
            governance_runner=fake_run_governance,
            fallback_executor=lambda *_args, **_kwargs: "fallback",
            direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
            activity_logger=None,
        )

        result = await service.execute(
            "write_file",
            {"path": "workspace/notes.md", "content": "x"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        hook_registry.clear()

    assert result == "OK"
    assert governance_resolver.context_calls[0][2]["path"] == "workspace/safe.md"
    assert registry.calls[0].arguments["path"] == "workspace/safe.md"
    assert hook_events == [
        ("pre_tool_use", {"path": "workspace/notes.md", "content": "x"}),
        ("post_tool_use", {"path": "workspace/safe.md", "content": "x"}),
    ]


@pytest.mark.asyncio
async def test_tool_runtime_service_blocks_hook_modified_args_that_violate_schema():
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    def pre_hook(_ctx):
        return HookResult(modified_args={"path": "workspace/safe.md"})

    hook_registry.clear()
    hook_registry.register(HookEvent.PRE_TOOL_USE, pre_hook)
    try:
        service = ToolRuntimeService(
            runtime_resolver=_FakeRuntimeResolver(context),
            governance_resolver=governance_resolver,
            registry=registry,
            ensure_registry=lambda: None,
            governance_runner=lambda *_args, **_kwargs: None,
            fallback_executor=lambda *_args, **_kwargs: "fallback",
            direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
            activity_logger=None,
        )

        result = await service.execute(
            "write_file",
            {"path": "workspace/notes.md", "content": "x"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        hook_registry.clear()

    assert "<tool_error>" in result
    assert "invalid_tool_arguments" in result
    assert "content" in result
    assert governance_resolver.context_calls == []
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_rejects_read_company_kb_singular_segment_id_before_execution():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="read_company_kb",
        arguments={},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    # Production regression (Company KB Run1/Run2, 2026-08-27): the singular
    # "segment_id" typo must be rejected by the schema admission gate before
    # governance, the handler, or the knowledge gateway run, with an actionable
    # schema-repair error instead of a silent full-document read.
    result = await service.execute(
        "read_company_kb",
        {"document_id": str(uuid4()), "segment_id": str(uuid4())},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert "<tool_error>" in result
    assert "invalid_tool_arguments" in result
    assert "segment_id" in result
    assert "Re-read the tool schema" in result
    assert governance_resolver.context_calls == []
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_threads_session_permission_context_into_delegation():
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="parent-session-1",
        permission_profile=PermissionProfileV1(
            mode=PermissionMode.BYPASS_PERMISSIONS,
            allowed_tools=("web_search", "feishu_doc_read"),
        ),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="delegate_to_agent",
        arguments={"agent_name": "Researcher", "message": "go"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("TASK")

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "delegate_to_agent",
        {"agent_name": "Researcher", "message": "go"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "TASK"
    governance_arguments = governance_resolver.context_calls[0][2]
    executed_arguments = registry.calls[0].arguments
    for arguments in (governance_arguments, executed_arguments):
        assert arguments["parent_session_id"] == "parent-session-1"
        assert arguments["_permission_profile"]["mode"] == "bypassPermissions"
        assert arguments["_permission_profile"]["allowed_tools"] == [
            "web_search",
            "feishu_doc_read",
        ]


@pytest.mark.asyncio
async def test_tool_runtime_service_threads_session_permission_context_into_agent_message():
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="parent-session-1",
        permission_profile=PermissionProfileV1(
            mode=PermissionMode.BYPASS_PERMISSIONS,
            allowed_tools=("web_search", "feishu_doc_read"),
        ),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_message_to_agent",
        arguments={"agent_name": "Knowledge", "message": "查一下飞书知识库"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("MESSAGE")

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    trace_metadata: dict = {}
    result = await service.execute(
        "send_message_to_agent",
        {"agent_name": "Knowledge", "message": "查一下飞书知识库"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace_metadata,
    )

    assert result == "MESSAGE"
    governance_arguments = governance_resolver.context_calls[0][2]
    executed_arguments = registry.calls[0].arguments
    for arguments in (governance_arguments, executed_arguments):
        assert arguments["parent_session_id"] == "parent-session-1"
        assert arguments["_permission_profile"]["mode"] == "bypassPermissions"
        assert arguments["_permission_profile"]["allowed_tools"] == [
            "web_search",
            "feishu_doc_read",
        ]
    assert trace_metadata["effective_arguments"] == executed_arguments
    assert trace_metadata["tool_decision"]["input_hash"] == trace_metadata["input_hash"]


@pytest.mark.asyncio
async def test_tool_runtime_service_logs_readonly_tool_calls():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="read_file",
        arguments={"path": "workspace/notes.md"},
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("file contents"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "read_file",
        {"path": "workspace/notes.md"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "file contents"
    assert logged
    assert logged[0][0][1] == "tool_call"
    assert logged[0][1]["tenant_id"] == context.tenant_id
    assert logged[0][1]["detail"]["tool"] == "read_file"


@pytest.mark.asyncio
async def test_tool_runtime_service_activity_summary_hides_raw_result_payload():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    raw_result = {"file_id": "internal-uuid-6f1c", "chunks": ["alpha", "beta"], "trace": "op-9931"}
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="read_file",
        arguments={"path": "workspace/notes.md"},
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry(raw_result),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "read_file",
        {"path": "workspace/notes.md"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == raw_result
    assert logged
    # The user-facing summary must stay a human-readable sentence: the raw tool
    # result (JSON payload, internal IDs) belongs to the structured detail only.
    assert logged[0][0][2] == "Called tool read_file"
    assert "internal-uuid-6f1c" not in logged[0][0][2]
    assert "chunks" not in logged[0][0][2]
    assert logged[0][1]["detail"]["result"] == str(raw_result)


@pytest.mark.asyncio
async def test_tool_runtime_service_returns_governance_block_without_registry_call():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    runtime_resolver = _FakeRuntimeResolver(context)
    governance_resolver = _FakeGovernanceResolver(
        ToolGovernanceContext(
            agent_id=context.agent_id,
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            tool_name="send_feishu_message",
            arguments={"message": "hi"},
        ),
        SimpleNamespace(),
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return "BLOCKED"

    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "hi"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "BLOCKED"
    assert registry.calls == []


@pytest.mark.asyncio
async def test_pre_effect_fence_runs_only_after_governance_allows_execution():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="read_file",
        arguments={"path": "workspace/notes.md"},
    )
    order: list[str] = []

    class _OrderedRegistry(_FakeRegistry):
        async def try_execute(self, request):
            order.append("executor")
            return await super().try_execute(request)

    async def allow_governance(*_args, **_kwargs):
        order.append("governance")
        return None

    async def durable_pre_effect_fence(payload):
        assert payload["tool_name"] == "read_file"
        assert payload["tool_call_id"]
        order.append("pre_effect_fence")

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_OrderedRegistry("file contents"),
        ensure_registry=lambda: None,
        governance_runner=allow_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "read_file",
        {"path": "workspace/notes.md"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        pre_effect_callback=durable_pre_effect_fence,
    )

    assert result == "file contents"
    assert order == ["governance", "pre_effect_fence", "executor"]


@pytest.mark.asyncio
async def test_pre_effect_fence_is_not_granted_when_governance_requires_approval():
    from app.tools.decision import ToolBoundaryBlock, ToolDecisionOutcome
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_feishu_message",
        arguments={"message": "hi"},
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    fence_calls: list[dict] = []

    async def require_approval(*_args, **_kwargs):
        return ToolBoundaryBlock(
            "Approval is required.",
            outcome=ToolDecisionOutcome.REQUIRE_APPROVAL,
            reason_code="company_policy_requires_approval",
            status="approval_required",
            retryable=False,
        )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=require_approval,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "hi"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        pre_effect_callback=lambda payload: fence_calls.append(payload),
    )

    assert str(result) == "Approval is required."
    assert fence_calls == []
    assert registry.calls == []


@pytest.mark.asyncio
async def test_governance_decision_never_comes_from_block_message_text():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_feishu_message",
        arguments={"message": "hi"},
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    trace: dict = {}

    async def legacy_untyped_block(_context, _deps, *, event_callback=None):
        # The words below previously forced REQUIRE_APPROVAL. An untyped legacy
        # return must instead be a typed infrastructure-contract failure.
        return "benign prose mentions approval_required and session_permission_required"

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=legacy_untyped_block,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "hi"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace,
    )

    assert str(result).startswith("benign prose")
    assert trace["tool_decision"]["outcome"] == "unavailable"
    assert trace["tool_decision"]["reason_codes"] == ("untyped_governance_block",)
    assert registry.calls == []


@pytest.mark.asyncio
async def test_governance_decision_consumes_typed_block_outcome():
    from app.tools.decision import ToolBoundaryBlock, ToolDecisionOutcome
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_feishu_message",
        arguments={"message": "hi"},
    )
    trace: dict = {}

    async def typed_block(_context, _deps, *, event_callback=None):
        return ToolBoundaryBlock(
            "No approval keywords are needed in this display string.",
            outcome=ToolDecisionOutcome.REQUIRE_APPROVAL,
            reason_code="company_policy_requires_approval",
            status="approval_required",
            retryable=False,
        )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("SHOULD_NOT_RUN"),
        ensure_registry=lambda: None,
        governance_runner=typed_block,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    await service.execute(
        "send_feishu_message",
        {"message": "hi"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace,
    )

    assert trace["tool_decision"]["outcome"] == "require_approval"
    assert trace["tool_decision"]["reason_codes"] == ("company_policy_requires_approval",)


def test_tool_result_failure_classification_uses_typed_machine_contract_only():
    """Benign model/tool prose must never become a hard outcome by keyword or emoji."""

    from app.tools.service import _tool_result_failed

    assert _tool_result_failed("❌ This heading discusses failure modes, but is not a tool failure receipt.") is False
    assert _tool_result_failed('{"status":"ok","message":"failed examples are documented"}') is False
    assert _tool_result_failed('{"status":"failed","message":"typed failure"}') is True


def test_tool_result_failure_classification_accepts_rendered_typed_error_receipt():
    from app.tools.result_envelope import render_tool_error
    from app.tools.service import _tool_result_failed

    receipt = render_tool_error(
        tool_name="read_file",
        error_class="not_found",
        message="The requested file does not exist.",
        retryable=False,
    )

    assert _tool_result_failed(receipt) is True


@pytest.mark.asyncio
async def test_tool_runtime_service_has_no_direct_governance_bypass():
    from app.tools.service import ToolRuntimeService

    assert not hasattr(ToolRuntimeService, "execute_direct")


@pytest.mark.asyncio
async def test_tool_runtime_service_execute_approved_logs_approval_metadata():
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    requested_by = uuid4()
    approved_by = uuid4()
    approval_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requested_by,
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    logged = []
    completions = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    async def consume_ticket(**kwargs):
        assert kwargs == {
            "approval_id": approval_id,
            "expected_agent_id": agent_id,
            "expected_user_id": approved_by,
        }
        arguments = {"path": "workspace/notes.md", "content": "done"}
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=requested_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="decision-approved-1",
            **_approved_ticket_runtime_fields(context, tool_name="write_file", arguments=arguments),
        )

    async def complete_ticket(**kwargs):
        completions.append(kwargs)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry("APPROVED"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
    )

    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result == "APPROVED"
    assert service.runtime_resolver.calls == [(agent_id, requested_by, None)]
    assert logged[0][0][1] == "tool_call_approved"
    assert logged[0][1]["detail"]["approved"] is True
    assert logged[0][1]["detail"]["approved_by_user_id"] == str(approved_by)
    assert logged[0][1]["detail"]["requested_by_user_id"] == str(requested_by)
    assert logged[0][1]["detail"]["approval_id"] == str(approval_id)
    assert logged[0][1]["detail"]["input_hash"] == completions[0]["receipt"]["input_hash"]
    assert completions[0]["status"] == "succeeded"
    assert completions[0]["receipt"]["decision_id"] == "decision-approved-1"
    assert [record["lifecycle_state"] for record in context.tool_lifecycle_records] == [
        "created",
        "validated",
        "governed",
        "preflight",
        "executing",
        "completed",
    ]
    assert logged[0][1]["detail"]["tool_call_lifecycle"]["lifecycle_state"] == "completed"
    assert logged[0][1]["detail"]["tool_execution_frame"]["status"] == "completed"


@pytest.mark.asyncio
async def test_execute_approved_satisfies_preflight_ask_and_executes_exact_external_effect():
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    agent_id = uuid4()
    requested_by = uuid4()
    approved_by = uuid4()
    approval_id = uuid4()
    arguments = {
        "open_id": "ou_example",
        "message": "Send the approved external vendor update.",
    }
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requested_by,
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/approved-preflight"),
    )
    completions = []
    registry = _FakeRegistry("SENT")

    async def consume_ticket(**_kwargs):
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=requested_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="preflight-decision-approved",
            **_approved_ticket_runtime_fields(
                context,
                tool_name="send_feishu_message",
                arguments=arguments,
            ),
        )

    async def complete_ticket(**kwargs):
        completions.append(kwargs)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result == "SENT"
    assert len(registry.calls) == 1
    assert registry.calls[0].tool_name == "send_feishu_message"
    assert completions[0]["status"] == "succeeded"
    preflight = completions[0]["receipt"]["runtime_evidence"]["preflight"]
    assert preflight["decision"] == "ask"
    assert preflight["approval_satisfied"] is True
    assert preflight["approval_id"] == str(approval_id)


@pytest.mark.asyncio
async def test_execute_approved_does_not_override_preflight_refuse_and_marks_ticket_failed():
    from app.services.action_preflight import ActionPreflightResult, PreflightDecision
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.decision import ToolDecisionOutcome
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    agent_id = uuid4()
    requested_by = uuid4()
    approved_by = uuid4()
    approval_id = uuid4()
    arguments = {"path": "workspace/blocked.md", "content": "blocked"}
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requested_by,
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/approved-preflight-refuse"),
    )
    completions = []
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    class _RefusingPreflight:
        def evaluate(self, _request):
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["hard_authority_denied"],
                requires_audit=True,
            )

    async def consume_ticket(**_kwargs):
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=requested_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="preflight-refuse-approved",
            **_approved_ticket_runtime_fields(
                context,
                tool_name="write_file",
                arguments=arguments,
            ),
        )

    async def complete_ticket(**kwargs):
        completions.append(kwargs)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        preflight_service=_RefusingPreflight(),
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result.outcome == ToolDecisionOutcome.DENY
    assert registry.calls == []
    assert completions[0]["status"] == "failed"
    assert completions[0]["receipt"]["boundary_outcome"] == "deny"
    assert completions[0]["receipt"]["boundary_reason_code"] == "preflight_refuse"


@pytest.mark.asyncio
async def test_execute_approved_does_not_override_owner_never_do_policy():
    from app.services.action_preflight import CharterZone
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        build_owner_action_policy,
    )
    from app.tools.decision import ToolDecisionOutcome
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    agent_id = uuid4()
    tenant_id = uuid4()
    requested_by = uuid4()
    approved_by = uuid4()
    approval_id = uuid4()
    arguments = {"open_id": "ou_example", "message": "This must remain blocked."}
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requested_by,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/owner-never-do"),
        owner_action_policy=build_owner_action_policy(
            agent_id=agent_id,
            tenant_id=tenant_id,
            actions={
                ACTION_EXTERNAL_EFFECT: CharterZone.NEVER_DO,
                ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
                ACTION_LOCAL_WRITE: CharterZone.FULL_AUTHORITY,
            },
            version=5,
            content_hash="owner-policy-v5",
        ),
    )
    completions = []
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    async def consume_ticket(**_kwargs):
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=requested_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="approval-policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="approval-before-never-do",
            **_approved_ticket_runtime_fields(
                context,
                tool_name="send_feishu_message",
                arguments=arguments,
            ),
        )

    async def complete_ticket(**kwargs):
        completions.append(kwargs)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result.outcome == ToolDecisionOutcome.DENY
    assert result.reason_code == "preflight_refuse"
    assert registry.calls == []
    assert completions[0]["status"] == "failed"
    policy_trace = completions[0]["receipt"]["runtime_evidence"]["preflight"]["owner_action_policy"]
    assert policy_trace["action_id"] == ACTION_EXTERNAL_EFFECT
    assert policy_trace["zone"] == "never_do"
    assert policy_trace["version"] == 5


@pytest.mark.asyncio
async def test_execute_approved_restores_external_execution_identity(monkeypatch):
    from app.core.execution_context import ExecutionIdentity
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    requested_by = uuid4()
    approved_by = uuid4()
    approval_id = uuid4()
    external_principal_id = uuid4()
    request_context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requested_by,
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/external-approved"),
        execution_identity=ExecutionIdentity(
            identity_type="external_principal_bound",
            identity_id=external_principal_id,
            label="Slack guest via slack",
        ),
    )
    arguments = {"path": "workspace/external.md", "content": "done"}

    async def consume_ticket(**_kwargs):
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=requested_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="external-decision",
            **_approved_ticket_runtime_fields(request_context, tool_name="write_file", arguments=arguments),
        )

    captured = {}

    async def complete_ticket(**_kwargs):
        return None

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(request_context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry("unused"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
    )

    async def capture_execute(*_args, **kwargs):
        captured.update(kwargs)
        return "APPROVED"

    monkeypatch.setattr(ToolRuntimeService, "execute", capture_execute)
    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result == "APPROVED"
    assert captured["execution_identity"].identity_type == "external_principal_bound"
    assert captured["execution_identity"].identity_id == external_principal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook_behavior", "error_code"),
    [
        ("modify", "approval_payload_mutation"),
        ("block", "approval_hook_block"),
    ],
)
async def test_execute_approved_rejects_hook_changes_after_ticket_consumption(
    hook_behavior,
    error_code,
):
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    requester_id = uuid4()
    approver_id = uuid4()
    approval_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    registry = _FakeRegistry("MUST_NOT_EXECUTE")
    completions = []

    async def consume_ticket(**_kwargs):
        arguments = {"path": "workspace/approved.md", "content": "approved"}
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=requester_id,
            approved_by_user_id=approver_id,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="decision-immutable-payload",
            **_approved_ticket_runtime_fields(context, tool_name="write_file", arguments=arguments),
        )

    async def complete_ticket(**kwargs):
        completions.append(kwargs)

    def mutate_after_approval(_context):
        if hook_behavior == "block":
            return HookResult(block=True, reason="policy changed")
        return HookResult(modified_args={"path": "workspace/replaced.md", "content": "different"})

    hook_registry.clear()
    hook_registry.register(HookEvent.PRE_TOOL_USE, mutate_after_approval)
    try:
        service = ToolRuntimeService(
            runtime_resolver=_FakeRuntimeResolver(context),
            governance_resolver=SimpleNamespace(),
            registry=registry,
            ensure_registry=lambda: None,
            governance_runner=lambda *_args, **_kwargs: None,
            fallback_executor=lambda *_args, **_kwargs: "fallback",
            direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
            approval_ticket_consumer=consume_ticket,
            approval_ticket_completer=complete_ticket,
        )

        result = await service.execute_approved(
            approval_id=approval_id,
            expected_agent_id=agent_id,
            approved_by_user_id=approver_id,
        )
    finally:
        hook_registry.clear()

    assert error_code in str(result)
    assert registry.calls == []
    assert completions[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_execute_approved_records_failed_receipt_when_runtime_bootstrap_raises():
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.service import ToolRuntimeService

    approval_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    approved_by = uuid4()
    completions = []
    from app.tools.runtime import ToolExecutionContext

    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=approved_by,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/ws"),
    )

    async def consume_ticket(**_kwargs):
        arguments = {"path": "workspace/notes.md", "content": "done"}
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=approved_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="decision-failed-1",
            **_approved_ticket_runtime_fields(context, tool_name="write_file", arguments=arguments),
        )

    async def complete_ticket(**kwargs):
        completions.append(kwargs)

    class CrashingRuntimeResolver:
        async def resolve(self, **_kwargs):
            raise RuntimeError("runtime bootstrap unavailable")

    service = ToolRuntimeService(
        runtime_resolver=CrashingRuntimeResolver(),
        governance_resolver=SimpleNamespace(),
        registry=_FakeRegistry("unused"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
    )

    with pytest.raises(RuntimeError, match="runtime bootstrap unavailable"):
        await service.execute_approved(
            approval_id=approval_id,
            expected_agent_id=agent_id,
            approved_by_user_id=approved_by,
        )

    assert completions[0]["status"] == "failed"
    assert completions[0]["receipt"]["error_class"] == "RuntimeError"
    assert completions[0]["receipt"]["idempotency_key"] == f"approval:{approval_id}"
    assert completions[0]["receipt"]["decision_id"] == "decision-failed-1"


@pytest.mark.asyncio
async def test_tool_runtime_service_execute_approved_logs_readonly_tools():
    from app.services.approval_ticket import ApprovalExecutionTicket
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    approved_by = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=approved_by,
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    logged = []
    approval_id = uuid4()

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    async def consume_ticket(**_kwargs):
        arguments = {"path": "workspace/notes.md"}
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            agent_id=agent_id,
            requested_by_user_id=approved_by,
            approved_by_user_id=approved_by,
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="decision-readonly-1",
            **_approved_ticket_runtime_fields(context, tool_name="read_file", arguments=arguments),
        )

    async def complete_ticket(**_kwargs):
        return None

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry("read result"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
        approval_ticket_consumer=consume_ticket,
        approval_ticket_completer=complete_ticket,
    )

    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result == "read result"
    assert logged
    assert logged[0][0][1] == "tool_call_approved"
    assert logged[0][1]["tenant_id"] == context.tenant_id
    assert logged[0][1]["detail"]["tool"] == "read_file"


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_tool_runtime_service_preflight_asks_before_external_visible_tool():
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    traces = InMemoryDecisionTraceStore()
    approval_id = uuid4()
    approval_requests = []
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_feishu_message",
        arguments={"message": "Send external vendor reply about pricing"},
        execution_envelope={"schema": "hive.approval_execution_envelope.v1"},
    )

    async def request_approval(**kwargs):
        approval_requests.append(kwargs)
        return {"allowed": False, "approval_id": str(approval_id)}

    async def resolve_security_zone(_agent_id):
        return "standard"

    async def check_capability(_tenant_id, _agent_id, _tool_name):
        return SimpleNamespace(allowed=True)

    async def write_audit_event(**_kwargs):
        return None

    dependencies = GovernanceDependencies(
        resolve_security_zone=resolve_security_zone,
        check_capability=check_capability,
        write_audit_event=write_audit_event,
        request_approval=request_approval,
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, dependencies),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "Send external vendor reply about pricing"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    payload = json.loads(result)
    assert payload["status"] == "approval_required"
    assert payload["approval_id"] == str(approval_id)
    assert payload["tool_name"] == "send_feishu_message"
    assert registry.calls == []
    assert len(approval_requests) == 1
    assert approval_requests[0]["approval_origin_type"] == "action_preflight"
    assert approval_requests[0]["execution_envelope"] == governance_context.execution_envelope
    assert approval_requests[0]["decision_id"] == governance_context.decision_id
    decisions = traces.decisions()
    assert len(decisions) == 1
    assert decisions[0].chosen == "ask"
    assert decisions[0].tenant_id == "tenant-1"
    assert decisions[0].agent_id == str(context.agent_id)
    assert decisions[0].user_id == str(context.user_id)
    assert decisions[0].session_id == "session-1"
    assert decisions[0].tool_name == "send_feishu_message"
    assert decisions[0].preflight["decision"] == "ask"
    assert "checkpoint_id" not in decisions[0].preflight
    assert "evidence_refs" not in decisions[0].preflight


@pytest.mark.asyncio
async def test_tool_runtime_service_executes_external_effect_under_owner_full_authority():
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        build_owner_action_policy,
    )
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    tenant_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=uuid4(),
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/owner-full-authority"),
        owner_action_policy=build_owner_action_policy(
            agent_id=agent_id,
            tenant_id=tenant_id,
            actions={
                ACTION_EXTERNAL_EFFECT: CharterZone.FULL_AUTHORITY,
                ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
                ACTION_LOCAL_WRITE: CharterZone.FULL_AUTHORITY,
            },
            version=3,
            content_hash="owner-policy-v3",
        ),
    )
    registry = _FakeRegistry("SENT")
    trace = {}
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "Send the owner-authorized external update."},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace,
    )

    assert result == "SENT"
    assert len(registry.calls) == 1
    assert trace["preflight"]["decision"] == "do"
    assert trace["preflight"]["owner_action_policy"] == {
        "schema": "hive.owner_action_policy.v1",
        "action_id": "tool.external_effect",
        "zone": "full_authority",
        "version": 3,
        "revision_id": None,
        "content_hash": "owner-policy-v3",
        "source": "runtime",
        "valid": True,
        "error_code": None,
    }


@pytest.mark.asyncio
async def test_tool_runtime_service_allows_delegated_user_feishu_message():
    from app.core.execution_context import ExecutionIdentity
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        execution_identity=ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=uuid4(),
            label="Rocky via web",
        ),
    )
    registry = _FakeRegistry("SENT")
    traces = InMemoryDecisionTraceStore()

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
    )

    result = await service.execute(
        "send_feishu_message",
        {
            "open_id": "ou_example",
            "message": "明天上午 9:00-9:30，常春藤办公室，讨论 Agent 新需求，有空吗？",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "SENT"
    assert len(registry.calls) == 1
    assert registry.calls[0].tool_name == "send_feishu_message"
    assert traces.decisions() == []


@pytest.mark.asyncio
async def test_tool_runtime_service_fails_typed_when_preflight_approval_ticket_cannot_be_created():
    from app.tools.decision import ToolDecisionOutcome
    from app.tools.governance import GovernanceDependencies, ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_feishu_message",
        arguments={"message": "Send external vendor reply about pricing"},
        execution_envelope={"schema": "hive.approval_execution_envelope.v1"},
    )

    async def request_approval(**_kwargs):
        return {"allowed": False, "message": "approval database unavailable"}

    dependencies = GovernanceDependencies(
        resolve_security_zone=lambda _agent_id: "standard",
        check_capability=lambda *_args: SimpleNamespace(allowed=True),
        write_audit_event=lambda **_kwargs: None,
        request_approval=request_approval,
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, dependencies),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "Send external vendor reply about pricing"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result.outcome == ToolDecisionOutcome.UNAVAILABLE
    assert result.reason_code == "approval_ticket_unavailable"
    assert result.retryable is True
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_executes_secret_shaped_fixture_without_binding():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    registry = _FakeRegistry("EXECUTED")
    traces = InMemoryDecisionTraceStore()

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
    )

    result = await service.execute(
        "write_file",
        {"path": "secrets.txt", "content": "api_key=sk-1234567890abcdefghijklmnop"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "EXECUTED"
    assert len(registry.calls) == 1
    assert traces.decisions() == []


@pytest.mark.asyncio
async def test_tool_runtime_service_refuses_exact_active_secret_before_hooks_or_governance(
    monkeypatch,
):
    from app.services.exact_secret_boundary import ExactSecretBoundary
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    active_secret = "sk-live-tool-secret-0123456789"
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        exact_secret_boundary=ExactSecretBoundary.from_pairs(
            (("tool-config://tenant-1/search/api_key", active_secret),)
        ),
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    traces = InMemoryDecisionTraceStore()
    trace_metadata: dict[str, object] = {}

    async def forbidden_hook(*_args, **_kwargs):
        raise AssertionError("exact unauthorized bytes must stop before hook disclosure")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", forbidden_hook)
    governance_resolver = _FakeGovernanceResolver(
        SimpleNamespace(),
        SimpleNamespace(),
    )
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
    )

    result = await service.execute(
        "write_file",
        {
            "path": "notes.txt",
            "content": f"prefix::{active_secret}::suffix",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace_metadata,
    )

    assert result.startswith("[Preflight:refuse]")
    assert "unauthorized_secret_bytes" in result
    assert registry.calls == []
    assert governance_resolver.context_calls == []
    assert active_secret not in json.dumps(trace_metadata, ensure_ascii=False)
    assert trace_metadata["secret_input_redaction"] == {
        "code": "exact_unauthorized_secret_bytes",
        "redacted_count": 1,
        "source_refs": ["tool-config://tenant-1/search/api_key"],
    }
    decisions = traces.decisions()
    assert len(decisions) == 1
    assert decisions[0].chosen == "refuse"
    assert decisions[0].sensitivity == "PL4_credential"


@pytest.mark.asyncio
async def test_tool_runtime_service_refuses_exact_secret_before_plan_mode_gate():
    from app.services.exact_secret_boundary import ExactSecretBoundary
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    active_secret = "sk-live-plan-secret-0123456789"
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        exact_secret_boundary=ExactSecretBoundary.from_pairs(
            (("tool-config://tenant-1/search/api_key", active_secret),)
        ),
    )

    class ForbiddenPlanGate:
        async def check(self, *_args, **_kwargs):
            raise AssertionError("exact unauthorized bytes must stop before plan authority")

    class SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(
            SimpleNamespace(),
            SimpleNamespace(),
        ),
        registry=_FakeRegistry("SHOULD_NOT_RUN"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=InMemoryDecisionTraceStore(),
        plan_mode_gate=ForbiddenPlanGate(),
        plan_mode_session_factory=SessionFactory(),
    )

    result = await service.execute(
        "set_trigger",
        {
            "name": "Daily brief",
            "type": "cron",
            "config": {
                "expr": "0 9 * * *",
                "payload": active_secret,
            },
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result.startswith("[Preflight:refuse]")
    assert "unauthorized_secret_bytes" in result


@pytest.mark.asyncio
async def test_tool_runtime_service_does_not_rescan_trusted_runtime_argument_injection(
    monkeypatch,
):
    from app.services.exact_secret_boundary import ExactSecretBoundary
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    active_secret = "runtime-owned-secret-0123456789"
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        exact_secret_boundary=ExactSecretBoundary.from_pairs((("tool-config://tenant-1/storage/path", active_secret),)),
    )
    registry = _FakeRegistry("EXECUTED")

    def inject_runtime_path(_tool_name, arguments, _runtime_context):
        return {**arguments, "path": active_secret}

    monkeypatch.setattr(
        "app.tools.service._inject_runtime_context_arguments",
        inject_runtime_path,
    )
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(
            SimpleNamespace(),
            SimpleNamespace(),
        ),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute(
        "read_file",
        {"path": "safe.txt"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        emit_runtime_hooks=False,
    )

    assert result == "EXECUTED"
    assert registry.calls[0].arguments["path"] == active_secret


@pytest.mark.asyncio
async def test_tool_runtime_service_redacts_exact_secret_from_structured_tool_result():
    from app.services.exact_secret_boundary import ExactSecretBoundary
    from app.tools.result_envelope import ToolContentEnvelope, ToolResultBlock
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    active_secret = "sk-live-tool-result-secret-0123456789"
    source_ref = "tool-config://tenant-1/search/api_key"
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        exact_secret_boundary=ExactSecretBoundary.from_pairs(((source_ref, active_secret),)),
    )
    registry = _FakeRegistry(
        ToolContentEnvelope(
            text=f"prefix::{active_secret}::suffix",
            blocks=(
                ToolResultBlock(
                    type="text",
                    text=f"block::{active_secret}",
                ),
            ),
            metadata={"nested": {"value": active_secret}},
        )
    )
    trace_metadata: dict[str, object] = {}
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(
            SimpleNamespace(),
            SimpleNamespace(),
        ),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute(
        "read_file",
        {"path": "safe.txt"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        emit_runtime_hooks=False,
        trace_metadata_sink=trace_metadata,
    )

    assert isinstance(result, ToolContentEnvelope)
    assert str(result) == "prefix::[REDACTED_SECRET]::suffix"
    assert result.blocks[0].text == "block::[REDACTED_SECRET]"
    assert result.metadata == {"nested": {"value": "[REDACTED_SECRET]"}}
    assert active_secret not in repr(result)
    assert trace_metadata["secret_egress_redaction"] == {
        "code": "exact_unauthorized_secret_bytes",
        "surfaces": {"tool_result": 3},
        "redacted_count": 3,
        "source_refs": [source_ref],
    }


@pytest.mark.asyncio
async def test_tool_runtime_service_redacts_exact_secret_from_executor_error():
    from app.services.exact_secret_boundary import ExactSecretBoundary
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    active_secret = "sk-live-tool-error-secret-0123456789"
    source_ref = "tool-config://tenant-1/search/api_key"
    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        exact_secret_boundary=ExactSecretBoundary.from_pairs(((source_ref, active_secret),)),
    )

    class RaisingRegistry:
        async def try_execute(self, _request):
            raise RuntimeError(f"provider rejected {active_secret}")

    trace_metadata: dict[str, object] = {}
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(
            SimpleNamespace(),
            SimpleNamespace(),
        ),
        registry=RaisingRegistry(),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=InMemoryDecisionTraceStore(),
    )

    result = await service.execute(
        "read_file",
        {"path": "safe.txt"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        emit_runtime_hooks=False,
        trace_metadata_sink=trace_metadata,
    )

    assert active_secret not in str(result)
    assert "[REDACTED_SECRET]" in str(result)
    assert trace_metadata["secret_egress_redaction"] == {
        "code": "exact_unauthorized_secret_bytes",
        "surfaces": {"tool_error": 1},
        "redacted_count": 1,
        "source_refs": [source_ref],
    }
    assert active_secret not in json.dumps(
        context.tool_execution_frames,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_tool_runtime_service_timeout_returns_structured_error():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    async def slow_execute(self, *_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", slow_execute)

    try:
        result = await service.execute(
            "web_search",
            {"query": "quota issue"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        monkeypatch.undo()

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "web_search"
    assert payload["error_class"] == "timeout"
    assert payload["retryable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "expected_min_timeout"),
    [
        ("spawn_subagent", 180.0),
        ("start_workflow", 180.0),
    ],
)
async def test_tool_runtime_service_long_running_tools_have_explicit_timeout(
    monkeypatch, tool_name, expected_min_timeout
):
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )
    seen = {}
    arguments = {"task": "long running"}
    if tool_name == "start_workflow":
        arguments = {"preview_id": str(uuid4())}

    async def fake_execute_with_context(self, *_args, **_kwargs):
        return "ok"

    async def fake_wait_for(awaitable, *, timeout):
        seen["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", fake_execute_with_context)
    monkeypatch.setattr("app.tools.service.asyncio.wait_for", fake_wait_for)

    result = await service.execute(
        tool_name,
        arguments,
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "ok"
    assert seen["timeout"] >= expected_min_timeout


@pytest.mark.asyncio
async def test_tool_runtime_service_exception_returns_structured_error():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    full_error = "invalid upstream payload " + ("E" * 700) + " END_OF_UPSTREAM_ERROR"

    async def broken_execute(self, *_args, **_kwargs):
        raise ValueError(full_error)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", broken_execute)

    try:
        result = await service.execute(
            "firecrawl_fetch",
            {"url": "https://example.com"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        monkeypatch.undo()

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "firecrawl_fetch"
    assert payload["error_class"] == "tool_execution_error"
    assert payload["retryable"] is False
    assert payload["provider"] == "runtime"
    assert full_error in payload["message"]


@pytest.mark.asyncio
async def test_tool_runtime_service_logs_structured_tool_errors():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    async def fake_fallback(*_args, **_kwargs):
        return (
            "❌ sample failure\n\n"
            '<tool_error>{"ok": false, "tool_name": "web_search", "error_class": "quota_or_billing", '
            '"message": "quota hit", "provider": "exa", "http_status": 402, "retryable": false}</tool_error>'
        )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=fake_fallback,
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "web_search",
        {"query": "quota issue"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert "<tool_error>" in result
    assert any(args[1] == "error" for args, _kwargs in logged)


@pytest.mark.asyncio
async def test_interactive_plan_mode_blocks_non_readonly_tools_before_gate():
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.service import ToolRuntimeService

    class _Resolver:
        async def resolve(self, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("runtime context should not be resolved for blocked tools")

    class _GovernanceResolver:
        async def build_context(self, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("governance should not run for blocked tools")

        def build_dependencies(self):  # pragma: no cover - must not be reached
            raise AssertionError("governance dependencies should not be built")

    service = ToolRuntimeService(
        runtime_resolver=_Resolver(),
        governance_resolver=_GovernanceResolver(),
        registry=_FakeRegistry("should not run"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "should not run",
        direct_fallback_executor=lambda *_args, **_kwargs: "should not run",
        plan_mode_gate=SimpleNamespace(check=lambda *_args, **_kwargs: None),
        plan_mode_session_factory=lambda: None,
        plan_mode_service=SimpleNamespace(),
    )

    token = set_interactive_plan_mode({"original_request": "plan first"})
    try:
        result = await service.execute("write_file", {"path": "x", "content": "y"}, agent_id=uuid4(), user_id=uuid4())
    finally:
        reset_interactive_plan_mode(token)

    assert "plan_mode_readonly_violation" in result
    assert "exit_plan_mode" in result


@pytest.mark.asyncio
async def test_interactive_plan_mode_allows_write_only_to_exact_plan_file():
    # Phase 4B: the gate reads plan_file_path off the ContextVar mirror and lets
    # writes hit only that exact file — other paths and delete stay blocked.
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.service import ToolRuntimeService

    pf = "workspace/plans/s1.plan.md"
    token = set_interactive_plan_mode({"original_request": "plan", "plan_file_path": pf})
    try:
        # Exact plan-file write → allowed (gate returns None).
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("write_file", {"path": pf}) is None
        # Any other workspace path → blocked.
        other = ToolRuntimeService._interactive_plan_mode_readonly_block("write_file", {"path": "soul.md"})
        assert other is not None and "plan_mode_readonly_violation" in other
        # Delete on the plan file → blocked (iron law ③).
        deleted = ToolRuntimeService._interactive_plan_mode_readonly_block("fs_write", {"path": pf, "mode": "delete"})
        assert deleted is not None and "plan_mode_readonly_violation" in deleted
    finally:
        reset_interactive_plan_mode(token)


def test_interactive_plan_mode_allows_only_narrow_readonly_subagent_lane():
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.service import ToolRuntimeService

    token = set_interactive_plan_mode({"original_request": "plan"})
    try:
        assert (
            ToolRuntimeService._interactive_plan_mode_readonly_block(
                "spawn_subagent",
                {"task": "inspect relevant files", "type": "explorer"},
            )
            is None
        )
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("preview_workflow", {"definition": {}}) is None
        assert (
            ToolRuntimeService._interactive_plan_mode_readonly_block("propose_dynamic_workflow", {"goal": "audit"})
            is None
        )
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("check_subagent", {}) is None
        start = ToolRuntimeService._interactive_plan_mode_readonly_block("start_workflow", {"definition": {}})
        assert start is not None and "plan_mode_readonly_violation" in start

        worker = ToolRuntimeService._interactive_plan_mode_readonly_block(
            "spawn_subagent",
            {"task": "make the change", "type": "worker"},
        )
        assert worker is not None and "plan_mode_readonly_violation" in worker

        background = ToolRuntimeService._interactive_plan_mode_readonly_block(
            "spawn_subagent",
            {"task": "inspect later", "run_in_background": True},
        )
        assert background is not None and "plan_mode_readonly_violation" in background
    finally:
        reset_interactive_plan_mode(token)


# ── Confirmation gate: tool intercept never auto-enters Plan Mode ──


def test_redact_args_drops_handshake_keys_and_masks_secrets():
    from app.tools.service import _redact_args

    out = _redact_args(
        {
            "cron": "0 9 * * *",
            "api_key": "sk-xxx",
            "confirmed_plan_id": "p1",
            "webhook_token": "t",
        }
    )
    assert out == {"cron": "0 9 * * *", "api_key": "[redacted]", "webhook_token": "[redacted]"}


def test_confirmation_required_payload_strips_legacy_plan_activation_signal():
    from app.tools.service import _confirmation_required_payload

    payload = {
        "status": "needs_plan",
        "activate_interactive_plan": True,
        "interactive_plan_seed": {"action_kind": "create_enabled_trigger"},
        "summary": "needs a plan",
    }
    out = _confirmation_required_payload(payload)

    assert out["status"] == "requires_confirmation"
    assert out["requires_confirmation"] is True
    assert "activate_interactive_plan" not in out
    assert "interactive_plan_seed" not in out
    assert payload["status"] == "needs_plan"


@pytest.mark.asyncio
async def test_live_intercept_returns_confirmation_required_without_plan_mode_activation():
    """A live source whose gated tool is blocked returns a confirmation block.

    It must not attach a Plan Mode activation signal; Plan Mode entry is explicit
    user/UI state only.
    """
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        tool_name="set_trigger",
        arguments={"name": "Daily brief"},
    )

    class _PlanGate:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(
                needs_plan=True,
                needs_plan_payload={
                    "status": "needs_plan",
                    "summary": "Scheduled autonomous work needs a plan.",
                    "next_action": "enter_plan_mode",
                },
            )

    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class _FailingPlanService:
        def __init__(self):
            self.calls = 0

        async def ensure_awaiting_plan(self, **_kwargs):
            self.calls += 1
            raise AssertionError("live interactive intercept should not materialise an RPC plan")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    plan_service = _FailingPlanService()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        plan_mode_gate=_PlanGate(),
        plan_mode_session_factory=_SessionFactory(),
        plan_mode_service=plan_service,
    )

    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}},
        agent_id=agent_id,
        user_id=user_id,
        plan_mode_interactive_available=True,
    )
    payload = json.loads(result)

    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload
    assert "interactive_plan_seed" not in payload
    assert "plan_id" not in payload
    assert plan_service.calls == 0
    assert registry.calls == []


@pytest.mark.asyncio
async def test_unattended_intercept_returns_confirmation_required_without_plan_mode_activation():
    """An unattended source must also fail closed without creating Plan Mode.

    Background runs cannot self-author their way into Plan Mode when blocked.
    """
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(agent_id=agent_id, user_id=user_id, tenant_id="tenant-1", workspace=Path("/tmp/ws"))
    governance_context = ToolGovernanceContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        tool_name="set_trigger",
        arguments={"name": "Daily brief"},
    )

    class _PlanGate:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(
                needs_plan=True,
                needs_plan_payload={
                    "status": "needs_plan",
                    "summary": "Scheduled autonomous work needs a plan.",
                    "next_action": "enter_plan_mode",
                },
            )

    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class _FailingPlanService:
        def __init__(self):
            self.calls = 0

        async def ensure_awaiting_plan(self, **_kwargs):
            self.calls += 1
            raise AssertionError("unattended intercept should defer to main-loop Plan Mode, not the RPC planner")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    plan_service = _FailingPlanService()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        plan_mode_gate=_PlanGate(),
        plan_mode_session_factory=_SessionFactory(),
        plan_mode_service=plan_service,
    )

    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}},
        agent_id=agent_id,
        user_id=user_id,
        plan_mode_interactive_available=False,
        plan_mode_unattended_available=True,
    )
    payload = json.loads(result)

    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload
    assert "interactive_plan_seed" not in payload
    assert "plan_id" not in payload
    assert plan_service.calls == 0
    assert registry.calls == []


@pytest.mark.asyncio
async def test_non_eligible_source_intercept_returns_confirmation_required_fail_closed():
    """A non-eligible source gets the same static confirmation block.

    The key invariant is source-independent: no tool intercept may enter Plan
    Mode for the user.
    """
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(agent_id=agent_id, user_id=user_id, tenant_id="tenant-1", workspace=Path("/tmp/ws"))
    governance_context = ToolGovernanceContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        tool_name="set_trigger",
        arguments={"name": "Daily brief"},
    )

    class _PlanGate:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(
                needs_plan=True,
                needs_plan_payload={"status": "needs_plan", "summary": "needs plan", "next_action": "enter_plan_mode"},
            )

    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        plan_mode_gate=_PlanGate(),
        plan_mode_session_factory=_SessionFactory(),
    )

    # Neither availability flag set → non-eligible source.
    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}},
        agent_id=agent_id,
        user_id=user_id,
        plan_mode_interactive_available=False,
        plan_mode_unattended_available=False,
    )
    payload = json.loads(result)

    assert payload["status"] == "requires_confirmation"  # blocked
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload  # agent does NOT plan
    assert "plan_id" not in payload  # nothing materialised — no RPC fallback
    assert registry.calls == []  # tool did NOT execute (fail-closed)
