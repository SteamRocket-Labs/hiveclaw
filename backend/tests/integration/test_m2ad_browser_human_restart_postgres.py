"""Actual human HTTP host loss preserves unknown effects and single consumption."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.audit import ApprovalRequest
from app.models.capability_policy import CapabilityPolicy
from app.models.environment_browser import EnvironmentBrowser
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_human_approval_postgres import approval_client

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

HOST = r"""
import asyncio,json,os,sys
from pathlib import Path
from uuid import UUID
import httpx
from fastapi import FastAPI
from app.config import Settings,get_settings
Settings.model_config['env_file']=None;get_settings.cache_clear()
from app.services.secrets_provider import init_secrets_provider
init_secrets_provider(os.environ['SECRETS_MASTER_KEY'])
from app.models import import_all_models
import_all_models()
import app.database as database
import app.api.browser_control as api
import app.services.browser_control as control
from app.core.security import get_current_user
from app.database import get_db,pin_rls_tenant_context
from app.models.user import User
from app.services.browser_pipe import BrowserLaunchSpec
from tests.integration.test_m2ad_browser_control_postgres import CHILD
ids=json.loads(os.environ['BROWSER_TEST_SOURCE']);root=Path(sys.argv[1])
script=CHILD.replace("if args['text']=='delayed-effect':time.sleep(.2)","if args['text']=='crash-effect':time.sleep(30)")
assert script!=CHILD
def spec(resource,tenant,agent):
    folder=root/str(resource);folder.mkdir(exist_ok=True)
    return BrowserLaunchSpec(Path(sys.executable).resolve(),('-B','-c',script,str(folder)),
        {'HOME':str(folder),'PATH':'/usr/bin:/bin'},folder)
control.browser_device_spec=spec
manager=control.BrowserControlService();api.browser_control=manager
app=FastAPI();app.include_router(api.router)
async def actor():
    async with database.tenant_scoped_session(UUID(ids['tenant_id']),require_tenant=True,source='native_human_browser_actor') as db:
        return await db.get(User,UUID(ids['user_id']))
async def scoped_db():
    async with database.async_session() as db:
        await pin_rls_tenant_context(db,UUID(ids['tenant_id']))
        yield db
        await db.commit()
app.dependency_overrides[get_current_user]=actor;app.dependency_overrides[get_db]=scoped_db
original=manager.execute
async def guarded(*args,**kwargs):
    if kwargs['action']=='type' and ids['window']=='before_effect':
        ready=root/'consumed.ready';ready.write_text('consumed');os.replace(ready,root/'consumed')
        await asyncio.Event().wait()
    return await original(*args,**kwargs)
manager.execute=guarded
async def main():
    base='/agents/'+ids['agent_id']+'/sessions/'+ids['session_id']+'/browser/'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://native-human-host') as client:
        result=await client.post(base+'start',json=ids['start'])
        assert result.status_code==200,result.text
        view=result.json()['browser']
        print(json.dumps({'browser':view,'child_pid':int((root/view['id']/'pid').read_text())}),flush=True)
        message=json.loads(await asyncio.to_thread(sys.stdin.readline))
        result=await client.post(base+'type',json=message)
        print(json.dumps({'unexpected_terminal':result.status_code}),flush=True)
