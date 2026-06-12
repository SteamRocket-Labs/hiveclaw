from __future__ import annotations

import contextlib
import uuid

import pytest

from app.services.workflow_runtime_service import WorkflowRuntimeService


@pytest.mark.asyncio
async def test_completion_signal_uses_gateway_scope(monkeypatch):
    from app.services import workflow_runtime_service as module

    calls: list[dict] = []
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    class FakeGateway:
        async def send_signal(self, **kwargs):
            calls.append({"signal": kwargs})

    @contextlib.asynccontextmanager
    async def fake_gateway_scope(*, tenant_id=None, explicit_gateway=None):
        calls.append({"tenant_id": tenant_id, "explicit_gateway": explicit_gateway})
        yield FakeGateway()

    monkeypatch.setattr(module, "gateway_scope", fake_gateway_scope)

    await WorkflowRuntimeService._emit_completion_signal(
        run_id,
        agent_id,
        "completed",
        tenant_id=tenant_id,
    )

    assert calls[0] == {"tenant_id": tenant_id, "explicit_gateway": None}
    assert calls[1]["signal"] == {
        "from_agent_id": f"workflow:{run_id}",
        "to_agent_id": str(agent_id),
        "content": f"workflow run {run_id} finished: completed",
        "signal_type": "workflow_completed",
        "thread_id": str(run_id),
    }


@pytest.mark.asyncio
async def test_completion_delivery_uses_runtime_delivery_target(monkeypatch):
    from app.services import workflow_runtime_service as module

    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    target = {"channel": "feishu", "chat_id": "oc_x"}
    sent: list[dict] = []

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_send_text(**kwargs):
        sent.append(kwargs)
        return None

    monkeypatch.setattr(module, "tenant_scoped_session", lambda *args, **kwargs: _DB())
    monkeypatch.setattr(module.ChannelDeliveryService, "send_text", fake_send_text)

    service = WorkflowRuntimeService()
    await service._deliver_completion_notification(
        run_id=run_id,
        agent_id=agent_id,
        status="completed",
        tenant_id=tenant_id,
        metadata={"delivery_target_json": target},
    )

    assert sent[0]["agent_id"] == agent_id
    assert sent[0]["reply_target"] == target
    assert sent[0]["delivery_mode"] == "async_completion"
    assert f"Workflow run {run_id}" in sent[0]["text"]
