"""Batched round reads refresh retained ORM rows after committed changes."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.core.resource_authority import load_explicit_resource_grant_ids_by_action
from app.database import tenant_scoped_session
from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts, _mid_run_items_to_user_messages
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from app.services.chat_transcript import lock_transcript_session
from app.services.session_model_inputs import build_model_input_bindings
from app.services.session_model_round import prepare_model_request
from app.services.session_semantic_history import load_session_semantic_history
from app.services.web_chat_runtime import conversation_from_history_messages
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.services.test_session_input_control_v2 import _seed_session
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_locked_provider_result_binding_refreshes_retained_result(owner_sessionmaker, app_user_sessionmaker):
    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        request = await prepare_model_request(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            turn_id=f"turn-{run.hex}",
            round_index=1,
            messages=_mid_run_items_to_user_messages(bound),
            tools=[],
            provider="openai",
            model="synthetic-no-provider",
        )
        await _seal_round(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            turn_id=f"turn-{run.hex}",
            round_index=1,
            provider_request_id=request,
            content="Retained assistant bytes",
        )
        result_id = await db.scalar(
            select(SessionModelResult.id).where(SessionModelResult.provider_request_id == request)
        )
        await db.commit()

    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        retained = await db.get(SessionModelResult, result_id)
        assert retained.state in {"sealed", "round_committed"}
        assert retained.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"]
        history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
        messages = _dicts_to_llm_messages(
            conversation_from_history_messages([message for message in history.messages if message.role == "assistant"])
        )
        assert len(messages) == 1
        messages[0].source_ref = f"session-provider-result:{request}"

        # Commit before the reader takes the transcript lock, as a prior writer would.
        async with owner_sessionmaker() as writer:
            await writer.execute(
                update(SessionModelResult).where(SessionModelResult.id == result_id).values(state="failed")
            )
            await writer.commit()

        await lock_transcript_session(db, session_id=session)
        locked_session = await db.get(ChatSession, session)
        binding = await build_model_input_bindings(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            messages=messages,
            wire_messages=_llm_messages_to_dicts(messages),
            history=history,
            locked_session=locked_session,
        )
        assert retained.state == "failed"
        assert binding["messages"][0]["dependency_status"] == "incomplete"
        assert binding["dependency_coverage"]["complete"] is False


async def test_bulk_grant_read_sees_revocation_in_the_same_session(owner_sessionmaker, app_user_sessionmaker):
    tenant, user_id, _agent, _session, _run = await _seed_session(owner_sessionmaker)
    permission_id, resource_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ResourcePermission(
                id=permission_id,
                tenant_id=tenant,
                principal_type="user",
                principal_id=user_id,
                resource_type="workspace_file",
                resource_id=resource_id,
                actions=["read", "write"],
                conditions={},
                effect="allow",
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, user_id)
        retained = await db.get(ResourcePermission, permission_id)
        assert retained.revoked_at is None
        before = await load_explicit_resource_grant_ids_by_action(
            db, user=user, resource_kind="workspace_file", actions=("read", "write")
        )
        assert before == {"read": {resource_id}, "write": {resource_id}}

        async with owner_sessionmaker() as writer:
            await writer.execute(
                update(ResourcePermission)
                .where(ResourcePermission.id == permission_id)
                .values(revoked_at=datetime.now(timezone.utc))
            )
            await writer.commit()

        after = await load_explicit_resource_grant_ids_by_action(
            db, user=user, resource_kind="workspace_file", actions=("read", "write")
        )
        assert after == {"read": set(), "write": set()}
        assert retained.revoked_at is not None
