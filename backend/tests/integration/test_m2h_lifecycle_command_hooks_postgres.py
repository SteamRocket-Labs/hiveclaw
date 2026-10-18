"""Lifecycle control references retain the subscriber, never Hook metadata."""

import asyncio
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize(
    "state", ["ready", "stopped", "revoked", "legacy", "group", "read_only", "inflight", "home_disabled"]
)
async def test_lifecycle_bus_uses_current_subscriber_and_canonical_history(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, state
):
    import app.database as database
    from app.models.audit import ChatMessage
    from app.models.agent import AgentPermission
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.models.tenant import Tenant
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookEvent, hook_registry
    from app.services import runtime_control_bus as bus

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    original = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    for key, value in {
        "HIVE_CODE_EXEC_PROVIDER": "local_os_sandbox",
        "HIVE_CODE_SANDBOX_MODE": "auto",
        "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0",
    }.items():
        monkeypatch.setenv(key, value)
    requester_id = seed["user_id"]
    async with owner_sessionmaker() as db:
        if state == "home_disabled":
            requester_id, home_tenant = uuid4(), uuid4()
            db.add(
                Tenant(
                    id=home_tenant,
                    name="Disabled principal home",
                    slug=f"disabled-home-{home_tenant.hex}",
                    is_active=False,
                )
            )
            await db.flush()
            db.add(
                User(
                    id=requester_id,
                    tenant_id=home_tenant,
                    role="platform_admin",
                    username=f"disabled-{requester_id.hex}",
                    email=f"{requester_id.hex}@lifecycle.example.com",
                    password_hash="x",
                    display_name="Disabled home principal",
                )
            )
            await db.flush()
        if state == "read_only":
            requester_id = uuid4()
            db.add(
                User(
                    id=requester_id,
                    tenant_id=seed["tenant_id"],
                    username=f"lifecycle-{requester_id.hex}",
                    email=f"{requester_id.hex}@lifecycle.example.com",
                    password_hash="x",
                    display_name="Read-only requester",
                )
            )
            await db.flush()
            (await db.get(ChatSession, seed["session_id"])).user_id = requester_id
            db.add(
                AgentPermission(
                    tenant_id=seed["tenant_id"],
                    agent_id=seed["agent_id"],
                    scope_type="user",
                    scope_id=requester_id,
                    access_level="read",
                )
            )
        if state == "group":
            session = await db.get(ChatSession, seed["session_id"])
            session.user_id = None
            session.audience_kind = "group"
            session.source_channel = "feishu"
            session.group_installation_ref = f"legacy:{uuid4()}"
            session.group_subject_id = "oc_lifecycle"
            (await db.get(User, seed["user_id"])).role = "org_admin"
        if state == "revoked":
            (await db.get(User, seed["user_id"])).is_active = False
        db.add(
            ChatMessage(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                user_id=requester_id,
                conversation_id=str(seed["session_id"]),
                role="user",
                content="CANONICAL_LIFECYCLE_HISTORY",
            )
        )
        db.add(
            ExecutionEnvironment(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                provider_key="local_os_sandbox",
                desired_state="stopped" if state == "stopped" else "ready",
                observed_state="stopped" if state == "stopped" else "ready",
            )
        )
        await db.commit()
    work, home = tmp_path / "lifecycle-work", tmp_path / "lifecycle-home"
    work.mkdir()
    home.mkdir()
    seen, events = [], []
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=work, allowed_env_vars={"HOME", "PATH"}),
        transcript_writer=events.append,
    )
    program = "import time; f=open('lifecycle.txt','w');\nfor _ in range(150): f.write('x'); f.flush(); time.sleep(.02)"
    command = (
        f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(program)}"
        if state == "inflight"
        else "printf LIFECYCLE_EFFECT > lifecycle.txt"
    )
    handler = runner.build_handler(
        HookSpec(
            key="subscriber-lifecycle",
            event=HookEvent.SESSION_IDLE,
            type="command",
            command=command,
            failure_mode="advisory",
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
    )

    async def lifecycle(context):
        seen.append(context.messages)
        if state != "group":
            forged = replace(
                context,
                agent_id=uuid4(),
                metadata={
                    "tenant_id": str(seed["tenant_id"]),
                    "user_id": str(seed["user_id"]),
                    "runtime_task_id": str(seed["run_id"]),
                },
            )
            if state != "inflight":
                return await handler(forged)
            pending = asyncio.create_task(handler(forged))
            try:
                async with asyncio.timeout(5):
                    while not (work / "lifecycle.txt").exists():
                        await asyncio.sleep(0.02)
                async with owner_sessionmaker() as db:
                    (await db.get(User, requester_id)).is_active = False
                    await db.commit()
                return await asyncio.wait_for(pending, 5)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    message = {
        "schema": bus.RUNTIME_CONTROL_SCHEMA,
        "type": "session_lifecycle_hook",
        "event": "session_idle",
        "agent_id": str(seed["agent_id"]),
        "session_id": str(seed["session_id"]),
        "source": "websocket",
        "metadata": {"tenant_id": str(uuid4()), "user_id": str(uuid4())},
    }
    if state != "legacy":
        message["origin"] = {
            "schema": "hive.session.lifecycle-origin.v1",
            "tenant_id": str(seed["tenant_id"]),
            "agent_id": str(seed["agent_id"]),
            "session_id": str(seed["session_id"]),
            "user_id": str(requester_id),
            "event": "session_idle",
        }
    hook_registry.register(HookEvent.SESSION_IDLE, lifecycle, key="subscriber-lifecycle", failure_mode="advisory")
    try:
        handled = await bus.handle_runtime_control_message(message)
    finally:
        hook_registry.unregister(HookEvent.SESSION_IDLE, lifecycle)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        leases = list(await db.scalars(select(EnvironmentLease)))
        if state in {"ready", "inflight"}:
            assert handled and (work / "lifecycle.txt").exists()
            assert len(leases) == 1 and leases[0].principal_id == requester_id and leases[0].runtime_task_id is None
            assert leases[0].outcome["source"]["session_id"] == str(seed["session_id"])
            if state == "inflight":
                assert leases[0].status == "released" and leases[0].outcome["resource_state"] == "stopped"
                before = (work / "lifecycle.txt").read_bytes()
                await asyncio.sleep(0.1)
                assert before == (work / "lifecycle.txt").read_bytes() and len(before) < 150
            else:
                assert (work / "lifecycle.txt").read_text() == "LIFECYCLE_EFFECT"
        else:
            assert not (work / "lifecycle.txt").exists(), "Lifecycle commands must retain current execution authority"
            assert leases == []
        if state in {"revoked", "legacy", "home_disabled"}:
            assert not handled and seen == []
        elif state == "group":
            assert handled and seen == [[]], "The worker must match the subscriber's empty group conversation"
        else:
            assert seen == [[{"role": "user", "content": "CANONICAL_LIFECYCLE_HISTORY"}]]
    (tmp_path / "lifecycle-observation.json").write_text(
        json.dumps(
            {
                "state": state,
                "handled": handled,
                "seen": seen,
                "events": events,
                "leases": [{"id": str(row.id), "outcome": row.outcome} for row in leases],
            },
            indent=2,
        )
    )


@pytest.mark.parametrize("process_role", ["api", "combined"])
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("global_requester", [False, True])
async def test_authenticated_websocket_idle_enters_the_same_governed_hook_path(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, process_role, stopped, global_requester
):
    import app.database as database
    from fastapi import WebSocketDisconnect
    from app.api import websocket as socket_api
    from app.core.security import create_access_token
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookEvent, hook_registry
    from app.services import memory_service, runtime_control_bus as bus
    from app.services.web_chat_broker import WebChatBroker

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    original = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    for key, value in {
        "HIVE_CODE_EXEC_PROVIDER": "local_os_sandbox",
        "HIVE_CODE_SANDBOX_MODE": "auto",
        "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0",
        "WS_IDLE_DREAM_SECONDS": "1",
    }.items():
        monkeypatch.setenv(key, value)
    requester_id, home_tenant = seed["user_id"], seed["tenant_id"]
    async with owner_sessionmaker() as db:
        if global_requester:
            requester_id, home_tenant = uuid4(), uuid4()
            db.add(
                Tenant(id=home_tenant, name="Lifecycle administrator home", slug=f"lifecycle-home-{home_tenant.hex}")
            )
            await db.flush()
            db.add(
                User(
                    id=requester_id,
                    tenant_id=home_tenant,
                    role="platform_admin",
                    username=f"lifecycle-{requester_id.hex}",
                    email=f"{requester_id.hex}@lifecycle.example.com",
                    password_hash="x",
                    display_name="Global lifecycle requester",
                )
            )
            await db.flush()
        (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
        for role in ("user", "assistant"):
            db.add(
                ChatMessage(
                    tenant_id=seed["tenant_id"],
                    agent_id=seed["agent_id"],
                    user_id=seed["user_id"],
                    conversation_id=str(seed["session_id"]),
                    role=role,
                    content=f"Stored {role} lifecycle message",
                )
            )
        db.add(
            ExecutionEnvironment(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                provider_key="local_os_sandbox",
                desired_state="stopped" if stopped else "ready",
                observed_state="stopped" if stopped else "ready",
            )
        )
        await db.commit()

    class Socket:
        def __init__(self):
            self.frames, self.closed = [], []
            self.subscribed = False
            self.done = asyncio.Event()

        async def accept(self):
            pass

        async def send_json(self, value):
            self.frames.append(value)

        async def close(self, code=1000, reason=""):
            self.closed.append({"code": code, "reason": reason})
            self.done.set()

        async def receive_json(self):
            if not self.subscribed:
                self.subscribed = True
                return {
                    "type": "session.subscribe",
                    "session_id": str(seed["session_id"]),
                    "schema_version": 2,
                    "after_sequence": 0,
                    "connection_attempt_id": "native-lifecycle-idle",
                }
            if self.done.is_set():
                raise WebSocketDisconnect()
            await asyncio.Event().wait()

    socket, published, broker = Socket(), [], WebChatBroker()

    class Redis:
        async def publish(self, _channel, value):
            message = json.loads(value)
            if message.get("type") == "session_lifecycle_hook":
                published.append(message)
                socket.done.set()
            return 1

    async def redis():
        return Redis()

    async def summary(*_args, **_kwargs):
        return "Synthetic summary at the external model boundary."

    monkeypatch.setattr(bus, "get_redis", redis)
    monkeypatch.setattr(socket_api, "_process_role", lambda: process_role)
    monkeypatch.setattr(socket_api, "manager", broker)
    monkeypatch.setattr(memory_service, "_generate_session_summary", summary)
    home = tmp_path / "home"
    home.mkdir()
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_env_vars={"HOME", "PATH"})
    )
    command = runner.build_handler(
        HookSpec(
            key="native-socket-idle",
            event=HookEvent.SESSION_IDLE,
            type="command",
            command="printf SOCKET_EFFECT > socket.txt",
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            failure_mode="advisory",
        )
    )

    async def on_idle(context):
        try:
            return await command(context)
        finally:
            socket.done.set()

    hook_registry.register(HookEvent.SESSION_IDLE, on_idle, key="native-socket-idle", failure_mode="advisory")
    try:
        token = create_access_token(
            str(requester_id), "platform_admin" if global_requester else "user", str(home_tenant)
        )
        await asyncio.wait_for(socket_api.websocket_chat(socket, seed["agent_id"], token, str(seed["session_id"])), 10)
        assert any(frame.get("type") == "session.ready" for frame in socket.frames), socket.frames
        if process_role == "api":
            assert len(published) == 1 and "messages" not in published[0]
            assert published[0]["origin"]["user_id"] == str(requester_id)
            assert await bus.handle_runtime_control_message(published[0])
        else:
            assert published == []
    finally:
        hook_registry.unregister(HookEvent.SESSION_IDLE, on_idle)
    assert not broker.active_connections.get(str(seed["agent_id"]))
    if global_requester:
        from app.models.audit import AuditLog

        async with owner_sessionmaker() as db:
            audit = list(
                await db.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "platform_security.tenant_impersonation",
                        AuditLog.details["actor"]["id"].as_string() == str(requester_id),
                    )
                )
            )
            assert len(audit) == 1 and audit[0].tenant_id is None
            assert audit[0].details["details"]["actor_home_tenant_id"] == str(home_tenant)
            assert audit[0].details["resource"]["id"] == str(seed["tenant_id"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        leases = list(await db.scalars(select(EnvironmentLease)))
        assert (await db.get(ChatSession, seed["session_id"])).user_id == seed["user_id"]
        if stopped:
            assert not (tmp_path / "socket.txt").exists() and leases == []
        else:
            assert (tmp_path / "socket.txt").read_text() == "SOCKET_EFFECT"
            assert len(leases) == 1 and leases[0].principal_id == requester_id and leases[0].status == "released"
