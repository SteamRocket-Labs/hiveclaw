"""X1: a Chinese-interface member gets the localized default assistant name.

New bindings opened with a zh interface language store 「我的助理」; the English
default stays for everyone else. The untouched-default provenance recognition
must accept both names. Existing rows are never rewritten.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.services.agent_manager import AgentManager
from tests.integration.test_m1f_assistant_binding_postgres import _actor, _app, _model, _user

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(autouse=True)
def _plain_default_bootstrap(monkeypatch):
    """Keep provisioning recognizable as the untouched default (a4sf pattern)."""

    initialize = AgentManager.initialize_agent_files

    async def old_initialize(self, db, agent, *args, **kwargs):
        kwargs.pop("original_author_id", None)
        return await initialize(self, db, agent, *args, **kwargs)

    monkeypatch.setattr(AgentManager, "initialize_agent_files", old_initialize)


async def _seed(owner_sessionmaker, tag: str):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"X1 name {tag}", slug=f"x1-name-{tag}"))
        await db.flush()
        model = await _model(db, tenant_id, label=f"X1 model {tag}")
        _user(db, id=user_id, username=f"x1-{tag}", email=f"x1-{tag}@example.test", tenant_id=tenant_id)
        db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(model.id)}))
        await db.commit()
    return tenant_id, user_id


async def test_zh_opener_provisions_the_localized_default_and_keeps_default_recognition(
    owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.agent import Agent
    from app.services.employee_creation_sources import load_known_native_default_context

    tag = uuid.uuid4().hex[:8]
    tenant_id, user_id = await _seed(owner_sessionmaker, tag)
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, holder)), base_url="http://x1") as client:
        opened = await client.post("/api/assistant/open", json={"ui_language": "zh-CN"})
        assert opened.status_code == 200, opened.text
        # The local fixture does not run the separate worker; readiness is not
        # under test here, the provisioned default name is.
        assert opened.json()["agent"]["name"] == "我的助理"
        agent_id = uuid.UUID(opened.json()["agent"]["id"])

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        assert agent.name == "我的助理"
        # The localized default is still a recognized untouched platform default.
        known = await load_known_native_default_context(db, tenant_id=tenant_id, agent_id=agent_id, ref="soul")
        assert known is not None and known["schema"] == "hive.native_default_context.v1"


async def test_opener_without_zh_language_keeps_the_english_default(
    owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.agent import Agent

    tag = uuid.uuid4().hex[:8]
    tenant_id, user_id = await _seed(owner_sessionmaker, tag)
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, holder)), base_url="http://x1") as client:
        opened = await client.post("/api/assistant/open", json={"ui_language": "en-US"})
        assert opened.status_code == 200 and opened.json()["agent"]["name"] == "My Assistant"
        agent_id = uuid.UUID(opened.json()["agent"]["id"])

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        assert agent.name == "My Assistant"


async def test_an_english_default_row_still_verifies_as_known_default(
    owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.agent import Agent
    from app.services.employee_creation_sources import load_known_native_default_context

    tag = uuid.uuid4().hex[:8]
    tenant_id, user_id = await _seed(owner_sessionmaker, tag)
    holder = {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, holder)), base_url="http://x1") as client:
        opened = await client.post("/api/assistant/open", json={})
        assert opened.status_code == 200 and opened.json()["agent"]["name"] == "My Assistant"
        agent_id = uuid.UUID(opened.json()["agent"]["id"])

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        assert agent.name == "My Assistant"
        known = await load_known_native_default_context(db, tenant_id=tenant_id, agent_id=agent_id, ref="soul")
        assert known is not None
