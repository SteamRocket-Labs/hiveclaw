from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.execution_context import ExecutionPrincipal
from app.services.runtime_task_authority import (
    RuntimeTaskRequesterUnavailable,
    authorize_runtime_task_record,
    execution_principal_from_tool_context,
    runtime_task_requester_user_id,
)


def _record(*, tenant_id, agent_id, user_id, session_id):
    return {
        "task_id": uuid4().hex,
        "task_type": "subagent",
        "tenant_id": str(tenant_id),
        "parent_agent_id": str(agent_id),
        "root_user_id": str(user_id),
        "root_session_id": str(session_id),
        "delegation_chain": [f"agent:{agent_id}", "subagent:scout"],
        "status": "running",
        "metadata": {},
    }


def _principal(*, tenant_id, agent_id, user_id, session_id):
    return ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=agent_id,
        requester_user_id=user_id,
        root_session_id=str(session_id),
        root_runtime_task_id="root-task-1",
        origin="agent_tool",
    )


def test_runtime_task_authority_binds_user_session_agent_and_tenant() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    record = _record(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id, session_id=session_id)

    allowed = authorize_runtime_task_record(
        record,
        principal=_principal(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        ),
        action="cancel",
    )
    assert allowed.allowed is True
    assert allowed.authority_source == "root_owner"
    assert allowed.evidence["root_user_id"] == str(user_id)
    assert allowed.evidence["root_session_id"] == str(session_id)

    wrong_user = authorize_runtime_task_record(
        record,
        principal=_principal(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=uuid4(),
            session_id=session_id,
        ),
        action="cancel",
    )
    assert wrong_user.allowed is False
    assert wrong_user.reason == "root_user_mismatch"

    wrong_session = authorize_runtime_task_record(
        record,
        principal=_principal(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            session_id=uuid4(),
        ),
        action="cancel",
    )
    assert wrong_session.allowed is False
    assert wrong_session.reason == "root_session_mismatch"


def test_tool_context_preserves_root_execution_principal_across_a2a_hops() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=agent_id,
        requester_user_id=user_id,
        root_session_id="root-session",
        root_runtime_task_id="root-task",
        origin="a2a_delegation",
        delegation_chain=("agent:parent", f"agent:{agent_id}"),
    )
    context = SimpleNamespace(
        tenant_id=str(tenant_id),
        agent_id=agent_id,
        user_id=user_id,
        session_id="child-session",
        runtime_task_id="child-task",
        execution_principal=principal,
        authority_frame_required=True,
    )

    resolved = execution_principal_from_tool_context(context)

    assert resolved is principal
    assert resolved.root_session_id == "root-session"
    assert resolved.root_runtime_task_id == "root-task"
    assert resolved.delegation_chain == ("agent:parent", f"agent:{agent_id}")


def test_tool_context_rejects_mismatched_a2a_execution_principal() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    context = SimpleNamespace(
        tenant_id=str(tenant_id),
        agent_id=agent_id,
        user_id=uuid4(),
        session_id="child-session",
        runtime_task_id="child-task",
        execution_principal=ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=agent_id,
            requester_user_id=uuid4(),
            root_session_id="root-session",
        ),
        authority_frame_required=True,
    )

    with pytest.raises(ValueError, match="requester"):
        execution_principal_from_tool_context(context)


def test_runtime_task_authority_fails_closed_without_root_evidence() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    record = {
        "task_id": uuid4().hex,
        "task_type": "subagent",
        "tenant_id": str(tenant_id),
        "parent_agent_id": str(agent_id),
        "status": "running",
        "metadata": {},
    }
    decision = authorize_runtime_task_record(
        record,
        principal=_principal(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        ),
        action="read",
    )
    assert decision.allowed is False
    assert decision.reason == "root_authority_missing"


def test_runtime_task_requester_uses_only_canonical_root_user_column() -> None:
    requester_id = uuid4()
    record = {
        "root_user_id": str(requester_id),
        "metadata": {
            "root_user_id": str(requester_id),
            "execution_principal": {"requester_user_id": str(requester_id)},
        },
    }

    assert runtime_task_requester_user_id(record) == requester_id


def test_runtime_task_requester_rejects_metadata_fallback_and_conflicts() -> None:
    metadata_requester = uuid4()
    with pytest.raises(RuntimeTaskRequesterUnavailable) as missing:
        runtime_task_requester_user_id(
            {
                "metadata": {
                    "root_user_id": str(metadata_requester),
                    "execution_principal": {"requester_user_id": str(metadata_requester)},
                }
            }
        )
    assert missing.value.reason_code == "root_user_id_missing"

    canonical_requester = uuid4()
    with pytest.raises(RuntimeTaskRequesterUnavailable) as conflict:
        runtime_task_requester_user_id(
            {
                "root_user_id": str(canonical_requester),
                "metadata": {"root_user_id": str(metadata_requester)},
            }
        )
    assert conflict.value.reason_code == "root_user_id_mismatch"


def test_manager_override_is_explicit_reasoned_and_never_implicit() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    owner_id = uuid4()
    manager_id = uuid4()
    session_id = uuid4()
    record = _record(tenant_id=tenant_id, agent_id=agent_id, user_id=owner_id, session_id=session_id)
    principal = _principal(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=manager_id,
        session_id=session_id,
    )

    implicit = authorize_runtime_task_record(record, principal=principal, action="read")
    assert implicit.allowed is False

    missing_reason = authorize_runtime_task_record(
        record,
        principal=principal,
        action="read",
        allow_operator_override=True,
        operator_user_id=manager_id,
    )
    assert missing_reason.allowed is False
    assert missing_reason.reason == "operator_reason_required"

    explicit = authorize_runtime_task_record(
        record,
        principal=principal,
        action="read",
        allow_operator_override=True,
        operator_user_id=manager_id,
        operator_reason="incident investigation INC-42",
    )
    assert explicit.allowed is True
    assert explicit.authority_source == "operator_override"
    assert explicit.evidence["operator_reason"] == "incident investigation INC-42"


def test_tool_context_builds_root_control_principal() -> None:
    context = SimpleNamespace(
        tenant_id=str(uuid4()),
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id=str(uuid4()),
        runtime_task_id=str(uuid4()),
    )
    principal = execution_principal_from_tool_context(context)
    assert principal.tenant_id == context.tenant_id
    assert principal.source_agent_id == context.agent_id
    assert principal.requester_user_id == context.user_id
    assert principal.root_session_id == context.session_id
    assert principal.root_runtime_task_id == context.runtime_task_id
