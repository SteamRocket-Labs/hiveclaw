"""Real loopback Redis, PostgreSQL and CDP pipes; no browser or model calls."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
import redis.asyncio as redis_async
from sqlalchemy import select

from app.models.execution_environment import EnvironmentLease
from app.services.browser_control import BrowserControlError, BrowserControlService
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_control_postgres import call

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def browser_redis(browser_case, monkeypatch):
    import app.core.events as events
    import app.services.browser_control as control

    _, _, owner, _, root = browser_case
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        port = endpoint.getsockname()[1]
    executable = shutil.which("redis-server")
    assert executable is not None
    with (root / "redis.log").open("wb") as output:
        server = subprocess.Popen(
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(root),
                "--daemonize",
                "no",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        client = redis_async.Redis(host="127.0.0.1", port=port, decode_responses=True)
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        if await client.ping():
                            break
                    except (OSError, redis_async.ConnectionError):
                        pass
                    assert server.poll() is None
                    await asyncio.sleep(0.02)
            monkeypatch.setattr(events, "_redis_client", client)
            monkeypatch.setattr(control, "browser_control", owner)
            yield client
        finally:
            await client.aclose()
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=5)


async def test_remote_human_input_is_encrypted_and_wakes_the_real_owner(
    browser_case, browser_redis, owner_sessionmaker
):
    seed, source, owner, _, root = browser_case
    from app.services.runtime_control_bus import RUNTIME_CONTROL_CHANNEL, handle_runtime_control_message

    view = (await call(owner, source, "start"))["browser"]
    view = (await call(owner, source, "take_control", view, actor_kind="human"))["browser"]
    remote = BrowserControlService()
    subscription = browser_redis.pubsub()
    await subscription.subscribe(RUNTIME_CONTROL_CHANNEL)

    async def wake():
        async with asyncio.timeout(5):
            async for message in subscription.listen():
                if message["type"] == "message":
                    payload = json.loads(message["data"])
                    assert payload["type"] == "browser_control_request"
                    assert "synthetic-broker-secret" not in message["data"]
                    assert await handle_runtime_control_message(payload) is True
                    return

    listener = asyncio.create_task(wake())
    try:
        result = await call(
            remote, source, "type", view, actor_kind="human", arguments={"text": "synthetic-broker-secret"}
        )
        await listener
        assert result["browser"]["control_transport"] == "remote_rpc"
        assert result["browser"]["attached_here"] is False
        assert (root / view["id"] / "effects").read_text().splitlines() == ["synthetic-broker-secret"]
        async with owner_sessionmaker() as db:
            leases = list(
                await db.scalars(select(EnvironmentLease).where(EnvironmentLease.principal_id == seed["user_id"]))
            )
            rpc = [lease for lease in leases if (lease.outcome or {}).get("browser_rpc")]
            assert len(rpc) == 1 and rpc[0].status == "released"
            assert rpc[0].outcome["browser_rpc"]["phase"] == "finished"
            assert "synthetic-broker-secret" not in json.dumps([lease.outcome for lease in leases])
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await subscription.aclose()


async def test_dropped_wakeup_is_recovered_without_duplicate_dispatch(browser_case, browser_redis, monkeypatch):
    _, source, owner, _, root = browser_case
    import app.services.runtime_control_bus as bus

    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)

    async def dropped(_):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(bus, "publish_runtime_control_event", dropped)
    remote = BrowserControlService()
    result = await call(remote, source, "type", view, arguments={"text": "one-forwarded-effect"})
    assert result["browser"]["last_effect"]["effect"] == "acknowledged"
    assert (root / view["id"] / "effects").read_text().splitlines() == ["one-forwarded-effect"]
    assert (
        owner.enqueue_rpc(source.tenant_id, UUID(view["id"]), UUID(result["browser"]["last_effect"]["lease_id"]))
        is True
    )
    jobs = list(owner._rpc_jobs.values())
    await asyncio.gather(*jobs)
    assert (root / view["id"] / "effects").read_text().splitlines() == ["one-forwarded-effect"]


async def test_cancel_before_owner_claim_releases_the_writer_without_dispatch(
    browser_case, browser_redis, owner_sessionmaker, monkeypatch
):
    _, source, owner, _, root = browser_case
    import app.services.runtime_control_bus as bus

    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)
    monitor = owner._monitors[UUID(view["id"])]
    monitor.cancel()
    await asyncio.gather(monitor, return_exceptions=True)
    published = asyncio.Event()

    async def held(_):
        published.set()

    monkeypatch.setattr(bus, "publish_runtime_control_event", held)
    remote = BrowserControlService()
    pending = asyncio.create_task(call(remote, source, "type", view, arguments={"text": "cancel-before-claim"}))
    try:
        await asyncio.wait_for(published.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with owner_sessionmaker() as db:
            leases = list(
                await db.scalars(select(EnvironmentLease).where(EnvironmentLease.principal_id == source.user_id))
            )
            forwarded = [lease for lease in leases if (lease.outcome or {}).get("browser_rpc")]
            assert len(forwarded) == 1
            lease = forwarded[0]
            assert lease.status == "released"
            assert lease.outcome["resource_state"] == "not_created"
            assert lease.outcome["browser_rpc"]["phase"] == "cancelled"
            assert owner.enqueue_rpc(source.tenant_id, UUID(view["id"]), lease.id) is True
        await asyncio.gather(*list(owner._rpc_jobs.values()))
        assert not (root / view["id"] / "effects").exists()
        await call(owner, source, "type", view, arguments={"text": "new-admitted-effect"})
        assert (root / view["id"] / "effects").read_text().splitlines() == ["new-admitted-effect"]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_tampered_transient_input_cannot_dispatch(browser_case, browser_redis, monkeypatch):
    _, source, owner, _, root = browser_case
    import app.services.runtime_control_bus as bus
    from app.services.browser_rpc import _key

    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)

    async def tampered(event):
        await browser_redis.set(_key(source.tenant_id, event["lease_id"], "request"), "forged-ciphertext")
        owner.enqueue_rpc(source.tenant_id, event["resource_id"], event["lease_id"])

    monkeypatch.setattr(bus, "publish_runtime_control_event", tampered)
    with pytest.raises(BrowserControlError, match="browser_rpc_payload_unavailable") as failure:
        await call(BrowserControlService(), source, "type", view, arguments={"text": "must-not-run"})
    assert failure.value.effect == "not_started"
    assert not (root / view["id"] / "effects").exists()
    await call(owner, source, "type", view, arguments={"text": "fresh-local-effect"})
    assert (root / view["id"] / "effects").read_text().splitlines() == ["fresh-local-effect"]


async def test_missing_response_preserves_acknowledged_mechanics_without_replay(
    browser_case, browser_redis, monkeypatch, owner_sessionmaker
):
    _, source, owner, _, root = browser_case
    original = browser_redis.blpop
    discard = True

    async def lost(*args, **kwargs):
        nonlocal discard
        response = await original(*args, **kwargs)
        assert response is not None
        if discard:
            discard = False
            return None
        return response

    monkeypatch.setattr(browser_redis, "blpop", lost)
    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)
    request_id = uuid4()
    remote = BrowserControlService()
    with pytest.raises(BrowserControlError, match="browser_rpc_response_unconfirmed") as failure:
        await call(remote, source, "type", view, request_id=request_id, arguments={"text": "effect-before-loss"})
    assert failure.value.effect == "unknown"
    async with owner_sessionmaker() as db:
        forwarded = [
            row
            for row in await db.scalars(select(EnvironmentLease).where(EnvironmentLease.principal_id == source.user_id))
            if row.outcome.get("browser_rpc")
        ]
        assert len(forwarded) == 1 and forwarded[0].status == "released"
        assert forwarded[0].outcome["resource_state"] == "command_completed"
        assert forwarded[0].outcome["resource_receipt"]["effect"] == "acknowledged"
    with pytest.raises(BrowserControlError, match="browser_request_already_recorded"):
        await call(remote, source, "type", view, request_id=request_id, arguments={"text": "effect-before-loss"})
    assert (root / view["id"] / "effects").read_text().splitlines() == ["effect-before-loss"]


async def test_rpc_cannot_adopt_a_newer_runtime_claim(browser_case, browser_redis, owner_sessionmaker, monkeypatch):
    seed, source, owner, _, root = browser_case
    import app.services.runtime_control_bus as bus
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)
    monitor = owner._monitors[UUID(view["id"])]
    monitor.cancel()
    await asyncio.gather(monitor, return_exceptions=True)

    async def reclaimed(event):
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, seed["run_id"])
            task.claim_version = 2
            task.claimed_by = "replacement-worker"
            await db.commit()
        owner.enqueue_rpc(source.tenant_id, event["resource_id"], event["lease_id"])

    monkeypatch.setattr(bus, "publish_runtime_control_event", reclaimed)
    remote = BrowserControlService()
    with pytest.raises(BrowserControlError, match="browser_rpc_runtime_fence_unavailable"):
        await call(remote, source, "observe", view, runtime_task_id=seed["run_id"])
    token = set_runtime_task_fence(task_id=seed["run_id"], claim_version=1, worker_id="kernel-worker-1")
    try:
        with pytest.raises(BrowserControlError, match="environment_state_unavailable"):
            await call(remote, source, "type", view, runtime_task_id=seed["run_id"], arguments={"text": "stale-claim"})
    finally:
        reset_runtime_task_fence(token)
    await asyncio.gather(*list(owner._rpc_jobs.values()))
    assert not (root / view["id"] / "effects").exists()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.claim_version == 2 and task.claimed_by == "replacement-worker"
        forwarded = [
            row
            for row in await db.scalars(select(EnvironmentLease).where(EnvironmentLease.principal_id == source.user_id))
            if row.outcome.get("browser_rpc")
        ]
        assert len(forwarded) == 1
        assert forwarded[0].outcome["browser_rpc"]["task_fence"]["claim_version"] == 1


async def test_remote_reconciliation_uses_late_ack_and_stop_is_supervised(browser_case, browser_redis, monkeypatch):
    _, source, owner, _, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call

    async def short_timeout(method, parameters, **kwargs):
        if method == "Input.insertText":
            kwargs["timeout"] = 0.05
        return await original(method, parameters, **kwargs)

    monkeypatch.setattr(pipe, "call", short_timeout)
    remote = BrowserControlService()
    with pytest.raises(BrowserControlError, match="browser_response_timeout") as failure:
        await call(remote, source, "type", view, arguments={"text": "delayed-effect"})
    assert failure.value.effect == "unknown"
    lost = await remote.read(source)
    assert lost["status"] == "lost"
    queued = await call(remote, source, "reconcile", lost, actor_kind="human")
    assert queued["browser"]["reconciliation_pending"] is True
    async with asyncio.timeout(5):
        while True:
            recovered = await remote.read(source)
            if recovered["status"] == "ready":
                break
            await asyncio.sleep(0.05)
    assert recovered["reconciliation_pending"] is False
    assert recovered["control_mode"] == "paused" and recovered["fresh_observation_required"] is True
    assert recovered["last_effect"]["business_outcome"] == "unverified"
    assert (root / view["id"] / "effects").read_text().splitlines() == ["delayed-effect"]
    stopping = await call(remote, source, "stop", recovered, actor_kind="human")
    assert stopping["browser"]["desired_state"] == "stopped"
    assert stopping["browser"]["next_action"] == "wait_for_stop"
    pid = int((root / view["id"] / "pid").read_text())
    async with asyncio.timeout(5):
        while (await remote.read(source))["status"] != "stopped":
            await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


RPC_HOST = r"""
import asyncio,json,os,sys
from pathlib import Path
from uuid import UUID,uuid4
from app.config import Settings,get_settings
Settings.model_config['env_file']=None;get_settings.cache_clear()
from app.services.secrets_provider import init_secrets_provider
init_secrets_provider(os.environ['SECRETS_MASTER_KEY'])
from app.models import import_all_models
import_all_models()
import app.services.browser_control as module
from app.services.browser_control import BrowserSource
from app.services.browser_pipe import BrowserLaunchSpec
from app.services.runtime_control_bus import RUNTIME_CONTROL_CHANNEL,handle_runtime_control_message
from app.core.events import get_redis,close_redis
from tests.integration.test_m2ad_browser_control_postgres import CHILD
ids=json.loads(os.environ['BROWSER_TEST_SOURCE']);root=Path(sys.argv[1])
source=BrowserSource(*(UUID(ids[key]) for key in ('tenant_id','agent_id','user_id','session_id')))
def spec(resource,tenant,agent):
    folder=root/str(resource);folder.mkdir(exist_ok=True)
    return BrowserLaunchSpec(Path(sys.executable).resolve(),('-B','-c',CHILD,str(folder)),
        {'HOME':str(folder),'PATH':'/usr/bin:/bin'},folder)
