"""Exercise the sidebar route as an authenticated member on PostgreSQL."""

from datetime import datetime, timedelta, timezone
from statistics import median
from time import perf_counter
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.chat_sessions import router
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent, AgentPermission
from app.models.audit import ApprovalRequest, ChatMessage
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.tenant import Tenant
from app.models.user import User
from app.services.chat_transcript import append_session_event
from app.services.session_v2_persistence import SessionEventDraft, append_session_events
from tests.integration.test_memory_source_read_work_postgres import statement_count

URL = "/api/agents/sessions/sidebar"
NOW = datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)


@pytest.fixture
async def members(owner_sessionmaker):
    tenant = uuid4()
    member, other = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name="Sidebar company", slug=f"ux1-{tenant.hex}"))
        await db.flush()
        for user in (member, other):
            db.add(User(id=user, tenant_id=tenant, username=user.hex, email=f"{user.hex}@example.test",
                password_hash="fixture", display_name="Member", role="member"))
        await db.commit()
    return tenant, member, other


@pytest.fixture
async def sidebar_client(members, app_user_sessionmaker):
    tenant, member, _other = members
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def request_db():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    token = create_access_token(str(member), "member", str(tenant))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidebar.test",
        headers={"Authorization": f"Bearer {token}"}) as client:
        yield client


async def _agent(db, tenant, owner):
    agent = Agent(id=uuid4(), tenant_id=tenant, owner_user_id=owner, creator_id=owner, name="Employee")
    db.add(agent)
    await db.flush()
    return agent.id


async def _session(db, agent, tenant, user, at=NOW, **fields):
    session = ChatSession(id=uuid4(), agent_id=agent, tenant_id=tenant, user_id=user,
        created_at=at - timedelta(hours=1), last_message_at=at, **fields)
    db.add(session)
    await db.flush()
    return session.id


def _message(db, agent, tenant, user, session, content, *, role="user", at=NOW):
    db.add(ChatMessage(agent_id=agent, tenant_id=tenant, user_id=user, conversation_id=str(session),
        role=role, content=content, created_at=at))


async def _sidebar(client):
    response = await client.get(URL)
    assert response.status_code == 200, response.text
    return {row["agent_id"]: row for row in response.json()}


async def test_sidebar_selects_latest_visible_own_session_and_first_plain_line(owner_sessionmaker, members, sidebar_client):
    tenant, member, other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        older = await _session(db, agent, tenant, member, NOW - timedelta(hours=1))
        session = await _session(db, agent, tenant, member)
        _message(db, agent, tenant, member, older, "Older conversation", at=NOW - timedelta(hours=1))
        _message(db, agent, tenant, member, session, "Previous question", at=NOW - timedelta(minutes=1))
        _message(db, agent, tenant, member, session, f"## **Updated** [answer](https://example.test) {'界' * 90}\nSecond line",
            role="assistant")
        _message(db, agent, tenant, member, session, "Internal tool payload", role="tool_call", at=NOW + timedelta(minutes=1))
        _message(db, agent, tenant, member, session, " \n\t", role="assistant", at=NOW + timedelta(minutes=2))
        for fields in (
            {"transcript_metadata_json": {"removed_from_list_by": {str(member): NOW.isoformat()}}},
            {"audience_kind": "group"}, {"listed_surface": "operator"}, {"source_channel": "heartbeat"},
        ):
            hidden = await _session(db, agent, tenant, member, NOW + timedelta(hours=1), **fields)
            _message(db, agent, tenant, member, hidden, "Unlisted conversation")
        orphan = await _session(db, agent, tenant, member, NOW + timedelta(hours=1), source_channel="feishu")
        _message(db, agent, tenant, member, orphan, "Channel orphan", role="assistant")
        foreign = await _session(db, agent, tenant, other, NOW + timedelta(hours=2))
        _message(db, agent, tenant, other, foreign, "Another member's message")
        db.add(RuntimeTask(tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(foreign),
            task_type="web_chat_turn", status="failed", root_user_id=other))
        empty = await _agent(db, tenant, member)
        foreign_only = await _session(db, empty, tenant, other)
        _message(db, empty, tenant, other, foreign_only, "No member conversation here")
        await db.commit()

    rows = await _sidebar(sidebar_client)
    assert set(rows) == {str(agent)}
    row = rows[str(agent)]
    assert row["session_id"] == str(session)
    assert datetime.fromisoformat(row["last_message_at"]) == NOW
    assert row["preview"] == "Updated answer " + "界" * 64 + "…"
    assert row["state"] is None


