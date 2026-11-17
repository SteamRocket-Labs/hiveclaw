"""Native member send statement budget, with real PostgreSQL and no provider network."""
import inspect
import json
import sys
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
_ACTIVE_METER = ContextVar("rl0c_active_meter", default=None)
SEND_STATEMENT_CEILING = 550


def caller():
    from greenlet import getcurrent
    current, frame = getcurrent(), inspect.currentframe()
    while current is not None:
        while frame is not None:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if "/app/" in filename:
                return "app/" + filename.split("/app/", 1)[1]
            frame = frame.f_back
        current = current.parent
        frame = current.gr_frame if current is not None else None
    return "framework"


class Meter:
    def __init__(self):
        self.rows = defaultdict(lambda: {"count": 0, "db_ms": 0.0, "errors": 0})
        self.started = None
        self.points = {}

    def mark(self, name):
        if name in self.points:
            return
        rows = {name: dict(row) for name, row in self.rows.items()}
        self.points[name] = {"wall_ms": (time.perf_counter() - self.started) * 1000,
            "modules": rows, "statements": sum(row["count"] for row in rows.values()),
            "db_ms": sum(row["db_ms"] for row in rows.values())}

    @contextmanager
    def measure(self):
        def before(_conn, _cursor, _statement, _params, context, _many):
            if _ACTIVE_METER.get() is not self:
                return
            module = caller()
            self.rows[module]["count"] += 1
            context.rl0c = (self, module, time.perf_counter())

        def finish(context, failed=False):
            saved = getattr(context, "rl0c", None)
            if saved is None or saved[0] is not self:
                return
            _, module, started = saved
            context.rl0c = None
            self.rows[module]["db_ms"] += (time.perf_counter() - started) * 1000
            self.rows[module]["errors"] += int(failed)

        def after(_conn, _cursor, _statement, _params, context, _many):
            finish(context)

        def error(context):
            finish(context.execution_context, True)

        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)
        event.listen(Engine, "handle_error", error)
        self.started = time.perf_counter()
        token = _ACTIVE_METER.set(self)
        try:
            yield self
        finally:
            _ACTIVE_METER.reset(token)
            event.remove(Engine, "before_cursor_execute", before)
            event.remove(Engine, "after_cursor_execute", after)
            event.remove(Engine, "handle_error", error)


def emit(item, trial, meter, **metadata):
    record = {"item": item, "trial": trial, "warmup": trial == 0, "points": meter.points, **metadata}
    print("SP1_SEND", json.dumps(record), flush=True)


