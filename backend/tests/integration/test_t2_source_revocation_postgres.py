"""T2 rechecks the completed turn's sources before model and file sinks."""

import json
import uuid
from copy import deepcopy
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events, seal_t0_session_segment
from app.memory.t2 import segment_package as t2
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.models.workspace_resource import WorkspaceResourceManifest
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages,
    native_workspace,  # noqa: F401
)
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create
from tests.integration.test_m1g29_workspace_input_evidence_postgres import _native_read
from tests.integration.test_m1g30_model_dependencies_postgres import _snapshot
from tests.memory.test_t2_segment_package_builder import _approved_review_xml, _labels_xml, _summary_xml
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def read_turn(native_workspace, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    from app.core.resource_authority import workspace_resource_id
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from app.services.llm_client import LLMMessage

    n = native_workspace
    path, body = "workspace/shared-source.md", "SHARED_SOURCE_T2_REVOKED_CONTENT"
    await _create(n, path, body)
    grant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        manifest.owner_user_id, manifest.root_session_id = n.foreign, None
        db.add(ResourcePermission(id=grant_id, tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path),
            actions=["read"], effect="allow", conditions={}))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        messages = await _admitted_write_messages(db, n, content="Read the shared file and write a report.")
        invocation, args = await _native_read(db, n, path, messages=messages)
    trace = {}
    raw = str(await n.service.execute("read_file", args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id,
        trace_metadata_sink=trace, emit_runtime_hooks=False))
    assert body in raw
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": "read_file", "args": args, "status": "done", "result": raw,
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        request_id, snapshot = await _snapshot(db, n.tenant, n.agent, n.session, n.run,
            [LLMMessage(role="tool", content=raw, tool_call_id=invocation.provider_tool_use_id,
                source_ref=f"session-tool-result:{invocation.id}")], round_index=2)
        assert snapshot["input_bindings"]["dependency_coverage"]["complete"] is True
        assert any(ref["source_kind"] == "workspace_artifact" for ref in snapshot["input_bindings"]["dependencies"])
        await _seal_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
            turn_id=f"turn-{n.run.hex}", round_index=2, provider_request_id=request_id,
            content="Report derived from " + body)
        await db.commit()
    async with owner_sessionmaker() as db:
        from app.services.chat_transcript import append_session_event

        for role, content in (("user", "REQUESTER_TYPED_INPUT_WITHOUT_SOURCE"),
            ("system", "PLATFORM_TEXT_WITHOUT_SOURCE")):
            await append_session_event(db=db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                user_id=n.user, event_type=f"{role}_message", actor_type=role, role=role, content=content)
        rows = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == n.session).order_by(ChatTranscriptEvent.sequence)))
        (await db.get(RuntimeTask, n.run)).status = "completed"
        await db.commit()
    for row in rows:
        assert await bridge_transcript_event_to_t0(transcript_event_id=row.id)
    events = replay_t0_session_events(agent_id=n.agent, session_id=n.session)
    assert any(body in event.content for event in events)
    seal = seal_t0_session_segment(agent_id=n.agent, session_id=n.session, reason="turn_stop")
    assert seal is not None
    return SimpleNamespace(n=n, grant_id=grant_id, body=body, segment=seal.segment_id,
        tainted_ids=[event.event_id for event in events if body in event.content])


def _model(monkeypatch, *, mode="available"):
    from app.services import llm_client

    calls, config_calls = [], []

    async def config(tenant):
        config_calls.append(str(tenant))
        return None if mode == "missing" else {"provider": "openai", "model": "synthetic-no-provider"}

    class Model:
        async def complete(self, *, messages, **_kwargs):
            payload = json.loads(messages[1].content)
            calls.append(deepcopy(payload))
            if mode == "failure":
                raise RuntimeError("local model unavailable")
            bundle = payload.get("source_bundle", payload)
            if "labels_md" in payload:
                answer = _approved_review_xml(package_id=bundle["package_id"], ref=bundle["source_refs"][0]["uri"])
            elif "summary_md" in payload:
                answer = _labels_xml(bundle)
            else:
                contents = json.dumps(bundle["t0_events"], ensure_ascii=False)
                answer = _summary_xml(bundle).replace("测试事实。", escape(contents))
            return SimpleNamespace(content=answer, finish_reason="stop")

        async def close(self):
            pass

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", config)
    monkeypatch.setattr(llm_client, "create_llm_client_from_config", lambda _config: Model())
    return calls, config_calls


