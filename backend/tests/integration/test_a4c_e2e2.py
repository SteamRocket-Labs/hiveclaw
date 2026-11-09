"""Member edits and explicit memory survive incomplete ambient context coverage."""

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context, tenant_scoped_session
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _root_item(db, n):
    from app.models.runtime_root_item import RuntimeRootItem

    db.add(RuntimeRootItem(
        tenant_id=n.tenant, root_runtime_task_id=n.run, runtime_task_id=n.run,
        source_agent_id=n.agent, root_user_id=n.user, root_session_id=str(n.session),
        intent_key=f"web-chat:{n.run}", work_type="web_chat_turn", target_ref=f"agent:{n.agent}",
        path_json=[f"agent:{n.agent}", f"session:{n.session}"], state="running",
        admission_disposition="admitted",
    ))
    await db.commit()


async def _tool(n, sessionmaker, tool_name, arguments, *, round_index):
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
        _admitted_write_messages, _prepare_write,
    )

    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        messages = await _admitted_write_messages(db, n, content="Update my report", round_index=round_index)
        invocation, arguments, turn = await _prepare_write(
            db, n, messages=messages, tool_name=tool_name, arguments=arguments, round_index=round_index,
        )
    trace = {}

    async def start_effect(payload):
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            "name": tool_name, "args": payload["arguments"], "status": "effect_started",
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(n.run),
        })

    raw = await n.service.execute(
        tool_name, arguments, agent_id=n.agent, user_id=n.user, session_id=str(n.session),
        runtime_task_id=str(n.run), turn_id=turn, tool_call_id=invocation.provider_tool_use_id,
        emit_runtime_hooks=False, trace_metadata_sink=trace, pre_effect_callback=start_effect,
    )
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool_name, "args": arguments, "status": "done", "result": str(raw),
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace),
    })
    return str(raw)


async def _finish(n, sessionmaker, *, round_index, incomplete):
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import commit_model_response, prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_terminal_outcome import commit_terminal_outcome, prepare_and_seal_run_outcome
    from app.services.web_chat_runtime import _declared_terminal_artifact_paths

    response = f"The report is ready.\n\nDELIVERABLE: {n.path}"
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
        if incomplete:
            messages.insert(0, LLMMessage(role="system", content="Platform formatting guidance for the reply."))
        request_id = await prepare_model_request(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
            turn_id=f"turn-{n.run.hex}", round_index=round_index, messages=messages, tools=[],
            provider="synthetic", model="local-reference",
        )
        committed = await commit_model_response(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
            turn_id=f"turn-{n.run.hex}", round_index=round_index, provider_request_id=request_id,
            response={"content": response, "tool_calls": [], "finish_reason": "stop", "usage": {}},
        )
        await db.commit()
    result_id = uuid.UUID(committed["result_id"])
    state = SimpleNamespace(
        agent=SimpleNamespace(id=n.agent, tenant_id=n.tenant), session_id=str(n.session), run_uuid=n.run,
        runtime_session_context=None,
        ports=SimpleNamespace(
            runtime=SimpleNamespace(tenant_scoped_session=lambda tenant: tenant_scoped_session(
                tenant, session_factory=sessionmaker)),
            artifacts=SimpleNamespace(artifact_paths=lambda *_args: [n.path],
                declared_paths=_declared_terminal_artifact_paths, rejected_paths=lambda *_args: []),
        ),
    )
    await orchestrator._record_canonical_terminal_artifact_selection(state, response, terminal_result_id=result_id)
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        outcome = await prepare_and_seal_run_outcome(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
            turn_id=f"turn-{n.run.hex}", terminal_result_id=result_id,
        )
        outcome_id = outcome.id
        await db.commit()
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        await commit_terminal_outcome(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, outcome_id=outcome_id)
        await db.commit()


