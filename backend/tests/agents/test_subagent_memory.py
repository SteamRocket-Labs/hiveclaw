"""Tests for cut ⑥: tenant-scoped subagent 记忆.md + governed write (How-not-What)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents import subagent_memory as mem_mod
from app.agents.subagent_memory import SubagentMemoryStore, distill_and_record


def _allowed_decision(content, **kwargs):
    return SimpleNamespace(
        rejected=False,
        reason="",
        content=content,
        metadata={"entry_id": "e1", "sensitivity": "internal", "status": "active"},
    )


def _rejected_decision(content, **kwargs):
    return SimpleNamespace(rejected=True, reason="PL4 credential", content="", metadata={})


def test_record_how_writes_when_gate_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "prepare_memory_write", _allowed_decision)
    store = SubagentMemoryStore(tmp_path)
    result = store.record_how("scout", "Prefer primary sources.", category="source_calibration")
    assert result.written is True
    loaded = store.load("scout")
    assert "Prefer primary sources." in loaded
    assert "source_calibration" in loaded


def test_record_how_aborts_when_gate_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "prepare_memory_write", _rejected_decision)
    store = SubagentMemoryStore(tmp_path)
    result = store.record_how("scout", "secret AKIA-style blob", category="pitfall")
    assert result.written is False
    assert result.rejected is True
    assert store.load("scout") == ""  # 铁律: no raw fallback — nothing written


def test_record_how_rejects_non_how_category(tmp_path):
    # the category guard runs BEFORE the gate — domain 'what' is never recorded
    store = SubagentMemoryStore(tmp_path)
    result = store.record_how("scout", "The capital of France is Paris.", category="domain_fact")
    assert result.rejected is True
    assert "How-craft" in result.reason
    assert store.load("scout") == ""


def test_load_missing_returns_empty(tmp_path):
    assert SubagentMemoryStore(tmp_path).load("nope") == ""


def test_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "prepare_memory_write", _allowed_decision)
    t1 = SubagentMemoryStore(tmp_path / "t1")
    t2 = SubagentMemoryStore(tmp_path / "t2")
    t1.record_how("scout", "t1 craft", category="pitfall")
    assert t2.load("scout") == ""  # no cross-tenant leakage


def test_store_rejects_path_traversal_names(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "prepare_memory_write", _allowed_decision)
    store = SubagentMemoryStore(tmp_path)

    with pytest.raises(ValueError, match="subagent name"):
        store.record_how("../escape", "bad", category="pitfall")

    with pytest.raises(ValueError, match="subagent name"):
        store.load("../escape")

    assert not (tmp_path.parent / "escape.memory.md").exists()


@pytest.mark.asyncio
async def test_distill_and_record_one_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "prepare_memory_write", _allowed_decision)
    store = SubagentMemoryStore(tmp_path)

    def distiller(run_log):
        return [("pitfall", "Avoid PDF blobs."), ("source_calibration", "Gov sources rank high.")]

    results = await distill_and_record(store, "scout", "<run log>", distiller=distiller)
    assert len(results) == 2
    assert all(r.written for r in results)
    loaded = store.load("scout")
    assert "Avoid PDF blobs." in loaded
    assert "Gov sources rank high." in loaded


@pytest.mark.asyncio
async def test_distill_and_record_accepts_async_distiller(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "prepare_memory_write", _allowed_decision)
    store = SubagentMemoryStore(tmp_path)

    async def distiller(run_log):
        return [("pointer_map", "Filings live under ir.example.com/annual.")]

    results = await distill_and_record(store, "scout", "<run log>", distiller=distiller)
    assert len(results) == 1 and results[0].written
    assert "ir.example.com" in store.load("scout")


# --- LLM How distiller (production wiring for the spawn path) -----------------


def _llm_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_distill_prompt_is_vendor_neutral_and_how_only():
    from app.agents.subagent_memory import DISTILL_HOW_SYSTEM_PROMPT

    lowered = DISTILL_HOW_SYSTEM_PROMPT.lower()
    for vendor in ("claude", "anthropic", "gpt", "openai", "gemini", "deepseek"):
        assert vendor not in lowered
    # the four How-craft categories are spelled out for the model
    for category in ("source_calibration", "judgment_calibration", "pitfall", "pointer_map"):
        assert category in DISTILL_HOW_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_llm_distiller_parses_lessons(monkeypatch):
    from app.agents.subagent_memory import make_llm_how_distiller

    async def fake_chat_complete(**kwargs):
        return _llm_response('[{"category": "pitfall", "lesson": "Search snippets truncate tables."}]')

    monkeypatch.setattr(mem_mod, "chat_complete", fake_chat_complete)
    distiller = make_llm_how_distiller({"provider": "p", "api_key": "k", "model": "m", "base_url": None})
    assert await distiller("<run log>") == [("pitfall", "Search snippets truncate tables.")]


@pytest.mark.asyncio
async def test_llm_distiller_empty_array_is_normal(monkeypatch):
    from app.agents.subagent_memory import make_llm_how_distiller

    async def fake_chat_complete(**kwargs):
        return _llm_response("[]")

    monkeypatch.setattr(mem_mod, "chat_complete", fake_chat_complete)
    distiller = make_llm_how_distiller({"provider": "p", "api_key": "k", "model": "m", "base_url": None})
    assert await distiller("<run log>") == []


@pytest.mark.asyncio
async def test_llm_distiller_fails_soft_on_garbage(monkeypatch):
    # Distillation is a bonus path: an unusable model response must not break
    # the spawn result — log and return no lessons.
    from app.agents.subagent_memory import make_llm_how_distiller

    async def fake_chat_complete(**kwargs):
        return _llm_response("I refuse to answer in JSON")

    monkeypatch.setattr(mem_mod, "chat_complete", fake_chat_complete)
    distiller = make_llm_how_distiller({"provider": "p", "api_key": "k", "model": "m", "base_url": None})
    assert await distiller("<run log>") == []


def test_record_how_real_gate_smoke(tmp_path):
    # integration: the real write_gate is invoked and record_how does not crash
    store = SubagentMemoryStore(tmp_path)
    result = store.record_how(
        "scout", "Prefer fetched primary pages over search snippets.", category="source_calibration"
    )
    assert isinstance(result.written, bool)
    if result.written:
        assert "Prefer fetched" in store.load("scout")