async def test_sidebar_uses_live_employee_grants(owner_sessionmaker, members, sidebar_client):
    tenant, member, other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, other)
        session = await _session(db, agent, tenant, member)
        _message(db, agent, tenant, member, session, "My history with a shared employee")
        grant = AgentPermission(agent_id=agent, tenant_id=tenant, scope_type="user", scope_id=member)
        db.add(grant)
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["preview"] == "My history with a shared employee"
        await db.delete(grant)
        await db.commit()
        assert str(agent) not in await _sidebar(sidebar_client)


@pytest.mark.parametrize(("status", "expected"), [
    ("pending", "running"), ("running", "running"), ("resumable", "running"),
    ("suspended", "waiting"), ("failed", "failed"), ("completed", None), ("killed", None),
])
async def test_sidebar_projects_latest_run_status(owner_sessionmaker, members, sidebar_client, status, expected):
    tenant, member, _other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        session = await _session(db, agent, tenant, member)
        _message(db, agent, tenant, member, session, "Can you check this?")
        db.add_all([
            RuntimeTask(tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(session),
                task_type=kind, status=value, created_at=at, root_user_id=member)
            for kind, value, at in [
                ("web_chat_turn", "failed", NOW - timedelta(minutes=1)),
                ("web_chat_turn", status, NOW), ("workflow", "failed", NOW + timedelta(minutes=1)),
            ]
        ])
        await db.commit()
    rows = await _sidebar(sidebar_client)
    assert rows[str(agent)]["state"] == expected
    assert rows[str(agent)]["preview"] == "Can you check this?"


async def test_sidebar_waits_for_the_open_question_until_the_member_answers(owner_sessionmaker, members, sidebar_client):
    tenant, member, _other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        session = await _session(db, agent, tenant, member)
        run, question = uuid4(), uuid4()
        db.add(RuntimeTask(id=run, tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(session),
            task_type="web_chat_turn", status="completed", result_summary="awaiting_user_clarification", root_user_id=member))
        await db.flush()
        scope = {"level": "run", "session_id": str(session), "thread_id": str(session),
            "run_id": str(run), "turn_id": f"turn-{run.hex}"}
        payload = {"requested_by_user_id": str(member), "questions": [{"question": "Which one?"}], "blocking": True}
        await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session, drafts=[SessionEventDraft(
            item_id=question, item_kind="user_question", lifecycle="waiting", scope=scope,
            actor={"type": "assistant"}, payload=payload,
        )])
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["state"] == "waiting"
        await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session, drafts=[SessionEventDraft(
            item_id=question, item_kind="user_question", lifecycle="completed", scope=scope,
            actor={"type": "user", "id": str(member)}, payload={**payload, "answer_text": "The first one"},
        )])
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["state"] is None


async def test_sidebar_reads_visible_zero_copy_final(owner_sessionmaker, members, sidebar_client):
    tenant, member, _other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        user_sent_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session = await _session(db, agent, tenant, member, user_sent_at)
        _message(db, agent, tenant, member, session, "Original question", at=user_sent_at)
        run, result, item = uuid4(), uuid4(), uuid4()
        db.add(RuntimeTask(id=run, tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(session),
            task_type="web_chat_turn", status="completed", root_user_id=member))
        await db.flush()
        db.add(SessionModelResult(id=result, tenant_id=tenant, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_id="round-1", provider_request_id=uuid4().hex, state="round_committed",
            model_request_hash="fixture", model_request_snapshot_json={},
            seal_json={"semantic_content": "**The final reply**\nMore detail"}))
        await db.flush()
        await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session, drafts=[SessionEventDraft(
            item_id=item, item_kind="assistant_final", lifecycle="completed", result_id=result,
            scope={"level": "round", "session_id": str(session), "thread_id": str(session), "run_id": str(run),
                "turn_id": f"turn-{run.hex}", "round_id": "round-1"},
            actor={"type": "assistant"}, payload={"zero_copy": True},
        )])
        await db.commit()
    assert (await _sidebar(sidebar_client))[str(agent)]["preview"] == "The final reply"


