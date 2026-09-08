"""Session ``/branch`` command guards on a canonical completed V2 turn.

Production B4 (2026-09-09 ~04:32, retried 05:04 on df463a8d): a real seed run
completed with 39 canonical V2 events (checkpoint ``human_input.accepted``),
then a formal ``POST /agents/{agent}/commands/branch/execute`` with only
``{"title": ...}`` returned HTTP 500 for an ordinary member principal. The
recovered exception — ``redaction path must resolve to an existing object
field: /payload/content`` — is reproduced here through the real endpoint when
the prefix contains a redaction-bearing private-reasoning event: the branch
prefix copy preserves the V2 visibility contract, and the legacy
re-serialization boundary must honor it instead of crashing (fixed
2026-09-09 in ``session_event_contract.serialize_session_event``).
"""

from __future__ import annotations

import uuid
import json

from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services.session_v2_persistence import SessionEventDraft, append_session_events


def _session_scope(session_id) -> dict[str, str]:
    return {"level": "session", "session_id": str(session_id), "thread_id": str(session_id)}


def _run_scope(session_id, turn_id, run_id) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
    }


def _round_scope(session_id, turn_id, run_id, round_id) -> dict[str, str]:
    return {
        "level": "round",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
        "round_id": round_id,
    }


async def _seed(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Branch V2 Tenant", slug=f"branch-v2-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"branch-v2-{suffix}",
                email=f"branch-v2-{suffix}@example.test",
                password_hash="x",
                display_name="Branch V2 Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Branch V2 Agent {suffix}",
            role_description="Runs the branch command regression.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"branch-v2-{suffix}",
            session_kind="human_chat",
            runtime_source="web_chat",
            source_channel="web",
        )
        db.add(session)
        await db.commit()
        return agent.id, session.id, tenant_id, user_id


async def _seed_completed_v2_turn(owner_sessionmaker, seed) -> None:
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    agent_id, session_id, tenant_id, user_id = seed
    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()
    round_id = f"round-{uuid.uuid4().hex[:8]}"
    assistant_text = "The current amount is 13."
    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        authority = await resolve_session_mutation_authority(
            db,
            user=user,
            agent_id=agent_id,
            session_id=session_id,
            action="mutate_session_input",
        )
        # Real canonical HumanInput acceptance: command + aggregate + admitted
        # checkpoint events through the authoritative V2 boundary.
        await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "queue_next_turn",
                "input_id": str(input_id),
                "idempotency_key": f"branch-v2-{input_id}",
                "session_id": str(session_id),
                "content_parts": [{"type": "text", "text": "current amount is 13"}],
            },
        )
        # The run authority row the run/round-scope events reference.
        from app.models.runtime_task import RuntimeTask

        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                status="completed",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                tenant_id=tenant_id,
                root_idempotency_key=f"branch-v2-run-{run_id}",
            )
        )
        await db.flush()
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=run_id,
                    item_kind="run",
                    lifecycle="starting",
                    scope=_run_scope(session_id, turn_id, run_id),
                    actor={"type": "runtime"},
                    payload={"reason": "user_turn"},
                ),
                SessionEventDraft(
                    item_id=run_id,
                    item_kind="run",
                    lifecycle="running",
                    scope=_run_scope(session_id, turn_id, run_id),
                    actor={"type": "runtime"},
                    payload={"reason": "user_turn"},
                ),
                # Production B4 shape (exact 2026-09-08T21:04:53 exception):
                # a private-reasoning stream row carries
                # redaction_paths=["/payload/content"]; the branch prefix copy
                # used to crash the legacy re-serialization with
                # "redaction path must resolve to an existing object field".
                SessionEventDraft(
                    item_id=uuid.uuid5(run_id, "assistant-reasoning-private:0"),
                    item_kind="assistant_reasoning_private",
                    lifecycle="delta",
                    scope=_round_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    visibility={"audience": "private_provider", "redaction_paths": ["/payload/content"]},
                    payload={
                        "phase": "reasoning_private",
                        "content": "private chain of thought",
                        "block_index": 0,
                    },
                ),
                SessionEventDraft(
                    item_id=uuid.uuid5(run_id, "assistant-visible-text:0"),
                    item_kind="assistant_text",
                    lifecycle="snapshot",
                    scope=_round_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    payload={"phase": "unknown", "content": assistant_text, "block_index": 0},
                ),
                SessionEventDraft(
                    item_id=uuid.uuid5(run_id, "assistant-visible-text:0"),
                    item_kind="assistant_text",
                    lifecycle="completed",
                    scope=_round_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    payload={"phase": "unknown", "content": "", "block_index": 0},
                ),
                SessionEventDraft(
                    item_id=uuid.uuid5(run_id, "assistant-final:0"),
                    item_kind="assistant_final",
                    lifecycle="completed",
                    scope=_round_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    payload={"phase": "final", "content": assistant_text},
                ),
                SessionEventDraft(
                    item_id=run_id,
                    item_kind="run",
                    lifecycle="completed",
                    scope=_run_scope(session_id, turn_id, run_id),
                    actor={"type": "runtime"},
                    payload={"reason": "completed"},
                ),
            ],
        )
        await db.commit()


