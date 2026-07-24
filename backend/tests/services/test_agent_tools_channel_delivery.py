from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.channel_delivery_service import DeliveryResult, channel_delivery_target
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


@pytest.mark.asyncio
async def test_send_channel_message_enforces_run_scoped_knowledge_sensitivity_at_effect_boundary(
    monkeypatch,
) -> None:
    import app.services.agent_tools as agent_tools

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    captured: dict[str, object] = {}
    db = object()

    @asynccontextmanager
    async def fake_tenant_scoped_session(value):
        captured["tenant_scope"] = value
        yield db

    async def fake_load(_db, **kwargs):
        captured["provenance_query"] = kwargs
        return {
            "schema": "hive.knowledge_provenance_aggregate.v1",
            "max_sensitivity": "PL3_sensitive",
            "semantic_memory_eligible": False,
            "source_event_refs": ["transcript://event/kb-result"],
        }

    async def fake_send_text(**kwargs):
        captured["delivery"] = kwargs
        return DeliveryResult(ok=True, status="sent", channel="slack", message="sent")

    monkeypatch.setattr(agent_tools, "tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(
        "app.services.knowledge_provenance.load_transcript_knowledge_provenance",
        fake_load,
    )
    monkeypatch.setattr(agent_tools.ChannelDeliveryService, "send_text", fake_send_text)

    target_token = channel_delivery_target.set({"channel": "slack", "channel_id": "C1"})
    try:
        request = ToolExecutionRequest(
            tool_name="send_channel_message",
            arguments={"message": "Use the governed source."},
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp"),
                session_id=str(session_id),
                runtime_task_id=str(runtime_task_id),
                turn_id="turn-1",
            ),
        )

        from app.tools.handlers.communication import send_channel_message

        result = await send_channel_message(request)
    finally:
        channel_delivery_target.reset(target_token)

    assert result == "✅ sent"
    assert captured["tenant_scope"] == tenant_id
    assert captured["provenance_query"] == {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": runtime_task_id,
        "turn_id": "turn-1",
    }
    delivery = captured["delivery"]
    assert isinstance(delivery, dict)
    assert delivery["content_sensitivity"] == "PL3_sensitive"
    assert delivery["extra_detail"]["knowledge_provenance"]["source_event_refs"] == ["transcript://event/kb-result"]


@pytest.mark.asyncio
async def test_send_channel_message_without_a_typed_run_scope_does_not_scan_message_text(
    monkeypatch,
) -> None:
    import app.services.agent_tools as agent_tools

    agent_id = uuid4()
    tenant_id = uuid4()
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_tenant_scoped_session(_value):
        yield object()

    async def forbidden_load(*_args, **_kwargs):
        raise AssertionError("no transcript scope means no heuristic provenance lookup")

    async def fake_send_text(**kwargs):
        captured.update(kwargs)
        return DeliveryResult(ok=True, status="sent", channel="slack", message="sent")

    monkeypatch.setattr(agent_tools, "tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(
        "app.services.knowledge_provenance.load_transcript_knowledge_provenance",
        forbidden_load,
    )
    monkeypatch.setattr(agent_tools.ChannelDeliveryService, "send_text", fake_send_text)

    target_token = channel_delivery_target.set({"channel": "slack", "channel_id": "C1"})
    try:
        request = ToolExecutionRequest(
            tool_name="send_channel_message",
            arguments={"message": "This benign prose says secret and PL4_credential."},
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=uuid4(),
                tenant_id=str(tenant_id),
                workspace=Path("/tmp"),
            ),
        )

        from app.tools.handlers.communication import send_channel_message

        result = await send_channel_message(request)
    finally:
        channel_delivery_target.reset(target_token)

    assert result == "✅ sent"
    assert captured["content_sensitivity"] is None
    assert captured["extra_detail"] == {"tool_name": "send_channel_message"}


@pytest.mark.asyncio
async def test_send_channel_file_does_not_bypass_unified_secret_denial(
    monkeypatch,
    tmp_path,
) -> None:
    import app.services.agent_tools as agent_tools

    agent_id = uuid4()
    tenant_id = uuid4()
    workspace = tmp_path / str(agent_id)
    file_path = workspace / "workspace" / "credential.txt"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("protected", encoding="utf-8")

    @asynccontextmanager
    async def fake_tenant_scoped_session(value):
        assert value == tenant_id
        yield object()

    async def fake_send_file(**_kwargs):
        return DeliveryResult(
            ok=False,
            status="denied",
            channel="slack",
            message="Outbound file contains protected credential bytes.",
        )

    async def fake_resolve_tenant(_agent_id):
        return tenant_id

    async def forbidden_legacy_sender(*_args, **_kwargs):
        raise AssertionError("legacy sender must not bypass unified delivery denial")

    monkeypatch.setattr(
        agent_tools,
        "resolve_tenant_for_agent",
        fake_resolve_tenant,
    )
    monkeypatch.setattr(
        agent_tools,
        "tenant_scoped_session",
        fake_tenant_scoped_session,
    )
    monkeypatch.setattr(
        agent_tools.ChannelDeliveryService,
        "send_file",
        fake_send_file,
    )
    target_token = channel_delivery_target.set({"channel": "slack", "channel_id": "C1"})
    sender_token = agent_tools.channel_file_sender.set(forbidden_legacy_sender)
    try:
        result = await agent_tools._send_channel_file(
            agent_id,
            workspace,
            {"file_path": "credential.txt"},
        )
    finally:
        agent_tools.channel_file_sender.reset(sender_token)
        channel_delivery_target.reset(target_token)

    assert result == "❌ Outbound file contains protected credential bytes."
