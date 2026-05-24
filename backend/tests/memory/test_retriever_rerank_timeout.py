from __future__ import annotations

import asyncio


async def test_rerank_semantic_items_times_out_and_falls_back(monkeypatch) -> None:
    from app.memory.retriever import _rerank_semantic_items
    from app.memory.types import MemoryItem, MemoryKind
    import app.services.llm_client as llm_client

    class SlowClient:
        closed = False

        async def stream(self, **_kwargs):
            await asyncio.sleep(10)
            return '{"selected":[1]}'

        async def close(self):
            self.closed = True

    client = SlowClient()
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: client)
    items = [
        MemoryItem(kind=MemoryKind.SEMANTIC, content=f"memory {index}", score=0.5)
        for index in range(8)
    ]

    result = await _rerank_semantic_items(
        items,
        "query",
        model_config={"provider": "openai", "api_key": "test", "model": "test"},
        max_select=3,
        timeout_seconds=0.01,
    )

    assert result == items[:3]
    assert client.closed is True
