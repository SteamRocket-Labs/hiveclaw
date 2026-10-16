"""A2A arguments cannot replace the trusted runtime's permission profile."""

from pathlib import Path
from dataclasses import replace
from uuid import uuid4

import pytest

from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1, permission_profile_snapshot
from app.tools.runtime import ToolExecutionContext
from app.tools.service import _inject_runtime_context_arguments
from app.services.approval_ticket import ApprovalDecisionSet, hash_tool_input


@pytest.mark.parametrize("tool_name", ["delegate_to_agent", "send_message_to_agent"])
@pytest.mark.parametrize("has_profile", [True, False])
def test_model_profile_never_enlarges_runtime_authority(tool_name, has_profile):
    profile = PermissionProfileV1(mode=PermissionMode.DONT_ASK, allowed_tools=("read_file",)) if has_profile else None
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()),
                                   workspace=Path("/synthetic/workspace"), permission_profile=profile)
    arguments = {"message": "A legitimate task", "_permission_profile": {
        "mode": "bypassPermissions", "allowed_tools": ["send_feishu_message"],
    }}
    result = _inject_runtime_context_arguments(tool_name, arguments, context)
    if has_profile:
        assert result["_permission_profile"] == permission_profile_snapshot(profile)
    else:
        assert "_permission_profile" not in result


@pytest.mark.parametrize("tool_name", ["delegate_to_agent", "send_message_to_agent"])
def test_exact_session_replay_retains_its_hash_bound_inherited_profile(tool_name):
    profile = PermissionProfileV1(mode=PermissionMode.DEFAULT)
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()),
                                   workspace=Path("/synthetic/workspace"), permission_profile=profile)
    canonical = _inject_runtime_context_arguments(tool_name, {"message": "Recorded task"}, context)
    input_hash = hash_tool_input(tool_name, canonical)
    approved = replace(profile, session_grant_scope="once", session_grant_tool_name=tool_name,
                       session_grant_input_hash=input_hash)
    resumed = _inject_runtime_context_arguments(tool_name, canonical, replace(context, permission_profile=approved))
    assert resumed["_permission_profile"] == permission_profile_snapshot(profile)
    assert hash_tool_input(tool_name, resumed) == input_hash


@pytest.mark.parametrize("tool_name", ["delegate_to_agent", "send_message_to_agent"])
def test_consumed_ticket_retains_an_absent_inherited_profile(tool_name):
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()),
                                   workspace=Path("/synthetic/workspace"))
    canonical = _inject_runtime_context_arguments(tool_name, {"message": "Recorded task"}, context)
    input_hash = hash_tool_input(tool_name, canonical)
    decision = ApprovalDecisionSet(approval_id=uuid4(), tenant_id=context.tenant_id, action_type=tool_name,
                                   tool_name=tool_name, input_hash=input_hash, policy_snapshot_hash="a" * 64,
                                   envelope_hash="b" * 64, decision_id="recorded-decision",
                                   requested_by_user_id=context.user_id, approved_by_user_id=uuid4())
    restored = replace(context, permission_profile=PermissionProfileV1(), approval_decision=decision)
    resumed = _inject_runtime_context_arguments(tool_name, canonical, restored)
    assert "_permission_profile" not in resumed
    assert hash_tool_input(tool_name, resumed) == input_hash


@pytest.mark.parametrize("gate_name", ["_session_no_policy_action", "_session_explicit_policy_action"])
@pytest.mark.parametrize("tool_name", ["delegate_to_agent", "send_message_to_agent"])
def test_local_permission_gate_consumes_only_the_exact_granted_task(gate_name, tool_name):
    from app.tools import governance

    arguments = {"message": "Recorded task"}
    profile = PermissionProfileV1(session_grant_scope="once", session_grant_tool_name=tool_name,
                                  session_grant_input_hash=hash_tool_input(tool_name, arguments))
    context = governance.ToolGovernanceContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()),
                                              tool_name=tool_name, arguments=arguments, permission_profile=profile)
    gate = getattr(governance, gate_name)
    assert gate(context) == "allow"
    assert gate(replace(context, arguments={"message": "A different task"})) == "ask"
