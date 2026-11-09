"""Only missing declared deliverables produce the member attachment notice."""
from uuid import UUID

import pytest
from sqlalchemy import select

from tests.services.test_session_terminal_outcome_v2 import _prepare, _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("case", ["mentioned", "reattached", "missing"])
async def test_attachment_notice_ignores_mentions_but_reports_missing_declarations(owner_sessionmaker, case):
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.services.session_model_round import commit_model_response
    from app.services.session_terminal_outcome import commit_terminal_outcome, prepare_and_seal_run_outcome
    from app.services.web_chat_runtime import _declared_terminal_artifact_paths, _rejected_terminal_artifact_paths_for_turn

    missing = case == "missing"
    tenant, agent, session_id, run, turn = await _seed(owner_sessionmaker)
    report, mentioned, absent = "workspace/report.md", "workspace/input.csv", "workspace/missing.md"
    content = f"报告已完成。\nDELIVERABLE: {report}\n使用的原始资料：{mentioned}"
    if missing:
        content += f"\nDELIVERABLE: {absent}"
    # The actual candidate producer treats a mentioned read-only input as rejected.
    from types import SimpleNamespace
    context = SimpleNamespace(current_turn_writes=[] if case == "reattached" else [report])
    rejected = _rejected_terminal_artifact_paths_for_turn(context, content)
    assert mentioned in rejected
    assert (report in rejected) == (case == "reattached")
    async with owner_sessionmaker() as db:
        request, _ = await _prepare(db, tenant_id=tenant, agent_id=agent, session_id=session_id, run_id=run, turn_id=turn)
        seal = await commit_model_response(db, tenant_id=tenant, agent_id=agent, session_id=session_id,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": content, "tool_calls": [], "finish_reason": "stop", "usage": {}})
        session = await db.get(ChatSession, session_id)
        message = ChatMessage(agent_id=agent, tenant_id=tenant, user_id=session.user_id, role="tool_call",
            content="File saved", conversation_id=str(session_id))
        db.add(message)
        await db.flush()
        artifact = ChatArtifact(agent_id=agent, tenant_id=tenant, session_id=session_id, message_id=message.id,
            runtime_task_id=run, owner_user_id=session.user_id, root_session_id=session_id, authority_state="owned",
            path=report, name="report.md", source="workspace_write", snapshot_hash="test-owned-report")
        db.add(artifact)
        await db.flush()
        task = await db.get(RuntimeTask, run)
        task.metadata_json = {**task.metadata_json, "artifact_ids": [str(artifact.id)], "artifact_paths": [report],
            "declared_artifact_paths": _declared_terminal_artifact_paths(content), "rejected_artifact_paths": rejected,
            "artifact_selection_result_id": seal["result_id"]}
        outcome = await prepare_and_seal_run_outcome(db, tenant_id=tenant, agent_id=agent, session_id=session_id,
            run_id=run, turn_id=turn, terminal_result_id=UUID(seal["result_id"]))
        await commit_terminal_outcome(db, tenant_id=tenant, agent_id=agent, session_id=session_id,
            run_id=run, outcome_id=outcome.id)
        await db.commit()
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run)))
        notices = [e for e in events if e.content == "部分文件未能附在这条回复中。"]
        assert len(notices) == int(missing)
        final = next(e for e in events if e.item_kind == "assistant_final")
        assert final.metadata_json["v2_payload"]["artifact_refs"] == [str(artifact.id)]
        assert task.metadata_json["rejected_artifact_paths"] == rejected


async def test_recovery_seal_reports_a_declared_deliverable_without_artifact_rows(owner_sessionmaker):
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.services.session_model_round import commit_model_response
    from app.services.session_terminal_outcome import commit_terminal_outcome, prepare_and_seal_run_outcome

    tenant, agent, session_id, run, turn = await _seed(owner_sessionmaker)
    missing = "workspace/missing.md"
    async with owner_sessionmaker() as db:
        request, _ = await _prepare(db, tenant_id=tenant, agent_id=agent, session_id=session_id, run_id=run, turn_id=turn)
        seal = await commit_model_response(db, tenant_id=tenant, agent_id=agent, session_id=session_id,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": f"报告已完成。\nDELIVERABLE: {missing}", "tool_calls": [], "finish_reason": "stop", "usage": {}})
        task = await db.get(RuntimeTask, run)
        assert "artifact_selection_result_id" not in task.metadata_json
        assert await db.scalar(select(ChatArtifact.id).where(ChatArtifact.runtime_task_id == run)) is None

        outcome = await prepare_and_seal_run_outcome(db, tenant_id=tenant, agent_id=agent, session_id=session_id,
            run_id=run, turn_id=turn, terminal_result_id=UUID(seal["result_id"]), recovery=True)
        await commit_terminal_outcome(db, tenant_id=tenant, agent_id=agent, session_id=session_id,
            run_id=run, outcome_id=outcome.id)
        await db.commit()

        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run)))
        assert [e.content for e in events if e.item_kind == "assistant_commentary"] == ["部分文件未能附在这条回复中。"]
        final = next(e for e in events if e.item_kind == "assistant_final")
        assert final.metadata_json["v2_payload"]["artifact_refs"] == []
        assert task.metadata_json["declared_artifact_paths"] == [missing]
        assert task.metadata_json["rejected_artifact_paths"] == [missing]
        assert task.metadata_json["artifact_selection_result_id"] == seal["result_id"]
