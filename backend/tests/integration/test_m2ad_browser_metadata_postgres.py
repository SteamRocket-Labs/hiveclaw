"""Human control metadata remains reachable while unrelated work is active."""

import asyncio
from uuid import UUID, uuid4

import pytest

from app.models.capability_policy import CapabilityPolicy
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.execution_environments import EnvironmentCommandFrame, environment_command_scope
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_control_postgres import call, human

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_heartbeat_and_return_do_not_wait_for_an_unrelated_writer(browser_case):
    seed, source, owner, client, _ = browser_case
    view = (await call(owner, source, "start"))["browser"]
    observed = await human(client, seed, "observe", view)
    assert observed.status_code == 200 and observed.headers["cache-control"] == "no-store"
    assert observed.json()["observation"]["viewport"] == {"width": 1280, "height": 720, "offsetX": 0, "offsetY": 0}
    view = (await human(client, seed, "take_control", view)).json()["browser"]
    admitted, finish = asyncio.Event(), asyncio.Event()

    async def long_command():
        admitted.set()
        await finish.wait()
        return CodeExecutionResult()

    frame = EnvironmentCommandFrame(
        source.tenant_id, source.agent_id, source.user_id, None, source_binding=source, session_id=source.session_id
    )
    async with environment_command_scope(frame, uuid4().hex) as scope:
        pending = asyncio.create_task(scope.execute(long_command, provider="local_os_sandbox", timeout=30))
        try:
            await asyncio.wait_for(admitted.wait(), 5)
            heartbeat = await asyncio.wait_for(human(client, seed, "heartbeat", view), 2)
            assert heartbeat.status_code == 200, heartbeat.text
            assert heartbeat.json()["browser"]["control_expires_at"] == view["control_expires_at"]
            returned = await asyncio.wait_for(human(client, seed, "return_control", view), 2)
            assert returned.status_code == 200, returned.text
            assert returned.json()["browser"]["fresh_observation_required"] is True
            assert not pending.done()
        finally:
            finish.set()
            result = await pending
            assert not result.error


async def test_disconnect_fences_an_admitted_browser_gesture_without_replay(browser_case, monkeypatch):
    seed, source, owner, client, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    view = (await human(client, seed, "take_control", view)).json()["browser"]
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call
    admitted, finish = asyncio.Event(), asyncio.Event()
    phases = []

    async def pause_after_press(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.dispatchMouseEvent":
            phases.append(parameters["type"])
            if parameters["type"] == "mousePressed":
                admitted.set()
                await finish.wait()
        return result

    monkeypatch.setattr(pipe, "call", pause_after_press)
    pending = asyncio.create_task(human(client, seed, "click", view, {"x": 1, "y": 2}))
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        blocked = await human(client, seed, "return_control", view)
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "browser_commands_active"
        disconnected = await asyncio.wait_for(human(client, seed, "disconnect", view), 2)
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["browser"]["control_mode"] == "paused"
        assert not pending.done()
    finally:
        finish.set()
        result = await pending
    assert result.status_code != 200 and result.json()["detail"]["effect"] == "unknown"
    assert phases == ["mousePressed"]
    current = await owner.read(source)
    assert current["status"] == "lost" and current["control_mode"] == "paused"
    assert not (root / view["id"] / "effects").exists()


async def test_human_gesture_cannot_bypass_a_company_browser_denial(browser_case, owner_sessionmaker):
    seed, source, owner, client, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    view = (await human(client, seed, "take_control", view)).json()["browser"]
    async with owner_sessionmaker() as db:
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"],
                capability="computer.browser.control",
                allowed=False,
                requires_approval=False,
            )
        )
        await db.commit()
    denied = await human(client, seed, "type", view, {"text": "must-not-bypass-company"})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "browser_capability_revoked"
    assert denied.json()["detail"]["effect"] == "not_started"
    assert not (root / view["id"] / "effects").exists()
    stopped = await human(client, seed, "stop", view)
    assert stopped.status_code == 200 and stopped.json()["browser"]["status"] == "stopped"
