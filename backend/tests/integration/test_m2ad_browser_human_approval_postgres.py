"""Human browser approval uses the existing original-work decision ledger."""

import json
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from app.models.audit import ApprovalRequest, AuditLog
from app.models.capability_policy import CapabilityPolicy
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_control_postgres import call
from tests.integration.test_m2ad_browser_rpc_postgres import browser_redis as browser_redis

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def request(seed, view, **arguments):
    return {"expected_user_id": str(seed["user_id"]), "expected_tenant_id": str(seed["tenant_id"]),
            "request_id": str(uuid4()), "expected_revision": view["revision"],
            "expected_epoch": view["control_epoch"], "arguments": arguments}


async def approval_client(seed, client, factory):
    from app.api import chat_sessions
    client._transport.app.include_router(chat_sessions.router)
    async def scoped_db():
        async with factory() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()
    client._transport.app.dependency_overrides[get_db] = scoped_db


@pytest.mark.parametrize("remote", [False, True])
async def test_human_company_approval_executes_exact_input_once_and_preserves_private_work(
    browser_case, browser_redis, owner_sessionmaker, app_user_sessionmaker, monkeypatch, remote
):
    seed, source, owner, client, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    await approval_client(seed, client, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="computer.browser.control",
            allowed=True, requires_approval=True))
        await db.commit()
    base = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    async def approved(action, body):
        initial = await client.post(base + "/browser/" + action, json=body)
        assert initial.status_code == 409 and initial.json()["detail"]["code"] == "browser_approval_required", initial.text
        ticket_id = initial.json()["detail"]["approval_id"]
        again = await client.post(base + "/browser/" + action, json=body)
        assert again.status_code == 409 and again.json()["detail"]["approval_id"] == ticket_id
        path = base + "/approvals/" + ticket_id
        loaded = await client.get(path)
        assert loaded.status_code == 200 and loaded.json()["can_resolve"], loaded.text
        decided = await client.post(path + "/resolve", json={"action": "approve"})
        assert decided.status_code == 200 and decided.json()["status"] == "approved", decided.text
        return {**body, "approval_id": ticket_id}
    takeover = await approved("take_control", request(seed, view))
    assert (await owner.read(source))["control_mode"] == "agent"
    taken = await client.post(base + "/browser/take_control", json=takeover)
    assert taken.status_code == 200, taken.text
    view = taken.json()["browser"]
    assert view["control_mode"] == "human"
    assert (await client.post(base + "/browser/take_control", json=takeover)).status_code == 409
    secret = "synthetic-human-login-only"
    if remote:
        from app.api import browser_control as api
        from app.services.browser_control import BrowserControlService
        monkeypatch.setattr(api, "browser_control", BrowserControlService())
    typed = await approved("type", request(seed, view, text=secret))
    changed = await client.post(base + "/browser/type", json={**typed, "arguments": {"text": "changed-input"}})
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "browser_approval_input_changed"
    async with owner_sessionmaker() as db:
        ticket = await db.get(ApprovalRequest, UUID(typed["approval_id"]))
        assert ticket.consumed_at is None and ticket.execution_status == "approved"
        logs = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == seed["agent_id"])))
        persisted = [ticket.details, ticket.execution_envelope, ticket.normalized_arguments,
                     *[row.details for row in logs]]
        assert secret not in json.dumps(persisted)
    current = await client.get(base + "/browser", params={"expected_user_id": str(seed["user_id"]),
        "expected_tenant_id": str(seed["tenant_id"])})
    assert current.status_code == 200 and secret not in current.text
    reference = next(row for row in current.json()["approvals"]["items"] if row["id"] == typed["approval_id"])
    assert reference["action"] == "type" and reference["request_id"] == typed["request_id"]
    opaque = {key: value for key, value in typed.items() if key != "arguments"}
    result = await client.post(base + "/browser/type", json=opaque)
    assert result.status_code == 200, result.text
    assert (await client.post(base + "/browser/type", json=typed)).status_code == 409
    assert (root / view["id"] / "effects").read_text().splitlines() == [secret]
    async with owner_sessionmaker() as db:
        ticket = await db.get(ApprovalRequest, UUID(typed["approval_id"]))
        assert ticket.consumed_at is not None and ticket.execution_status == "succeeded"
        assert ticket.execution_receipt["automatic_replay"] is False
        assert ticket.execution_receipt["origin_session_id"] == str(seed["session_id"])
        assert ticket.tool_name is None and ticket.execution_task_id is None
    returned = await client.post(base + "/browser/return_control", json=request(seed, result.json()["browser"]))
    assert returned.status_code == 200 and returned.json()["browser"]["fresh_observation_required"], returned.text