@pytest.mark.parametrize("capture_failed", [False, True])
async def test_next_turn_edit_completes_even_when_an_attachment_is_unavailable(
    native_workspace, app_user_sessionmaker, monkeypatch, capture_failed,  # noqa: F811
):
    from app.api import files
    from app.config import get_settings
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    n = native_workspace
    # Other suites clear the settings cache; the API must use this fixture's root.
    monkeypatch.setattr(files, "settings", get_settings())
    n.path = "workspace/a4c-report.md"
    original = "# Sales report\nRevenue: 42\n"
    revised = original + "\n## Next steps\nReview next Friday.\n"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        await _root_item(db, n)
    await _tool(n, app_user_sessionmaker, "write_file", {"path": n.path, "content": original}, round_index=1)
    await _finish(n, app_user_sessionmaker, round_index=2, incomplete=False)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        first = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run))
        assert first is not None
        first_id = first.id
        assert (await db.get(RuntimeTask, n.run)).status == "completed"
        n.run = uuid.uuid4()
        db.add(RuntimeTask(
            id=n.run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(n.session),
            child_session_id=str(n.session), root_user_id=n.user, root_session_id=str(n.session),
            root_runtime_task_id=n.run, prompt="Add next steps to my report", metadata_json={"turn_id": f"turn-{n.run.hex}"},
        ))
        await db.flush()
        await _root_item(db, n)
    if capture_failed:
        def fail_capture(**_kwargs):
            raise OSError("Fixture snapshot storage unavailable")
        monkeypatch.setattr("app.services.chat_artifact_delivery.build_artifact_candidate", fail_capture)
    await _tool(n, app_user_sessionmaker, "edit_file", {
        "path": n.path, "old_text": original, "new_text": revised,
    }, round_index=1)
    assert (n.root / n.path).read_text() == revised
    read = json.loads(await _tool(n, app_user_sessionmaker, "read_file", {"path": n.path}, round_index=2))
    assert read["ok"] is True and read["content"].startswith(revised)
    await _finish(n, app_user_sessionmaker, round_index=3, incomplete=True)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        task = await db.get(RuntimeTask, n.run)
        assert task.status == "completed"
        parts = task.metadata_json["artifacts"]
        if capture_failed:
            assert parts == []
            assert n.path in task.metadata_json["rejected_artifact_paths"]
            notices = list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == n.run, ChatTranscriptEvent.item_kind == "assistant_commentary")))
            assert any("文件" in (row.content or "") and "未能附" in (row.content or "") for row in notices)
        else:
            assert len(parts) == 1
            row = await db.get(ChatArtifact, uuid.UUID(parts[0]["artifact_id"]))
            assert row.snapshot_json["content_hash"] == hashlib.sha256(revised.encode()).hexdigest()
            downloaded = await n.client.get(f"/agents/{n.agent}/files/artifacts/{row.id}/download")
            assert downloaded.status_code == 200 and downloaded.content == revised.encode()
    old = await n.client.get(f"/agents/{n.agent}/files/artifacts/{first_id}/download")
    assert old.status_code == 200 and old.content == original.encode()


