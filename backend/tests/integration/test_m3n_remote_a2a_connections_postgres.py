"""Real application-role endpoint configuration and governed local HTTP discovery."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.remote_a2a import RemoteA2AConnection
from app.models.user import User
from tests.integration.test_m3h_machine_identity_postgres import machine_api as _machine_api
from tests.services.test_remote_a2a_transport import remote_server

machine_api = _machine_api
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def request_frame(seed, version=None):
    return {
        "expected_user_id": str(seed["owner"]),
        "expected_tenant_id": str(seed["tenant"]),
        "expected_version": version,
    }


def configuration(seed, remote):
    return {
        **request_frame(seed),
        "request_id": str(uuid4()),
        "card_url": remote.origin + "/card",
        "rpc_url": remote.origin + "/rpc",
        "bearer_token": "synthetic-remote-credential",
        "allowed_networks": ["127.0.0.1/32"],
    }


async def test_remote_configuration_encrypts_credentials_and_discovers_without_claiming_control(
    machine_api, owner_sessionmaker
):
    seed, api = machine_api
    path = f"/agents/{seed['agent']}/remote-a2a"
    async with owner_sessionmaker() as db:
        initial_agent = await db.get(Agent, seed["agent"])
        original_runtime = (initial_agent.agent_type, initial_agent.primary_model_id, initial_agent.fallback_model_id)
    with remote_server() as remote:
        body = configuration(seed, remote)
        created = await api.put(path, json=body)
        assert created.status_code == 200, created.text
        assert created.json()["state"] == "configured" and created.json()["version"] == 1
        assert "synthetic-remote-credential" not in created.text
        replay = await api.put(path, json=body)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] and replay.json()["version"] == 1
        async with owner_sessionmaker() as db:
            stored = await db.scalar(
                text("SELECT credential_config FROM remote_a2a_connections WHERE agent_id=:id"), {"id": seed["agent"]}
            )
            assert "__hive_agent_tool_config_v1__" in stored and "synthetic-remote-credential" not in str(stored)
        remote.card["vendorDeclaration"] = {"actionInterception": True, "cost": 0}
        found = await api.post(path + "/discover", json=request_frame(seed, 1))
        assert found.status_code == 200, found.text
        assert found.json()["state"] == "discovered" and found.json()["agent_card"]["name"] == "Reference Agent"
        assert found.json()["effect_control"] == "unverified" and found.json()["cost_metering"] == "unknown"
        assert found.json()["runtime_activation"] == "not_activated"
        assert remote.requests[0][2]["Authorization"] == "Bearer synthetic-remote-credential"
        listed = await api.get(path)
        assert listed.status_code == 200 and "synthetic-remote-credential" not in listed.text
        assert listed.headers["cache-control"] == "private, no-store"
        async with owner_sessionmaker() as db:
            agent = await db.get(Agent, seed["agent"])
            assert (agent.agent_type, agent.primary_model_id, agent.fallback_model_id) == original_runtime


async def test_remote_discovery_cannot_restore_configuration_disabled_while_reply_in_flight(machine_api, monkeypatch):
    from app.services.remote_a2a_transport import RemoteA2AEndpoint

    seed, api = machine_api
    path = f"/agents/{seed['agent']}/remote-a2a"
    received, release = asyncio.Event(), asyncio.Event()
    original = RemoteA2AEndpoint.discover

    async def delayed(endpoint):
        result = await original(endpoint)
        received.set()
        await release.wait()
        return result

    monkeypatch.setattr(RemoteA2AEndpoint, "discover", delayed)
    with remote_server() as remote:
        assert (await api.put(path, json=configuration(seed, remote))).status_code == 200
        pending = asyncio.create_task(api.post(path + "/discover", json=request_frame(seed, 1)))
        try:
            await asyncio.wait_for(received.wait(), 10)
            disabled = await asyncio.wait_for(api.post(path + "/disable", json=request_frame(seed, 1)), 5)
            assert disabled.status_code == 200 and disabled.json()["state"] == "disabled"
            release.set()
            stale = await asyncio.wait_for(pending, 10)
            assert stale.status_code == 409
            listed = await api.get(path)
            assert listed.json()["state"] == "disabled" and listed.json()["agent_card"] is None
            assert listed.json()["credential_configured"] is False and len(remote.requests) == 1
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_remote_connection_keeps_current_admin_scope_and_owner_can_disable(machine_api, app_user_sessionmaker):
    seed, api = machine_api
    path = f"/agents/{seed['agent']}/remote-a2a"
    with remote_server() as remote:
        body = configuration(seed, remote)
        assert (await api.put(path, json={**body, "expected_tenant_id": str(uuid4())})).status_code == 409
        assert (await api.put(path, json=body)).status_code == 200
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            owner = await db.get(User, seed["owner"])
            owner.role = "member"
            await db.commit()
        assert (await api.post(path + "/discover", json=request_frame(seed, 1))).status_code == 403
        updated = {**body, "expected_version": 1, "request_id": str(uuid4())}
        assert (await api.put(path, json=updated)).status_code == 403
        assert not (await api.get(path)).json()["can_configure"] and not remote.requests
        revoked = await api.post(path + "/disable", json=request_frame(seed, 1))
        assert revoked.status_code == 200 and revoked.json()["state"] == "disabled"


async def test_remote_connection_is_forced_tenant_scoped(machine_api, app_user_sessionmaker, owner_sessionmaker):
    from tests.integration.test_local_agent_channel_protocol import _seed

    seed, api = machine_api
    with remote_server() as remote:
        assert (
            await api.put(f"/agents/{seed['agent']}/remote-a2a", json=configuration(seed, remote))
        ).status_code == 200
    other_tenant, _, other_agent, _ = await _seed(owner_sessionmaker)
    assert (await api.get(f"/agents/{other_agent}/remote-a2a")).status_code == 404
    async with tenant_scoped_session(other_tenant, session_factory=app_user_sessionmaker) as db:
        assert await db.scalar(select(RemoteA2AConnection).where(RemoteA2AConnection.agent_id == seed["agent"])) is None
        superuser, bypass = (
            await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one()
        assert not superuser and not bypass
        enabled, forced = (
            await db.execute(
                text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='remote_a2a_connections'")
            )
        ).one()
        assert enabled and forced


async def test_remote_configuration_preserves_secret_only_with_current_owner_scope(machine_api, app_user_sessionmaker):
    from app.models.audit import AuditLog

    seed, api = machine_api
    path = f"/agents/{seed['agent']}/remote-a2a"
    with remote_server() as remote:
        body = configuration(seed, remote)
        assert (await api.put(path, json=body)).status_code == 200
        updated = {**body, "request_id": str(uuid4()), "expected_version": 1}
        updated.pop("bearer_token")
        leaked = await api.put(
            path,
            json={
                **updated,
                "bearer_token": body["bearer_token"],
                "rpc_url": remote.origin + "/synthetic-remote-credential",
            },
        )
        assert leaked.status_code == 422 and "synthetic-remote-credential" not in leaked.text
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            db.add(
                AuditLog(
                    tenant_id=seed["tenant"],
                    agent_id=seed["agent"],
                    user_id=seed["owner"],
                    action="agent:handover",
                    details={"reason": "Ownership epoch changed"},
                )
            )
            await db.commit()
        assert (await api.get(path)).json()["state"] == "needs_reauthorization"
        stale = await api.put(path, json=updated)
        assert stale.status_code == 409
        assert (await api.post(path + "/discover", json=request_frame(seed, 1))).status_code == 409
        assert not remote.requests


async def test_actual_management_tab_configures_discovers_and_disables_remote_connection(
    machine_api,
    app_user_sessionmaker,
    tmp_path,
):
    from fastapi import FastAPI
    from app.api import a2a, agents
    from app.database import get_db
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

    seed, _api = machine_api
    web = FastAPI()
    web.include_router(agents.router, prefix="/api")
    web.include_router(a2a.router, prefix="/api")

    async def database():
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            yield db

    web.dependency_overrides[get_db] = database
    with remote_server() as remote:
        await _exercise_dom_bridge(
            web,
            {"user_id": seed["owner"], "tenant_id": seed["tenant"], "agent_id": seed["agent"]},
            tmp_path,
            entry_file="e2e/remote-a2a-connection-native.tsx",
            extra_env={"HIVE_M3N_TOKEN": seed["user_token"], "HIVE_M3N_REMOTE_ORIGIN": remote.origin},
        )
        assert len(remote.requests) == 1 and remote.requests[0][0] == "GET"
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        connection = await db.get(RemoteA2AConnection, seed["agent"])
        credentials = await db.scalar(
            select(RemoteA2AConnection.credential_config).where(RemoteA2AConnection.agent_id == seed["agent"])
        )
        assert connection.state == "disabled" and connection.version == 2 and credentials == {}


async def test_owner_can_replace_or_disable_an_unreadable_remote_credential(machine_api, owner_sessionmaker):
    import json

    seed, api = machine_api
    path = f"/agents/{seed['agent']}/remote-a2a"

    async def corrupt():
        async with owner_sessionmaker() as db:
            await db.execute(
                text("UPDATE remote_a2a_connections SET credential_config=CAST(:value AS json) WHERE agent_id=:id"),
                {
                    "id": seed["agent"],
                    "value": json.dumps(
                        {"__hive_agent_tool_config_v1__": "hive:agent-tool-config:v1:retired:unreadable"}
                    ),
                },
            )
            await db.commit()

    with remote_server() as remote:
        body = configuration(seed, remote)
        assert (await api.put(path, json=body)).status_code == 200
        await corrupt()
        listed = await api.get(path)
        assert listed.status_code == 200 and listed.json()["credential_configured"]
        failed = await api.post(path + "/discover", json=request_frame(seed, 1))
        assert failed.status_code == 503 and not remote.requests
        replaced = await api.put(path, json={**body, "request_id": str(uuid4()), "expected_version": 1})
        assert replaced.status_code == 200 and replaced.json()["version"] == 2
        found = await api.post(path + "/discover", json=request_frame(seed, 2))
        assert found.status_code == 200 and found.json()["state"] == "discovered"
        await corrupt()
        disabled = await api.post(path + "/disable", json=request_frame(seed, 2))
        assert disabled.status_code == 200 and not disabled.json()["credential_configured"]


async def test_changing_remote_addresses_cannot_forward_a_previously_stored_secret(machine_api):
    seed, api = machine_api
    path = f"/agents/{seed['agent']}/remote-a2a"
    with remote_server() as original, remote_server() as other:
        assert (await api.put(path, json=configuration(seed, original))).status_code == 200
        changed = {**configuration(seed, other), "expected_version": 1}
        changed.pop("bearer_token")
        response = await api.put(path, json=changed)
        if response.status_code == 200:
            # Capture the real credential sink reached by the vulnerable path.
            await api.post(path + "/discover", json=request_frame(seed, response.json()["version"]))
        assert response.status_code == 409, {
            "response": response.json(),
            "other_endpoint_requests": len(other.requests),
        }
        assert not other.requests
        current = await api.get(path)
        assert current.json()["version"] == 1 and current.json()["rpc_url"] == original.origin + "/rpc"
        authorized = await api.put(
            path, json={**changed, "request_id": str(uuid4()), "bearer_token": "synthetic-new-destination"}
        )
        assert authorized.status_code == 200 and authorized.json()["version"] == 2
        discovered = await api.post(path + "/discover", json=request_frame(seed, 2))
        assert discovered.status_code == 200 and discovered.json()["state"] == "discovered"
        assert len(other.requests) == 1
        assert other.requests[0][2]["Authorization"] == "Bearer synthetic-new-destination"