module.browser_device_spec=spec
async def main():
    manager=module.browser_control;redis=await get_redis();sub=redis.pubsub()
    await sub.subscribe(RUNTIME_CONTROL_CHANNEL)
    async def listen():
        async for event in sub.listen():
            if event['type']=='message':await handle_runtime_control_message(json.loads(event['data']))
    listener=asyncio.create_task(listen())
    try:
        view=(await manager.execute(source,action='start',request_id=uuid4(),expected_revision=0,expected_epoch=0))['browser']
        print(json.dumps({'view':view,'owner':manager.instance_id,'pid':os.getpid()},default=str),flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        listener.cancel();await asyncio.gather(listener,return_exceptions=True)
        await manager.close();await sub.aclose();await close_redis()
asyncio.run(main())
"""


async def test_actual_separate_owner_process_routes_and_rechecks_control(browser_case, browser_redis, app_user_engine):
    seed, source, _, _, root = browser_case
    port = browser_redis.connection_pool.connection_kwargs["port"]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONPATH": os.pathsep.join(sys.path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": app_user_engine.url.render_as_string(hide_password=False),
        "REDIS_URL": f"redis://127.0.0.1:{port}/0",
        "SECRETS_MASTER_KEY": os.environ["SECRETS_MASTER_KEY"],
        "SECRET_KEY": "synthetic-browser-rpc-runtime",
        "JWT_SECRET_KEY": "synthetic-browser-rpc-runtime",
        "BROWSER_TEST_SOURCE": json.dumps(
            {key: str(seed[key]) for key in ("tenant_id", "agent_id", "user_id", "session_id")}
        ),
    }
    errors = root / "rpc-owner.stderr"
    with errors.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            RPC_HOST,
            str(root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            assert line, errors.read_text()
            started = json.loads(line)
            assert started["pid"] != os.getpid()
            remote = BrowserControlService()
            assert started["owner"] != remote.instance_id
            view = started["view"]
            observed = await call(remote, source, "observe", view)
            assert observed["browser"]["fresh_observation_required"] is False
            assert observed["browser"]["control_transport"] == "remote_rpc"
            view = (await call(remote, source, "take_control", view, actor_kind="human"))["browser"]
            with pytest.raises(BrowserControlError, match="browser_human_control_active"):
                await call(remote, source, "type", view, arguments={"text": "denied-agent-effect"})
            await call(remote, source, "type", view, actor_kind="human", arguments={"text": "separate-process-effect"})
            view = (await call(remote, source, "return_control", view, actor_kind="human"))["browser"]
            with pytest.raises(BrowserControlError, match="browser_fresh_observation_required"):
                await call(remote, source, "type", view, arguments={"text": "stale-observation"})
            await call(remote, source, "observe", view)
            await call(remote, source, "type", view, arguments={"text": "fresh-agent-effect"})
            assert (root / view["id"] / "effects").read_text().splitlines() == [
                "separate-process-effect",
                "fresh-agent-effect",
            ]
        finally:
            if process.returncode is None:
                process.stdin.write(b"stop\n")
                await process.stdin.drain()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        assert process.returncode == 0, errors.read_text()
