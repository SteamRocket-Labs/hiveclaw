"""Unavailable or expired RPC ownership never recreates an admitted effect."""

import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.environment_browser import EnvironmentBrowser
from app.models.execution_environment import EnvironmentLease
from app.services.browser_control import BrowserControlError, BrowserControlService
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_control_postgres import call
from tests.integration.test_m2ad_browser_rpc_postgres import RPC_HOST
from tests.integration.test_m2ad_browser_rpc_postgres import browser_redis as browser_redis

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("expired", [False, True])
async def test_unclaimed_response_loss_withdraws_the_request_before_a_late_owner(
    browser_case, browser_redis, owner_sessionmaker, monkeypatch, expired
):
    import app.services.runtime_control_bus as bus
    _, source, owner, _, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)
    monitor = owner._monitors[UUID(view["id"])]
    monitor.cancel()
    await asyncio.gather(monitor, return_exceptions=True)
    event = {}
    async def unclaimed(payload):
        event.update(payload)
        if expired:
            async with owner_sessionmaker() as db:
                (await db.get(EnvironmentLease, UUID(payload["lease_id"]))).expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
    original = browser_redis.blpop
    async def bounded_wait(key, **_kwargs):
        return await original(key, timeout=1)
    monkeypatch.setattr(bus, "publish_runtime_control_event", unclaimed)
    monkeypatch.setattr(browser_redis, "blpop", bounded_wait)
    remote = BrowserControlService()
    request_id = uuid4()
    with pytest.raises(BrowserControlError) as failure:
        await call(remote, source, "type", view, request_id=request_id, arguments={"text": "withdrawn-before-claim"})
    assert failure.value.effect == "unknown"
    async with owner_sessionmaker() as db:
        lease = await db.get(EnvironmentLease, UUID(event["lease_id"]))
        assert lease.status == "released" and lease.outcome["resource_state"] == "not_created"
        assert lease.outcome["browser_rpc"]["phase"] == "cancelled"
    assert owner.enqueue_rpc(source.tenant_id, UUID(view["id"]), UUID(event["lease_id"]))
    await asyncio.gather(*list(owner._rpc_jobs.values()))
    assert not (root / view["id"] / "effects").exists()
    with pytest.raises(BrowserControlError, match="browser_request_already_recorded"):
        await call(remote, source, "type", view, request_id=request_id, arguments={"text": "withdrawn-before-claim"})
    await call(owner, source, "type", view, arguments={"text": "new-authorized-request"})
    assert (root / view["id"] / "effects").read_text().splitlines() == ["new-authorized-request"]


