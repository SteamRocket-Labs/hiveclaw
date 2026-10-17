"""Native knowledge results retain the read version in durable Session V2 evidence."""

import json
import uuid

import pytest

from app.database import pin_rls_tenant_context
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("tool, metadata_only", [("read_personal_kb", False), ("search_personal_kb", False), ("read_personal_kb", True)])
async def test_real_knowledge_tool_version_survives_native_settlement_and_replay(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool, metadata_only,
):
    from app.config import get_settings
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.knowledge import KnowledgeDocument
    from app.services import agent_tools
    from app.services.knowledge_provenance import KNOWLEDGE_PROVENANCE_KEY, load_transcript_knowledge_provenance
    from app.services.personal_knowledge_service import PersonalKnowledgeService
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation
    from app.services.web_chat_runtime import _knowledge_tool_replay_projection

    tenant, user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        imported = await PersonalKnowledgeService(data_root=tmp_path, extractor=None).ingest_markdown(db,
            tenant_id=tenant, owner_user_id=user, title="Versioned knowledge",
            markdown="# Input\nVERSIONED_PRIVATE_INPUT\n", source_kind="paste", sensitivity="PL3_sensitive",
            doc_metadata={"source_version": "forged-from-document-metadata"})
        await db.commit()
        document = await db.get(KnowledgeDocument, imported.document_id)
        version = document.canonical_md_sha256
        assert version and version != document.source_sha256
        arguments = {"document_id": str(document.id)} if tool == "read_personal_kb" else {"query": "VERSIONED_PRIVATE_INPUT"}
        if metadata_only:
            arguments["segment_ids"] = [str(uuid.uuid4())]
        turn, call = f"turn-{run.hex}", f"knowledge-{run.hex}"
        tools = [{"type": "function", "function": {"name": tool}}]
        messages = [{"role": "user", "content": "Read the authorized input"}]
        request_id = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1, messages=messages, tools=tools,
            provider="openai", model="synthetic-no-provider")
        await seal_model_response(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=turn, round_index=1, provider_request_id=request_id, response={"content": None,
                "tool_calls": [{"id": call, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}],
                "finish_reason": "tool_calls", "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        await commit_sealed_model_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=turn, round_index=1, provider_request_id=request_id)
        invocation = await prepare_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, provider_request_id=request_id, provider_tool_use_id=call, tool_name=tool, arguments=arguments)
        await db.commit()
        trace = {}
        raw = await agent_tools._get_tool_runtime_service().execute(tool, arguments, agent_id=agent,
            user_id=user, session_id=str(session), runtime_task_id=str(run), turn_id=turn, tool_call_id=call,
            emit_runtime_hooks=False, trace_metadata_sink=trace)
        payload = json.loads(str(raw))
        assert payload["status"] == "ok", payload
        seen_versions = [row["source_version"] for row in payload["results"]] if tool.startswith("search_") else [payload["source_version"]]
        assert seen_versions and set(seen_versions) == {version}
        assert ("VERSIONED_PRIVATE_INPUT" in str(raw)) is not metadata_only
        assert not metadata_only or payload["title"] == "Versioned knowledge"
        document.canonical_md_sha256 = "f" * 64
        await db.commit()
        replay = _knowledge_tool_replay_projection(tool_name=tool, args=arguments, raw_result=str(raw))
        events = await complete_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id, provider_result_content=str(raw), model_visible_content=replay,
            execution_evidence=_execution_evidence(trace))
        await db.commit()
        event = next(row for row in events if row.item_kind == "tool_result")
        event_id, sequence = event.id, event.sequence
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        event = await db.get(ChatTranscriptEvent, event_id)
        provenance = event.metadata_json.get(KNOWLEDGE_PROVENANCE_KEY)
        assert provenance is not None, event.metadata_json
        assert {source["source_version"] for source in provenance["sources"]} == {version}
        assert provenance["tool_name"] == tool and provenance["max_sensitivity"] == "PL3_sensitive"
        assert json.loads(event.metadata_json["v2_payload"]["model_visible_content"])["references"][0]["source_version"] == version
        if metadata_only:
            assert provenance["coverage"]["result_count"] == 0 and provenance["coverage"]["source_count"] == 1
            assert json.loads(event.metadata_json["v2_payload"]["model_visible_content"])["result_count"] == 0
        aggregate = await load_transcript_knowledge_provenance(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=run)
        assert aggregate and aggregate["source_event_refs"] == [f"transcript://event/{event_id}"]
        assert await load_transcript_knowledge_provenance(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=run, before_sequence=sequence) is None
        assert await load_transcript_knowledge_provenance(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=uuid.uuid4()) is None
        retried = await complete_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id, provider_result_content="Changed late result", execution_evidence=_execution_evidence(trace))
        await db.commit()
        same = next(row for row in retried if row.item_kind == "tool_result")
        assert same.id == event_id and same.content == str(raw)
        assert same.metadata_json[KNOWLEDGE_PROVENANCE_KEY] == provenance


