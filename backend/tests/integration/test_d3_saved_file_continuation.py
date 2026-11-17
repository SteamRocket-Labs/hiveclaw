"""A saved-file reference checks a version without granting file authority."""

import hashlib
import json
import sys
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
    "invalid_utf8", "storage_unavailable", "saved_docx", "pdf_no_hash", "over_cap", "repeated_refs",
    "no_hash_changed", "deleted", "unreceipted_change", "symlink", "rolled_over", "replay", "count_limit",
])
async def test_saved_file_is_checked_at_the_real_model_input_boundary(
    native_workspace, app_user_sessionmaker, case,  # noqa: F811
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
    path = "workspace/d3-report.docx" if case == "saved_docx" else "workspace/d3-report.pdf" if case == "pdf_no_hash" else "workspace/d3-report.md"
    saved_text = "# Report\nHuman saved correction: 人修改的决定性内容\n"
    file_url = f"/agents/{n.agent}/files/content"
    saved = await n.client.put(file_url, params={"path": path}, json={"content": saved_text})
    assert saved.status_code == 200, saved.text
    saved_hash = saved.json()["content_hash"]
    assert saved_hash == hashlib.sha256(saved_text.encode()).hexdigest()
    checked_by_tools = case in {"invalid_utf8", "saved_docx", "over_cap"}
    if checked_by_tools or case == "pdf_no_hash":
        from app.services.content_input_receipts import latest_workspace_receipt, record_workspace_receipt
        from app.services.workspace_resource_authority import register_workspace_path

        if case == "saved_docx":
            import io
            from docx import Document

            document = Document()
            document.add_paragraph("Human saved Office correction")
            buffer = io.BytesIO()
            document.save(buffer)
            raw = buffer.getvalue()
        elif case == "over_cap":
            raw = b"LARGE_FILE_BYTES_MUST_NOT_BE_INLINED\n" * 250000
        elif case == "pdf_no_hash":
            raw = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
        else:
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

        n.monkeypatch.setattr("app.services.artifact_continuation.open_workspace_file", unavailable)
    reference = {"type": "workspace_file_ref", "agent_id": str(n.agent), "path": path,
                 "human_saved_hash": saved_hash}
    if case in {"pdf_no_hash", "no_hash_changed"}:
        reference.pop("human_saved_hash")
    if case in {"changed", "no_hash_changed"}:
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
    elif case == "deleted":
        (n.root / path).unlink()
    elif case == "unreceipted_change":
        (n.root / path).write_text("UNRECEIPTED_BYTES_MUST_NOT_REACH_MODEL")
    elif case == "symlink":
        (n.root / path).unlink()
        (n.root / path).symlink_to(n.target)
    references = [reference] * (30 if case == "repeated_refs" else 1)
    if case == "count_limit":
        for index in range(1, 9):
            extra_path = f"workspace/extra-{index}.md"
            extra = await n.client.put(file_url, params={"path": extra_path}, json={"content": f"EXTRA_BODY_{index}"})
            assert extra.status_code == 200, extra.text
            references.append(dict(reference, path=extra_path, human_saved_hash=extra.json()["content_hash"]))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, n.agent),
            user=await db.get(User, n.user), session=await db.get(ChatSession, n.session),
            content="请接着修改我保存的报告，也回答另外的问题。",
            parts=references,
            source="web", source_channel="web", requested_kind="steer_current_turn",
            expected_turn_id=f"turn-{n.run.hex}", expected_run_id=n.run)
        await db.commit()
        row = await db.get(SessionTurnInput, uuid.UUID(str(receipt["input_id"])))
        assert reference in row.content_parts_json
        if case == "rolled_over":
            from app.models.session_v2 import SessionCommand
            from app.services.session_human_input import rollover_terminal_steer
            from app.services.session_v2_persistence import resolve_session_mutation_authority

            authority = await resolve_session_mutation_authority(db, user=await db.get(User, n.user),
                agent_id=n.agent, session_id=n.session, action="mutate_session_input")
            await rollover_terminal_steer(db, authority=authority, row=row,
                command=await db.get(SessionCommand, row.command_id))
            old_run = await db.get(RuntimeTask, n.run)
            old_run.status = "completed"
            n.run = uuid.uuid4()
            turn_id = row.rolled_over_to_turn_id
            db.add(RuntimeTask(id=n.run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
                parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(n.session),
                child_session_id=str(n.session), root_user_id=n.user, root_session_id=str(n.session),
                root_runtime_task_id=n.run, prompt="Continue the saved report", metadata_json={"turn_id": turn_id}))
            await db.commit()
        else:
            turn_id = f"turn-{n.run.hex}"
    broadcast = AsyncMock()
    state = SimpleNamespace(agent=SimpleNamespace(id=n.agent, tenant_id=n.tenant),
        session_id=str(n.session), run_uuid=n.run, actor_user_id=n.user,
        metadata={"turn_id": turn_id,
            **({"session_v2_rolled_over_input_id": receipt["input_id"]} if case == "rolled_over" else {})},
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=lambda tenant:
            tenant_scoped_session(tenant, session_factory=app_user_sessionmaker)),
            events=SimpleNamespace(broadcast=broadcast)))
    from app.services.session_human_input import input_parts_to_runtime_messages

    root_messages = input_parts_to_runtime_messages([row]) if case == "rolled_over" else []
    messages = _mid_run_items_to_user_messages([*root_messages, *await _bind_session_round_inputs(state, 1)])
    wire_text = json.dumps([m.content for m in messages], ensure_ascii=False)
    assert "LATER_OTHER_VERSION_MUST_NOT_REACH_MODEL" not in wire_text
    assert "PRIVATE_FOREIGN_FILE_BODY" not in wire_text
    assert "INVALID_UTF8_FILE_BYTES_MUST_NOT_REACH_MODEL" not in wire_text
    assert "LARGE_FILE_BYTES_MUST_NOT_BE_INLINED" not in wire_text
    assert "UNRECEIPTED_BYTES_MUST_NOT_REACH_MODEL" not in wire_text
    inline = case in {"same", "repeated_refs", "rolled_over", "replay", "count_limit"}
    assert (saved_text in wire_text.replace("\\n", "\n")) is inline
    selected = [m for m in messages if m.context_origin and m.context_origin.kind == "selected_workspace_file"]
    assert len(selected) == (0 if case in {"pdf_no_hash", "no_hash_changed"} else 9 if case == "count_limit" else 1)
    if case == "count_limit":
        assert "EXTRA_BODY_7" in wire_text and "EXTRA_BODY_8" not in wire_text
        assert "remaining saved file references" in wire_text
    if checked_by_tools:
        assert "saved version was checked and is current" in wire_text
        assert "file tools" in wire_text
    assert len(wire_text) < 70000
    changed_notice = "文件已不是你保存时的版本，本次未使用它。请重新打开确认后再继续，其他问题可以继续处理。"
    unreadable_notice = "这个文件目前无法读取，本次未使用它。你可以继续处理其他问题，或选择有权打开的文件。"
    notice = changed_notice if case in {"changed", "forged_hash", "unreceipted_change"} else unreadable_notice
    has_notice = not inline and not checked_by_tools and case not in {"pdf_no_hash", "no_hash_changed"}
    if has_notice:
        assert notice in wire_text
        assert (changed_notice in wire_text) is (case in {"changed", "forged_hash", "unreceipted_change"})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, turn_id=turn_id, round_index=1,
            messages=messages, tools=[], provider="synthetic", model="d3-local")
        await db.commit()
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        captured = json.dumps(result.model_request_snapshot_json, ensure_ascii=False)
        assert "请接着修改我保存的报告，也回答另外的问题。" in captured, result.model_request_snapshot_json["input_bindings"]
        assert ("人修改的决定性内容" in captured) is inline
        assert "PRIVATE_FOREIGN_FILE_BODY" not in captured
        assert "INVALID_UTF8_FILE_BYTES_MUST_NOT_REACH_MODEL" not in captured
        assert (await db.get(RuntimeTask, n.run)).status == "running"
        notices = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == n.session, ChatTranscriptEvent.item_kind == "assistant_commentary")))
        if has_notice:
            assert notice in captured
            assert (changed_notice in captured) is (case in {"changed", "forged_hash", "unreceipted_change"})
            assert any(notice in event.content for event in notices)
            assert any(changed_notice in event.content for event in notices) is (case in {"changed", "forged_hash", "unreceipted_change"})
        else:
            assert notices == []
        if inline or checked_by_tools:
            assert any(m.context_origin and m.context_origin.sources[0]["source_kind"] == "workspace_artifact"
                       for m in messages)
        if case == "replay":
            # An already used round must keep its bound bytes after a later edit.
            result.state = "round_committed"
            result.seal_json = {"response": {"tool_calls": [{"id": "prior-effect"}]}}
            await db.commit()
    if case == "replay":
        changed = await n.client.put(file_url, params={"path": path}, json={
            "content": "LATER_OTHER_VERSION_MUST_NOT_REACH_MODEL", "expected_current_hash": saved_hash})
        assert changed.status_code == 200, changed.text
        reentered = _mid_run_items_to_user_messages(await _bind_session_round_inputs(state, 1))
        assert [m.content for m in reentered] == [m.content for m in messages]
        from app.services.session_model_inputs import message_snapshot
        from app.services.session_model_round import _committed_request_content_matches

        snapshot = dict(result.model_request_snapshot_json, messages=[message_snapshot(m) for m in reentered])
        assert _committed_request_content_matches(result, snapshot)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            replay_request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=n.session, run_id=n.run, turn_id=turn_id, round_index=1,
                messages=reentered, tools=[], provider="synthetic", model="d3-local")
            assert replay_request_id == request_id
            assert not list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == n.run, ChatTranscriptEvent.item_kind == "assistant_commentary")))