async def test_expiry_after_effect_preserves_unknown_and_never_resets_execution_to_pending(
    browser_case, browser_redis, owner_sessionmaker, monkeypatch
):
    import app.services.runtime_control_bus as bus
    _, source, owner, _, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    await call(owner, source, "observe", view)
    event = {}
    async def dispatch(payload):
        event.update(payload)
        owner.enqueue_rpc(source.tenant_id, UUID(view["id"]), UUID(payload["lease_id"]))
    monkeypatch.setattr(bus, "publish_runtime_control_event", dispatch)
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call
    affected, held = asyncio.Event(), asyncio.Event()
    async def effect_without_returned_ack(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.insertText":
            affected.set()
            await held.wait()
        return result
    monkeypatch.setattr(pipe, "call", effect_without_returned_ack)
    remote = BrowserControlService()
    request_id = uuid4()
    pending = asyncio.create_task(call(remote, source, "type", view, request_id=request_id,
        arguments={"text": "effect-before-executor-expiry"}))
    try:
        await asyncio.wait_for(affected.wait(), 5)
        async with owner_sessionmaker() as db:
            lease = await db.get(EnvironmentLease, UUID(event["lease_id"]))
            assert lease.outcome["browser_rpc"]["phase"] == "executing"
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        with pytest.raises(BrowserControlError) as failure:
            await asyncio.wait_for(pending, 10)
        assert failure.value.effect == "unknown"
        await asyncio.wait_for(asyncio.gather(*list(owner._rpc_jobs.values())), 5)
        current = await owner.read(source)
        assert current["status"] == "lost" and current["last_effect"]["effect"] == "unknown"
        async with owner_sessionmaker() as db:
            lease = await db.get(EnvironmentLease, UUID(event["lease_id"]))
            assert lease.outcome["browser_rpc"]["phase"] == "finished"
        assert owner.enqueue_rpc(source.tenant_id, UUID(view["id"]), UUID(event["lease_id"]))
        await asyncio.gather(*list(owner._rpc_jobs.values()))
        stopped = (await call(owner, source, "stop", current))["browser"]
        assert stopped["status"] == "stopped"
        with pytest.raises(BrowserControlError, match="browser_request_already_recorded"):
            await call(remote, source, "type", stopped, request_id=request_id, arguments={"text": "effect-before-executor-expiry"})
        assert (root / view["id"] / "effects").read_text().splitlines() == ["effect-before-executor-expiry"]
    finally:
        held.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.gather(*list(owner._rpc_jobs.values()), return_exceptions=True)


async def test_separate_executing_rpc_owner_sigkill_keeps_closure_and_unknown_receipts(
    browser_case, browser_redis, app_user_engine, owner_sessionmaker
):
    seed, source, _, _, root = browser_case
    host = RPC_HOST.replace("ids=json.loads(os.environ['BROWSER_TEST_SOURCE']);root=Path(sys.argv[1])",
        "CHILD=CHILD.replace(\"if args['text']=='delayed-effect':time.sleep(.2)\",\"if args['text']=='crash-rpc-effect':time.sleep(30)\")\nids=json.loads(os.environ['BROWSER_TEST_SOURCE']);root=Path(sys.argv[1])")
    assert host != RPC_HOST
    redis_url = f"redis://127.0.0.1:{browser_redis.connection_pool.connection_kwargs['port']}/0"
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root),
        "PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": app_user_engine.url.render_as_string(hide_password=False), "REDIS_URL": redis_url,
        "SECRETS_MASTER_KEY": os.environ["SECRETS_MASTER_KEY"], "SECRET_KEY": "synthetic-rpc-owner",
        "JWT_SECRET_KEY": "synthetic-rpc-owner", "BROWSER_TEST_SOURCE": json.dumps(
            {key: str(seed[key]) for key in ("tenant_id", "agent_id", "user_id", "session_id")})}
    remote = BrowserControlService()
    pending = None
    child_pid = None
    with (root / "executing-rpc-owner.stderr").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", host, str(root), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr)
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            assert line, (root / "executing-rpc-owner.stderr").read_text()
            started = json.loads(line)
            view = started["view"]
            child_pid = int((root / view["id"] / "pid").read_text())
            await call(remote, source, "observe", view)
            request_id = uuid4()
            pending = asyncio.create_task(call(remote, source, "type", view, request_id=request_id,
                arguments={"text": "crash-rpc-effect"}))
            effect = root / view["id"] / "effects"
            async with asyncio.timeout(10):
                while not effect.exists() or effect.read_text() != "crash-rpc-effect\n":
                    await asyncio.sleep(.01)
            async with owner_sessionmaker() as db:
                lease = await db.scalar(select(EnvironmentLease).where(EnvironmentLease.invocation_key ==
                    f"browser:{source.session_id}:{source.user_id}:{request_id}"))
                lease_id = lease.id
                assert lease.outcome["browser_rpc"]["phase"] == "executing"
                receipt = Path((await db.get(EnvironmentBrowser, UUID(view["id"]))).lifecycle_receipt_path)
            process.kill()
            await process.wait()
            assert process.returncode == -signal.SIGKILL
            async with asyncio.timeout(5):
                while not receipt.exists() or json.loads(receipt.read_text())["payload"][4] != "closed":
                    await asyncio.sleep(.01)
            closed = await remote.read(source)
            assert closed["status"] == "stopped" and closed["last_effect"]["effect"] == "unknown"
            with pytest.raises(BrowserControlError) as failure:
                await asyncio.wait_for(pending, 5)
            assert failure.value.effect == "unknown"
            async with owner_sessionmaker() as db:
                lease = await db.get(EnvironmentLease, lease_id)
                assert lease.status == "released" and lease.outcome["resource_state"] == "stopped"
                assert lease.outcome["browser_rpc"]["phase"] == "executing"
                assert lease.outcome["browser_reconciliation"]["effect"] == "unknown"
                assert lease.outcome["browser_reconciliation"]["business_outcome"] == "unverified"
            current = (await call(remote, source, "start", closed))["browser"]
            await call(remote, source, "observe", current)
            with pytest.raises(BrowserControlError, match="browser_request_already_recorded"):
                await call(remote, source, "type", current, request_id=request_id, arguments={"text": "crash-rpc-effect"})
            assert effect.read_text().splitlines() == ["crash-rpc-effect"]
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if process.returncode is None:
                process.kill()
                await process.wait()
            await remote.close()
            if child_pid is not None:
                check = await asyncio.create_subprocess_exec("ps", "-p", str(child_pid), "-o", "command=",
                    stdout=asyncio.subprocess.PIPE)
                command, _ = await check.communicate()
                if str(root).encode() in command:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
