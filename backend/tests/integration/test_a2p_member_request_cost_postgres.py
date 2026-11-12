"""Real member endpoints, live JWT/RLS dependencies and a 2.35 ms SQL RTT."""

import inspect
import json
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core.security import create_access_token
from app.models.agent import AgentPermission
from app.models.chat_session import ChatSession
from app.models.user import User
from app.models.user_assistant_binding import UserAssistantBinding
from tests.integration.conftest import (  # noqa: F401
    app_user_engine, app_user_sessionmaker, owner_engine, owner_sessionmaker, migrated_pg_url,
)
from tests.integration.test_m1f_assistant_binding_postgres import _agent, _model
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
RTT = 0.00235
CEILINGS = {"create_run": 141, "upload": 18, "assistant": 35, "allow_once": 184, "deny": 85,
    "approve_post": 201, "deny_post": 85}


def _caller():
    from greenlet import getcurrent

    current, frame = getcurrent(), inspect.currentframe()
    while current is not None:
        while frame is not None:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if "/app/" in filename:
                return filename.split("/app/", 1)[1], frame.f_lineno, frame.f_code.co_name
            frame = frame.f_back
        current = current.parent
        frame = current.gr_frame if current is not None else None
    return "framework", 0, "framework"


@contextmanager
def _profile(sample):
    totals = defaultdict(int)
    sample["statements"] = {}

    def before(_conn, _cursor, statement, _params, _context, _many):
        if sample.get("finished"):
            return
        kind = statement.split(None, 1)[0].upper()
        key = (*_caller(), kind)
        totals[key] += 1
        sample["statements"].setdefault(key, " ".join(statement.split()))
        # A monotonic deadline avoids macOS timer coalescing at this short RTT.
        started = time.perf_counter()
        while time.perf_counter() < started + RTT:
            pass
        sample["injected_seconds"] += time.perf_counter() - started

    event.listen(Engine, "before_cursor_execute", before)
    try:
        yield totals
    finally:
        event.remove(Engine, "before_cursor_execute", before)