def _disk_text(result):
    return "\n".join(path.read_text() for root in (result.staging_dir, result.package_dir)
        if root.exists() for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize("mode", ["available", "missing", "failure"])
async def test_revoked_shared_read_and_derived_answer_never_reach_t2_sinks(
    read_turn, owner_sessionmaker, monkeypatch, mode,
):
    s = read_turn
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, s.grant_id)).effect = "deny"
        await db.commit()
    calls, _ = _model(monkeypatch, mode=mode)
    result = await t2.run_t2_segment_package_job(agent_id=s.n.agent, tenant_id=s.n.tenant,
        session_id=s.n.session, t0_segment_id=s.segment)
    assert result.status == ("committed" if mode == "available" else "held"), result.issues
    assert len(calls) == {"available": 3, "missing": 0, "failure": 1}[mode]
    bundle = json.loads((result.staging_dir / "source_bundle.json").read_text())
    for retained in ("Read the shared file", "REQUESTER_TYPED_INPUT_WITHOUT_SOURCE", "PLATFORM_TEXT_WITHOUT_SOURCE"):
        assert retained in json.dumps(bundle["t0_events"])
    sinks = json.dumps(calls) + _disk_text(result)
    assert s.body not in sinks
    for event_id in s.tainted_ids:
        assert event_id not in sinks
    if mode == "available":
        assert "Read the shared file" in (result.package_dir / "summary.md").read_text()


@pytest.mark.parametrize("origin", ["missing", "foreign_session", "unbound_model", "other_member_input"])
async def test_unresolvable_transcript_origin_is_dropped(read_turn, owner_sessionmaker, monkeypatch, origin):
    s = read_turn
    marker = "UNRESOLVED_TRANSCRIPT_SOURCE_CONTENT"
    event_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        if origin == "foreign_session":
            from tests.services.test_session_input_control_v2 import _seed_session
            from app.services.chat_transcript import append_session_event

            tenant, user, agent, session, _run = await _seed_session(owner_sessionmaker)
            row = await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
                user_id=user, event_type="assistant_message", actor_type="assistant", role="assistant", content=marker,
                bridge_to_t0=False)
            event_id = row.event_id
        elif origin == "other_member_input":
            from app.services.chat_transcript import append_session_event

            row = await append_session_event(db=db, tenant_id=s.n.tenant, agent_id=s.n.agent,
                session_id=s.n.session, user_id=s.n.foreign, event_type="user_message", actor_type="user",
                role="user", content=marker, bridge_to_t0=False)
            event_id = row.event_id
        elif origin == "unbound_model":
            from app.models.session_v2 import SessionModelResult

            row = (await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == s.n.session,
                ChatTranscriptEvent.item_kind == "assistant_text"))).first()
            assert row is not None
            model = await db.get(SessionModelResult, row.result_id)
            snapshot = deepcopy(model.model_request_snapshot_json)
            snapshot["input_bindings"]["dependency_coverage"]["complete"] = False
            model.model_request_snapshot_json = snapshot
            event_id = row.id
        await db.commit()
    event = append_t0_session_event(agent_id=s.n.agent, tenant_id=s.n.tenant, session_id=s.n.session,
        event_type="assistant_message", role="assistant", content=marker,
        metadata={"transcript_event_id": str(event_id)})
    calls, _ = _model(monkeypatch)
    result = await t2.run_t2_segment_package_job(agent_id=s.n.agent, tenant_id=s.n.tenant,
        session_id=s.n.session, t0_segment_id=event.segment_id)
    assert marker not in json.dumps(calls) + _disk_text(result)
    assert result.status == "not_applicable"
    assert not result.package_dir.exists()
    assert calls == []


async def test_dropping_last_semantic_event_uses_existing_empty_segment_outcome(monkeypatch, tmp_path):
    agent, tenant, session = (uuid.uuid4() for _ in range(3))
    event = append_t0_session_event(agent_id=agent, tenant_id=tenant, session_id=session,
        event_type="assistant_message", role="assistant", content="ONLY_UNRESOLVED_DERIVED_EVENT",
        metadata={"transcript_event_id": str(uuid.uuid4())}, data_root=tmp_path)
    calls, config_calls = _model(monkeypatch)
    result = await t2.run_t2_segment_package_job(agent_id=agent, tenant_id=tenant, session_id=session,
        t0_segment_id=event.segment_id, data_root=tmp_path)
    assert result.status == "not_applicable"
    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text())
    assert report["reason_code"] == "no_semantic_t0_events"
    assert calls == config_calls == []
    assert not result.package_dir.exists()
    assert not (result.staging_dir / "source_bundle.json").exists()


@pytest.mark.parametrize("text_ids", [False, True], ids=["uuid", "text"])
async def test_unchanged_completed_turn_keeps_base_source_bundle(read_turn, monkeypatch, text_ids):
    from app.config import get_settings
    from pathlib import Path

    s = read_turn
    monkeypatch.setattr(t2, "_now", lambda: "2026-11-07T00:00:00+00:00")
    expected = t2._build_source_bundle(root=Path(get_settings().AGENT_DATA_DIR), agent_id=s.n.agent,
        tenant_id=s.n.tenant, session_id=s.n.session, t0_segment_id=s.segment, package_id="t2-characterization")
    calls, _ = _model(monkeypatch)
    identity = str if text_ids else lambda value: value
    result = await t2.run_t2_segment_package_job(agent_id=identity(s.n.agent), tenant_id=identity(s.n.tenant),
        session_id=identity(s.n.session), t0_segment_id=s.segment, package_id="t2-characterization")
    assert result.status == "committed", result.issues
    actual = json.loads((result.staging_dir / "source_bundle.json").read_text())
    assert actual == expected
    assert calls[0] == expected
    assert s.body in (result.package_dir / "summary.md").read_text()