@pytest.mark.parametrize("source_access", ["member_grant", "revoked", "agent_only", "unverifiable"])
async def test_saved_derived_file_uses_the_same_source_reader_as_file_tools(
    native_workspace, app_user_sessionmaker, source_access,  # noqa: F811
):
    from datetime import datetime, timedelta, timezone

    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.models.session_v2 import SessionModelResult
    from app.models.user import User
    from app.services import artifact_continuation as continuation
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.personal_knowledge_service import PersonalKnowledgeService
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_model_round import prepare_model_request
    from app.services.web_chat_run_orchestrator import _bind_session_round_inputs
    from tests.integration.test_artifact_publication_transactions import stage_report

    n = native_workspace
    path = "workspace/saved-derived-report.md"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        imported = await PersonalKnowledgeService(data_root=n.root.parent, extractor=None).ingest_markdown(
            db, tenant_id=n.tenant, owner_user_id=n.foreign, title="Report source",
            markdown="# Source\nAuthorized source material\n", source_kind="paste")
        document = await db.get(KnowledgeDocument, imported.document_id)
        source = {"source_kind": "knowledge_document", "source_id": str(document.id),
                  "source_version": document.canonical_md_sha256}
        grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign,
            resource_type="document", resource_id=document.id, document_id=document.id,
            grantee_type="user", grantee_id=n.user, created_by_user_id=n.foreign, permission="read",
            sensitivity_ceiling="PL3_sensitive")
        db.add(grant)
        if source_access == "agent_only":
            grant.grantee_type, grant.grantee_id = "agent", n.agent
            grant.requester_user_id, grant.session_id = n.user, str(n.session)
            grant.purpose = "interactive_session"
            grant.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        intermediate = await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root, path="workspace/source-notes.md", dependencies=[source])
        dependency = {"source_kind": "workspace_artifact", "source_id": str(intermediate.id),
                      "source_version": intermediate.content_sha256}
        original = await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root, path=path, dependencies=[dependency])
        await db.commit()

    saved_text = "# Report\nHuman saved correction: keep this version.\n"
    saved = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": path}, json={
        "content": saved_text, "expected_current_hash": original.content_sha256})
    assert saved.status_code == 200, saved.text
    saved_hash = saved.json()["content_hash"]
    assert saved_hash == hashlib.sha256(saved_text.encode()).hexdigest()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        assert receipt.id != original.id and receipt.revision == original.revision + 1
        assert receipt.content_sha256 == saved_hash and receipt.dependencies_json == [dependency]
        if source_access == "revoked":
            (await db.get(KnowledgeGrant, grant.id)).revoked_at = datetime.now(timezone.utc)
        elif source_access == "unverifiable":
            (n.root.parent / document.canonical_md_path).unlink()
        accepted = await submit_live_human_input(db=db, agent=await db.get(Agent, n.agent),
            user=await db.get(User, n.user), session=await db.get(ChatSession, n.session),
            content="Continue on the version I saved; also answer my other question.",
            parts=[{"type": "workspace_file_ref", "agent_id": str(n.agent), "path": path,
                    "human_saved_hash": saved_hash}],
            source="web", source_channel="web", requested_kind="steer_current_turn",
            expected_turn_id=f"turn-{n.run.hex}", expected_run_id=n.run)
        assert accepted["input_id"]
        await db.commit()

    # Observe the exception handled by the real notice path without changing it.
    caught = []
    append_events = continuation.append_session_events

    async def observe_notice(*args, **kwargs):
        error = sys.exception()
        caught.append({"type": type(error).__name__, "code": getattr(error, "code", None),
                       "message": str(error), "cause": str(error.__cause__) if error else None})
        return await append_events(*args, **kwargs)

    n.monkeypatch.setattr(continuation, "append_session_events", observe_notice)
    state = SimpleNamespace(agent=SimpleNamespace(id=n.agent, tenant_id=n.tenant),
        session_id=str(n.session), run_uuid=n.run, actor_user_id=n.user,
        metadata={"turn_id": f"turn-{n.run.hex}"},
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=lambda tenant:
            tenant_scoped_session(tenant, session_factory=app_user_sessionmaker)),
            events=SimpleNamespace(broadcast=AsyncMock())))
    messages = _mid_run_items_to_user_messages(await _bind_session_round_inputs(state, 1))
    selected = [m for m in messages if m.context_origin and m.context_origin.kind == "selected_workspace_file"]
    assert len(selected) == 1
    allowed = source_access == "member_grant"
    tool_result = str(await n.service.execute("read_file", {"path": path}, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert (saved_text in tool_result.replace("\\n", "\n")) is allowed, tool_result
    notice = "这个文件目前无法读取，本次未使用它。你可以继续处理其他问题，或选择有权打开的文件。"
    assert (saved_text in selected[0].content) is allowed, caught
    assert (notice in selected[0].content) is (not allowed)
    assert bool(caught) is (not allowed)
    if not allowed:
        assert caught[0]["code"] == (
            "content_source_unavailable" if source_access == "unverifiable" else "content_source_access_denied"
        )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=1,
            messages=messages, tools=[], provider="synthetic", model="q2b-local")
        await db.commit()
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        snapshot = json.dumps(result.model_request_snapshot_json)
        assert (saved_text in snapshot.replace("\\n", "\n")) is allowed
        assert "also answer my other question" in snapshot
        notices = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == n.run, ChatTranscriptEvent.item_kind == "assistant_commentary")))
        assert any(notice in event.content for event in notices) is (not allowed)
