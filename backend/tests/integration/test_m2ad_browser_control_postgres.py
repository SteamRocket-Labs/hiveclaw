"""Real RLS, source authorization and private pipes, with a non-browser child."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.models.agent import Agent
from app.models.environment_browser import EnvironmentBrowser
from app.models.execution_environment import EnvironmentLease
from app.services.browser_control import BrowserControlError, BrowserControlService, BrowserSource
from app.services.browser_pipe import BrowserLaunchSpec
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

CHILD = r"""
import base64,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]);root.mkdir(exist_ok=True)
(root/'pid.ready').write_text(str(os.getpid()));os.replace(root/'pid.ready',root/'pid')
reader=os.fdopen(3,'rb',buffering=0);writer=os.fdopen(4,'wb',buffering=0);buffer=b''
while True:
    data=reader.read(65536)
    if not data:break
    buffer+=data
    while b'\0' in buffer:
        body,buffer=buffer.split(b'\0',1);message=json.loads(body);method=message['method'];args=message['params']
        with (root/'protocol.log').open('a') as log:log.write(method+'\n')
        result={}
        if method=='Target.createTarget':result={'targetId':'owned-page'}
        if method=='Target.getTargets':result={'targetInfos':[{'type':'page','targetId':'owned-page','url':'about:blank','title':'Protocol child'}]}
        if method=='Target.attachToTarget':result={'sessionId':'owned-session'}
        if method=='Accessibility.getFullAXTree':result={'nodes':[{'nodeId':'secret','backendDOMNodeId':42,'value':{'type':'string','value':'synthetic-protected-value'},'childIds':['secret-text'],'properties':[{'name':'valuetext','value':{'value':'synthetic-protected-value'}}]},{'nodeId':'button','name':{'value':'Submit'}},{'nodeId':'secret-text','name':{'value':'synthetic-protected-value'}}]}
        if method=='DOMSnapshot.captureSnapshot':result={'strings':['INPUT','type','password'],'documents':[{'nodes':{'backendNodeId':[42],'nodeName':[0],'attributes':[[1,2]]}}]}
        if method=='Page.captureScreenshot':result={'data':base64.b64encode(b'synthetic-image-boundary').decode()}
        if method=='Page.getLayoutMetrics':result={'cssVisualViewport':{'clientWidth':1280,'clientHeight':720,'offsetX':0,'offsetY':0}}
        if method=='Input.insertText':
            with (root/'effects').open('a') as output:output.write(args['text']+'\n')
            if args['text']=='delayed-effect':time.sleep(.2)
        writer.write(json.dumps({'id':message['id'],'result':result}).encode()+b'\0')
        if method=='Browser.close':raise SystemExit(0)