@pytest.mark.parametrize("phase", ["content_rebuild", "sensitivity_change"])
async def test_concurrent_rebuild_cannot_pair_old_version_with_new_segments(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, phase,
):
    from app.models.knowledge import KnowledgeDocument
    from app.services.personal_knowledge_access import HumanBrowserPrincipal
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant, user, _agent, _session, _run = await _seed_session(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=None)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        first = await service.ingest_markdown(db, tenant_id=tenant, owner_user_id=user,
            title="Atomic knowledge read", markdown="# Input\nOLD_SNAPSHOT\n", source_kind="paste")
        await db.commit()
        execute = db.execute
        interleaved = False
        rebuilt_version = None

        async def rebuild_after_document_query(statement, *args, **kwargs):
            nonlocal interleaved, rebuilt_version
            result = await execute(statement, *args, **kwargs)
            target = KnowledgeDocument if phase == "content_rebuild" else KnowledgeDocument.sensitivity
            if not interleaved and any(column.get("expr") is target
                for column in getattr(statement, "column_descriptions", ())):
                interleaved = True
                async with app_user_sessionmaker() as writer:
                    await pin_rls_tenant_context(writer, tenant)
                    second = await service.ingest_markdown(writer, tenant_id=tenant, owner_user_id=user,
                        title="Atomic knowledge read", markdown="# Input\nNEW_SNAPSHOT\n", source_kind="paste",
                        source_sha256=first.source_sha256,
                        sensitivity="PL3_sensitive" if phase == "sensitivity_change" else "internal")
                    await writer.commit()
                    assert second.document_id == first.document_id and second.artifact_hash != first.artifact_hash
                    rebuilt_version = second.artifact_hash
            return result

        monkeypatch.setattr(db, "execute", rebuild_after_document_query)
        result = await service.get_personal_document_with_authority(db, tenant_id=tenant,
            owner_user_id=user, document_id=first.document_id, principal=HumanBrowserPrincipal(user_id=user))
        assert interleaved and result.status == "ok"
        if phase == "sensitivity_change":
            assert result.document.canonical_md_sha256 == rebuilt_version
            assert result.document.sensitivity == result.authority.document_sensitivity == "PL3_sensitive"
            assert "NEW_SNAPSHOT" in "\n".join(segment.content for segment in result.document.segments)
            return
        assert result.document.canonical_md_sha256 == first.artifact_hash
        assert "OLD_SNAPSHOT" in "\n".join(segment.content for segment in result.document.segments)
        assert all("NEW_SNAPSHOT" not in segment.content for segment in result.document.segments)


async def test_search_does_not_attach_a_cached_document_version_to_fresh_segments(
    owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    from app.models.knowledge import KnowledgeDocument
    from app.services.personal_knowledge_access import HumanBrowserPrincipal
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant, user, _agent, _session, _run = await _seed_session(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=None)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        first = await service.ingest_markdown(db, tenant_id=tenant, owner_user_id=user,
            title="Versioned search", markdown="# Input\nOLD_SEARCH_INPUT\n", source_kind="paste")
        await db.commit()
        cached = await db.get(KnowledgeDocument, first.document_id)
        async with app_user_sessionmaker() as writer:
            await pin_rls_tenant_context(writer, tenant)
            second = await service.ingest_markdown(writer, tenant_id=tenant, owner_user_id=user,
                title="Versioned search", markdown="# Input\nNEW_SEARCH_INPUT\n", source_kind="paste",
                source_sha256=first.source_sha256)
            await writer.commit()
        assert cached.canonical_md_sha256 == first.artifact_hash != second.artifact_hash
        hits = await service.search_personal(db, tenant_id=tenant, owner_user_id=user,
            query="NEW_SEARCH_INPUT", principal=HumanBrowserPrincipal(user_id=user))
        assert hits and {hit.source_version for hit in hits} == {second.artifact_hash}
        assert all("NEW_SEARCH_INPUT" in hit.snippet for hit in hits)