async def configure(member_request, owner, factory, monkeypatch, tmp_path):
    from app.services import agent_context, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider
    import app.database as database
    from sqlalchemy.ext.asyncio import async_sessionmaker

    seed, client, _sample, _factory = member_request
    # Route every captured DB factory to the same real tenant-RLS role. The
    # owner factory is used only for seeding and explicit worker claim setup.
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if isinstance(value, async_sessionmaker):
                    monkeypatch.setattr(module, name, factory)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    root_path = tmp_path / str(seed.agent_id)
    held_path = tmp_path / "a2p-seed-files"
    root_path.rename(held_path)
    async with owner() as db:
        agent = await db.get(Agent, seed.agent_id)
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        (await db.get(User, seed.user_id)).role = "member"
        await AgentManager().initialize_agent_files(db, agent, original_author_id=seed.user_id)
        for capability in ("workspace.file.write", "workspace.file.read", "agent.memory.read", "agent.memory.write"):
            db.add(CapabilityPolicy(tenant_id=seed.tenant_id, capability=capability, allowed=True, requires_approval=False))
        task = await db.get(RuntimeTask, seed.run_id)
        task.metadata_json = {**task.metadata_json, "user_id": str(seed.user_id), "source": "websocket_message"}
        from app.services.web_chat_runtime import _create_runtime_budget_root_run_for_chat
        from app.services.runtime_budget_failover import apply_runtime_budget_root_binding
        binding = await _create_runtime_budget_root_run_for_chat(db=db, agent=agent,
            user=await db.get(User, seed.user_id), session=await db.get(ChatSession, seed.session_id),
            run_uuid=seed.run_id, source="web", profile="web_chat_turn", interactive=True)
        task.budget_run_id, task.budget_admission_status = binding.budget_run_id, "root"
        task.metadata_json = apply_runtime_budget_root_binding(task.metadata_json, binding)
        await db.commit()
    for item in (held_path / "workspace").iterdir():
        item.rename(root_path / "workspace" / item.name)
    (held_path / "workspace").rmdir()
    held_path.rmdir()
    async def no_wakeup(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with tenant_scoped_session(seed.tenant_id, session_factory=factory) as db:
        assert tuple((await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()) == (False, False)
        assert (await db.get(User, seed.user_id)).role == "member"
    return seed, client


async def new_work(seed, owner):
    session, run = uuid.uuid4(), uuid.uuid4()
    async with owner() as db:
        db.add(ChatSession(id=session, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            user_id=seed.user_id, source_channel="web", audience_kind="private"))
        await db.flush()
        db.add(RuntimeTask(id=run, tenant_id=seed.tenant_id, task_type="web_chat_turn", status="running",
            parent_agent_id=seed.agent_id, child_agent_id=seed.agent_id, parent_session_id=str(session),
            child_session_id=str(session), root_session_id=str(session), root_user_id=seed.user_id,
            root_runtime_task_id=run, prompt="Prepare the authorized report.", metadata_json={"turn_id": f"turn-{run.hex}"}))
        await db.commit()
    return session, run


async def claim(seed, run, owner):
    async with owner() as db:
        task = await db.get(RuntimeTask, run)
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "rl0c", 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()


async def execute(run):
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    fence = set_runtime_task_fence(task_id=run, worker_id="rl0c", claim_version=1)
    try:
        await execute_web_chat_run(run)
    finally:
        reset_runtime_task_fence(fence)


class Provider(ScriptedLLMClient):
    def __init__(self, meter):
        super().__init__([{"content": "The local result is ready.", "stream_text": "The local result is ready."}])
        self.meter = meter

    async def stream(self, **kwargs):
        self.meter.mark("provider")
        return await super().stream(**kwargs)


async def seed_history(seed, session, run, owner, factory):
    from app.services.session_model_round import bind_round_inputs, prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.kernel.engine import _mid_run_items_to_user_messages
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority
    from app.runtime.hooks import HookResult
    async def allow(**_kwargs):
        return HookResult()
    async with tenant_scoped_session(seed.tenant_id, session_factory=factory) as db:
        for index in range(1, 21):
            authority = await _authority(db, user_id=seed.user_id, agent_id=seed.agent_id, session_id=session)
            receipt = await _accepted_input(db, authority=authority, session_id=session, run_id=run,
                kind="steer_current_turn", content=f"Earlier round {index}: report the local value.")
            await run_user_prompt_admission(db, authority=authority, input_id=receipt.input_id,
                worker_id="rl0c-seed", hook_executor=allow)
            await queue_admitted_human_input(db, authority=authority, input_id=receipt.input_id)
            args = dict(tenant_id=seed.tenant_id, agent_id=seed.agent_id, session_id=session, run_id=run,
                turn_id=f"turn-{run.hex}", round_index=index)
            bound = await bind_round_inputs(db, **args)
            request_id = await prepare_model_request(db, **args, messages=_mid_run_items_to_user_messages(bound),
                tools=[], provider="openai", model="local-reference")
            await seal_model_response(db, **args, provider_request_id=request_id, response={"content": f"Earlier committed answer {index}.",
                "tool_calls": [], "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
            await commit_sealed_model_round(db, **args, provider_request_id=request_id)
            await db.commit()
    async with owner() as db:
        assert await db.scalar(select(func.count()).select_from(SessionModelResult).where(
            SessionModelResult.session_id == session, SessionModelResult.state == "round_committed")) == 20
        task = await db.get(RuntimeTask, run)
        task.status = "completed"
        from tests.services.test_session_input_control_v2 import _mark_web_terminal_boundary_delivered
        await _mark_web_terminal_boundary_delivered(db, task=task, agent_id=seed.agent_id, session_id=session)
        await db.commit()


@pytest.mark.parametrize("trial", range(4))
async def test_send_statement_budget(member_request, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, trial):
    import app.runtime.invoker as invoker
    import app.services.web_chat_runtime as runtime
    seed, client = await configure(member_request, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    session, old_run = await new_work(seed, owner_sessionmaker)
    await seed_history(seed, session, old_run, owner_sessionmaker, app_user_sessionmaker)
    meter = Meter()
    provider = Provider(meter)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    class Socket:
        async def accept(self):
            pass
        async def send_json(self, payload):
            kind = payload.get("item_kind")
            if kind in {"assistant_text", "assistant_commentary", "assistant_final"} and (payload.get("payload") or {}).get("content"):
                meter.mark("visible")
    socket = Socket()
    await runtime.web_chat_broker.connect(str(seed.agent_id), socket, str(session))
    try:
        with meter.measure():
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/{session}/runs",
                json={"content": "Continue the report.", "input_id": str(uuid.uuid4())})
            meter.mark("http")
            assert response.status_code == 201, {k: v for k,v in response.json().items() if k in {"status", "code", "reason"}}
            run = uuid.UUID(response.json()["run_id"])
            # The native worker claim setup is excluded from request accounting.
            with event_pause(meter):
                await claim(seed, run, owner_sessionmaker)
            await execute(run)
        history_wire = json.dumps(provider.requests[0]["messages"])
        history_seen = sum(f"Earlier committed answer {index}." in history_wire for index in range(1, 21))
        emit("send", trial, meter, status=response.status_code, committed_history=20,
            provider_history_answers=history_seen, provider_calls=len(provider.requests))
        assert history_seen == 20, history_seen
        assert meter.points["provider"]["statements"] <= SEND_STATEMENT_CEILING, meter.points["provider"]
        assert "provider" in meter.points and "visible" in meter.points, list(meter.points)
    finally:
        await runtime.web_chat_broker.disconnect(str(seed.agent_id), socket)


@contextmanager
def event_pause(meter):
    """Exclude only test-owned worker claim setup, including its wall time."""
    token = _ACTIVE_METER.set(None)
    start = time.perf_counter()
    try:
        yield
    finally:
        meter.started += time.perf_counter() - start
        _ACTIVE_METER.reset(token)




@pytest.mark.parametrize("trial", range(4))
async def test_ten_concurrent_agent_charges(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, trial,
):
    import asyncio
    from app.models.tenant import Tenant
    from app.models.token_usage_event import TokenUsageEvent
    from app.services.token_tracker import record_token_usage
    import app.database as database

    tenant_id = uuid.uuid4()
    users, agents = [uuid.uuid4() for _ in range(10)], [uuid.uuid4() for _ in range(10)]
    previous = datetime(2025, 12, 31, tzinfo=timezone.utc)
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Concurrent charges", slug=tenant_id.hex,
            tokens_used_today=20, tokens_used_month=30, tokens_used_total=100, tokens_reset_at=previous))
        for user_id in users:
            db.add(User(id=user_id, tenant_id=tenant_id, username=user_id.hex,
                email=f"{user_id.hex}@sp1.test", password_hash="fixture", display_name="Charge owner",
                tokens_used_today=5, tokens_used_month=6, tokens_used_total=7, tokens_reset_at=previous))
        await db.flush()
        for agent_id, user_id in zip(agents, users, strict=True):
            db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Charge agent", creator_id=user_id,
                owner_user_id=user_id, tokens_used_today=8, tokens_used_month=9, tokens_used_total=11,
                last_daily_reset=previous, last_monthly_reset=previous))
        await db.commit()
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    start = asyncio.Event()
    finished = asyncio.Event()
    observations = []

    async def charge(index):
        await start.wait()
        return await record_token_usage(agents[index], index + 1, user_id=users[index], tenant_id=tenant_id,
            idempotency_key=f"sp1-{agents[index]}", raise_on_error=True)

    async def observe(db):
        # This task is outside the measured charge context. The only polling
        # is the explicitly requested local PostgreSQL lock observation.
        while not finished.is_set():
            rows = (await db.execute(text("""
                SELECT a.pid, a.wait_event_type, a.wait_event, l.locktype, l.mode,
                       l.granted, l.relation::regclass::text AS relation
                FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid
                WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
                  AND (NOT l.granted OR l.relation IN
                       ('tenants'::regclass, 'users'::regclass, 'agents'::regclass))
            """))).mappings().all()
            observations.append([dict(row) for row in rows])
            await db.commit()
            await asyncio.sleep(0.001)

    async with owner_sessionmaker() as observer:
        await observer.execute(text("SELECT 1"))
        watcher = asyncio.create_task(observe(observer))
        meter = Meter()
        try:
            with meter.measure():
                charges = [asyncio.create_task(charge(index)) for index in range(10)]
                start.set()
                outcomes = await asyncio.wait_for(asyncio.gather(*charges), timeout=20)
                meter.mark("charges")
        finally:
            finished.set()
            await watcher
    point = meter.points["charges"]
    summary = {"item": "concurrency", "trial": trial, "warmup": trial == 0,
        **point, "samples": len(observations),
        "peak_waiting_pids": max((len({r['pid'] for r in rows if r['wait_event_type'] == 'Lock'})
            for rows in observations), default=0),
        "ungranted_transactionid_sharelock": sum(r['locktype'] == 'transactionid'
            and r['mode'] == 'ShareLock' and not r['granted'] for rows in observations for r in rows),
        "tenant_tuple_lock_observations": sum(r['locktype'] == 'tuple' and r['relation'] == 'tenants'
            for rows in observations for r in rows),
    }
    print("SP1_CONCURRENCY", json.dumps(summary), flush=True)
    assert outcomes == [True] * 10
    async with owner_sessionmaker() as db:
        tenant = await db.get(Tenant, tenant_id)
        assert (tenant.tokens_used_today, tenant.tokens_used_month, tenant.tokens_used_total) == (55, 55, 155)
        for index, (agent_id, user_id) in enumerate(zip(agents, users, strict=True), 1):
            user = await db.get(User, user_id)
            agent = await db.get(Agent, agent_id)
            assert (user.tokens_used_today, user.tokens_used_month, user.tokens_used_total) == (index, index, 7 + index)
            assert (agent.tokens_used_today, agent.tokens_used_month, agent.tokens_used_total) == (index, index, 11 + index)
        assert await db.scalar(select(func.count()).select_from(TokenUsageEvent).where(
            TokenUsageEvent.tenant_id == tenant_id)) == 10
    assert point["statements"] <= 62, summary


