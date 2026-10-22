"""Status receipts preserve the exact trusted requester without granting the target."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.execution_context import ExecutionIdentity
from app.services.connector_acl import (
    connector_item_visible, extract_connector_source_items, filter_connector_payload_for_prompt,
    source_items_from_tool_call,
)
from app.tools.decision import ToolBoundaryBlock, ToolDecisionOutcome
from app.tools.execution_pipeline import _approval_handoff_result
from app.tools.runtime import ToolExecutionContext


@pytest.mark.parametrize('mode', ['web', 'bound', 'bound_explicit', 'machine', 'wrong_reader', 'wrong_context', 'bytes'])
def test_pipeline_approval_status_is_bound_to_its_actual_execution_identity(mode):
    tenant_id, user_id, agent_id, principal_id, approval_id = (uuid4() for _ in range(5))
    machine = mode == 'machine'
    text = json.dumps({'status': 'approval_required', 'approval_id': str(approval_id), 'tool_name': 'office_document_create'})
    block = ToolBoundaryBlock(text, outcome=ToolDecisionOutcome.REQUIRE_APPROVAL,
        reason_code='company_policy', status='approval_required', retryable=False)
    context = ToolExecutionContext(agent_id=agent_id, user_id=None if machine else user_id,
        tenant_id=str(tenant_id), workspace=Path('/synthetic/workspace'),
        external_principal_id=principal_id if machine or mode == 'bound_explicit' else uuid4() if mode == 'wrong_context' else None,
        execution_identity=ExecutionIdentity(identity_type='delegated_user' if mode == 'web' else
            'external_principal' if machine else 'external_principal_bound',
            identity_id=user_id if mode == 'web' else principal_id, label='Synthetic requester'))
    state = SimpleNamespace(governance_context=SimpleNamespace(approval_id=str(approval_id)), runtime_context=context,
        ports=SimpleNamespace(json=json),
        request=SimpleNamespace(tool_name='office_document_create', trace_metadata_sink={}),
        service=SimpleNamespace(_redact_runtime_egress=lambda value, **_: (value, None)))
    result = _approval_handoff_result(state, block)
    target, = source_items_from_tool_call('office_document_create', {'path': 'workspace/pending.xlsx'})
    items = [*extract_connector_source_items(result), target]
    if mode == 'bytes':
        result = replace(result, text=text + ' extra content')
    reader_principal = None if mode == 'web' else uuid4() if mode == 'wrong_reader' else principal_id
    filtered = filter_connector_payload_for_prompt(result, source_items=items, tenant_id=tenant_id,
        current_user_id=None if machine else user_id, agent_id=agent_id, external_principal_id=reader_principal)
    assert target['source'] in filtered.forbidden_sources
    assert not connector_item_visible(target, tenant_id=tenant_id, current_user_id=user_id, agent_id=agent_id,
        external_principal_id=principal_id)
    if mode in {'wrong_reader', 'wrong_context', 'bytes'}:
        assert json.loads(str(filtered.payload))['status'] == 'source_permission_denied'
    else:
        assert str(filtered.payload) == text
        assert filtered.allowed_sources == (f'approval://{approval_id}',)
