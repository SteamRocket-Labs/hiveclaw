"""Governed runtime browser tools with native RLS and a non-browser child."""

import asyncio
import json
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.capability_policy import CapabilityPolicy
from app.models.environment_browser import EnvironmentBrowser
from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from app.tools.result_envelope import ToolContentEnvelope
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_rpc_postgres import browser_redis as browser_redis

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def browser_tools(browser_case, app_user_sessionmaker, owner_sessionmaker, monkeypatch):
    from app.config import get_settings
    from app.services import agent_tools
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import OWNER_ACTION_IDS, save_owner_action_policy
    from app.tools import resolver, workspace
    from app.tools.handlers import browser

    seed, _, owner, _, _ = browser_case
    factory = resolver.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(browser, "browser_control", owner)
    async with owner_sessionmaker() as db:
        for capability in ("computer.browser.read", "computer.browser.control"):
            db.add(
                CapabilityPolicy(
                    tenant_id=seed["tenant_id"], capability=capability, allowed=True, requires_approval=False
                )
            )
        await save_owner_action_policy(
            db,
            agent_id=seed["agent_id"],
            tenant_id=seed["tenant_id"],
            changed_by_user_id=seed["user_id"],
            actions={action: CharterZone.FULL_AUTHORITY for action in OWNER_ACTION_IDS},
        )
        await db.commit()
    token = set_runtime_task_fence(task_id=seed["run_id"], claim_version=1, worker_id="kernel-worker-1")

    async def execute(name, arguments=None, **kwargs):
        return await agent_tools.execute_tool(
            name,
            arguments or {},
            agent_id=seed["agent_id"],
            user_id=seed["user_id"],
            session_id=str(seed["session_id"]),
            runtime_task_id=str(seed["run_id"]),
            tool_call_id=kwargs.pop("tool_call_id", str(uuid4())),
            emit_runtime_hooks=False,
            **kwargs,
        )

    try:
        yield execute
    finally:
        reset_runtime_task_fence(token)


def operation(action, view=None, **arguments):
    return {
        "action": action,
        "expected_revision": view["revision"] if view else 0,
        "expected_epoch": view["control_epoch"] if view else 0,
        "arguments": arguments,
    }


async def test_real_runtime_discovers_executes_and_returns_typed_observations(
    browser_case, browser_tools, owner_sessionmaker
):
    from app.services.agent_tools import get_agent_tools_for_llm

    seed, _, _, _, root = browser_case
    names = {row["function"]["name"] for row in await get_agent_tools_for_llm(seed["agent_id"])}
    assert {"browser_observe", "browser_manage", "browser_action"} <= names
    assert json.loads(str(await browser_tools("browser_observe")))["browser"] is None
    started = await browser_tools("browser_manage", operation("start"))
    view = json.loads(str(started))["browser"]
    result = await browser_tools("browser_observe")
    assert isinstance(result, ToolContentEnvelope)
    assert len(result.blocks) == 1 and result.blocks[0].type == "image"
    assert result.blocks[0].media_type == "image/png"
    assert "synthetic-protected-value" not in str(result)
    assert "screenshot" not in json.loads(result.text)["observation"]
    receipt = json.loads(result.text)
    assert receipt["browser"]["fresh_observation_required"] is False
    identifier = str(uuid4())
    args = operation("type", view, text="governed-browser-effect")
    action = await browser_tools("browser_action", args, tool_call_id=identifier)
    assert json.loads(str(action))["business_outcome"] == "unverified"
    repeated = await browser_tools("browser_action", args, tool_call_id=identifier)
    assert "browser_request_already_recorded" in str(repeated)
    assert (root / view["id"] / "effects").read_text().splitlines() == ["governed-browser-effect"]
    from app.models.activity_log import AgentActivityLog

    async with owner_sessionmaker() as db:
        audit = list(
            await db.scalars(
                select(AgentActivityLog).where(
                    AgentActivityLog.agent_id == seed["agent_id"], AgentActivityLog.action_type == "action_preflight"
                )
            )
        )
        assert audit and all(
            row.tenant_id == seed["tenant_id"] and row.root_session_id == seed["session_id"] for row in audit
        )


