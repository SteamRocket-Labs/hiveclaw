"""Read-side tests for complete evidence and model-owned semantic selection.

- Resident plane (P0, no retrieval, no LLM): self/self.md + profiles/*.md stay
  resident; explicit overlay contributes a bounded ID/preview index, never all
  bodies. Full entries remain recoverable through load_memory.
- Retrieval plane: knowledge/milestones pages expose every active page and its
  complete Markdown to the model selector. BM25/PPR remain ranking evidence;
  they never choose a top-k or hide zero-score pages from model judgment.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


SELF_MD = """# Self — 我对自己的认识
<!-- last_reflected: 2026-06-28 -->

## 能力
### 深度研究 — 熟练
拆解、多源检索、交叉验证。
- 证据: t2-a1b2

## 失败模式
### 需求含糊时爱自己猜 — active
触发: 目标不明确。 规避: 先问一个澄清问题。
- 状态: active(2026-06-20 起)

### 长任务忘记推进 — 规避中
触发: 多步任务。 规避: work ledger 记录。
- 状态: 规避中(2026-06-01 起)

### 硬编码配置 — 已根除
- 状态: 已根除(2026-05-01)

## 风格
简洁直接。
"""


def _write_resident_files(tmp_path: Path, agent_id, *, self_md: str = SELF_MD) -> None:
    mem = _mem_dir(tmp_path, agent_id)
    (mem / "self").mkdir(parents=True, exist_ok=True)
    (mem / "self" / "self.md").write_text(self_md, encoding="utf-8")
    (mem / "profiles").mkdir(parents=True, exist_ok=True)
    (mem / "profiles" / "owner.md").write_text("# Owner\n偏好简洁中文汇报。\n", encoding="utf-8")
    (mem / "profiles" / "domain.md").write_text("# Domain\nWeb3 研究领域,ZK 增长快。\n", encoding="utf-8")


def _write_overlay_entry(tmp_path: Path, agent_id, *, entry_id: str, status: str = "active") -> None:
    overlay = _mem_dir(tmp_path, agent_id) / "explicit"
    (overlay / "entries").mkdir(parents=True, exist_ok=True)
    with (overlay / "manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": entry_id, "status": status, "category": "preference"}) + "\n")
    (overlay / "entries" / f"{entry_id}.md").write_text(
        "<normalized_memory>周报永远用中文。</normalized_memory>", encoding="utf-8"
    )


def _write_knowledge_page(tmp_path: Path, agent_id, *, slug: str, title: str, body: str) -> None:
    kdir = _mem_dir(tmp_path, agent_id) / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / f"{slug}.md").write_text(f"---\ntitle: {title}\nstatus: active\n---\n{body}\n", encoding="utf-8")


# --- resident plane ---


def test_resident_memory_loads_profiles_and_explicit_index_with_active_failures_on_top(tmp_path: Path) -> None:
    from app.memory.profile_plane import load_resident_memory

    agent_id = uuid4()
    _write_resident_files(tmp_path, agent_id)
    _write_overlay_entry(tmp_path, agent_id, entry_id="ex-lang")

    resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=20_000)

    assert "需求含糊时爱自己猜" in resident.active_failure_modes
    assert "长任务忘记推进" not in resident.active_failure_modes
    assert "硬编码配置" not in resident.active_failure_modes
    # active failures float above the full self text
    assert resident.text.index("需求含糊时爱自己猜") < resident.text.index("深度研究")
    # Whole identity profiles remain resident; explicit memory is an index.
    assert "偏好简洁中文汇报" in resident.text
    assert "Web3 研究领域" in resident.text
    assert "周报永远用中文" in resident.text
    assert "ex-lang" in resident.text
    assert 'load_memory(ids=["ex-lang"])' in resident.text
    assert resident.text.index("深度研究") < resident.text.index("偏好简洁中文汇报")
    assert "self" in resident.sections
    assert "profiles/owner" in resident.sections
    assert resident.over_budget is False


def test_explicit_resident_index_is_bounded_to_cc_index_limits(tmp_path: Path) -> None:
    from app.memory.profile_plane import (
        RESIDENT_INDEX_MAX_BYTES,
        RESIDENT_INDEX_MAX_LINES,
        load_resident_memory,
    )

    agent_id = uuid4()
    for index in range(400):
        _write_overlay_entry(tmp_path, agent_id, entry_id=f"ex-{index:04d}")

    resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=50_000)
    index = resident.text.split("### Explicit Memory Index (active)\n", 1)[1]

    assert len(index.encode("utf-8")) <= RESIDENT_INDEX_MAX_BYTES
    assert len(index.splitlines()) <= RESIDENT_INDEX_MAX_LINES
    assert "400 active entries" in index
    assert "search_memory" in index


def test_resident_memory_empty_for_new_agent(tmp_path: Path) -> None:
    from app.memory.profile_plane import load_resident_memory

    resident = load_resident_memory(agent_id=uuid4(), data_root=tmp_path, budget_chars=20_000)

    assert resident.text == ""
    assert resident.sections == ()
    assert resident.over_budget is False


def test_resident_reader_reports_unreadable_identity_section(monkeypatch, tmp_path: Path) -> None:
    from app.memory.profile_plane import load_resident_memory

    agent_id = uuid4()
    owner_path = _mem_dir(tmp_path, agent_id) / "profiles" / "owner.md"
    owner_path.parent.mkdir(parents=True)
    owner_path.write_text("owner", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == owner_path:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path)

    assert resident.text == ""
    assert resident.read_errors == ("profiles/owner",)


def test_resident_memory_skips_inactive_overlay_entries(tmp_path: Path) -> None:
    from app.memory.profile_plane import load_resident_memory

    agent_id = uuid4()
    _write_resident_files(tmp_path, agent_id)
    _write_overlay_entry(tmp_path, agent_id, entry_id="ex-gone", status="absorbed")

    resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=20_000)

    assert "周报永远用中文" not in resident.text


def test_resident_over_budget_flags_but_never_trims(tmp_path: Path) -> None:
    from app.memory.profile_plane import load_resident_memory

    agent_id = uuid4()
    _write_resident_files(tmp_path, agent_id)

    resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=50)

    assert resident.over_budget is True
    # NO hard trim — convergence failure is a write-side problem (工序 4)
    assert "深度研究" in resident.text
    assert "偏好简洁中文汇报" in resident.text


@pytest.mark.asyncio
async def test_resident_budget_alert_is_one_shot_and_clears_on_recovery(tmp_path: Path, monkeypatch) -> None:
    from app.memory.profile_plane import check_resident_budget, load_resident_memory

    agent_id = uuid4()
    _write_resident_files(tmp_path, agent_id)
    alerts: list[dict] = []

    async def fake_write_audit_log(action: str, payload: dict | None = None, **kwargs):
        alerts.append({"action": action, "payload": payload or {}, **kwargs})

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_write_audit_log)

    over = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=50)
    first = await check_resident_budget(agent_id=agent_id, data_root=tmp_path, resident=over)
    second = await check_resident_budget(agent_id=agent_id, data_root=tmp_path, resident=over)

    assert first is True
    assert second is False
    assert len(alerts) == 1
    assert alerts[0]["action"] == "memory_resident_over_budget"

    ok = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=50_000)
    await check_resident_budget(agent_id=agent_id, data_root=tmp_path, resident=ok)
    again = load_resident_memory(agent_id=agent_id, data_root=tmp_path, budget_chars=50)
    realerted = await check_resident_budget(agent_id=agent_id, data_root=tmp_path, resident=again)

    assert realerted is True
    assert len(alerts) == 2


# --- retrieval plane: knowledge/milestones over the parameterized link graph ---


def test_relation_graph_supports_knowledge_and_milestone_dirs(tmp_path: Path) -> None:
    from app.memory.relation_graph import build_relation_graph

    agent_id = uuid4()
    _write_knowledge_page(
        tmp_path,
        agent_id,
        slug="l2-rollup",
        title="L2 Rollup",
        body="## Current Claim\nL2 把计算移到链下。\n## Relations\n- is_a [[k:Scaling Solution]]\n",
    )
    mdir = _mem_dir(tmp_path, agent_id) / "milestones"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "ms-web3-report.md").write_text(
        "---\ntitle: Web3 研报首胜\nstatus: active\n---\n首次交付完整研报。参考 [[k:L2 Rollup]]。\n",
        encoding="utf-8",
    )

    graph = build_relation_graph(tmp_path, agent_id, page_dirs=("knowledge", "milestones"))

    node_ids = {node.node_id for node in graph.nodes}
    assert "knowledge/l2-rollup" in node_ids
    assert "milestones/ms-web3-report" in node_ids
    # [[k:Scaling Solution]] resolves into knowledge/ as a forward reference
    assert "knowledge/scaling-solution" in node_ids
    forward = graph.node_map()["knowledge/scaling-solution"]
    assert forward.exists is False
    # milestone → knowledge edge resolved through the k: prefix
    assert any(
        edge.source == "milestones/ms-web3-report" and edge.target == "knowledge/l2-rollup" for edge in graph.edges
    )


def test_knowledge_pages_are_retrieved_by_query(tmp_path: Path) -> None:
    from app.memory.retriever import MemoryRetriever

    agent_id = uuid4()
    _write_knowledge_page(
        tmp_path,
        agent_id,
        slug="l2-rollup",
        title="L2 Rollup",
        body="## Current Claim\nL2 rollup 通过链下计算扩容以太坊。\n## Relations\n- is_a [[k:Scaling]]\n",
    )

    retriever = MemoryRetriever(data_root=tmp_path)
    items = retriever._retrieve_knowledge_pages(agent_id, query="rollup 扩容", limit=5)

    assert items
    assert any("L2 Rollup" in item.content for item in items)
    assert all(item.metadata.get("source_type") == "knowledge_ppr" for item in items)


def test_knowledge_search_returns_every_active_page_with_complete_markdown(tmp_path: Path) -> None:
    from app.memory.wiki_retrieval import search_wiki_pages

    agent_id = uuid4()
    decisive_tail = "DECISIVE_TAIL_" + ("x" * 240)
    for index in range(7):
        body = (
            f"## Current Claim\n{decisive_tail}\n"
            if index == 6
            else f"## Current Claim\nunrelated knowledge page {index}\n"
        )
        _write_knowledge_page(
            tmp_path,
            agent_id,
            slug=f"page-{index}",
            title=f"Page {index}",
            body=body,
        )

    hits = search_wiki_pages(
        tmp_path,
        agent_id,
        "salary planning",
        limit=None,
        page_dirs=("knowledge", "milestones"),
    )

    assert len(hits) == 7
    decisive = next(hit for hit in hits if hit["page_id"] == "knowledge/page-6")
    assert decisive_tail in decisive["content"]
    assert decisive["score"] == 0.0


def test_legacy_threshold_rerank_machinery_is_retired() -> None:
    """The model selector must not inherit the retired score-threshold reranker."""
    from app.memory import retriever as retriever_module

    assert not hasattr(retriever_module, "_rerank_semantic_items")
    assert not hasattr(retriever_module, "_RERANK_THRESHOLD")


# --- end-to-end assembly: resident + retrieved, resident never trimmed ---


@pytest.mark.asyncio
async def test_build_memory_context_prepends_resident_untrimmed(tmp_path: Path, monkeypatch) -> None:
    from app.memory.activation import ActivationContext
    from app.services import memory_service

    agent_id = uuid4()
    tenant_id = uuid4()
    _write_resident_files(tmp_path, agent_id)
    _write_overlay_entry(tmp_path, agent_id, entry_id="ex-lang")
    _write_knowledge_page(
        tmp_path,
        agent_id,
        slug="l2-rollup",
        title="L2 Rollup",
        body="## Current Claim\nrollup 扩容。\n## Relations\n- is_a [[k:Scaling]]\n",
    )

    class _Principal:
        def can_access_sensitivity(self, _s: str) -> bool:
            return True

    async def fake_activation(**_kwargs):
        return ActivationContext(query="rollup", principal_stack=_Principal())

    async def fake_rerank(_tenant_id):
        return {"provider": "fake", "model": "fake"}

    async def select_knowledge(self, *, items, **_kwargs):
        candidate = next(item for item in items if item.metadata.get("source_type") == "knowledge_ppr")
        return [candidate.metadata["selection_candidate_id"]], "knowledge page answers the query"

    monkeypatch.setattr(memory_service, "_resolve_activation_context", lambda **kw: fake_activation(**kw))
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", fake_rerank)
    monkeypatch.setattr(memory_service.MemoryRetriever, "_select_with_model", select_knowledge)
    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: type("S", (), {"AGENT_DATA_DIR": str(tmp_path), "MEMORY_RESIDENT_BUDGET_CHARS": 20_000.0})(),
    )

    context = await memory_service.build_memory_context(agent_id, tenant_id, query="rollup 扩容")

    assert "需求含糊时爱自己猜" in context  # active failure mode on top
    assert "偏好简洁中文汇报" in context  # profiles whole
    assert "周报永远用中文" in context  # overlay in resident
    assert "L2 Rollup" in context  # retrieved knowledge
    assert context.index("需求含糊时爱自己猜") < context.index("L2 Rollup")


def test_resident_budget_setting_exists() -> None:
    from app.config import get_settings

    assert get_settings().MEMORY_RESIDENT_BUDGET_CHARS > 0
