"""Approved execution keeps exact failure and unknown receipts truthful."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.approval_ticket import (
    ApprovalExecutionTicket,
    build_approval_execution_envelope,
    hash_approval_execution_envelope,
    hash_tool_input,
)
from app.tools.runtime import ToolExecutionContext
from app.tools.service import ToolRuntimeService


@pytest.mark.asyncio
@pytest.mark.parametrize(('result', 'frame_status', 'expected'), [
    ('{"ok":false,"error":"operation_failed"}', 'failed', 'failed'),
    ('{"status":"failed"}', 'failed', 'failed'),
    ('{"status":"blocked"}', 'blocked', 'failed'),
    ('{"status":"rejected"}', 'blocked', 'failed'),
    ('{"ok":true,"message":"failed is a quoted word"}', 'completed', 'succeeded'),
    ('A previous operation failed; this draft explains it.', 'completed', 'succeeded'),
    ('{"ok":false,"error":"connection_lost"}', 'unknown', 'needs_reconciliation'),
    ('{"ok":true}', 'unknown', 'needs_reconciliation'),
])
async def test_approved_execution_preserves_machine_outcome(result, frame_status, expected):
    tenant_id, agent_id, requester_id, approver_id, approval_id = (uuid4() for _ in range(5))
    context = ToolExecutionContext(agent_id=agent_id, user_id=requester_id,
        tenant_id=str(tenant_id), workspace=Path('/synthetic/workspace'))
    envelope = build_approval_execution_envelope(context=context,
        tool_call_id='approved-outcome-test', emit_runtime_hooks=True)
    arguments = {'path': 'workspace/report.xlsx'}
    ticket = ApprovalExecutionTicket(approval_id=approval_id, tenant_id=tenant_id, agent_id=agent_id,
        requested_by_user_id=requester_id, approved_by_user_id=approver_id,
        tool_name='office_document_create', arguments=arguments,
        action_type='test.office_document_create', input_hash=hash_tool_input('office_document_create', arguments),
        policy_snapshot_hash='policy', idempotency_key=f'approval:{approval_id}', decision_id='decision-1',
        execution_envelope=envelope, execution_envelope_hash=hash_approval_execution_envelope(envelope))
    completions = []
    calls = []

    async def consume(**kwargs):
        assert kwargs == {'approval_id': approval_id, 'expected_agent_id': agent_id,
            'expected_user_id': approver_id}
        return ticket

    async def execute(tool_name, tool_arguments, **kwargs):
        calls.append((tool_name, tool_arguments, kwargs))
        kwargs['trace_metadata_sink']['tool_execution_frame'] = {'status': frame_status}
        return result

    async def complete(**kwargs):
        completions.append(kwargs)

    service = SimpleNamespace(approval_ticket_consumer=consume, approval_ticket_completer=complete, execute=execute)
    assert await ToolRuntimeService.execute_approved(service, approval_id=approval_id,
        expected_agent_id=agent_id, approved_by_user_id=approver_id) == result
    assert len(calls) == len(completions) == 1
    assert calls[0][0:2] == ('office_document_create', arguments)
    assert calls[0][2]['user_id'] == requester_id
    completion, = completions
    assert completion['status'] == expected
    assert completion['result'] == result
    assert completion['receipt']['status'] == expected
    assert completion['receipt']['runtime_evidence']['tool_execution_frame'] == {'status': frame_status}
    assert completion['receipt']['decision_id'] == ticket.decision_id
    if expected == 'needs_reconciliation':
        assert completion['receipt']['side_effect_state'] == 'unknown'
        assert completion['receipt']['automatic_replay'] is False
    elif expected == 'failed':
        assert json.loads(completion['result']).get('ok') is False or json.loads(result)['status'] in {
            'failed', 'blocked', 'rejected'}
