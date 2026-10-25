"""A native first task publishes one inspectable answer with its real file."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agents import orchestrator
from app.api import chat_sessions, files
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.chat_artifact import ChatArtifact
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.direct_invocation_terminal_boundary_processor import _validate_response_payload
from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch
from tests.integration import test_m1g49_delegation_instruction_origin_postgres as native_cases


@pytest.mark.usefixtures("migrated_pg_url")
async def test_first_task_final_answer_and_pinned_file_survive_sealed_round_replay(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, migrated_pg_url
):
    original_delegate = orchestrator._delegate
    observed = set()

    async def inspect_delivery(request, result):
        tenant_id = request.tenant_id
        session_id = UUID(result.child_session_id)
        run_id = UUID(str(request.runtime_task_id))
        app = FastAPI()
        app.include_router(chat_sessions.router)
        app.include_router(files.router)

        async def viewer():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant_id)
                return await db.get(User, request.owner_id)

        async def scoped_db():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant_id)
                yield db

        app.dependency_overrides[get_current_user] = viewer
        app.dependency_overrides[get_db] = scoped_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
            response = await browser.get(
                f"/agents/{request.parent_agent_id}/sessions/{session_id}/transcript",
                params={"schema_version": 2, "limit": 1000},
            )
            assert response.status_code == 200, response.text
            events = response.json()
            finals = [
                event
                for event in events
                if event.get("item_kind") == "assistant_final" and event.get("lifecycle") == "completed"
            ]
            (tmp_path / "child-final-delivery-observation.json").write_text(
                json.dumps({"kinds": [event.get("item_kind") for event in events], "finals": finals}, indent=2)
            )
            assert len(finals) == 1, "A committed model round is not an inspectable final answer"
            final = finals[0]
            assert final["payload"]["content"] == result.content
            assert final["scope"]["run_id"] == str(run_id)
            parts = [part for part in final["payload"]["parts"] if part.get("type") == "artifact"]
            assert len(parts) == 1
            part = parts[0]
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant_id)
                artifact = await db.scalar(
                    select(ChatArtifact).where(
                        ChatArtifact.id == UUID(part["artifact_id"]),
                        ChatArtifact.agent_id == request.target.id,
                        ChatArtifact.session_id == session_id,
                        ChatArtifact.runtime_task_id == run_id,
                        ChatArtifact.owner_user_id == request.owner_id,
                    )
                )
                assert artifact is not None
                expected = artifact.snapshot_json["preview_content"]
                event = await db.get(ChatTranscriptEvent, UUID(final["event_id"]))
                task = await db.get(RuntimeTask, run_id)
                payload = result.response_complete_payload
                await _validate_response_payload(
                    db=db,
                    task=task,
                    agent_id=request.target.id,
                    session_id=str(session_id),
                    source="agent",
                    terminal_event=event,
                    payload=payload,
                )
                copied = SimpleNamespace(
                    **{
                        field: getattr(event, field)
                        for field in (
                            "id",
                            "tenant_id",
                            "agent_id",
                            "session_id",
                            "run_id",
                            "actor_type",
                            "event_type",
                            "content",
                        )
                    },
                    metadata_json=deepcopy(event.metadata_json),
                )
                copied.metadata_json["native_final_model_result_id"] = str(uuid4())
                with pytest.raises(TerminalBoundaryCanonicalMismatch):
                    await _validate_response_payload(
                        db=db,
                        task=task,
                        agent_id=request.target.id,
                        session_id=str(session_id),
                        source="agent",
                        terminal_event=copied,
                        payload=payload,
                    )
                copied.metadata_json = deepcopy(event.metadata_json)
                copied.content = "An answer absent from the committed model result."
                forged_payload = deepcopy(payload)
                forged_payload["metadata"]["final_response"] = copied.content
                with pytest.raises(TerminalBoundaryCanonicalMismatch):
                    await _validate_response_payload(
                        db=db,
                        task=task,
                        agent_id=request.target.id,
                        session_id=str(session_id),
                        source="agent",
                        terminal_event=copied,
                        payload=forged_payload,
                    )
            downloaded = await browser.get(
                f"/agents/{part['download_agent_id']}/files/artifacts/{part['artifact_id']}/content"
            )
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.json()["content"] == expected
            assert hashlib.sha256(expected.encode()).hexdigest() == part["content_hash"]
            return final["event_id"]

    async def observe_delegate(request):
        result = await original_delegate(request)
        if request.interaction_type == "delegation" and request.runtime_task_id and not result.failed:
            first_event = await inspect_delivery(request, result)
            # Simulate recovery after final publication but before outer task
            # settlement. The sealed round must not make another provider call
            # or append another answer; the reused native case checks call count.
            replay = await original_delegate(request)
            assert not replay.failed and replay.transcript_committed and replay.content == result.content
            assert await inspect_delivery(request, replay) == first_event
            observed.add(str(request.runtime_task_id))
        return result

    monkeypatch.setattr(orchestrator, "_delegate", observe_delegate)
    await native_cases.test_governed_delegation_preserves_its_parent_instruction_origin(
        owner_sessionmaker=owner_sessionmaker,
        app_user_sessionmaker=app_user_sessionmaker,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        input_case="first_task_child_readback",
        migrated_pg_url=migrated_pg_url,
    )
    assert len(observed) == 1
