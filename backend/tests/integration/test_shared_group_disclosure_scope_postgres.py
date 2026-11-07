"""Keep exact source validation inside each native shared-work disclosure."""

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_call, group_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("entry", ["model", "history"])
async def test_native_disclosure_rechecks_changes_and_rejects_cycles(
    group_memory,  # noqa: F811
    owner_sessionmaker,
    app_user_sessionmaker,
    entry,
):
    from fastapi import HTTPException

    import app.services.content_input_receipts as receipts
    from app.database import pin_rls_tenant_context
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionToolInvocation
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.shared_group_authority import read_shared_group_history, validate_shared_group_model_inputs

    g = group_memory
    raw, invocation = await group_call(
        g,
        app_user_sessionmaker,
        "save_memory",
        {"content": "Remember our authorized shared operating guideline.", "category": "project"},
    )
    assert "Saved to explicit memory overlay" in raw
    reference = f"session-tool-result:{invocation.id}"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, g.n.tenant)
        session = await db.get(ChatSession, g.session)
        dependencies, complete = await _origin_dependencies(
            db,
            tenant_id=g.n.tenant,
            agent_id=g.n.agent,
            ref=reference,
        )
        assert complete
        tool = next(row for row in dependencies if row["source_kind"] == "session_tool_invocation")
        assert tool["source_id"] == str(invocation.id)
        history = await load_session_semantic_history(
            db,
            tenant_id=g.n.tenant,
            agent_id=g.n.agent,
            session_id=g.session,
        )
        group_ids = {message.group_id for message in history.messages if message.id == reference}
        assert len(group_ids) == 1
        messages = [message for message in history.messages if message.group_id in group_ids]
        assert messages
        async def expose():
            if entry == "model":
                await validate_shared_group_model_inputs(
                    db,
                    tenant_id=g.n.tenant,
                    agent_id=g.n.agent,
                    session_id=g.session,
                    run_id=g.runs[g.n.user],
                    input_bindings={
                        "dependency_coverage": {"complete": True},
                        "dependencies": dependencies,
                    },
                )
                return messages, []
            return await read_shared_group_history(
                db,
                session=session,
                messages=messages,
                run_id=g.runs[g.n.user],
            )

        allowed, held = await expose()
        assert allowed == messages and held == []
        async with owner_sessionmaker() as writer:
            current = await writer.get(SessionToolInvocation, invocation.id)
            result_event = current.result_event_id
            assert result_event is not None
            current.result_event_id = None
            await writer.commit()
        if entry == "model":
            with pytest.raises(receipts.ContentInputReceiptError) as changed:
                await expose()
            assert changed.value.status_code == 403
        else:
            allowed, held = await expose()
            assert allowed == [] and held and held[0]["code"] == "group_history_sources_unavailable"
        async with owner_sessionmaker() as writer:
            (await writer.get(SessionToolInvocation, invocation.id)).result_event_id = result_event
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

        # A successful sibling cannot hide a cyclic dependency.
        principal = AgentRuntimePrincipal(
            agent_id=g.n.agent,
            requester_user_id=g.n.user,
            session_id=str(g.session),
            runtime_task_id=str(g.runs[g.n.user]),
        )
        with receipts.group_source_read():
            await receipts._validate_input_dependencies(
                db,
                g.n.tenant,
                [tool],
                runtime_request=principal,
            )
            token = receipts._ACTIVE_GROUP_TOOL_SOURCES.set({(tool["source_id"], tool["source_version"])})
            try:
                with pytest.raises(receipts.ContentInputReceiptError, match="cyclic dependency") as cycle:
                    await receipts._validate_input_dependencies(
                        db,
                        g.n.tenant,
                        [tool],
                        runtime_request=principal,
                    )
                assert cycle.value.status_code == 503
            finally:
                receipts._ACTIVE_GROUP_TOOL_SOURCES.reset(token)
