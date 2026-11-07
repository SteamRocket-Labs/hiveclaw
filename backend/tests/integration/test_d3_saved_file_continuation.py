"""A saved-file reference checks a version without granting file authority."""

import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context, tenant_scoped_session
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("case", [
    "same", "changed", "forged_hash", "foreign_path", "foreign_agent", "escape",
    "invalid_utf8", "storage_unavailable",
])
async def test_saved_file_is_checked_at_the_real_model_input_boundary(
    native_workspace, app_user_sessionmaker, case,
):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_model_round import prepare_model_request
    from app.services.web_chat_run_orchestrator import _bind_session_round_inputs

    n = native_workspace
    path = "workspace/d3-report.md"
    saved_text = "# Report\nHuman saved correction: 人修改的决定性内容\n"
    file_url = f"/agents/{n.agent}/files/content"
    saved = await n.client.put(file_url, params={"path": path}, json={"content": saved_text})
    assert saved.status_code == 200, saved.text
    saved_hash = saved.json()["content_hash"]
    assert saved_hash == hashlib.sha256(saved_text.encode()).hexdigest()
    if case == "invalid_utf8":
        from app.services.content_input_receipts import latest_workspace_receipt, record_workspace_receipt
        from app.services.workspace_resource_authority import register_workspace_path

        raw = b"INVALID_UTF8_FILE_BYTES_MUST_NOT_REACH_MODEL\xff"
        (n.root / path).write_bytes(raw)
        saved_hash = hashlib.sha256(raw).hexdigest()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            previous = await latest_workspace_receipt(db, n.tenant, n.agent, path)
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=saved_hash)
            record_workspace_receipt(db, n.tenant, n.agent, path, saved_hash, previous)
            await db.commit()
    elif case == "storage_unavailable":
        from app.services.workspace_resource_authority import WorkspaceAuthorityError

        def unavailable(*_args):
            raise WorkspaceAuthorityError("workspace_storage_unavailable", "Snapshot storage is unavailable.")

        n.monkeypatch.setattr("app.services.artifact_continuation.snapshot_workspace_file", unavailable)
    reference = {"type": "workspace_file_ref", "agent_id": str(n.agent), "path": path,
                 "human_saved_hash": saved_hash}
    if case == "changed":
        changed = await n.client.put(file_url, params={"path": path}, json={
            "content": "LATER_OTHER_VERSION_MUST_NOT_REACH_MODEL", "expected_current_hash": saved_hash})
        assert changed.status_code == 200, changed.text
    elif case == "forged_hash":
        reference["human_saved_hash"] = "0" * 64
    elif case == "foreign_path":
        reference.update(path=n.path, human_saved_hash=hashlib.sha256(n.target.read_bytes()).hexdigest())
    elif case == "foreign_agent":
        reference["agent_id"] = str(uuid.uuid4())
    elif case == "escape":
        reference["path"] = "../workspace/d3-report.md"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, n.agent),
            user=await db.get(User, n.user), session=await db.get(ChatSession, n.session),
            content="请接着修改我保存的报告，也回答另外的问题。", parts=[reference],
            source="web", source_channel="web", requested_kind="steer_current_turn",
            expected_turn_id=f"turn-{n.run.hex}", expected_run_id=n.run)
        await db.commit()
        row = await db.get(SessionTurnInput, uuid.UUID(str(receipt["input_id"])))
        assert reference in row.content_parts_json
    broadcast = AsyncMock()
    state = SimpleNamespace(agent=SimpleNamespace(id=n.agent, tenant_id=n.tenant),
        session_id=str(n.session), run_uuid=n.run, actor_user_id=n.user,
        metadata={"turn_id": f"turn-{n.run.hex}"},
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=lambda tenant:
            tenant_scoped_session(tenant, session_factory=app_user_sessionmaker)),
            events=SimpleNamespace(broadcast=broadcast)))
    messages = _mid_run_items_to_user_messages(await _bind_session_round_inputs(state, 1))
    wire_text = json.dumps([m.content for m in messages], ensure_ascii=False)
    assert "LATER_OTHER_VERSION_MUST_NOT_REACH_MODEL" not in wire_text
    assert "PRIVATE_FOREIGN_FILE_BODY" not in wire_text
    assert "INVALID_UTF8_FILE_BYTES_MUST_NOT_REACH_MODEL" not in wire_text
    assert (saved_text in wire_text.replace("\\n", "\n")) is (case == "same")
    changed_notice = "文件已不是你保存时的版本，本次未使用它。请重新打开确认后再继续，其他问题可以继续处理。"
    unreadable_notice = "这个文件目前无法读取，本次未使用它。你可以继续处理其他问题，或选择有权打开的文件。"
    notice = changed_notice if case in {"changed", "forged_hash"} else unreadable_notice
    if case != "same":
        assert notice in wire_text
        assert (changed_notice in wire_text) is (case in {"changed", "forged_hash"})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=1,
            messages=messages, tools=[], provider="synthetic", model="d3-local")
        await db.commit()
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        captured = json.dumps(result.model_request_snapshot_json, ensure_ascii=False)
        assert "请接着修改我保存的报告，也回答另外的问题。" in captured, result.model_request_snapshot_json["input_bindings"]
        assert ("人修改的决定性内容" in captured) is (case == "same")
        assert "PRIVATE_FOREIGN_FILE_BODY" not in captured
        assert "INVALID_UTF8_FILE_BYTES_MUST_NOT_REACH_MODEL" not in captured
        assert (await db.get(RuntimeTask, n.run)).status == "running"
        notices = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == n.session, ChatTranscriptEvent.item_kind == "assistant_commentary")))
        if case != "same":
            assert notice in captured
            assert (changed_notice in captured) is (case in {"changed", "forged_hash"})
            assert any(notice in event.content for event in notices)
            assert any(changed_notice in event.content for event in notices) is (case in {"changed", "forged_hash"})
        if case == "same":
            assert any(m.context_origin and m.context_origin.sources[0]["source_kind"] == "workspace_artifact"
                       for m in messages)
