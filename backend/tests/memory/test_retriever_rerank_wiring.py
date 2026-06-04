"""A3 (docs/agent-lifecycle-cc-alignment.md 主题 A): the LLM rerank pass must
actually run in the production retrieve() path — before this fix,
`rerank_model_config` was a dead parameter and semantic activation was 100%
mechanical (keyword + fixed weights, zero content understanding)."""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_retrieve_invokes_rerank_when_semantic_pool_contested(monkeypatch, tmp_path) -> None:
    from app.memory import retriever as retriever_mod
    from app.memory.retriever import MemoryRetriever
    from app.memory.types import MemoryItem, MemoryKind

    semantic_items = [MemoryItem(kind=MemoryKind.SEMANTIC, content=f"semantic fact {i}", score=0.5) for i in range(8)]
    calls: dict = {}

    async def fake_rerank(items, query, model_config, **kwargs):
        calls["items"] = list(items)
        calls["query"] = query
        calls["model_config"] = model_config
        return items[:2]  # LLM picked two

    monkeypatch.setattr(retriever_mod, "_rerank_semantic_items", fake_rerank)

    retriever = MemoryRetriever(data_root=tmp_path)
    monkeypatch.setattr(retriever, "_retrieve_working", lambda _aid: [])
    monkeypatch.setattr(retriever, "_retrieve_t3_direct", lambda _aid, query="": [])
    monkeypatch.setattr(retriever, "_retrieve_understandings", lambda _aid, query="": list(semantic_items))

    async def _none(*_a, **_kw):
        return []

    monkeypatch.setattr(retriever, "_retrieve_episodic", _none)
    monkeypatch.setattr(retriever, "_retrieve_semantic_backend", _none)
    monkeypatch.setattr(retriever, "_retrieve_external", _none)

    result = await retriever.retrieve(
        uuid.uuid4(),
        "deploy pipeline",
        None,
        None,
        rerank_model_config={"provider": "openai", "model": "m", "api_key": "k"},
    )

    assert calls["query"] == "deploy pipeline"
    assert len(calls["items"]) == 8  # the full contested pool reached the LLM
    semantic_result = [i for i in result if i.kind == MemoryKind.SEMANTIC]
    assert len(semantic_result) == 2  # LLM's pick replaced the mechanical order


@pytest.mark.asyncio
async def test_retrieve_skips_rerank_for_small_pool(monkeypatch, tmp_path) -> None:
    from app.memory import retriever as retriever_mod
    from app.memory.retriever import MemoryRetriever
    from app.memory.types import MemoryItem, MemoryKind

    called = {"n": 0}

    async def fake_rerank(items, query, model_config, **kwargs):
        called["n"] += 1
        return items

    monkeypatch.setattr(retriever_mod, "_rerank_semantic_items", fake_rerank)

    retriever = MemoryRetriever(data_root=tmp_path)
    small = [MemoryItem(kind=MemoryKind.SEMANTIC, content="only fact", score=0.5)]
    monkeypatch.setattr(retriever, "_retrieve_working", lambda _aid: [])
    monkeypatch.setattr(retriever, "_retrieve_t3_direct", lambda _aid, query="": [])
    monkeypatch.setattr(retriever, "_retrieve_understandings", lambda _aid, query="": small)

    async def _none(*_a, **_kw):
        return []

    monkeypatch.setattr(retriever, "_retrieve_episodic", _none)
    monkeypatch.setattr(retriever, "_retrieve_semantic_backend", _none)
    monkeypatch.setattr(retriever, "_retrieve_external", _none)

    await retriever.retrieve(
        uuid.uuid4(),
        "q",
        None,
        None,
        rerank_model_config={"provider": "openai", "model": "m", "api_key": "k"},
    )

    assert called["n"] == 0  # ≤ threshold: all candidates fit, no LLM needed
