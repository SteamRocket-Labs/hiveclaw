"""Recheck revocation across a complete history disclosure."""

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_call, group_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_history_window_rechecks_revocation_across_native_protocol_groups(
    owner_sessionmaker,
    app_user_sessionmaker,
    group_memory,  # noqa: F811
):
    from fastapi import HTTPException

    import app.services.content_input_receipts as receipts
    from app.database import pin_rls_tenant_context
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.shared_group_authority import read_shared_group_history

    g = group_memory
    first, first_call = await group_call(
        g, app_user_sessionmaker, "save_memory", {"content": "Use the shared release checklist.", "category": "project"}
    )
    second, second_call = await group_call(
        g,
        app_user_sessionmaker,
        "save_memory",
        {"content": "Review the shared checklist before release.", "category": "project"},
        include_history=True,
    )
    assert "Saved to explicit memory overlay" in first and "Saved to explicit memory overlay" in second
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, g.n.tenant)
        session = await db.get(ChatSession, g.session)
        first_ref, second_ref = [f"session-tool-result:{row.id}" for row in (first_call, second_call)]
        first_sources, complete = await _origin_dependencies(
            db, tenant_id=g.n.tenant, agent_id=g.n.agent, ref=first_ref
        )
        assert complete
        command = next(row for row in first_sources if row["source_kind"] == "session_command")
        second_sources, complete = await _origin_dependencies(
            db, tenant_id=g.n.tenant, agent_id=g.n.agent, ref=second_ref
        )
        assert complete and command in second_sources
        history = await load_session_semantic_history(
            db, tenant_id=g.n.tenant, agent_id=g.n.agent, session_id=g.session
        )
        groups = {message.group_id for message in history.messages if message.id in {first_ref, second_ref}}
        assert len(groups) == 2
        messages = [message for message in history.messages if message.group_id in groups]
        async def expose():
            return await read_shared_group_history(db, session=session, messages=messages, run_id=g.runs[g.n.user])

        allowed, held = await expose()
        assert allowed == messages and held == []
        async with owner_sessionmaker() as writer:
            current = await writer.get(SessionToolInvocation, first_call.id)
            result_event = current.result_event_id
            assert result_event is not None
            current.result_event_id = None
            await writer.commit()
        allowed, held = await expose()
        assert allowed == [] and len(held) == 2
        assert {row["code"] for row in held} == {"group_history_sources_unavailable", "content_source_changed"}, held
        async with owner_sessionmaker() as writer:
            (await writer.get(SessionToolInvocation, first_call.id)).result_event_id = result_event
            await writer.commit()
        allowed, held = await expose()
        assert allowed == messages and held == []
        g.members.remove("memory-author")
        with pytest.raises((HTTPException, receipts.ContentInputReceiptError)) as departed:
            await expose()
        assert departed.value.status_code == 403
        g.members.add("memory-author")
        allowed, held = await expose()
        assert allowed == messages and held == []