@pytest.mark.parametrize("kind", ["permission", "approval"])
async def test_sidebar_waits_for_open_decision_even_between_runtime_status_updates(
    owner_sessionmaker, members, sidebar_client, kind,
):
    tenant, member, _other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        session = await _session(db, agent, tenant, member)
        run = uuid4()
        db.add(RuntimeTask(id=run, tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(session),
            task_type="web_chat_turn", status="running" if kind == "permission" else "completed", root_user_id=member))
        await db.flush()
        if kind == "permission":
            request_id = uuid4().hex
            db.add(SessionModelResult(id=uuid4(), tenant_id=tenant, session_id=session, run_id=run,
                turn_id=f"turn-{run.hex}", round_id="round-1", provider_request_id=request_id,
                model_request_hash="fixture", model_request_snapshot_json={}))
            await db.flush()
            decision = SessionToolInvocation(id=uuid4(), tenant_id=tenant, session_id=session, run_id=run,
                round_id="round-1", provider_request_id=request_id, provider_tool_use_id="tool-1", tool_name="edit_file",
                invocation_item_id=uuid4(), args_hash="fixture", authority_snapshot_hash="fixture",
                effect_idempotency_key=uuid4().hex, permission_item_id=uuid4(), permission_state="waiting")
        else:
            decision = ApprovalRequest(tenant_id=tenant, agent_id=agent, requested_by=member,
                status="pending", action_type="edit_file", execution_envelope={"runtime_task_id": str(run), "session_id": str(session)})
        db.add(decision)
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["state"] == "waiting"
        if kind == "permission":
            decision.permission_state = "approved"
        else:
            decision.status = "approved"
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["state"] == ("running" if kind == "permission" else None)


async def test_sidebar_keeps_the_rewind_hidden_suffix_out_of_the_preview(owner_sessionmaker, members, sidebar_client):
    tenant, member, _other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        session_id = await _session(db, agent, tenant, member)
        checkpoint = None
        for content in ("Kept message", "Rewind checkpoint", "Hidden reply"):
            event = await append_session_event(db=db, agent_id=agent, tenant_id=tenant, session_id=session_id,
                user_id=member, actor_type="user", event_type="user_message", role="user", content=content,
                bridge_to_t0=False, created_at=NOW)
            if content == "Rewind checkpoint":
                checkpoint = event.event_id
        session = await db.get(ChatSession, session_id)
        session.transcript_metadata_json = {"active_projection": {
            "projection_reason": "rewind", "checkpoint_event_id": str(checkpoint),
        }}
        await db.commit()
    assert (await _sidebar(sidebar_client))[str(agent)]["preview"] == "Kept message"


async def test_sidebar_and_active_run_agree_during_a_colleague_handoff(owner_sessionmaker, members, sidebar_client):
    tenant, member, _other = members
    async with owner_sessionmaker() as db:
        agent = await _agent(db, tenant, member)
        session = await _session(db, agent, tenant, member)
        parent, child = uuid4(), uuid4()
        db.add_all([
            RuntimeTask(id=parent, tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(session),
                task_type="web_chat_turn", status="completed", root_user_id=member,
                metadata_json={"terminal_execution_fence_ref": "fixture"}),
            RuntimeTask(id=child, tenant_id=tenant, parent_agent_id=agent, parent_session_id=str(session),
                task_type="delegation", status="running", root_user_id=member, root_runtime_task_id=parent),
        ])
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["state"] == "running"
        response = await sidebar_client.get(f"/api/agents/{agent}/sessions/{session}/runs/active")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "running"
        (await db.get(RuntimeTask, child)).status = "completed"
        await db.commit()
        assert (await _sidebar(sidebar_client))[str(agent)]["state"] is None


async def test_sidebar_sql_count_and_local_request_time(owner_sessionmaker, app_user_engine, members, sidebar_client):
    tenant, member, _other = members
    counts, seeded = [], 0
    for agents in (1, 5, 20):
        async with owner_sessionmaker() as db:
            for _ in range(agents - seeded):
                agent = await _agent(db, tenant, member)
                session = await _session(db, agent, tenant, member)
                _message(db, agent, tenant, member, session, "**A sidebar reply**", role="assistant")
            await db.commit()
        seeded = agents

        durations = []
        for _ in range(1 if agents == 5 else 5):
            with statement_count(app_user_engine) as statements:
                start = perf_counter()
                rows = await _sidebar(sidebar_client)
                durations.append((perf_counter() - start) * 1000)
            assert len(rows) == agents
            reads = sum(stmt.lstrip().startswith(("SELECT", "WITH")) for stmt in statements)
            counts.append(len(statements))
            assert reads == 2, "Expected one authentication SELECT and one sidebar read"
        print(f"UX1 agents={agents} cursor_statements={counts[-1]} reads={reads} "
            f"median_ms={median(durations):.2f} min_ms={min(durations):.2f} max_ms={max(durations):.2f} n={len(durations)}")
    assert len(set(counts)) == 1
