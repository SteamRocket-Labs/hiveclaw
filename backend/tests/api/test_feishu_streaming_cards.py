from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_serial_patch_queue_preserves_enqueue_order():
    import app.api.feishu as feishu_api

    queue = feishu_api._SerialPatchQueue()
    events: list[str] = []

    async def job_one():
        events.append("job_one:start")
        await asyncio.sleep(0)
        events.append("job_one:end")

    async def job_two():
        events.append("job_two:start")
        await asyncio.sleep(0)
        events.append("job_two:end")

    queue.enqueue(job_one)
    queue.enqueue(job_two)
    await queue.drain()

    assert events == [
        "job_one:start",
        "job_one:end",
        "job_two:start",
        "job_two:end",
    ]


@pytest.mark.asyncio
async def test_call_agent_llm_forwards_tool_callback(monkeypatch):
    import app.api.feishu as feishu_api

    captured = {}

    async def fake_call_llm(*args, **kwargs):
        captured["on_tool_call"] = kwargs.get("on_tool_call")
        return "ok"

    class _ScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeDB:
        async def execute(self, _stmt):
            return _ScalarResult(
                SimpleNamespace(
                    id=uuid4(),
                    name="Feishu Bot",
                    role_description="",
                    tenant_id=uuid4(),
                    primary_model_id=uuid4(),
                    fallback_model_id=None,
                )
            )

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)

    db = _FakeDB()

    def tool_callback(evt):
        return evt

    result = await feishu_api._call_agent_llm(
        db,
        uuid4(),
        "hello",
        on_tool_call=tool_callback,
    )

    assert result == "ok"
    assert captured["on_tool_call"] is tool_callback


def test_process_feishu_event_source_uses_cardkit_primary_flow():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/api/feishu.py").read_text()

    assert "create_card_entity" in source
    assert "send_card_by_card_id" in source
    assert "set_card_streaming_mode" in source
    assert "update_cardkit_card" in source
    assert "patch_message" in source


def test_split_feishu_markdown_text_chunks_long_body_without_loss():
    import app.api.feishu as feishu_api

    unit = "这是一段很长的飞书回复，用来验证卡片 markdown 会被安全拆分。\n"
    long_text = unit * 240

    chunks = feishu_api._split_feishu_markdown_text(long_text, limit=280)

    assert len(chunks) > 1
    assert all(len(chunk) <= 280 for chunk in chunks)
    assert "".join(chunks) == long_text


def test_feishu_markdown_elements_fallback_to_placeholder_for_empty_text():
    import app.api.feishu as feishu_api

    elements = feishu_api._feishu_markdown_elements("", limit=280)

    assert elements == [{"tag": "markdown", "content": "..."}]
