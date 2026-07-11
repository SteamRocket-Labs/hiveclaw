from __future__ import annotations

from pathlib import Path
import time
import uuid

import pytest


def test_approval_execution_envelope_round_trips_every_runtime_authority() -> None:
    from app.agents.delegation_token import DelegationToken
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1, SandboxProfile
    from app.services.approval_ticket import (
        build_approval_execution_envelope,
        hash_approval_execution_envelope,
        restore_approval_execution_envelope,
    )
    from app.tools.runtime import ToolExecutionContext

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/hive-approved-workspace"),
        session_id="session-42",
        permission_profile=PermissionProfileV1(
            mode=PermissionMode.ACCEPT_EDITS,
            readable_roots=("workspace/",),
            denied_reads=(".env",),
            sandbox=SandboxProfile.WORKSPACE_WRITE,
            allowed_tools=("write_file",),
        ),
        turn_id="turn-9",
        runtime_task_id=str(uuid.uuid4()),
        budget_run_id=str(uuid.uuid4()),
        origin_channel="web",
        round_state={"round": 3, "nested": {"ok": True}},
        t0_refs=("t0:event:1", "t0:event:2"),
    )
    context.delegation_token = DelegationToken(
        delegation_id="delegation-7",
        parent_agent_id=parent_id,
        child_agent_id=agent_id,
        issued_at=time.time(),
        expires_at=time.time() + 300,
        granted_capabilities=frozenset({"workspace.file.write"}),
        inherit_parent_capabilities=False,
    )

    envelope = build_approval_execution_envelope(
        context=context,
        tool_call_id="tool-call-7",
        emit_runtime_hooks=True,
        plan_mode_interactive_available=True,
        plan_mode_unattended_available=False,
    )
    envelope_hash = hash_approval_execution_envelope(envelope)
    restored = restore_approval_execution_envelope(envelope, expected_hash=envelope_hash)

    assert restored.tenant_id == tenant_id
    assert restored.agent_id == agent_id
    assert restored.requester_user_id == requester_id
    assert restored.session_id == "session-42"
    assert restored.tool_call_id == "tool-call-7"
    assert restored.turn_id == "turn-9"
    assert restored.runtime_task_id == context.runtime_task_id
    assert restored.budget_run_id == context.budget_run_id
    assert restored.origin_channel == "web"
    assert restored.workspace == Path("/tmp/hive-approved-workspace")
    assert restored.permission_profile.mode is PermissionMode.ACCEPT_EDITS
    assert restored.permission_profile.sandbox is SandboxProfile.WORKSPACE_WRITE
    assert restored.permission_profile.denied_reads == (".env",)
    assert restored.delegation_token is not None
    assert restored.delegation_token.parent_agent_id == parent_id
    assert restored.delegation_token.child_agent_id == agent_id
    assert restored.delegation_token.granted_capabilities == frozenset({"workspace.file.write"})
    assert restored.round_state == {"round": 3, "nested": {"ok": True}}
    assert restored.t0_refs == ("t0:event:1", "t0:event:2")
    assert restored.emit_runtime_hooks is True
    assert restored.plan_mode_interactive_available is True
    assert restored.plan_mode_unattended_available is False


def test_approval_execution_envelope_rejects_tamper_and_wrong_schema() -> None:
    from app.services.approval_ticket import (
        ApprovalTicketError,
        build_approval_execution_envelope,
        hash_approval_execution_envelope,
        restore_approval_execution_envelope,
    )
    from app.tools.runtime import ToolExecutionContext

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/hive-approved-workspace"),
    )
    envelope = build_approval_execution_envelope(
        context=context,
        tool_call_id="tool-call-8",
        emit_runtime_hooks=True,
    )
    envelope_hash = hash_approval_execution_envelope(envelope)

    tampered = {**envelope, "agent_id": str(uuid.uuid4())}
    with pytest.raises(ApprovalTicketError, match="envelope hash mismatch"):
        restore_approval_execution_envelope(tampered, expected_hash=envelope_hash)

    wrong_schema = {**envelope, "schema": "hive.approval_execution_envelope.v0"}
    with pytest.raises(ApprovalTicketError, match="unsupported approval execution envelope"):
        restore_approval_execution_envelope(
            wrong_schema,
            expected_hash=hash_approval_execution_envelope(wrong_schema),
        )


def test_approval_execution_envelope_requires_tenant_and_requester_authority() -> None:
    from app.services.approval_ticket import ApprovalTicketError, build_approval_execution_envelope
    from app.tools.runtime import ToolExecutionContext

    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=None,
        workspace=Path("/tmp/hive-approved-workspace"),
    )

    with pytest.raises(ApprovalTicketError, match="tenant authority"):
        build_approval_execution_envelope(
            context=context,
            tool_call_id="tool-call-9",
            emit_runtime_hooks=True,
        )