class _ResponseClock:
    """Stop the server clock and SQL counter at the final response body."""

    def __init__(self, app, sample):
        self.app, self.sample = app, sample

    async def __call__(self, scope, receive, send):
        started = time.perf_counter()

        async def observed(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.sample.update(finished=True, seconds=time.perf_counter() - started)
            await send(message)

        await self.app(scope, receive, observed)


@pytest.fixture
async def member_request(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, request):  # noqa: F811
    from app.api import assistant, chat_sessions, upload
    from app.config import get_settings
    from app.core.tenant_middleware import TenantMiddleware
    from app.services import agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    import app.database as database

    # Seal a local reference model result, without contacting a provider. The
    # permission resolve endpoints still execute a real local tool. Posting
    # matches the lab approval case, confined to this disposable database.
    callspec = getattr(request.node, "callspec", None)
    post = callspec is not None and callspec.params.get("endpoint", "").endswith("_post")
    seed = await _seed_waiting_batch(owner_sessionmaker,
        tool_name="plaza_create_post" if post else "get_current_time",
        tool_arguments={"content": "[A2p] Local approval fixture"} if post else {"timezone": "UTC"})
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(get_settings(), "BLOB_STORAGE_PROVIDER", "filesystem")
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_manager, "agent_manager", AgentManager())
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", tmp_path)
    root = tmp_path / str(seed.agent_id)
    (root / "workspace").mkdir(parents=True)
    for index in range(12):
        (root / f"workspace/report-{index}.csv").write_text("region,sales\nWest,42\n")
    async with owner_sessionmaker() as db:
        from app.models.agent import Agent
        from app.models.tool import Tool, AgentTool
        from app.models.tenant_tool_config import TenantToolConfig

        model = await _model(db, seed.tenant_id, label="A2p local reference")
        for index in range(3):
            await _model(db, seed.tenant_id, label=f"A2p alternate {index}")
        agent = await db.get(Agent, seed.agent_id)
        agent.primary_model_id = model.id
        agent.default_session_permission_mode = "default"
        other_id = uuid.uuid4()
        db.add(User(id=other_id, tenant_id=seed.tenant_id, username=other_id.hex,
            email=f"{other_id.hex}@fixture.test", password_hash="fixture", display_name="Other member", role="member"))
        await db.flush()
        for index in range(6):
            tool = Tool(tenant_id=seed.tenant_id, name=f"a2p_reference_{index}",
                display_name=f"Reference tool {index}", config={"region": "local"})
            db.add(tool)
            await db.flush()
            db.add(TenantToolConfig(tenant_id=seed.tenant_id, tool_id=tool.id, config={"region": "west"}))
            db.add(AgentTool(tenant_id=seed.tenant_id, agent_id=seed.agent_id, tool_id=tool.id, enabled=True))
        # 24 native employees: 8 owned, 8 shared, 8 inaccessible. ACLs are
        # stored rows; no permission resolver or endpoint is mocked.
        for index in range(23):
            candidate = await _agent(db, tenant_id=seed.tenant_id,
                owner_id=seed.user_id if index < 7 else other_id,
                name=f"A2p employee {index}", model_id=model.id)
            if 7 <= index < 15:
                db.add(AgentPermission(tenant_id=seed.tenant_id, agent_id=candidate.id,
                    scope_type="company", access_level="use"))
        db.add(UserAssistantBinding(tenant_id=seed.tenant_id, user_id=seed.user_id, agent_id=seed.agent_id,
            source="explicit_selection", status="active", owner_user_id_at_bind=seed.user_id,
            requester_user_id=seed.user_id))
        for index in range(60):
            db.add(ChatSession(tenant_id=seed.tenant_id, agent_id=seed.agent_id, user_id=seed.user_id,
                source_channel="web", title=f"Retained work {index}", last_message_at=datetime.now(timezone.utc)))
        await db.commit()
    async with app_user_sessionmaker() as db:
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, seed.tenant_id)
        assert tuple((await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()) == (False, False)
    app = FastAPI()
    app.add_middleware(TenantMiddleware)
    for router in (assistant.router, chat_sessions.router, upload.router):
        app.include_router(router, prefix="/api")
    sample = {"injected_seconds": 0.0}
    token = create_access_token(str(seed.user_id), "member", tenant_id=str(seed.tenant_id))
    async with AsyncClient(transport=ASGITransport(app=_ResponseClock(app, sample)), base_url="http://a2p",
        headers={"Authorization": f"Bearer {token}"}) as client:
        yield seed, client, sample, app_user_sessionmaker


@pytest.mark.parametrize("endpoint", CEILINGS)
async def test_member_request_statement_ceiling(member_request, endpoint):
    seed, client, sample, factory = member_request
    with _profile(sample) as totals:
        if endpoint == "create_run":
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/runs",
                json={"content": "Summarize the sales report", "input_id": str(uuid.uuid4())})
        elif endpoint == "upload":
            content = b"region,sales\n" + b"West,42\n" * 89
            assert len(content) == 725
            response = await client.post("/api/chat/upload", data={"agent_id": str(seed.agent_id)},
                files={"file": ("sales.csv", content, "text/csv")})
        elif endpoint == "assistant":
            response = await client.get("/api/assistant")
        else:
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}"
                f"/permissions/{seed.permission_ids[0]}/resolve", json={"action": {
                    "approve_post": "allow_once", "deny_post": "deny"}.get(endpoint, endpoint)})
    result = {"endpoint": endpoint, "statements": sum(totals.values()), "injected_rtt": RTT,
        "seconds": round(sample["seconds"], 4), "injected_seconds": round(sample["injected_seconds"], 6),
        "status": response.status_code, "shape": {"tenant_bots": 24, "sessions": 61,
            "workspace_files": 12, "pending_permissions": 1, "company_grants": 8,
            "enabled_models": 4, "configured_tools": 6},
        "by_function_kind": [{"file": f"app/{file}", "line": line, "function": function,
            "kind": kind, "statements": count, "sql": sample["statements"][(file, line, function, kind)]}
            for (file, line, function, kind), count in sorted(totals.items())]}
    print("A2P_REQUEST", json.dumps(result), flush=True)
    assert response.status_code == (201 if endpoint == "create_run" else 200), response.text
    body = response.json()
    if endpoint == "assistant":
        assert len(body["candidates"]) == 16
        assert body["agent"]["id"] == str(seed.agent_id)
    elif endpoint in {"allow_once", "deny", "approve_post", "deny_post"}:
        assert body["run_id"] == str(seed.run_id)
        assert body["run_status"] == "resumable", body
        if endpoint in {"allow_once", "approve_post", "deny_post"}:
            from app.database import tenant_scoped_session
            from app.models.session_v2 import SessionToolInvocation

            async with tenant_scoped_session(seed.tenant_id, session_factory=factory) as db:
                invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
                if endpoint != "deny_post":
                    assert invocation.effect_state == "effect_committed"
                if endpoint.endswith("_post"):
                    from sqlalchemy import func, select
                    from app.models.plaza import PlazaPost

                    count = await db.scalar(select(func.count()).select_from(PlazaPost).where(
                        PlazaPost.tenant_id == seed.tenant_id, PlazaPost.author_id == seed.agent_id))
                    assert count == (1 if endpoint == "approve_post" else 0)
    elif endpoint == "upload":
        assert body["size"] == 725 and body["original_attachment"]
        assert body["conversion"]["status"] == "converted", body
        assert body["personal_kb_candidate"]["status"] == "queued", body
    else:
        assert body["run"]["run_id"]
    assert result["statements"] <= CEILINGS[endpoint], result
    assert result["seconds"] <= 1.0, result


async def test_later_candidate_observes_revocation_within_the_same_request(
    member_request, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from sqlalchemy import delete, select
    from app.models.agent import Agent
    from app.services import assistant_binding

    seed, client, _sample, _factory = member_request
    async with owner_sessionmaker() as db:
        target = await db.scalar(select(Agent.id).where(Agent.tenant_id == seed.tenant_id,
            Agent.name == "A2p employee 7"))
    original = assistant_binding._eligible
    revoked = False

    async def between_checkpoints(db, user, agent, **kwargs):
        nonlocal revoked
        result = await original(db, user, agent, **kwargs)
        if not revoked:
            assert agent.id != target
            async with owner_sessionmaker() as writer:
                await writer.execute(delete(AgentPermission).where(AgentPermission.agent_id == target))
                await writer.commit()
            revoked = True
        return result

    monkeypatch.setattr(assistant_binding, "_eligible", between_checkpoints)
    response = await client.get("/api/assistant")
    assert response.status_code == 200, response.text
    assert revoked
    candidates = response.json()["candidates"]
    assert len(candidates) == 15
    assert str(target) not in {candidate["id"] for candidate in candidates}


async def test_candidate_sql_structure_is_built_once_without_reusing_rows(member_request, monkeypatch):
    from app.services import assistant_binding

    _seed, client, _sample, _factory = member_request
    original, built = assistant_binding.source_reader_statement, []

    def statement(*args, **kwargs):
        built.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(assistant_binding, "source_reader_statement", statement)
    response = await client.get("/api/assistant")
    assert response.status_code == 200
    assert len(response.json()["candidates"]) == 16
    assert len(built) == 1
