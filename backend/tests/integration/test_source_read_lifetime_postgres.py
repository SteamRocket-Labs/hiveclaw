"""Inherited source-read contexts must observe revoked native result receipts."""

import asyncio

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_call, group_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_inherited_disclosure_rechecks_a_revoked_native_result(
    group_memory,  # noqa: F811
    owner_sessionmaker,
    app_user_sessionmaker,
):
    from app.database import pin_rls_tenant_context
    from app.models.session_v2 import SessionToolInvocation
    from app.services import content_input_receipts as receipts
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.session_model_inputs import _origin_dependencies

    g = group_memory
    raw, invocation = await group_call(
        g,
        app_user_sessionmaker,
        "save_memory",
        {"content": "Use the approved shared delivery checklist.", "category": "project"},
    )
    assert "Saved to explicit memory overlay" in raw
    principal = AgentRuntimePrincipal(
        agent_id=g.n.agent,
        requester_user_id=g.n.user,
        session_id=str(g.session),
        runtime_task_id=str(g.runs[g.n.user]),
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, g.n.tenant)
        dependencies, complete = await _origin_dependencies(
            db, tenant_id=g.n.tenant, agent_id=g.n.agent, ref=f"session-tool-result:{invocation.id}"
        )
        assert complete
        source = next(row for row in dependencies if row["source_kind"] == "session_tool_invocation")
        assert source["source_id"] == str(invocation.id)
        release = asyncio.Event()

        async def read(reader_db):
            await receipts._validate_input_dependencies(reader_db, g.n.tenant, [source], runtime_request=principal)

        async def inherited_read():
            await release.wait()
            await read(db)

        with receipts.group_source_read():
            await read(db)
            child = asyncio.create_task(inherited_read())
        await db.commit()
        async with owner_sessionmaker() as writer:
            current = await writer.get(SessionToolInvocation, invocation.id)
            result_event = current.result_event_id
            assert result_event is not None
            current.result_event_id = None
            await writer.commit()
        try:
            async with app_user_sessionmaker() as fresh:
                await pin_rls_tenant_context(fresh, g.n.tenant)
                with pytest.raises(receipts.ContentInputReceiptError) as revoked:
                    await read(fresh)
                assert revoked.value.code == "content_source_changed"
                assert revoked.value.status_code == 403
            await pin_rls_tenant_context(db, g.n.tenant)
            release.set()
            with pytest.raises(receipts.ContentInputReceiptError) as inherited:
                await child
            assert inherited.value.code == revoked.value.code
            assert inherited.value.status_code == revoked.value.status_code
        finally:
            if not child.done():
                child.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await child
            async with owner_sessionmaker() as writer:
                (await writer.get(SessionToolInvocation, invocation.id)).result_event_id = result_event
                await writer.commit()
        await read(db)
