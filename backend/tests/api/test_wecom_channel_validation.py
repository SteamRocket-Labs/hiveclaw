from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    async def execute(self, _stmt):
        return _ScalarResult(None)


@pytest.mark.asyncio
async def test_wecom_webhook_mode_requires_wecom_agent_id(monkeypatch):
    import app.api.wecom as wecom_api

    async def fake_check_agent_access(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(wecom_api, "is_agent_creator", lambda current_user, agent: True)

    with pytest.raises(HTTPException) as exc:
        await wecom_api.configure_wecom_channel(
            agent_id=uuid4(),
            data={
                "corp_id": "corp-id",
                "secret": "secret",
                "token": "token",
                "encoding_aes_key": "encoding-key",
            },
            current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
            db=_FakeDB(),
        )

    assert exc.value.status_code == 422
    assert "wecom_agent_id" in str(exc.value.detail)