"""


@pytest.fixture
async def browser_case(owner_sessionmaker, app_user_sessionmaker, tmp_path, monkeypatch):
    import app.database as database
    import app.api.browser_control as api
    import app.services.browser_control as service
    import app.services.secrets_provider as secrets_provider
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.user import User

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    source = BrowserSource(*(seed[key] for key in ("tenant_id", "agent_id", "user_id", "session_id")))
    manager = BrowserControlService()
    # Match the application's real encrypted runtime initialization.
    monkeypatch.setattr(secrets_provider, "_provider", secrets_provider._provider)
    secrets_provider.init_secrets_provider(os.environ["SECRETS_MASTER_KEY"])
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(api, "browser_control", manager)

    def spec(resource_id, tenant_id, agent_id):
        assert tenant_id == seed["tenant_id"] and agent_id == seed["agent_id"]
        root = tmp_path / str(resource_id)
        root.mkdir(exist_ok=True)
        return BrowserLaunchSpec(
            Path(sys.executable).resolve(),
            ("-B", "-c", CHILD, str(root)),
            {"HOME": str(root), "PATH": "/usr/bin:/bin"},
            root,
        )

    monkeypatch.setattr(service, "browser_device_spec", spec)

    async def actor():
        async with owner_sessionmaker() as db:
            return await db.get(User, seed["user_id"])

    async def session():
        async with app_user_sessionmaker() as db:
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = actor
    app.dependency_overrides[get_db] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://browser-control.test"
    ) as client:
        try:
            yield seed, source, manager, client, tmp_path
        finally:
            await manager.close()


@pytest.fixture
async def unavailable_browser_rpc(monkeypatch):
    import socket

    import redis.asyncio as redis_async

    from app.services import browser_rpc

    # Reserve an endpoint without listening, so only this RPC transport refuses
    # connections even when the host has a working shared Redis service.
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        client = redis_async.Redis(host="127.0.0.1", port=endpoint.getsockname()[1], decode_responses=True)

        async def unavailable():
            return client

        try:
            monkeypatch.setattr(browser_rpc, "get_redis", unavailable)
            yield
        finally:
            await client.aclose()


async def call(manager, source, action, view=None, **kwargs):
    return await manager.execute(
        source,
        action=action,
        request_id=kwargs.pop("request_id", uuid4()),
        expected_revision=view["revision"] if view else 0,
        expected_epoch=view["control_epoch"] if view else 0,
        **kwargs,
    )


async def human(client, seed, action, view, arguments=None):
    return await client.post(
        f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/browser/{action}",
        json={
            "expected_user_id": str(seed["user_id"]),
            "expected_tenant_id": str(seed["tenant_id"]),
            "request_id": str(uuid4()),
            "expected_revision": view["revision"],
            "expected_epoch": view["control_epoch"],
            "arguments": arguments or {},
        },
    )


async def test_human_control_and_return_require_a_fresh_agent_observation(browser_case, owner_sessionmaker):
    seed, source, manager, client, root = browser_case
    view = (await call(manager, source, "start"))["browser"]
    observed = await call(manager, source, "observe", view)
    assert "synthetic-protected-value" not in json.dumps(observed["observation"])
    assert observed["observation"]["accessibility"][1]["name"]["value"] == "Submit"
    view = (await call(manager, source, "request_control", view))["browser"]
    take = await human(client, seed, "take_control", view)
    assert take.status_code == 200, take.text
    view = take.json()["browser"]
    assert view["control_mode"] == "human"
    assert take.headers["cache-control"] == "no-store"
    for action, args in [("type", {"text": "agent-must-not-type"}), ("request_control", {})]:
        with pytest.raises(BrowserControlError, match="browser_human_control_active"):
            await call(manager, source, action, view, arguments=args)
    entered = await human(client, seed, "type", view, {"text": "synthetic-human-login-secret"})
    assert entered.status_code == 200, entered.text
    returned = await human(client, seed, "return_control", view)
    assert returned.status_code == 200, returned.text
    view = returned.json()["browser"]
    assert view["fresh_observation_required"] is True
    with pytest.raises(BrowserControlError, match="browser_fresh_observation_required"):
        await call(manager, source, "type", view, arguments={"text": "stale-agent-action"})
    observed = await call(manager, source, "observe", view)
    assert observed["browser"]["fresh_observation_required"] is False
    request_id = uuid4()
    await call(manager, source, "type", view, request_id=request_id, arguments={"text": "one-agent-effect"})
    with pytest.raises(BrowserControlError, match="browser_request_already_recorded"):
        await call(manager, source, "type", view, request_id=request_id, arguments={"text": "one-agent-effect"})
    assert (root / view["id"] / "effects").read_text().splitlines() == [
        "synthetic-human-login-secret",
        "one-agent-effect",
    ]
    async with owner_sessionmaker() as db:
        resource = await db.get(EnvironmentBrowser, UUID(view["id"]))
        receipts = list(
            await db.scalars(
                select(EnvironmentLease.outcome).where(EnvironmentLease.environment_id == resource.environment_id)
            )
        )
        assert "synthetic-human-login-secret" not in json.dumps([resource.last_effect, *receipts])


async def test_disconnect_expiry_does_not_automatically_return_control(browser_case, owner_sessionmaker):
    seed, source, manager, client, _ = browser_case
    view = (await call(manager, source, "start"))["browser"]
    taken = await human(client, seed, "take_control", view)
    assert taken.status_code == 200, taken.text
    view = taken.json()["browser"]
    heartbeat = await human(client, seed, "heartbeat", view)
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["browser"]["control_expires_at"] == view["control_expires_at"]
    async with owner_sessionmaker() as db:
        resource = await db.get(EnvironmentBrowser, UUID(view["id"]))
        resource.viewer_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    expired = await manager.read(source)
    assert expired["control_mode"] == "paused" and expired["resource_error"] == "browser_viewer_disconnected"
    assert expired["control_epoch"] > view["control_epoch"]
    with pytest.raises(BrowserControlError, match="browser_human_control_active"):
        await call(manager, source, "observe", expired)
    returned = await human(client, seed, "return_control", expired)
    assert returned.status_code == 200, returned.text
    assert returned.json()["browser"]["fresh_observation_required"] is True


async def test_current_authority_revocation_stops_the_owned_child(browser_case, owner_sessionmaker):
    seed, source, manager, _, root = browser_case
    view = (await call(manager, source, "start"))["browser"]
    pid = int((root / view["id"] / "pid").read_text())
    monitor = manager._monitors[UUID(view["id"])]
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.deactivated_at = datetime.now(UTC)
        await db.commit()
    with pytest.raises(BrowserControlError, match="browser_authority_revoked"):
        await call(manager, source, "observe", view)
    await asyncio.wait_for(asyncio.shield(monitor), 3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    async with owner_sessionmaker() as db:
        row = await db.get(EnvironmentBrowser, UUID(view["id"]))
        assert row.status == "revoked" and row.last_effect["closure_confirmed"] is True


async def test_unknown_mutation_uses_the_late_ack_without_replaying(browser_case, owner_sessionmaker, monkeypatch):
    seed, source, manager, client, root = browser_case
    view = (await call(manager, source, "start"))["browser"]
    await call(manager, source, "observe", view)
    pipe = manager._pages[UUID(view["id"])].pipe
    original = pipe.call

    async def short_timeout(method, parameters, **kwargs):
        if method == "Input.insertText":
            kwargs["timeout"] = 0.05
        return await original(method, parameters, **kwargs)

    monkeypatch.setattr(pipe, "call", short_timeout)
    request_id = uuid4()
    with pytest.raises(BrowserControlError, match="browser_response_timeout") as error:
        await call(manager, source, "type", view, request_id=request_id, arguments={"text": "delayed-effect"})
    assert error.value.effect == "unknown"
    lost = await manager.read(source)
    assert lost["status"] == "lost"
    with pytest.raises(BrowserControlError, match="environment_reconciliation_required"):
        await call(manager, source, "type", lost, arguments={"text": "must-not-retry"})
    await asyncio.sleep(0.25)
    reconciled = await human(client, seed, "reconcile", lost)
    assert reconciled.status_code == 200, reconciled.text
    view = reconciled.json()["browser"]
    assert view["control_mode"] == "paused" and view["fresh_observation_required"] is True
    assert view["last_effect"]["business_outcome"] == "unverified"
    assert (root / view["id"] / "effects").read_text().splitlines() == ["delayed-effect"]
    async with owner_sessionmaker() as db:
        row = await db.get(EnvironmentBrowser, UUID(view["id"]))
        assert not list(
            await db.scalars(
                select(EnvironmentLease).where(
                    EnvironmentLease.environment_id == row.environment_id, EnvironmentLease.status == "active"
                )
            )
        )


async def test_another_worker_cannot_duplicate_or_claim_the_private_resource(browser_case, unavailable_browser_rpc):
    _, source, manager, _, root = browser_case
    view = (await call(manager, source, "start"))["browser"]
    other = BrowserControlService()
    observed = await other.read(source)
    assert observed["attached_here"] is False and observed["next_action"] == "inspect_original_worker"
    with pytest.raises(BrowserControlError, match="browser_original_worker_required"):
        await call(other, source, "observe", view)
    with pytest.raises(BrowserControlError, match="browser_resource_already_started"):
        await call(other, source, "start", view)
    assert (root / view["id"] / "protocol.log").read_text().count("Target.createTarget") == 1


async def test_cancelled_start_records_verified_closure_instead_of_an_orphan(
    browser_case, owner_sessionmaker, monkeypatch
):
    _, source, manager, _, root = browser_case
    import app.services.browser_control as service
    from dataclasses import replace

    original = service.browser_device_spec

    def delayed_spec(*args):
        spec = original(*args)
        script = CHILD.replace(
            "if method=='Target.createTarget':result=", "if method=='Target.createTarget':time.sleep(2);result="
        )
        return replace(spec, arguments=("-B", "-c", script, str(spec.cwd)))

    monkeypatch.setattr(service, "browser_device_spec", delayed_spec)
    running = asyncio.create_task(call(manager, source, "start"))
    try:
        async with asyncio.timeout(3):
            while not list(root.glob("*/pid")) and not running.done():
                await asyncio.sleep(0.01)
        paths = list(root.glob("*/pid"))
        assert len(paths) == 1
        pid = int(paths[0].read_text())
        resource_id = UUID(paths[0].parent.name)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        async with owner_sessionmaker() as db:
            row = await db.get(EnvironmentBrowser, resource_id)
            assert row.status == "stopped" and row.last_effect["closure_confirmed"] is True
            assert not list(
                await db.scalars(
                    select(EnvironmentLease).where(
                        EnvironmentLease.environment_id == row.environment_id, EnvironmentLease.status == "active"
                    )
                )
            )
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_worker_shutdown_reaps_a_pending_start_without_publishing_ready(browser_case, monkeypatch):
    _, source, manager, _, root = browser_case
    import app.services.browser_control as service
    from dataclasses import replace

    original = service.browser_device_spec

    def delayed_spec(*args):
        spec = original(*args)
        script = CHILD.replace(
            "if method=='Target.createTarget':result=", "if method=='Target.createTarget':time.sleep(2);result="
        )
        return replace(spec, arguments=("-B", "-c", script, str(spec.cwd)))

    monkeypatch.setattr(service, "browser_device_spec", delayed_spec)
    running = asyncio.create_task(call(manager, source, "start"))
    try:
        async with asyncio.timeout(3):
            while not list(root.glob("*/pid")) and not running.done():
                await asyncio.sleep(0.01)
        paths = list(root.glob("*/pid"))
        assert len(paths) == 1
        pid = int(paths[0].read_text())
        await manager.close()
        with pytest.raises(BrowserControlError, match="browser_worker_stopping"):
            await running
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_foreign_tenant_rls_cannot_read_the_browser_resource(browser_case, app_user_sessionmaker):
    _, source, manager, _, _ = browser_case
    from app.database import pin_rls_tenant_context

    view = (await call(manager, source, "start"))["browser"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, uuid4())
        assert await db.get(EnvironmentBrowser, UUID(view["id"])) is None
        db.add(
            EnvironmentBrowser(
                tenant_id=source.tenant_id,
                agent_id=source.agent_id,
                environment_id=uuid4(),
                session_id=source.session_id,
                principal_id=source.user_id,
                generation=1,
                worker_instance="foreign-write",
            )
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            await db.flush()