@pytest.mark.parametrize("change", ["expiry", "reject", "company", "account", "principal", "control"])
async def test_human_approval_cannot_survive_denial_or_changed_source(
    browser_case, owner_sessionmaker, app_user_sessionmaker, change
):
    from app.models.user import User
    seed, source, owner, client, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    view = (await call(owner, source, "take_control", view, actor_kind="human"))["browser"]
    await approval_client(seed, client, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        policy = CapabilityPolicy(tenant_id=seed["tenant_id"], capability="computer.browser.control",
            allowed=True, requires_approval=True)
        db.add(policy)
        await db.commit()
        policy_id = policy.id
    base = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    body = request(seed, view, text="denied-human-effect")
    pending = await client.post(base + "/browser/type", json=body)
    assert pending.status_code == 409, pending.text
    approval_id = pending.json()["detail"]["approval_id"]
    decided = await client.post(base + f"/approvals/{approval_id}/resolve",
        json={"action": "reject" if change == "reject" else "approve"})
    assert decided.status_code == 200, decided.text
    if change == "account":
        body["expected_user_id"] = str(uuid4())
    elif change == "control":
        await call(owner, source, "return_control", view, actor_kind="human")
    else:
        async with owner_sessionmaker() as db:
            if change == "expiry":
                (await db.get(ApprovalRequest, UUID(approval_id))).expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif change == "company":
                (await db.get(CapabilityPolicy, policy_id)).allowed = False
            elif change == "principal":
                (await db.get(User, seed["user_id"])).is_active = False
            await db.commit()
    refused = await client.post(base + "/browser/type", json={**body, "approval_id": approval_id})
    assert refused.status_code in {403, 409}, refused.text
    assert refused.json()["detail"]["effect"] == "not_started"
    assert not (root / view["id"] / "effects").exists()
    async with owner_sessionmaker() as db:
        assert (await db.get(ApprovalRequest, UUID(approval_id))).consumed_at is None


@pytest.mark.parametrize("remote", [False, True])
async def test_human_unknown_effect_persists_consumed_approval_without_replay(
    browser_case, browser_redis, owner_sessionmaker, app_user_sessionmaker, monkeypatch, remote
):
    seed, source, owner, client, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    view = (await call(owner, source, "take_control", view, actor_kind="human"))["browser"]
    await approval_client(seed, client, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="computer.browser.control",
            allowed=True, requires_approval=True))
        await db.commit()
    base = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    body = request(seed, view, text="human-effect-before-error")
    pending = await client.post(base + "/browser/type", json=body)
    assert pending.status_code == 409, pending.text
    approval_id = pending.json()["detail"]["approval_id"]
    decided = await client.post(base + f"/approvals/{approval_id}/resolve", json={"action": "approve"})
    assert decided.status_code == 200, decided.text
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call
    async def fail_after_effect(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.insertText":
            raise TypeError("synthetic-private-internal-error")
        return result
    monkeypatch.setattr(pipe, "call", fail_after_effect)
    if remote:
        from app.api import browser_control as api
        from app.services.browser_control import BrowserControlService
        monkeypatch.setattr(api, "browser_control", BrowserControlService())
    failed = await client.post(base + "/browser/type", json={**body, "approval_id": approval_id})
    assert failed.status_code != 200 and failed.json()["detail"]["effect"] == "unknown", failed.text
    assert "synthetic-private-internal-error" not in failed.text
    assert (await client.post(base + "/browser/type", json={**body, "approval_id": approval_id})).status_code == 409
    assert (root / view["id"] / "effects").read_text().splitlines() == ["human-effect-before-error"]
    async with owner_sessionmaker() as db:
        ticket = await db.get(ApprovalRequest, UUID(approval_id))
        assert ticket.execution_status == "needs_reconciliation" and ticket.consumed_at is not None
        assert ticket.execution_receipt["automatic_replay"] is False
    current = await owner.read(source)
    assert current["status"] == "lost"
    monitor = owner._monitors[UUID(view["id"])]
    stopped = await client.post(base + "/browser/stop", json=request(seed, current))
    if remote:
        assert stopped.status_code == 200 and stopped.json()["browser"]["desired_state"] == "stopped", stopped.text
        await asyncio.wait_for(asyncio.shield(monitor), 5)
        stopped = await client.get(base + "/browser", params={"expected_user_id": str(seed["user_id"]),
            "expected_tenant_id": str(seed["tenant_id"])})
    assert stopped.status_code == 200 and stopped.json()["browser"]["status"] == "stopped", stopped.text


async def test_foreign_work_binding_is_rejected_before_saved_input_decryption(
    browser_case, owner_sessionmaker, app_user_sessionmaker, monkeypatch
):
    from app.models.chat_session import ChatSession
    from app.services import browser_human_approval as approval

    seed, source, owner, client, root = browser_case
    view = (await call(owner, source, "start"))["browser"]
    view = (await call(owner, source, "take_control", view, actor_kind="human"))["browser"]
    await approval_client(seed, client, app_user_sessionmaker)
    other_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="computer.browser.control",
            allowed=True, requires_approval=True))
        db.add(ChatSession(id=other_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=seed["user_id"]))
        await db.commit()
    base = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    body = request(seed, view, text="original-private-work-only")
    pending = await client.post(base + "/browser/type", json=body)
    assert pending.status_code == 409, pending.text
    ticket_id = pending.json()["detail"]["approval_id"]
    decided = await client.post(base + f"/approvals/{ticket_id}/resolve", json={"action": "approve"})
    assert decided.status_code == 200, decided.text
    decryptions = []
    original = approval.decrypt_channel_secret
    def observed_decryption(value):
        decryptions.append(True)
        return original(value)
    monkeypatch.setattr(approval, "decrypt_channel_secret", observed_decryption)
    wrong = f"/agents/{seed['agent_id']}/sessions/{other_id}/browser/type"
    refused = await client.post(wrong, json={**body, "approval_id": ticket_id})
    assert refused.status_code in {403, 409}, refused.text
    assert decryptions == []
    assert not (root / view["id"] / "effects").exists()
    async with owner_sessionmaker() as db:
        assert (await db.get(ApprovalRequest, UUID(ticket_id))).consumed_at is None