async def test_charge_event_failure_rolls_back_all_counters(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.tenant import Tenant
    from app.models.token_usage_event import TokenUsageEvent
    from app.services.token_tracker import record_token_usage
    from tests.integration.test_web_chat_worker_restart_post_commit_accounting import _seed_offboarding_fixture
    import app.database as database

    seed = await _seed_offboarding_fixture(owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)

    def fail_event(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("INSERT INTO token_usage_events"):
            raise RuntimeError("SP1 injected charge event failure")

    event.listen(Engine, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError, match="SP1 injected charge event failure"):
            await record_token_usage(seed["agent_id"], 17, tenant_id=seed["tenant_id"],
                user_id=seed["user_id"], idempotency_key="sp1-rollback", raise_on_error=True)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_event)
    async with owner_sessionmaker() as db:
        for kind, key in ((Tenant, "tenant_id"), (User, "user_id"), (Agent, "agent_id")):
            row = await db.get(kind, seed[key])
            assert (row.tokens_used_today, row.tokens_used_month, row.tokens_used_total) == (0, 0, 0)
        assert await db.scalar(select(func.count()).select_from(TokenUsageEvent).where(
            TokenUsageEvent.agent_id == seed["agent_id"])) == 0
    assert await record_token_usage(seed["agent_id"], 17, tenant_id=seed["tenant_id"],
        user_id=seed["user_id"], idempotency_key="sp1-rollback", raise_on_error=True) is True
    assert await record_token_usage(seed["agent_id"], 17, tenant_id=seed["tenant_id"],
        user_id=seed["user_id"], idempotency_key="sp1-rollback", raise_on_error=True) is False


@pytest.mark.parametrize("reset_at, expected", [
    (None, (12, 16, 18)),
    (datetime(2026, 1, 2, tzinfo=timezone.utc), (12, 16, 18)),
    (datetime(2026, 1, 1, tzinfo=timezone.utc), (5, 16, 18)),
    (datetime(2025, 12, 31, tzinfo=timezone.utc), (5, 5, 18)),
    (datetime(2027, 1, 2, tzinfo=timezone.utc), (12, 5, 18)),
])
async def test_atomic_charge_preserves_counter_reset_boundaries(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, reset_at, expected,
):
    from app.models.tenant import Tenant
    from app.services import token_tracker
    from tests.integration.test_web_chat_worker_restart_post_commit_accounting import _seed_offboarding_fixture
    import app.database as database

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 2, 12, tzinfo=timezone.utc)

    seed = await _seed_offboarding_fixture(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        for kind, key in ((Tenant, "tenant_id"), (User, "user_id"), (Agent, "agent_id")):
            row = await db.get(kind, seed[key])
            row.tokens_used_today, row.tokens_used_month, row.tokens_used_total = 7, 11, 13
            if kind is Agent:
                row.last_daily_reset = row.last_monthly_reset = reset_at
            else:
                row.tokens_reset_at = reset_at
        await db.commit()
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(token_tracker, "datetime", Clock)
    assert await token_tracker.record_token_usage(seed["agent_id"], 5, tenant_id=seed["tenant_id"],
        idempotency_key="sp1-reset", raise_on_error=True) is True
    async with owner_sessionmaker() as db:
        for kind, key in ((Tenant, "tenant_id"), (User, "user_id"), (Agent, "agent_id")):
            row = await db.get(kind, seed[key])
            assert (row.tokens_used_today, row.tokens_used_month, row.tokens_used_total) == expected
