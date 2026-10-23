"""Kill the actual owned runtime host; reconcile resource facts without replay."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.environment_browser import EnvironmentBrowser
from app.models.execution_environment import EnvironmentLease
from app.services.browser_control import BrowserControlError, BrowserControlService
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_control_postgres import call

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_late_original_worker_shutdown_cannot_revoke_a_replacement_resource(browser_case):
    _, source, original, _, root = browser_case
    first = (await call(original, source, "start"))["browser"]
    await original._pages[UUID(first["id"])].close()
    replacement = BrowserControlService()
    try:
        closed = await replacement.read(source)
        assert closed["status"] == "stopped"
        current = (await call(replacement, source, "start", closed))["browser"]
        pid = int((root / current["id"] / "pid").read_text())
        await original.close()
        os.kill(pid, 0)
        assert (await replacement.read(source))["status"] == "ready"
    finally:
        await replacement.close()


HOST = r"""
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
from app.services.browser_control import BrowserSource,BrowserControlService
from app.services.browser_pipe import BrowserLaunchSpec
from tests.integration.test_m2ad_browser_control_postgres import CHILD
ids=json.loads(os.environ['BROWSER_TEST_SOURCE']);root=Path(sys.argv[1])
source=BrowserSource(*(UUID(ids[key]) for key in ('tenant_id','agent_id','user_id','session_id')))
script=CHILD.replace("if args['text']=='delayed-effect':time.sleep(.2)","if args['text']=='crash-effect':time.sleep(30)")
assert script!=CHILD
def spec(resource,tenant,agent):
    folder=root/str(resource);folder.mkdir(exist_ok=True)
    return BrowserLaunchSpec(Path(sys.executable).resolve(),('-B','-c',script,str(folder)),
        {'HOME':str(folder),'PATH':'/usr/bin:/bin'},folder)
module.browser_device_spec=spec
async def main():
    manager=BrowserControlService()
    view=(await manager.execute(source,action='start',request_id=uuid4(),expected_revision=0,expected_epoch=0))['browser']
    await manager.execute(source,action='observe',request_id=uuid4(),expected_revision=view['revision'],expected_epoch=view['control_epoch'])
    print(json.dumps({'resource_id':view['id'],'child_pid':int((root/view['id']/'pid').read_text())}),flush=True)
    await manager.execute(source,action='type',request_id=ids['request_id'],expected_revision=view['revision'],expected_epoch=view['control_epoch'],arguments={'text':'crash-effect'})
asyncio.run(main())
"""


async def test_host_sigkill_releases_only_a_verified_closed_resource_and_never_replays(
    browser_case, app_user_engine, owner_sessionmaker
):
    seed, source, _, _, root = browser_case
    request_id = uuid4()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONPATH": os.pathsep.join(sys.path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": app_user_engine.url.render_as_string(hide_password=False),
        "SECRETS_MASTER_KEY": os.environ["SECRETS_MASTER_KEY"],
        "SECRET_KEY": "synthetic-browser-runtime",
        "JWT_SECRET_KEY": "synthetic-browser-runtime",
        "BROWSER_TEST_SOURCE": json.dumps(
            {
                **{key: str(seed[key]) for key in ("tenant_id", "agent_id", "user_id", "session_id")},
                "request_id": str(request_id),
            }
        ),
    }
    error_path = root / "runtime-host.stderr"
    with error_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            HOST,
            str(root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )
        child_pid = None
        replacement = BrowserControlService()
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            assert line, error_path.read_text()
            started = json.loads(line)
            resource_id = UUID(started["resource_id"])
            child_pid = started["child_pid"]
            effect = root / started["resource_id"] / "effects"
            async with asyncio.timeout(5):
                while not effect.exists():
                    await asyncio.sleep(0.01)
            assert effect.read_text().splitlines() == ["crash-effect"]
            process.kill()
            await process.wait()
            assert process.returncode == -signal.SIGKILL
            async with owner_sessionmaker() as db:
                row = await db.get(EnvironmentBrowser, resource_id)
                path = Path(row.lifecycle_receipt_path)
                assert row.lifecycle_secret.startswith("hive:channel-secret:v1:")
            async with asyncio.timeout(5):
                while not path.exists() or json.loads(path.read_text())["payload"][4] != "closed":
                    await asyncio.sleep(0.01)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            frozen = path.read_text()
            forged = json.loads(frozen)
            forged["signature"] = "0" * 64
            path.write_text(json.dumps(forged))
            await replacement.read(source)
            async with owner_sessionmaker() as db:
                row = await db.get(EnvironmentBrowser, resource_id)
                assert row.status == "ready"
                assert (
                    len(
                        list(
                            await db.scalars(
                                select(EnvironmentLease).where(
                                    EnvironmentLease.environment_id == row.environment_id,
                                    EnvironmentLease.status == "active",
                                )
                            )
                        )
                    )
                    == 1
                )
            path.write_text(frozen)
            closed = await replacement.read(source)
            assert closed["status"] == "stopped" and closed["last_effect"]["effect"] == "unknown"
            assert closed["last_effect"]["business_outcome"] == "unverified"
            async with owner_sessionmaker() as db:
                row = await db.get(EnvironmentBrowser, resource_id)
                assert not list(
                    await db.scalars(
                        select(EnvironmentLease).where(
                            EnvironmentLease.environment_id == row.environment_id, EnvironmentLease.status == "active"
                        )
                    )
                )
            reopened = (await call(replacement, source, "start", closed))["browser"]
            assert reopened["id"] == closed["id"] and reopened["control_epoch"] > closed["control_epoch"]
            await call(replacement, source, "observe", reopened)
            with pytest.raises(BrowserControlError, match="browser_request_already_recorded"):
                await call(
                    replacement, source, "type", reopened, request_id=request_id, arguments={"text": "crash-effect"}
                )
            assert effect.read_text().splitlines() == ["crash-effect"]
            async with owner_sessionmaker() as db:
                row = await db.get(EnvironmentBrowser, resource_id)
                new_path = Path(row.lifecycle_receipt_path)
            current = new_path.read_text()
            new_path.write_text(frozen)
            assert (await replacement.read(source))["status"] == "ready"
            new_path.write_text(current)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await replacement.close()
            if child_pid is not None:
                try:
                    command = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(child_pid), "-o", "command=", stdout=asyncio.subprocess.PIPE
                    )
                    data, _ = await command.communicate()
                    if str(root).encode() in data:
                        os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