@pytest.mark.parametrize("remote", [False, True])
async def test_company_revocation_between_click_steps_preserves_unknown_whole_action(
    browser_case, browser_tools, browser_redis, owner_sessionmaker, monkeypatch, remote
):
    from app.services.browser_control import BrowserControlService
    from app.tools.handlers import browser

    seed, source, owner, _, _ = browser_case
    started = await browser_tools("browser_manage", operation("start"))
    view = json.loads(str(started))["browser"]
    await browser_tools("browser_observe")
    if remote:
        monkeypatch.setattr(browser, "browser_control", BrowserControlService())
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call
    phases = []

    async def revoked_after_press(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.dispatchMouseEvent":
            phases.append(parameters["type"])
            async with owner_sessionmaker() as db:
                policy = await db.scalar(
                    select(CapabilityPolicy).where(
                        CapabilityPolicy.tenant_id == seed["tenant_id"],
                        CapabilityPolicy.agent_id.is_(None),
                        CapabilityPolicy.capability == "computer.browser.control",
                    )
                )
                policy.allowed = False
                await db.commit()
        return result

    monkeypatch.setattr(pipe, "call", revoked_after_press)
    trace = {}
    result = await browser_tools("browser_action", operation("click", view, x=1, y=2), trace_metadata_sink=trace)
    assert "browser_capability_revoked" in str(result)
    assert '"effect": "unknown"' in str(result)
    assert phases == ["mousePressed"]
    assert trace["tool_execution_frame"]["status"] == "unknown"
    if remote:
        await asyncio.wait_for(asyncio.gather(*list(owner._rpc_jobs.values())), 5)
    async with owner_sessionmaker() as db:
        resource = await db.get(EnvironmentBrowser, UUID(view["id"]))
        assert resource.status == "lost"
        assert resource.last_effect["effect"] == "unknown"
    assert (await owner.read(source))["status"] == "lost"


@pytest.mark.parametrize("remote", [False, True])
async def test_internal_error_after_effect_is_unknown_not_invalid_arguments(
    browser_case, browser_tools, browser_redis, owner_sessionmaker, monkeypatch, remote
):
    from app.services.browser_control import BrowserControlService
    from app.tools.handlers import browser

    _, source, owner, _, root = browser_case
    view = json.loads(str(await browser_tools("browser_manage", operation("start"))))["browser"]
    await browser_tools("browser_observe")
    if remote:
        monkeypatch.setattr(browser, "browser_control", BrowserControlService())
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call

    async def fail_after_insert(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.insertText":
            raise TypeError("synthetic-internal-error-with-private-details")
        return result

    monkeypatch.setattr(pipe, "call", fail_after_insert)
    trace = {}
    call_id = str(uuid4())
    args = operation("type", view, text="effect-before-internal-error")
    result = await browser_tools("browser_action", args, tool_call_id=call_id, trace_metadata_sink=trace)
    assert (root / view["id"] / "effects").read_text().splitlines() == ["effect-before-internal-error"]
    assert '"effect": "unknown"' in str(result)
    assert "browser_arguments_invalid" not in str(result)
    assert "synthetic-internal-error-with-private-details" not in str(result)
    assert trace["tool_execution_frame"]["status"] == "unknown"
    if remote:
        await asyncio.wait_for(asyncio.gather(*list(owner._rpc_jobs.values())), 5)
    async with owner_sessionmaker() as db:
        resource = await db.get(EnvironmentBrowser, UUID(view["id"]))
        assert resource.status == "lost"
        assert resource.last_effect["effect"] == "unknown"
    current = await owner.read(source)
    assert current["status"] == "lost"
    blocked = await browser_tools("browser_action", args, tool_call_id=call_id)
    assert "environment_reconciliation_required" in str(blocked)
    assert (root / view["id"] / "effects").read_text().splitlines() == ["effect-before-internal-error"]
    stopped = await owner.execute(
        source,
        action="stop",
        request_id=uuid4(),
        expected_revision=current["revision"],
        expected_epoch=current["control_epoch"],
    )
    assert stopped["browser"]["status"] == "stopped"
    repeated = await browser_tools("browser_action", args, tool_call_id=call_id)
    assert "browser_request_already_recorded" in str(repeated)
    assert (root / view["id"] / "effects").read_text().splitlines() == ["effect-before-internal-error"]


async def test_company_denial_wins_over_employee_allow_without_removing_observation(
    browser_case, browser_tools, owner_sessionmaker
):
    seed, _, owner, _, root = browser_case
    started = await browser_tools("browser_manage", operation("start"))
    view = json.loads(str(started))["browser"]
    await browser_tools("browser_observe")
    async with owner_sessionmaker() as db:
        company = await db.scalar(
            select(CapabilityPolicy).where(
                CapabilityPolicy.tenant_id == seed["tenant_id"],
                CapabilityPolicy.agent_id.is_(None),
                CapabilityPolicy.capability == "computer.browser.control",
            )
        )
        company.allowed = False
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                capability="computer.browser.control",
                allowed=True,
                requires_approval=False,
            )
        )
        await db.commit()
    denied = await browser_tools("browser_action", operation("type", view, text="company-denied"))
    assert "denied" in str(denied).lower()
    assert not (root / view["id"] / "effects").exists()
    observed = await browser_tools("browser_observe")
    assert isinstance(observed, ToolContentEnvelope) and observed.blocks[0].type == "image"
    assert UUID(view["id"]) in owner._pages


async def test_model_arguments_cannot_choose_identity_or_human_control(browser_case, browser_tools):
    from app.tools.service import _extract_tool_error_payload

    _, _, _, _, root = browser_case
    injected = await browser_tools("browser_manage", {**operation("start"), "user_id": str(uuid4())})
    assert _extract_tool_error_payload(str(injected))["error_class"] == "invalid_tool_arguments"
    takeover = await browser_tools("browser_manage", operation("take_control"))
    assert _extract_tool_error_payload(str(takeover))["error_class"] == "invalid_tool_arguments"
    assert not list(root.glob("*/pid"))


async def test_malformed_navigation_is_known_not_started_and_preserves_ready_resource(browser_case, browser_tools):
    _, source, owner, _, root = browser_case
    view = json.loads(str(await browser_tools("browser_manage", operation("start"))))["browser"]
    await browser_tools("browser_observe")
    trace = {}
    result = await browser_tools("browser_action", operation("navigate", view, url="http://["), trace_metadata_sink=trace)
    assert "browser_navigation_url_invalid" in str(result)
    assert '"effect": "not_started"' in str(result)
    assert trace["tool_execution_frame"]["status"] == "failed"
    assert (await owner.read(source))["status"] == "ready"
    assert "Page.navigate" not in (root / view["id"] / "protocol.log").read_text().splitlines()
