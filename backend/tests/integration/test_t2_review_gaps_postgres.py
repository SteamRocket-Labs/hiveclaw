"""Round-two T2 authority, availability and live-writer regression evidence."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events
from app.memory.t2 import segment_package as t2
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from tests.integration.test_t2_source_revocation_postgres import (
    _disk_text, _model, native_workspace, read_turn,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _run(s, **kwargs):
    return await t2.run_t2_segment_package_job(agent_id=s.n.agent, tenant_id=s.n.tenant,
        session_id=s.n.session, t0_segment_id=kwargs.pop("segment", s.segment), **kwargs)


@pytest.mark.parametrize("read_turn", ["live_v1"], indirect=True)
async def test_live_web_bundle_keeps_bound_content_and_drops_v1_telemetry_and_notice(read_turn, monkeypatch):
    s = read_turn
    monkeypatch.setattr(t2, "_now", lambda: "2026-11-07T00:00:00+00:00")
    events = replay_t0_session_events(agent_id=s.n.agent, session_id=s.n.session)
    legacy = [e for e in events if e.content.startswith("LIVE_V1_")]
    assert len(legacy) == 11
    bound = [e for e in events if e not in legacy]
    assert all(e.event_type == "segment_boundary" or "transcript_event_id" in e.metadata
        or "v2_payload" in e.metadata for e in bound)
    expected = t2._build_source_bundle(root=Path(get_settings().AGENT_DATA_DIR), agent_id=s.n.agent,
        tenant_id=s.n.tenant, session_id=s.n.session, t0_segment_id=s.segment,
        package_id="live-characterization", session_events=bound)
    calls, _ = _model(monkeypatch)
    result = await _run(s, package_id="live-characterization")
    assert result.status == "committed", result.issues
    actual = json.loads((result.staging_dir / "source_bundle.json").read_text())
    assert actual == expected == calls[0]
    assert s.body in _disk_text(result)
    assert "LIVE_V1_" not in json.dumps(calls) + _disk_text(result)


@pytest.mark.parametrize("role", ["assistant", "tool", "user", "system"])
async def test_unbound_v1_content_including_owner_trigger_context_is_dropped(
    read_turn, owner_sessionmaker, monkeypatch, role,
):
    from app.services.chat_transcript import append_session_event
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0

    s, marker = read_turn, "UNBOUND_TRIGGER_CONTEXT_OR_V1_CONTENT"
    async with owner_sessionmaker() as db:
        row = await append_session_event(db=db, tenant_id=s.n.tenant, agent_id=s.n.agent,
            session_id=s.n.session, user_id=s.n.user, event_type=f"{role}_message", actor_type=role,
            role=role, content=marker, metadata={"source": "trigger"}, bridge_to_t0=False)
        await db.commit()
    assert await bridge_transcript_event_to_t0(transcript_event_id=row.event_id)
    event = replay_t0_session_events(agent_id=s.n.agent, session_id=s.n.session)[-1]
    calls, _ = _model(monkeypatch)
    result = await _run(s, segment=event.segment_id)
    assert marker not in json.dumps(calls) + _disk_text(result)
    assert result.status == "not_applicable"
    assert calls == []


@pytest.mark.parametrize("kind", ["tool_result", "hr_agent_created"])
async def test_unbound_t0_tool_and_hr_attribution_are_dropped(read_turn, monkeypatch, kind):
    s, marker = read_turn, "UNBOUND_T0_QUOTED_FILE"
    event = append_t0_session_event(agent_id=s.n.agent, tenant_id=s.n.tenant, session_id=s.n.session,
        event_type=kind, role="tool", content=marker if kind == "tool_result" else
        "Created digital employee from confirmed HR blueprint.",
        metadata={"source_attributions": [{"value_summary": marker}]})
    calls, _ = _model(monkeypatch)
    result = await _run(s, segment=event.segment_id)
    assert result.status == "not_applicable"
    assert marker not in json.dumps(calls) + _disk_text(result)
    assert calls == []
    assert not (result.staging_dir / "source_bundle.json").exists()


async def test_revoked_finding_never_reaches_model_or_bundle(read_turn, owner_sessionmaker, monkeypatch):
    from app.services.agent_work_ledger import append_agent_work_ledger_finding

    s = read_turn
    append_agent_work_ledger_finding(agent_id=s.n.agent, session_id=s.n.session,
        finding_type="finding", summary="Finding quotes " + s.body, trust="verified",
        source_refs=["workspace/shared-source.md"])
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, s.grant_id)).effect = "deny"
        await db.commit()
    calls, _ = _model(monkeypatch)
    result = await _run(s)
    assert result.status == "committed", result.issues
    assert s.body not in json.dumps(calls) + _disk_text(result)
    assert calls[0]["work_ledger"]["findings"] == []
    assert calls[0]["work_ledger"]["source_refs"] == []


async def test_trigger_reader_is_autonomous_and_revoked_read_is_dropped(
    read_turn, owner_sessionmaker, monkeypatch,
):
    from app.services import content_input_receipts as receipts

    s, seen, validate = read_turn, [], receipts._validate_input_dependencies
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, s.n.run)).task_type = "trigger"
        (await db.get(ResourcePermission, s.grant_id)).effect = "deny"
        await db.commit()

    async def check(db, tenant, dependencies, **kwargs):
        if kwargs.get("runtime_request") is not None:
            seen.append(kwargs["runtime_request"])
        return await validate(db, tenant, dependencies, **kwargs)

    monkeypatch.setattr(receipts, "_validate_input_dependencies", check)
    calls, _ = _model(monkeypatch)
    result = await _run(s)
    assert seen and all(p.purpose == "autonomous_agent" and p.autonomous
        and p.runtime_task_id == str(s.n.run) for p in seen)
    assert result.status == "committed", result.issues
    assert s.body not in json.dumps(calls) + _disk_text(result)


@pytest.mark.parametrize("mapping_present", [True, False])
async def test_delegated_answer_uses_persisted_parent_reader_or_is_dropped(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, mapping_present,  # noqa: F811
):
    from types import SimpleNamespace
    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.services import content_input_receipts as receipts
    from app.services.delegation_input_origin import capture_delegation_instruction_origin, load_delegation_instruction_origin
    from app.services.llm_client import LLMMessage
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.tools.runtime import ToolExecutionContext
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write
    from tests.integration.test_m1g30_model_dependencies_postgres import _snapshot
    from tests.services.test_session_semantic_history import _seal_round

    n = native_workspace
    child, session, run = (uuid.uuid4() for _ in range(3))
    instruction, answer = "Summarize the authorized request.", "DELEGATED_SOURCE_BOUND_ANSWER"
    async with owner_sessionmaker() as db:
        db.add(Agent(id=child, tenant_id=n.tenant, creator_id=n.user, name="Delegated reader"))
        await db.flush()
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=child, user_id=n.user,
            peer_agent_id=n.agent, source_channel="agent", session_kind="delegation_run",
            parent_session_id=n.session))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        messages = await _admitted_write_messages(db, n, content="Delegate the request summary.")
        call, arguments, _ = await _prepare_write(db, n, messages=messages, tool_name="delegate_to_agent",
            arguments={"agent_name": "Delegated reader", "target_agent_id": str(child), "message": instruction})
        call.effect_state = "effect_started"
        await db.commit()
    context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
        workspace=n.root, session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=call.provider_tool_use_id)
    pointer = await capture_delegation_instruction_origin(context, arguments, recipient_agent_id=child)
    assert pointer is not None
    conversation = [{"role": "user", "content": instruction, "a2a_instruction_origin": pointer}]
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=run, tenant_id=n.tenant, task_type="delegation", status="running",
            parent_agent_id=n.agent, child_agent_id=child, parent_session_id=str(n.session),
            child_session_id=str(session), root_user_id=n.user, prompt=instruction,
            metadata_json={"conversation_messages": conversation}))
        await db.flush()
        (await db.get(ChatSession, session)).runtime_task_id = run
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        origin = await load_delegation_instruction_origin(db, tenant_id=n.tenant, content=instruction,
            request=SimpleNamespace(conversation_messages=conversation, parent_agent_id=n.agent,
                parent_session_id=str(n.session), owner_id=n.user, target=SimpleNamespace(id=child)))
        assert origin is not None and not origin.unbound
        request_id, snapshot = await _snapshot(db, n.tenant, child, session, run,
            [LLMMessage(role="user", content=instruction, context_origin=origin)])
        assert snapshot["input_bindings"]["dependency_coverage"]["complete"]
        await _seal_round(db, tenant_id=n.tenant, agent_id=child, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=1, provider_request_id=request_id, content=answer)
        await db.commit()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run)
        task.status = "completed"
        if not mapping_present:
            task.metadata_json = {}
        rows = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session)))
        await db.commit()
    for row in sorted(rows, key=lambda row: row.sequence):
        assert await bridge_transcript_event_to_t0(transcript_event_id=row.id)
    events = replay_t0_session_events(agent_id=child, session_id=session)
    assert any(answer in e.content for e in events)
    seen, validate = [], receipts._validate_input_dependencies

    async def check(db, tenant, dependencies, **kwargs):
        if kwargs.get("runtime_request") is not None:
            seen.append(kwargs["runtime_request"])
        return await validate(db, tenant, dependencies, **kwargs)

    monkeypatch.setattr(receipts, "_validate_input_dependencies", check)
    calls, _ = _model(monkeypatch)
    result = await t2.run_t2_segment_package_job(agent_id=child, tenant_id=n.tenant,
        session_id=session, t0_segment_id=events[-1].segment_id)
    assert result.status == ("committed" if mapping_present else "not_applicable"), result.issues
    if mapping_present:
        assert seen and all(p.agent_id == n.agent and p.session_id == str(n.session)
            and p.runtime_task_id == str(n.run) and p.requester_user_id == n.user for p in seen)
        assert answer in json.dumps(calls) + _disk_text(result)
    else:
        assert calls == []
        assert answer not in _disk_text(result)


@pytest.mark.parametrize("failure", ["denied", "execution_denied", "unavailable", "execution_unavailable"])
async def test_authority_denial_drops_one_event_but_availability_holds_without_bundle(
    read_turn, monkeypatch, failure,
):
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.services import content_input_receipts as receipts

    s, validate = read_turn, receipts._validate_input_dependencies

    async def check(db, tenant, dependencies, **kwargs):
        if kwargs.get("runtime_request") is not None and any(d["source_kind"] == "workspace_artifact" for d in dependencies):
            if failure.startswith("execution"):
                task = await db.get(RuntimeTask, s.n.run)
                raise ExecutionAuthorityUnavailable(code="t2_authority_probe", task=task,
                    status="unavailable" if failure.endswith("unavailable") else "denied")
            raise receipts.ContentInputReceiptError("t2_authority_probe", "Authority service unavailable",
                503 if failure == "unavailable" else 403)
        return await validate(db, tenant, dependencies, **kwargs)

    monkeypatch.setattr(receipts, "_validate_input_dependencies", check)
    calls, _ = _model(monkeypatch)
    result = await _run(s)
    unavailable = failure.endswith("unavailable")
    assert result.status == ("held" if unavailable else "committed"), result.issues
    assert s.body not in json.dumps(calls) + _disk_text(result)
    if unavailable:
        assert calls == []
        assert not (result.staging_dir / "source_bundle.json").exists()
        assert "t2_authority_probe" in _disk_text(result) or "Authority service unavailable" in _disk_text(result)
    else:
        assert "Read the shared file" in json.dumps(calls)


async def test_missing_tenant_is_resolved_from_existing_agent(read_turn, monkeypatch):
    s = read_turn
    calls, configs = _model(monkeypatch)
    result = await t2.run_t2_segment_package_job(agent_id=s.n.agent, tenant_id=None,
        session_id=s.n.session, t0_segment_id=s.segment)
    assert result.status == "committed", result.issues
    assert configs == [str(s.n.tenant)]
    assert calls[0]["tenant_id"] == str(s.n.tenant)
    assert s.body in _disk_text(result)


async def test_unresolvable_tenant_holds_with_content_free_error(read_turn, monkeypatch):
    s = read_turn

    async def missing(*args, **kwargs):
        raise ConnectionError("tenant lookup offline")

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", missing)
    calls, _ = _model(monkeypatch)
    result = await t2.run_t2_segment_package_job(agent_id=s.n.agent, tenant_id=None,
        session_id=s.n.session, t0_segment_id=s.segment)
    assert result.status == "held", result.issues
    assert calls == []
    assert not (result.staging_dir / "source_bundle.json").exists()
    assert "ConnectionError: tenant lookup offline" in _disk_text(result)
    assert s.body not in _disk_text(result)


async def test_stale_forward_id_falls_back_to_reverse_bridge(read_turn, owner_sessionmaker, monkeypatch):
    s = read_turn
    async with owner_sessionmaker() as db:
        row = (await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == s.n.session,
            ChatTranscriptEvent.item_kind == "assistant_text",
            ChatTranscriptEvent.lifecycle == "snapshot",
            ChatTranscriptEvent.content.contains(s.body)))).first()
        assert row is not None and s.body in row.content
        event = append_t0_session_event(agent_id=s.n.agent, tenant_id=s.n.tenant, session_id=s.n.session,
            runtime_task_id=s.n.run, event_type=row.event_type, role="assistant", content=row.content,
            metadata={"transcript_event_id": str(uuid.uuid4())})
        row.metadata_json = {**row.metadata_json, "t0_bridge_event_id": event.event_id}
        await db.commit()
    calls, _ = _model(monkeypatch)
    result = await _run(s, segment=event.segment_id)
    assert result.status == "committed", result.issues
    assert s.body in json.dumps(calls)


async def test_completed_group_turn_fails_closed_without_live_claim(read_turn, owner_sessionmaker, monkeypatch):
    from app.models.chat_session import ChatSession

    s = read_turn
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, s.n.session)).audience_kind = "group"
        await db.commit()
    calls, _ = _model(monkeypatch)
    result = await _run(s)
    assert result.status in {"not_applicable", "committed"}, result.issues
    assert s.body not in json.dumps(calls) + _disk_text(result)
