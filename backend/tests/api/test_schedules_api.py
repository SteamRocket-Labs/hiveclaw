from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.schedules as schedules_mod
from app.api.schedules import router
from app.core.security import get_current_user
from app.database import get_db


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results=None):
        self._execute_results = list(execute_results or [])
        self.added = []
        self.deleted = []
        self.flushed = False

    async def execute(self, _stmt):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def delete(self, obj):
        self.deleted.append(obj)


def _build_client(fake_db: _FakeDB):
    app = FastAPI()
    app.include_router(router)

    current_user = SimpleNamespace(id=uuid4(), username="zhangsan")

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), current_user


def test_create_schedule_persists_trigger_surface_record():
    fake_db = _FakeDB()
    client, current_user = _build_client(fake_db)
    agent = SimpleNamespace(id=uuid4())

    with (
        patch.object(schedules_mod, "check_agent_access", new_callable=AsyncMock, return_value=(agent, None)),
        patch.object(schedules_mod, "is_agent_creator", return_value=True),
    ):
        response = client.post(
            f"/agents/{agent.id}/schedules/",
            json={
                "name": "日报",
                "instruction": "生成日报",
                "cron_expr": "0 9 * * *",
                "delivery_target_json": {"channel": "feishu"},
            },
        )

    assert response.status_code == 201
    assert fake_db.flushed is True
    assert len(fake_db.added) == 1
    trigger = fake_db.added[0]
    assert trigger.type == "cron"
    assert trigger.config["_surface"] == "schedule_api"
    assert trigger.config["instruction"] == "生成日报"
    assert trigger.config["expr"] == "0 9 * * *"
    assert trigger.config["created_by"] == str(current_user.id)


def test_manual_run_marks_trigger_pending_instead_of_calling_scheduler():
    schedule_id = uuid4()
    agent_id = uuid4()
    compat_trigger = SimpleNamespace(
        id=schedule_id,
        agent_id=agent_id,
        name="日报",
        type="cron",
        config={"_surface": "schedule_api", "expr": "0 9 * * *", "instruction": "生成日报"},
        is_enabled=False,
    )
    fake_db = _FakeDB([_ScalarResult(compat_trigger)])
    client, _ = _build_client(fake_db)
    agent = SimpleNamespace(id=agent_id)

    with (
        patch.object(schedules_mod, "check_agent_access", new_callable=AsyncMock, return_value=(agent, None)),
        patch.object(schedules_mod, "is_agent_expired", return_value=False),
    ):
        response = client.post(f"/agents/{agent_id}/schedules/{schedule_id}/run")

    assert response.status_code == 200
    assert compat_trigger.config["_manual_pending"] is True
    assert compat_trigger.config["_manual_request_id"]
    assert response.json()["schedule_id"] == str(schedule_id)