asyncio.run(main())
"""


@pytest.mark.parametrize("window", ["before_effect", "after_effect"])
async def test_immediate_host_restart_quarantines_a_consumed_human_request_without_replay(
    browser_case, owner_sessionmaker, app_user_sessionmaker, app_user_engine, window
):
    seed, source, replacement, client, root = browser_case
    await approval_client(seed, client, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="computer.browser.control",
            allowed=True, requires_approval=True))
        await db.commit()
    base = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    async def approved(action, view=None, **arguments):
        body = {"expected_user_id": str(seed["user_id"]), "expected_tenant_id": str(seed["tenant_id"]),
            "request_id": str(uuid4()), "expected_revision": view["revision"] if view else 0,
            "expected_epoch": view["control_epoch"] if view else 0, "arguments": arguments}
        pending = await client.post(base + "/browser/" + action, json=body)
        assert pending.status_code == 409, pending.text
        ticket_id = pending.json()["detail"]["approval_id"]
        decided = await client.post(base + f"/approvals/{ticket_id}/resolve", json={"action": "approve"})
        assert decided.status_code == 200, decided.text
        return {**body, "approval_id": ticket_id}
    start = await approved("start")
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root),
        "PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": app_user_engine.url.render_as_string(hide_password=False),
        "SECRETS_MASTER_KEY": os.environ["SECRETS_MASTER_KEY"], "SECRET_KEY": "synthetic-human-host",
        "JWT_SECRET_KEY": "synthetic-human-host", "BROWSER_TEST_SOURCE": json.dumps({
            **{key: str(seed[key]) for key in ("tenant_id", "agent_id", "user_id", "session_id")},
            "start": start, "window": window})}
    child_pid = None
    with (root / "human-host.stderr").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", HOST, str(root), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr)
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            assert line, (root / "human-host.stderr").read_text()
            started = json.loads(line)
            view, child_pid = started["browser"], started["child_pid"]
            takeover = await approved("take_control", view)
            taken = await client.post(base + "/browser/take_control", json=takeover)
            assert taken.status_code == 200, taken.text
            view = taken.json()["browser"]
            body = await approved("type", view, text="crash-effect")
            process.stdin.write((json.dumps(body) + "\n").encode())
            await process.stdin.drain()
            marker = root / "consumed" if window == "before_effect" else root / view["id"] / "effects"
            expected = "consumed" if window == "before_effect" else "crash-effect\n"
            async with asyncio.timeout(10):
                while not marker.exists() or marker.read_text() != expected:
                    assert process.returncode is None
                    await asyncio.sleep(.01)
            async with owner_sessionmaker() as db:
                ticket = await db.get(ApprovalRequest, UUID(body["approval_id"]))
                assert ticket.consumed_at is not None and ticket.execution_status == "executing"
                assert ticket.execution_task_id is None
                resource = await db.get(EnvironmentBrowser, UUID(view["id"]))
                receipt_path = Path(resource.lifecycle_receipt_path)
            live = await client.get(base + "/browser", params={"expected_user_id": str(seed["user_id"]),
                "expected_tenant_id": str(seed["tenant_id"])})
            assert live.status_code == 200, live.text
            active = next(row for row in live.json()["approvals"]["items"] if row["id"] == body["approval_id"])
            assert active["execution_status"] == "executing"
            assert live.json()["browser"]["status"] == "ready"
            os.kill(child_pid, 0)
            process.kill()
            await process.wait()
            assert process.returncode == -signal.SIGKILL
            async with asyncio.timeout(5):
                while not receipt_path.exists() or json.loads(receipt_path.read_text())["payload"][4] != "closed":
                    await asyncio.sleep(.01)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            restored = await client.get(base + "/browser", params={"expected_user_id": str(seed["user_id"]),
                "expected_tenant_id": str(seed["tenant_id"])})
            assert restored.status_code == 200, restored.text
            assert restored.json()["browser"]["status"] == "stopped"
            entry = next(row for row in restored.json()["approvals"]["items"] if row["id"] == body["approval_id"])
            assert entry["execution_status"] == "needs_reconciliation"
            async with owner_sessionmaker() as db:
                ticket = await db.get(ApprovalRequest, UUID(body["approval_id"]))
                assert ticket.execution_receipt["automatic_replay"] is False
                assert ticket.execution_receipt["side_effect_state"] == "unknown"
            repeated = await client.post(base + "/browser/type", json=body)
            assert repeated.status_code == 409, repeated.text
            effect = root / view["id"] / "effects"
            assert effect.read_text().splitlines() == ["crash-effect"] if window == "after_effect" else not effect.exists()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if child_pid is not None:
                check = await asyncio.create_subprocess_exec("ps", "-p", str(child_pid), "-o", "command=",
                    stdout=asyncio.subprocess.PIPE)
                command, _ = await check.communicate()
                if str(root).encode() in command:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
