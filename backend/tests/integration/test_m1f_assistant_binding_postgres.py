"""Mounted My Assistant binding, provisioning, and continuation.

Routes, transactions, RLS, and session admission are real. Model dispatch after
admission is synthetic_transport. These assertions record the failing cases
from before the binding existed; do not weaken them to make a new path pass.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from app.api.agents import router as agents_router
from app.api.assistant import router as assistant_router
from app.api.chat_sessions import router as chat_sessions_router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.agent_manager import AgentManager


def _user(db, **kwargs) -> User:
    kwargs.setdefault("password_hash", "stored-password-hash")
    kwargs.setdefault("role", "member")
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("display_name", kwargs["username"])
    row = User(**kwargs)
    db.add(row)
    return row


def _actor(**kwargs):
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("department_id", None)
    return SimpleNamespace(**kwargs)


def _app(sessionmaker, holder):
    async def db_override():
        async with sessionmaker() as session:
            try:
                await pin_rls_tenant_context(session, getattr(holder["actor"], "tenant_id", None))
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(assistant_router, prefix="/api")
    app.include_router(agents_router, prefix="/api")
    app.include_router(chat_sessions_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: holder["actor"]
    app.dependency_overrides[get_db] = db_override
    return app


def _client(sessionmaker, holder) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(sessionmaker, holder)), base_url="http://m1f")


async def _rows(sessionmaker, sql: str, **params) -> list[tuple]:
    async with sessionmaker() as db:
        return [tuple(row) for row in (await db.execute(text(sql), params)).all()]


async def _scalar(sessionmaker, sql: str, **params):
    found = await _rows(sessionmaker, sql, **params)
    return found[0][0] if found else None


def _detail(response) -> dict:
    body = response.json()
    detail = body.get("detail")
    return detail if isinstance(detail, dict) else {"message": detail}


async def _model(db, tenant_id, *, label: str, enabled: bool = True) -> LLMModel:
    row = LLMModel(
        provider="openai",
        model=f"synthetic-{label}",
        api_key_encrypted="synthetic-not-a-key",
        label=label,
        tenant_id=tenant_id,
        enabled=enabled,
    )
    db.add(row)
    await db.flush()
    return row


async def _agent(db, *, tenant_id, owner_id, name: str, model_id=None, **extra) -> Agent:
    agent_id = uuid.uuid4()
    participant = Participant(type="agent", ref_id=agent_id, display_name=name[:100])
    db.add(participant)
    await db.flush()
    agent = Agent(
        id=agent_id,
        name=name[:100],
        role_description="Existing employee",
        creator_id=owner_id,
        sponsor_user_id=owner_id,
        owner_user_id=extra.pop("owner_user_id", owner_id),
        participant_id=participant.id,
        tenant_id=tenant_id,
        status="idle",
        primary_model_id=model_id,
        **extra,
    )
    db.add(agent)
    await db.flush()
    return agent


@pytest.fixture(autouse=True)
def _agent_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.agent_manager.settings.AGENT_DATA_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path / "agents")
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path / "tool-workspaces")
    (tmp_path / "agents").mkdir()
    monkeypatch.setattr("app.services.runtime_task_worker.runtime_task_worker_snapshot", lambda: {"running": True})


def test_assistant_router_is_mounted_for_both_api_prefixes():
    from app.main import _api_routers, app

    assert assistant_router in _api_routers
    paths = set(app.openapi()["paths"])
    assert "/api/assistant" in paths
    assert "/api/v1/assistant" in paths


async def test_existing_employees_are_not_guessed_and_select_is_explicit(
    owner_sessionmaker, app_user_sessionmaker,
):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    user_id, peer_id = uuid.uuid4(), uuid.uuid4()
    secret = f"secret-foreign-company-{tag}"

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1F A {tag}", slug=f"m1f-a-{tag}"))
        db.add(Tenant(id=tenant_b, name=secret, slug=f"m1f-b-{tag}"))
        await db.flush()
        approved = await _model(db, tenant_a, label=f"Approved {tag}")
        await _model(db, tenant_a, label=f"Other {tag}")
        db.add(TenantSetting(tenant_id=tenant_a, key="default_model_id", value={"model_id": str(approved.id)}))
        _user(db, id=user_id, username=f"member-{tag}", email=f"member-{tag}@example.test", tenant_id=tenant_a)
        _user(db, id=peer_id, username=f"peer-{tag}", email=f"peer-{tag}@example.test", tenant_id=tenant_a)
        root = await _agent(db, tenant_id=tenant_a, owner_id=user_id, name="Root", model_id=approved.id)
        named = await _agent(db, tenant_id=tenant_a, owner_id=user_id, name="助理", model_id=approved.id)
        chosen = await _agent(db, tenant_id=tenant_a, owner_id=user_id, name=f"Ledger {tag}", model_id=approved.id)
        await _agent(db, tenant_id=tenant_a, owner_id=user_id, name="Child", model_id=approved.id, parent_agent_id=root.id)
        await _agent(
            db, tenant_id=tenant_a, owner_id=user_id, name="__system_hr__", model_id=approved.id, agent_class="internal_system",
        )
        await _agent(db, tenant_id=tenant_a, owner_id=user_id, name="Local", model_id=approved.id, agent_type="local_agent")
        foreign = await _agent(db, tenant_id=tenant_b, owner_id=peer_id, name=secret, model_id=None)
        deleted = await _agent(db, tenant_id=tenant_a, owner_id=user_id, name="Deleted", model_id=approved.id)
        deleted.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        ids = {
            "root": root.id, "named": named.id, "chosen": chosen.id, "foreign": foreign.id, "deleted": deleted.id,
        }

    holder = {"actor": _actor(id=user_id, tenant_id=tenant_a, role="member")}
    async with _client(app_user_sessionmaker, holder) as client:
        loaded = await client.get("/api/assistant")
        assert loaded.status_code == 200, loaded.text
        body = loaded.json()
        assert body["schema"] == "hive.assistant_binding.v1"
        assert body["state"] == "unbound"
        assert body["agent"] is None
        assert body["readiness"]["model"] == "ok"
        assert body["readiness"]["model_label"] == f"Approved {tag}"
        assert body["readiness"]["ready"] is False
        assert body["applied"] is False and body["created"] is False
        assert body["legacy_provenance"] == "not_established"
        assert body["binding_grants_access"] is False
        assert body["candidates"] and all(item["selected"] is False for item in body["candidates"])
        assert str(ids["root"]) != body.get("agent")
        assert secret not in loaded.text
        for key in ("foreign", "deleted"):
            rejected = await client.post("/api/assistant/select", json={"agent_id": str(ids[key])})
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["applied"] is False
            assert secret not in rejected.text
        child = await _rows(owner_sessionmaker, "SELECT id::text FROM agents WHERE parent_agent_id = :id", id=ids["root"])
        rejected_child = await client.post("/api/assistant/select", json={"agent_id": child[0][0]})
        assert rejected_child.status_code == 200 and rejected_child.json()["applied"] is False
        hr = await _rows(owner_sessionmaker, "SELECT id::text FROM agents WHERE name = '__system_hr__' AND tenant_id = :t", t=tenant_a)
        rejected_hr = await client.post("/api/assistant/select", json={"agent_id": hr[0][0]})
        assert rejected_hr.status_code == 200 and rejected_hr.json()["applied"] is False
        local = await _rows(owner_sessionmaker, "SELECT id::text FROM agents WHERE agent_type = 'local_agent' AND tenant_id = :t", t=tenant_a)
        rejected_local = await client.post("/api/assistant/select", json={"agent_id": local[0][0]})
        assert rejected_local.status_code == 200 and rejected_local.json()["applied"] is False
        assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM user_assistant_bindings WHERE user_id = :u", u=user_id) == 0

        permissions_before = await _scalar(
            owner_sessionmaker, "SELECT count(*) FROM agent_permissions WHERE agent_id = :id", id=ids["chosen"],
        )
        resources_before = await _scalar(
            owner_sessionmaker, "SELECT count(*) FROM resource_permissions WHERE resource_id = :id", id=ids["chosen"],
        )
        selected = await client.post("/api/assistant/select", json={"agent_id": str(ids["chosen"])})
        assert selected.status_code == 200, selected.text
        chosen_body = selected.json()
        assert chosen_body["applied"] is True and chosen_body["created"] is False
        assert chosen_body["agent"]["id"] == str(ids["chosen"])
        assert chosen_body["agent"]["name"] == f"Ledger {tag}"
        assert chosen_body["state"] == "active"
        assert chosen_body["readiness"]["ready"] is False
        assert "workspace_missing" in chosen_body["readiness"]["blockers"]
        assert chosen_body["binding_grants_access"] is False
        assert all(item["selected"] is False for item in chosen_body["candidates"])
        again = await client.get("/api/assistant")
        assert again.json()["agent"]["id"] == str(ids["chosen"])

    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agents WHERE name = 'My Assistant' AND creator_id = :u", u=user_id) == 0
    assert await _scalar(owner_sessionmaker, "SELECT agent_id::text FROM user_assistant_bindings WHERE user_id = :u", u=user_id) == str(ids["chosen"])
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agent_permissions WHERE agent_id = :id", id=ids["chosen"]) == permissions_before
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM resource_permissions WHERE resource_id = :id", id=ids["chosen"]) == resources_before
    owner = await _rows(
        owner_sessionmaker,
        "SELECT creator_id::text, owner_user_id::text, sponsor_user_id::text FROM agents WHERE id = :id",
        id=ids["chosen"],
    )
    assert owner == [(str(user_id), str(user_id), str(user_id))]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_b)
        visible = (await db.execute(text("SELECT id FROM user_assistant_bindings"))).all()
        assert visible == []


async def test_unbound_model_preview_uses_only_enabled_same_company_default(
    owner_sessionmaker, app_user_sessionmaker,
):
    tag = uuid.uuid4().hex[:8]
    tenant_id, foreign_tenant, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add_all([
            Tenant(id=tenant_id, name=f"Preview {tag}", slug=f"preview-{tag}"),
            Tenant(id=foreign_tenant, name=f"Foreign {tag}", slug=f"foreign-preview-{tag}"),
        ])
        await db.flush()
        _user(db, id=user_id, username=f"preview-{tag}", email=f"preview-{tag}@example.test", tenant_id=tenant_id)
        await _model(db, tenant_id, label=f"Unselected oldest {tag}")
        approved = await _model(db, tenant_id, label=f"Preview approved {tag}")
        disabled = await _model(db, tenant_id, label=f"Disabled private {tag}", enabled=False)
        foreign = await _model(db, foreign_tenant, label=f"Foreign private {tag}")
        approved_id, disabled_id, foreign_id = approved.id, disabled.id, foreign.id
        await db.commit()

    cases = [
        (None, "unconfigured", None),
        ("malformed-id", "forbidden", None),
        (str(disabled_id), "forbidden", None),
        (str(foreign_id), "forbidden", None),
        (str(approved_id), "ok", f"Preview approved {tag}"),
    ]
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}
    async with _client(app_user_sessionmaker, holder) as client:
        for model_id, expected_state, label in cases:
            async with owner_sessionmaker() as db:
                await db.execute(text("DELETE FROM tenant_settings WHERE tenant_id = :t AND key = 'default_model_id'"), {"t": tenant_id})
                if model_id is not None:
                    db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": model_id}))
                await db.commit()
            response = await client.get("/api/assistant")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["state"] == "unbound" and body["agent"] is None
            assert body["readiness"]["model"] == expected_state
            assert body["readiness"]["model_label"] == label
            assert body["readiness"]["ready"] is False
            assert body["created"] is False and body["applied"] is False
            assert f"Foreign private {tag}" not in response.text
            assert f"Disabled private {tag}" not in response.text
            assert f"Unselected oldest {tag}" not in response.text
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM user_assistant_bindings WHERE user_id = :u", u=user_id) == 0
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agents WHERE creator_id = :u", u=user_id) == 0


@pytest.mark.parametrize("fail_before_workspace", [False, True])
async def test_opened_assistant_retains_original_context_sources_without_attesting_existing_files(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, fail_before_workspace,
):
    import hashlib

    from app.models.config_revision import ConfigRevision
    from app.services.agent_context import agent_identity_content, load_agent_context_resource
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.native_employee_provisioning import resume_native_employee

    tag = uuid.uuid4().hex[:8]
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"Source {tag}", slug=f"assistant-source-{tag}"))
        await db.flush()
        model = await _model(db, tenant_id, label=f"Source model {tag}")
        _user(db, id=user_id, username=f"source-{tag}", email=f"source-{tag}@example.test", tenant_id=tenant_id)
        db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(model.id)}))
        await db.commit()

    initialize = AgentManager.initialize_agent_files

    async def initialize_once(self, db, agent, *args, **kwargs):
        if fail_before_workspace and not getattr(initialize_once, "failed", False):
            initialize_once.failed = True
            raise OSError("Synthetic failure before any workspace write")
        return await initialize(self, db, agent, *args, **kwargs)

    monkeypatch.setattr(AgentManager, "initialize_agent_files", initialize_once)
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}
    async with _client(app_user_sessionmaker, holder) as client:
        opened = await client.post("/api/assistant/open", json={"idempotency_key": f"source-{tag}"})
        assert opened.status_code == 200, opened.text
        agent_id = uuid.UUID(opened.json()["agent"]["id"])
        if fail_before_workspace:
            assert opened.json()["state"] == "provision_incomplete"
            opened = await client.post("/api/assistant/open", json={"idempotency_key": f"source-{tag}"})
            assert opened.status_code == 200 and opened.json()["agent"]["id"] == str(agent_id)
        assert opened.json()["state"] == "active"

    async def context_dependencies(agent):
        soul = await load_agent_context_resource(agent_id=agent.id, tenant_id=tenant_id, resource_ref="soul")
        assert soul.status == "ok"
        self_path = AgentManager()._agent_dir(agent.id) / "memory/self/self.md"
        return [
            {"source_kind": "agent_identity", "source_id": f"{agent.id}\nidentity",
             "source_version": canonical_content_hash(agent_identity_content(agent.name, agent.role_description))},
            {"source_kind": "agent_context_resource", "source_id": f"{agent.id}\nsoul", "source_version": soul.sha256},
            {"source_kind": "agent_resident_memory", "source_id": f"{agent.id}\nmemory/self/self.md",
             "source_version": hashlib.sha256(self_path.read_bytes()).hexdigest()},
        ]

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        actor = await db.get(User, user_id)
        agent = await db.get(Agent, agent_id)
        dependencies = await context_dependencies(agent)
        await _validate_input_dependencies(db, tenant_id, dependencies, user=actor)
        revision = (await db.execute(select(ConfigRevision).where(
            ConfigRevision.tenant_id == tenant_id, ConfigRevision.entity_id == agent_id,
            ConfigRevision.entity_type == "agent_bootstrap",
        ))).scalar_one()
        assert revision.changed_by_user_id == user_id and revision.changed_by_agent_id is None
        assert revision.change_source == "user" and revision.version == 1

    async with owner_sessionmaker() as db:
        legacy = await _agent(db, tenant_id=tenant_id, owner_id=user_id, name=f"Legacy source {tag}")
        await AgentManager().initialize_agent_files(db, legacy)
        legacy_id = legacy.id
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        actor = await db.get(User, user_id)
        legacy = await db.get(Agent, legacy_id)
        dependencies = await context_dependencies(legacy)
        await resume_native_employee(db, legacy, actor_id=user_id, audit_action="assistant.provisioned")
        with pytest.raises(ContentInputReceiptError, match="complete original creation sources"):
            await _validate_input_dependencies(db, tenant_id, dependencies, user=actor)
        assert (await db.execute(select(ConfigRevision.id).where(
            ConfigRevision.tenant_id == tenant_id, ConfigRevision.entity_id == legacy_id,
            ConfigRevision.entity_type == "agent_bootstrap",
        ))).scalar_one_or_none() is None


async def test_open_is_stable_and_resume_does_not_duplicate(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, owner_engine,
):
    tag = uuid.uuid4().hex[:8]
    tenant_id, user_id, bare_id, blocked_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"M1F open {tag}", slug=f"m1f-open-{tag}"))
        await db.flush()
        approved = await _model(db, tenant_id, label=f"Approved {tag}")
        other = await _model(db, tenant_id, label=f"Other {tag}")
        disabled = await _model(db, tenant_id, label=f"Disabled {tag}", enabled=False)
        _user(db, id=user_id, username=f"open-{tag}", email=f"open-{tag}@example.test", tenant_id=tenant_id)
        _user(db, id=bare_id, username=f"bare-{tag}", email=f"bare-{tag}@example.test", tenant_id=tenant_id)
        _user(db, id=blocked_id, username=f"blocked-{tag}", email=f"blocked-{tag}@example.test", tenant_id=tenant_id)
        db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(approved.id)}))
        await db.commit()
        model_ids = {"approved": approved.id, "other": other.id, "disabled": disabled.id}

    monkeypatch.setattr("app.services.runtime_task_worker.runtime_task_worker_enabled", lambda: True)
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}
    async with _client(app_user_sessionmaker, holder) as client:
        first, second = await asyncio.gather(
            client.post("/api/assistant/open", json={"idempotency_key": f"one-{tag}"}),
            client.post("/api/assistant/open", json={"idempotency_key": f"two-{tag}"}),
        )
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert first.json()["agent"]["id"] == second.json()["agent"]["id"]
    agent_id = first.json()["agent"]["id"]
    assert {first.json()["created"], second.json()["created"]} == {True, False}
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agents WHERE creator_id = :u AND name = 'My Assistant'", u=user_id) == 1
    facts = await _rows(
        owner_sessionmaker,
        """
        SELECT creator_id::text, sponsor_user_id::text, owner_user_id::text, agent_type, agent_class,
               primary_model_id::text, fallback_model_id::text
        FROM agents WHERE id = :id
        """,
        id=agent_id,
    )
    assert facts == [(str(user_id), str(user_id), str(user_id), "native", "internal_tenant", str(model_ids["approved"]), None)]
    permissions = await _rows(
        owner_sessionmaker,
        "SELECT scope_type::text, scope_id::text, access_level FROM agent_permissions WHERE agent_id = :id",
        id=agent_id,
    )
    assert permissions == [("user", str(user_id), "manage")]
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM resource_permissions WHERE resource_id = :id", id=agent_id) == 0
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE agent_id = :id", id=agent_id) == 0
    monkeypatch.setattr("app.services.runtime_task_worker.runtime_task_worker_snapshot", lambda: {"running": False})
    async with _client(app_user_sessionmaker, holder) as client:
        unverified_worker = await client.get("/api/assistant")
    assert unverified_worker.json()["readiness"]["ready"] is False
    assert unverified_worker.json()["readiness"]["worker"] == "unknown"
    monkeypatch.setattr("app.services.runtime_task_worker.runtime_task_worker_snapshot", lambda: {"running": True})
    assert await _scalar(
        owner_sessionmaker,
        "SELECT count(*) FROM security_audit_events WHERE coalesce(details->>'blueprint_click', 'false') = 'true'",
    ) == 0
    async with _client(app_user_sessionmaker, holder) as restarted:
        loaded = await restarted.get("/api/assistant")
        assert loaded.status_code == 200 and loaded.json()["agent"]["id"] == agent_id
        assert loaded.json()["readiness"]["ready"] is True

    url = owner_engine.url.render_as_string(hide_password=False)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import asyncio,sys\n"
        "from sqlalchemy import text\n"
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "async def main():\n"
        "    engine=create_async_engine(sys.argv[1])\n"
        "    async with engine.connect() as conn:\n"
        "        row=(await conn.execute(text('SELECT agent_id::text FROM user_assistant_bindings WHERE user_id=:u'), {'u': sys.argv[2]})).one()\n"
        "        print(row[0])\n"
        "    await engine.dispose()\n"
        "asyncio.run(main())\n",
        url, str(user_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert stdout.decode().strip() == agent_id

    holder["actor"] = _actor(id=bare_id, tenant_id=tenant_id, role="member")
    async with owner_sessionmaker() as db:
        await db.execute(text("DELETE FROM tenant_settings WHERE tenant_id = :t AND key = 'default_model_id'"), {"t": tenant_id})
        await db.commit()
    async with _client(app_user_sessionmaker, holder) as client:
        missing = await client.post("/api/assistant/open", json={})
        assert missing.status_code == 200, missing.text
        assert missing.json()["state"] == "model_unconfigured" and missing.json()["applied"] is False
        assert missing.json()["agent"] is None
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM user_assistant_bindings WHERE user_id = :u", u=bare_id) == 0
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agents WHERE primary_model_id = :id", id=model_ids["other"]) == 0

    async with owner_sessionmaker() as db:
        db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(model_ids["disabled"])}))
        await db.commit()
    holder["actor"] = _actor(id=blocked_id, tenant_id=tenant_id, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        forbidden = await client.post("/api/assistant/open", json={})
        assert forbidden.status_code == 200, forbidden.text
        assert forbidden.json()["state"] == "model_forbidden" and forbidden.json()["applied"] is False
        assert forbidden.json()["created"] is False
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agents WHERE creator_id = :u", u=blocked_id) == 0

    original = AgentManager.initialize_agent_files

    async def flaky(self, db, agent, *args, **kwargs):
        if not getattr(flaky, "tripped", False):
            flaky.tripped = True
            raise RuntimeError("workspace unavailable")
        return await original(self, db, agent, *args, **kwargs)

    monkeypatch.setattr(AgentManager, "initialize_agent_files", flaky)
    async with owner_sessionmaker() as db:
        await db.execute(
            text("UPDATE tenant_settings SET value = CAST(:value AS jsonb) WHERE tenant_id = :t AND key = 'default_model_id'"),
            {"t": tenant_id, "value": f'{{"model_id": "{model_ids["approved"]}"}}'},
        )
        await db.commit()
    retry_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        _user(db, id=retry_id, username=f"retry-{tag}", email=f"retry-{tag}@example.test", tenant_id=tenant_id)
        await db.commit()
    holder["actor"] = _actor(id=retry_id, tenant_id=tenant_id, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        partial = await client.post("/api/assistant/open", json={"idempotency_key": f"resume-{tag}"})
        assert partial.status_code == 200, partial.text
        assert partial.json()["state"] == "provision_incomplete" and partial.json()["created"] is True
        partial_agent = partial.json()["agent"]["id"]
        resumed = await client.post("/api/assistant/open", json={"idempotency_key": f"resume-{tag}"})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["agent"]["id"] == partial_agent and resumed.json()["created"] is False
        assert resumed.json()["state"] == "active" and resumed.json()["readiness"]["ready"] is True
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM agents WHERE creator_id = :u AND name = 'My Assistant'", u=retry_id) == 1
    if directory := os.environ.get("M1F_WIRE_DIR"):
        Path(directory, "assistant-open.json").write_text(json.dumps({
            "created": first.json(), "loaded": loaded.json(), "worker_unknown": unverified_worker.json(),
            "missing_model": missing.json(), "forbidden_model": forbidden.json(),
            "incomplete": partial.json(), "resumed": resumed.json(),
        }, indent=2))


async def test_readiness_recovery_scope_and_private_continuation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    user_id, peer_id, platform_id, outsider_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secret = f"secret-moved-company-{tag}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1F life {tag}", slug=f"m1f-life-{tag}"))
        db.add(Tenant(id=tenant_b, name=secret, slug=f"m1f-life-b-{tag}"))
        await db.flush()
        approved = await _model(db, tenant_a, label=f"Approved {tag}")
        _user(db, id=user_id, username=f"life-{tag}", email=f"life-{tag}@example.test", tenant_id=tenant_a)
        _user(db, id=peer_id, username=f"lifepeer-{tag}", email=f"lifepeer-{tag}@example.test", tenant_id=tenant_a)
        _user(db, id=outsider_id, username=f"lifeout-{tag}", email=f"lifeout-{tag}@example.test", tenant_id=tenant_a)
        _user(db, id=platform_id, username=f"plat-{tag}", email=f"plat-{tag}@example.test", tenant_id=None, role="platform_admin")
        db.add(TenantSetting(tenant_id=tenant_a, key="default_model_id", value={"model_id": str(approved.id)}))
        await db.commit()

    monkeypatch.setattr("app.services.runtime_task_worker.runtime_task_worker_enabled", lambda: False)
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_a, role="member")}
    async with _client(app_user_sessionmaker, holder) as client:
        opened = await client.post("/api/assistant/open", json={"idempotency_key": f"life-{tag}"})
        assert opened.status_code == 200, opened.text
        assert opened.json()["state"] == "worker_unavailable"
        assert opened.json()["readiness"]["ready"] is False
        assert opened.json()["readiness"]["worker"] == "unavailable"
        agent_id = opened.json()["agent"]["id"]
    monkeypatch.setattr("app.services.runtime_task_worker.runtime_task_worker_enabled", lambda: True)

    async with owner_sessionmaker() as db:
        empty_id, hidden_id = uuid.uuid4(), uuid.uuid4()
        db.add(ChatSession(id=empty_id, agent_id=agent_id, tenant_id=tenant_a, user_id=user_id, title="empty draft", source_channel="web"))
        db.add(ChatSession(
            id=hidden_id, agent_id=agent_id, tenant_id=tenant_a, user_id=user_id, title="hidden",
            source_channel="trigger", last_message_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        kept = {"empty": empty_id, "hidden": hidden_id}

    dispatched = {"n": 0}

    async def synthetic_dispatch(**kwargs):
        dispatched["n"] += 1
        return {"run_id": str(kwargs["run_id"]), "status": "synthetic_transport"}

    monkeypatch.setattr("app.services.web_chat_runtime.start_web_chat_run", synthetic_dispatch)
    async with _client(app_user_sessionmaker, holder) as client:
        started = await client.post(
            f"/api/agents/{agent_id}/sessions/runs",
            json={"content": "First piece of work", "idempotency_key": f"work-{tag}"},
        )
        assert started.status_code == 201, started.text
        assert "synthetic_transport" in started.text
        session_id = started.json()["session"]["id"]
        repeated = await client.get("/api/assistant")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["entry"]["kind"] == "session"
        assert repeated.json()["entry"]["session_id"] == session_id
        assert repeated.json()["entry"]["path"] == f"/agents/{agent_id}/sessions/{session_id}"
        again = await client.get("/api/assistant")
        assert again.json()["entry"]["session_id"] == session_id
        holder["actor"] = _actor(id=outsider_id, tenant_id=tenant_a, role="member")
        private = await client.get(f"/api/agents/{agent_id}/sessions/{session_id}/messages")
        assert private.status_code == 403
    assert dispatched["n"] == 1
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE agent_id = :id AND user_id = :u AND source_channel = 'web'", id=agent_id, u=user_id) == 2
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM session_turn_inputs WHERE session_id = :id", id=session_id) == 1
    assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE id = :id", id=kept["empty"]) == 1

    holder["actor"] = _actor(id=user_id, tenant_id=tenant_a, role="member")
    async with owner_sessionmaker() as db:
        await db.execute(text("UPDATE agents SET owner_user_id = :peer WHERE id = :id"), {"peer": peer_id, "id": agent_id})
        await db.commit()
    async with _client(app_user_sessionmaker, holder) as client:
        transferred = await client.get("/api/assistant")
        assert transferred.status_code == 200, transferred.text
        assert transferred.json()["state"] == "transferred"
        assert transferred.json()["agent"]["id"] == agent_id
        await db_permission_clear(owner_sessionmaker, agent_id)
        revoked = await client.get("/api/assistant")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["state"] == "inaccessible"
        async with owner_sessionmaker() as db:
            await db.execute(text("UPDATE agents SET owner_user_id = :owner, deactivated_at = now() WHERE id = :id"), {"owner": user_id, "id": agent_id})
            await db.commit()
        inactive = await client.get("/api/assistant")
        assert inactive.json()["state"] == "employee_inactive"
        async with owner_sessionmaker() as db:
            await db.execute(text("UPDATE agents SET deactivated_at = NULL, deleted_at = now() WHERE id = :id"), {"id": agent_id})
            await db.commit()
        deleted = await client.get("/api/assistant")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["state"] == "employee_deleted"
        assert deleted.json()["history_retained"] is True
        assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE id = :id", id=session_id) == 1
        replaced = await client.post("/api/assistant/open", json={"replace_unavailable": True, "idempotency_key": f"replace-{tag}"})
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["created"] is True
        assert replaced.json()["agent"]["id"] != agent_id
        assert replaced.json()["prior_agent_id"] == agent_id
        moved_id = replaced.json()["agent"]["id"]
        async with owner_sessionmaker() as db:
            await db.execute(
                text("UPDATE agents SET tenant_id = :tenant, primary_model_id = NULL, fallback_model_id = NULL WHERE id = :id"),
                {"tenant": tenant_b, "id": moved_id},
            )
            await db.commit()
        moved = await client.get("/api/assistant")
        assert moved.status_code == 200, moved.text
        assert moved.json()["state"] == "cross_company"
        assert moved.json()["agent"] is None
        assert secret not in moved.text and str(tenant_b) not in moved.text
        assert await _scalar(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE id = :id", id=session_id) == 1

    holder["actor"] = _actor(id=platform_id, tenant_id=tenant_b, role="platform_admin")
    async with _client(app_user_sessionmaker, holder) as client:
        late = await client.post(
            "/api/assistant/open",
            json={"expected_tenant_id": str(tenant_a), "expected_user_id": str(platform_id)},
        )
        assert late.status_code == 409, late.text
        assert _detail(late)["state"] == "scope_changed" and _detail(late)["applied"] is False
        assert _detail(late)["scope"]["tenant_id"] == str(tenant_b)
        assert str(tenant_a) not in late.text
        assert secret not in late.text
    assert await _scalar(
        owner_sessionmaker,
        "SELECT count(*) FROM user_assistant_bindings WHERE user_id = :u AND tenant_id = :t",
        u=platform_id,
        t=tenant_b,
    ) == 0

    holder["actor"] = _actor(id=user_id, tenant_id=None, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        missing_company = await client.get("/api/assistant")
        assert missing_company.status_code == 400
        assert _detail(missing_company)["state"] == "company_required"


    if directory := os.environ.get("M1F_WIRE_DIR"):
        Path(directory, "assistant-work.json").write_text(json.dumps({
            "started": started.json(), "continued": repeated.json(),
        }, indent=2))


async def db_permission_clear(sessionmaker, agent_id) -> None:
    async with sessionmaker() as db:
        await db.execute(text("DELETE FROM agent_permissions WHERE agent_id = :id"), {"id": agent_id})
        await db.commit()


@pytest.mark.parametrize('change', ['deleted', 'transferred'])
async def test_incomplete_open_rechecks_authority_before_workspace_retry(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, change,
):
    tenant, user_id, peer_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name='Resume authority', slug=f'resume-{tenant.hex}'))
        await db.flush()
        for uid in [user_id, peer_id]:
            _user(db, id=uid, username=uid.hex, email=f'{uid.hex}@example.test', tenant_id=tenant)
        model = await _model(db, tenant, label='Approved')
        db.add(TenantSetting(tenant_id=tenant, key='default_model_id', value={'model_id': str(model.id)}))
        await db.commit()
    attempted = []

    async def unavailable(self, db, agent, **kwargs):
        attempted.append(str(agent.id))
        raise RuntimeError('synthetic workspace unavailable')

    monkeypatch.setattr(AgentManager, 'initialize_agent_files', unavailable)
    holder = {'actor': _actor(id=user_id, tenant_id=tenant, role='member')}
    async with _client(app_user_sessionmaker, holder) as client:
        partial = await client.post('/api/assistant/open', json={})
        assert partial.status_code == 200 and partial.json()['state'] == 'provision_incomplete', partial.text
        agent_id = partial.json()['agent']['id']
        async with owner_sessionmaker() as db:
            if change == 'deleted':
                await db.execute(text('UPDATE agents SET deleted_at = now() WHERE id = :id'), {'id': agent_id})
            else:
                await db.execute(text("UPDATE agents SET owner_user_id = :peer, name = 'New private identity' WHERE id = :id"), {'id': agent_id, 'peer': peer_id})
                await db.execute(text('DELETE FROM agent_permissions WHERE agent_id = :id'), {'id': agent_id})
            await db.commit()
        retried = await client.post('/api/assistant/open', json={})
    assert retried.status_code == 200, retried.text
    assert attempted == [agent_id], (attempted, retried.text)
    assert retried.json()['state'] == ('employee_deleted' if change == 'deleted' else 'inaccessible')
    assert 'New private identity' not in retried.text


async def test_read_only_binding_does_not_claim_execution(owner_sessionmaker, app_user_sessionmaker):
    from app.models.agent import AgentPermission

    tenant, owner, viewer = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name='Read-only binding', slug=f'read-{tenant.hex}'))
        await db.flush()
        for uid in [owner, viewer]:
            _user(db, id=uid, username=uid.hex, email=f'{uid.hex}@example.test', tenant_id=tenant)
        model = await _model(db, tenant, label='Approved')
        agent = await _agent(db, tenant_id=tenant, owner_id=owner, name='Visible employee', model_id=model.id)
        db.add(AgentPermission(agent_id=agent.id, tenant_id=tenant, scope_type='user', scope_id=viewer, access_level='read'))
        await db.commit()
        agent_id = agent.id
    holder = {'actor': _actor(id=viewer, tenant_id=tenant, role='member')}
    async with _client(app_user_sessionmaker, holder) as client:
        selected = await client.post('/api/assistant/select', json={'agent_id': str(agent_id)})
    assert selected.status_code == 200, selected.text
    assert selected.json()['state'] == 'denied'
    assert selected.json()['readiness']['ready'] is False
    assert 'first_work' not in selected.json()['next_actions']