@pytest.mark.parametrize("pasted_key", [False, True])
async def test_member_can_remember_own_request_with_incomplete_ambient_context(
    native_workspace, app_user_sessionmaker, monkeypatch, pasted_key,  # noqa: F811
):
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.capability_policy import CapabilityPolicy
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round
    from app.services.credential_boundary_loader import redact_runtime_ingress_payload
    from app.services.llm_client import LLMMessage
    from app.services.session_model_inputs import _model_dependencies
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    key = "sk-a4c-local-probe-9f8e7d6c5b4a"
    command = f"This is a fake key {key}; remember it." if pasted_key else "Remember: our weekly report goes out on Friday afternoon."
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        for capability in ("agent.memory.write", "agent.memory.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        protected = await redact_runtime_ingress_payload(db, tenant_id=n.tenant, agent_id=n.agent,
            requester_user_id=None, source_channel="web", payload={"content": command})
        command = protected.value["content"]
        if pasted_key:
            assert key not in command and "[REDACTED_SECRET]" in command
        await db.commit()
    original_prepare = session_model_round.prepare_model_request
    async def prepare_with_ambient_guidance(db, **kwargs):
        from app.runtime.context_candidates import ContextMessageOrigin

        text = "Platform formatting guidance for the reply."
        kwargs["messages"].insert(0, LLMMessage(role="system", content=text,
            context_origin=ContextMessageOrigin.capture(kind="platform_guidance", role="system", content=text, unbound=())))
        kwargs["messages"].insert(1, LLMMessage(role="user", content="UNBOUND_MEMORY_SECRET"))
        result = await original_prepare(db, **kwargs)
        # A copying model can only use the messages left at the shared entry.
        if any("UNBOUND_MEMORY_SECRET" in str(message.content) for message in kwargs["messages"]):
            memory_arguments["content"] += " UNBOUND_MEMORY_SECRET"
        return result

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr(session_model_round, "prepare_model_request", prepare_with_ambient_guidance)
    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)
    memory_content = "A4c test credential value: [REDACTED_SECRET]." if pasted_key else command
    memory_arguments = {"category": "project", "content": memory_content}
    saved, invocation = await memory_call(n, app_user_sessionmaker, "save_memory", memory_arguments, command=command)
    assert "Saved memory [project] (capabilities):" in saved, saved
    entries = list((n.root / "memory/explicit/entries").glob("*.md"))
    assert len(entries) == 1 and memory_content in entries[0].read_text()
    assert "UNBOUND_MEMORY_SECRET" not in entries[0].read_text()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        dependencies, complete = _model_dependencies(model)
        assert complete and dependencies
        assert "UNBOUND_MEMORY_SECRET" not in json.dumps(model.model_request_snapshot_json["wire_request"])
        assert "Platform formatting guidance" in json.dumps(model.model_request_snapshot_json["wire_request"])
    if pasted_key:
        assert key not in entries[0].read_text() and key not in saved


@pytest.mark.parametrize("source_state", ["owned", "another_member", "revoked"])
async def test_created_profile_keeps_memory_scoped_to_its_original_bot(
    shared_memory, owner_sessionmaker, app_user_sessionmaker, source_state,  # noqa: F811
):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.employee_creation_sources import validate_creation_publication_inputs
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency, validate_explicit_memory_dependency
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s = shared_memory
    n = s.n
    entry_id = s.entry_id
    if source_state == "another_member":
        saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
            {"content": "Other member private source", "category": "project"},
            user=n.foreign, session=s.second_session, run=s.second_run)
        assert "Saved memory [project] (capabilities):" in saved
        entry_id = json.loads((n.root / "memory/explicit/manifest.jsonl").read_text().splitlines()[-1])["id"]
    elif source_state == "revoked":
        retired, _ = await memory_call(n, app_user_sessionmaker, "retire_memory",
            {"memory_id": entry_id, "reason": "privacy"})
        assert json.loads(retired)["status"] == "completed"
    child_id, child_session = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=child_id, tenant_id=n.tenant, name="Profile child", creator_id=n.user,
            sponsor_user_id=n.user, owner_user_id=n.user))
        await db.flush()
        db.add(ChatSession(id=child_session, tenant_id=n.tenant, agent_id=child_id,
            user_id=n.user, audience_kind="private"))
        await db.commit()
    principal = AgentRuntimePrincipal(agent_id=child_id, requester_user_id=n.user,
        session_id=str(child_session), runtime_task_id=str(uuid.uuid4()), purpose="interactive_session")
    draft = SimpleNamespace(tenant_id=n.tenant, hr_agent_id=n.agent, created_agent_id=child_id,
        requested_by_user_id=n.user, session_id=n.session)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipt = await latest_memory_receipt(db, n.tenant, n.agent, entry_id)
        dependency = source_dependency(receipt)
        with pytest.raises(ContentInputReceiptError):
            await validate_creation_publication_inputs(db, draft=draft, dependencies=[dependency],
                user=None, runtime_request=principal)
        # Publishing the confirmed profile does not give the employee a raw
        # upstream memory read, even for the same owner.
        with pytest.raises(ContentInputReceiptError):
            await validate_explicit_memory_dependency(db, n.tenant, dependency, request=principal)