async def test_branch_command_on_completed_v2_turn(owner_sessionmaker) -> None:
    from app.services.session_command_runtime import SessionCommandContext, execute_session_command

    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed)
    agent_id, session_id, tenant_id, user_id = seed

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        result = await execute_session_command(
            context=SessionCommandContext(
                db=db,
                agent=agent,
                user=user,
                access_level="owner",
                session_id=str(session_id),
                arguments={"title": "WRC-FUNCTIONAL-B4 Command branch"},
            ),
            command_name="branch",
        )
        await db.commit()

    assert result["action"] == "branch_created"
    branch_session_id = result["session_id"]
    assert str(branch_session_id) != str(session_id)

    async with owner_sessionmaker() as db:
        branch = await db.get(ChatSession, branch_session_id)
        assert branch is not None
        assert branch.parent_session_id == session_id
        copied = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.session_id == branch_session_id)
                    .order_by(ChatTranscriptEvent.sequence.asc())
                )
            ).scalars()
        )
        assert copied, "branch prefix projection must copy canonical prefix events"
        user_rows = [e for e in copied if str(e.event_type or "").startswith("human_input.")]
        assert user_rows and user_rows[0].content, "branch prefix must carry the user checkpoint content"
        # The session_branch control event is recorded on the SOURCE session,
        # not on the branch copy.
        source_control = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type == "session_branch",
                    )
                )
            ).scalars()
        )
        assert source_control, "source session must record the branch control event"
        # The redaction-bearing private-reasoning row must survive the prefix
        # copy with its exact bytes (operator truth) and stay redacted in the
        # user projection — the production 2026-09-08T21:04:53 exception shape.
        from app.services.session_event_contract import serialize_session_event

        copied_private = [e for e in copied if str(e.event_type or "").startswith("assistant_reasoning_private.")]
        assert copied_private, "branch prefix must preserve the private-reasoning event"
        operator_view = serialize_session_event(copied_private[0], audience="operator")
        assert operator_view["payload"]["content"] == "private chain of thought"
        user_view = serialize_session_event(copied_private[0], audience="user")
        assert "content" not in user_view["payload"]
        assert "private chain of thought" not in json.dumps(user_view)
        assert operator_view["payload"]["metadata"]["v2_payload"]["content"] == "private chain of thought"
        recorded_redactions = set(user_view.get("redacted_fields") or []) | set(
            user_view.get("visibility", {}).get("redacted_fields") or []
        )
        assert "/payload/content" in recorded_redactions


async def test_branch_execute_endpoint_on_completed_v2_turn(owner_sessionmaker) -> None:
    """Production shape: formal POST /agents/{agent}/commands/branch/execute
    (origin=web, session + title arguments) must not return HTTP 500."""
    import httpx
    from app.database import get_db
    from app.main import app as fastapi_app

    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed)
    agent_id, session_id, tenant_id, user_id = seed

    async def _override_db():
        async with owner_sessionmaker() as db:
            yield db

    async def _override_user():
        async with owner_sessionmaker() as db:
            user = await db.get(User, user_id)
            db.expunge(user)
            return user

    from app.core.security import get_current_user

    fastapi_app.dependency_overrides[get_db] = _override_db
    fastapi_app.dependency_overrides[get_current_user] = _override_user
    try:
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/agents/{agent_id}/commands/branch/execute",
                json={
                    "origin": "web",
                    "session_id": str(session_id),
                    "arguments": {"title": "WRC-FUNCTIONAL-B4-20260909 Command branch"},
                },
            )
        assert response.status_code == 200, f"branch execute failed: {response.status_code} {response.text}"
        body = response.json()
        assert body["ok"] is True
    finally:
        fastapi_app.dependency_overrides.clear()


async def test_branch_execute_endpoint_as_ordinary_member_owner(owner_sessionmaker) -> None:
    """Production shape: an ordinary member (non-admin) user owns the agent and
    session — main's B4 principal — and POSTs the formal branch/execute
    command through the real API handler. This is the exact production
    failure shape: pre-fix it raises the recovered 2026-09-08T21:04:53
    exception, post-fix it must succeed."""
    import httpx
    from app.database import get_db
    from app.main import app as fastapi_app

    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed)
    agent_id, session_id, tenant_id, user_id = seed
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        await db.commit()

    async def _override_db():
        async with owner_sessionmaker() as db:
            yield db

    async def _override_user():
        async with owner_sessionmaker() as db:
            user = await db.get(User, user_id)
            db.expunge(user)
            return user

    from app.core.security import get_current_user

    fastapi_app.dependency_overrides[get_db] = _override_db
    fastapi_app.dependency_overrides[get_current_user] = _override_user
    try:
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/agents/{agent_id}/commands/branch/execute",
                json={
                    "origin": "web",
                    "session_id": str(session_id),
                    "arguments": {"title": "WRC-FUNCTIONAL-B4-20260909 Member branch"},
                },
            )
        assert response.status_code == 200, f"branch execute failed: {response.status_code} {response.text}"
        assert response.json()["ok"] is True
    finally:
        fastapi_app.dependency_overrides.clear()


async def test_rewind_command_on_completed_v2_turn(owner_sessionmaker) -> None:
    """Guard the shared anchor/checkpoint boundary used by /rewind."""
    from app.services.session_command_runtime import SessionCommandContext, execute_session_command

    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed)
    agent_id, session_id, tenant_id, user_id = seed

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        result = await execute_session_command(
            context=SessionCommandContext(
                db=db,
                agent=agent,
                user=user,
                access_level="owner",
                session_id=str(session_id),
                arguments={},
            ),
            command_name="rewind",
        )
        await db.commit()

    assert result["action"] in {"rewound", "rewind_status", "open_checkpoint_selector"}
