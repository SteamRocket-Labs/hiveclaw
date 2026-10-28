"""A retained ORM work row cannot preserve a superseded runtime reader."""

import pytest

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_runtime_reader_rechecks_a_retained_sessions_current_requester(
    native_workspace,  # noqa: F811
    owner_sessionmaker,
    app_user_sessionmaker,
):
    n = native_workspace
    request = ToolExecutionRequest(
        tool_name="write_file",
        arguments={"path": "workspace/report.md", "content": "Authorized output"},
        context=ToolExecutionContext(
            agent_id=n.agent,
            user_id=n.user,
            tenant_id=str(n.tenant),
            session_id=str(n.session),
            runtime_task_id=str(n.run),
            workspace=n.root,
        ),
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        retained = await db.get(ChatSession, n.session)
        principal, reader = await _runtime_input_actor(db, n.tenant, request)
        assert principal.agent_id == n.agent and reader.id == n.user
        assert retained.user_id == n.user
        await db.commit()

        async with owner_sessionmaker() as writer:
            (await writer.get(ChatSession, n.session)).user_id = n.foreign
            await writer.commit()
        try:
            # A fresh reader establishes the existing policy for the changed work.
            async with app_user_sessionmaker() as fresh:
                await pin_rls_tenant_context(fresh, n.tenant)
                with pytest.raises(ContentInputReceiptError) as current:
                    await _runtime_input_actor(fresh, n.tenant, request)
                assert current.value.status_code == 403
                assert current.value.code == "content_source_access_denied"

            await pin_rls_tenant_context(db, n.tenant)
            with pytest.raises(ContentInputReceiptError) as cached:
                await _runtime_input_actor(db, n.tenant, request)
            assert cached.value.status_code == 403
            assert cached.value.code == current.value.code
        finally:
            async with owner_sessionmaker() as writer:
                (await writer.get(ChatSession, n.session)).user_id = n.user
                await writer.commit()

        await pin_rls_tenant_context(db, n.tenant)
        principal, reader = await _runtime_input_actor(db, n.tenant, request)
        assert principal.agent_id == n.agent and reader.id == n.user
