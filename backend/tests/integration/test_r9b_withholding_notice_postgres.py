"""Replacement constants survive dictionary compilation and inherited-source withholding."""

import json

import pytest

from app.database import pin_rls_tenant_context
from app.kernel.engine import _dicts_to_llm_messages
from app.runtime.context_candidates import ContextMessageOrigin
from app.services.session_model_inputs import message_snapshot
from app.services.session_model_round import _withhold_model_source, prepare_model_request
from tests.integration.test_a4c_one_item_withheld import _bindings
from tests.integration.test_r7e_workspace_kernel_inputs_postgres import (
    _create, _tool_round, native_workspace, read_policy,  # noqa: F401
)


@pytest.mark.parametrize("variant", ["dictionary", "skill_catalog", "inherited"])
async def test_platform_notice_is_bound_after_dictionary_or_inherited_source_replacement(
    native_workspace, app_user_sessionmaker, variant,
):
    from app.models.session_v2 import SessionModelResult
    from sqlalchemy import select

    n = native_workspace
    await _create(n, "workspace/report.md", "LOCAL_SEARCH")
    await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "LOCAL_SEARCH", "root": "workspace"})
    source = {"source_kind": "explicit_memory", "source_id": "unavailable", "source_version": "v1"}
    messages = [] if variant == "inherited" else [{"role": "system", "content": "UNPROVEN_SOURCE_BYTES"}]
    if variant == "skill_catalog":
        messages[0]["context_origin"] = ContextMessageOrigin.capture(
            kind="dynamic_prompt", role="system", content=messages[0]["content"], sections=[{"name": "skill_catalog"}])
    wire = {"messages": [message_snapshot(message) for message in messages]}
    bindings = _bindings(len(messages), {0: [source]} if messages else {})
    bindings["dependencies"] = [source]
    _withhold_model_source(messages, wire, bindings, source, "content_source_changed")
    if variant != "inherited":
        assert bindings["messages"][0]["status"] == "withheld"
        messages = _dicts_to_llm_messages(messages)
    expected = ("技能目录内容暂时无法核实，已略过；请使用其余内容继续，需要时向用户询问。" if variant == "skill_catalog"
        else "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。")
    assert messages[0].content == expected
    assert isinstance(messages[0].context_origin, ContextMessageOrigin)
    assert messages[0].context_origin.sources == () and messages[0].context_origin.unbound == ()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=2, messages=messages,
            tools=[], provider="openai", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = row.model_request_snapshot_json
        entry = snapshot["input_bindings"]["messages"][0]
        assert entry["status"] == "bound" and entry["dependency_status"] == "resolved", entry
        assert entry["dependencies"] == []
        assert snapshot["wire_request"]["messages"] == [{"role": "system", "content": expected}]
        assert "UNPROVEN_SOURCE_BYTES" not in json.dumps(snapshot)
        await db.commit()
