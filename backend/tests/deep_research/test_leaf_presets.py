"""DR-2 red tests: the explorer leaf preset — worker.py's governed shape
(web allow-list + source capture) migrated onto the leaf preset registry,
with deterministic refinement (RC2 binary guard, cleaning, grading) and
per-leaf ledger shards written on the system side."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agents.subagent import SubagentHandle, SubagentResult, SubagentSpawnContext
from app.runtime.workflow_engine import LeafRequest
from app.services.deep_research.leaf_presets import (
    register_deep_research_leaf_presets,
    run_artifact_dir,
)
from app.services.deep_research.worker import (
    RESEARCH_WORKER_ALLOWED_TOOLS,
)
from app.services.workflow_launch import build_subagent_leaf_executor
from app.services.workflow_leaf_presets import reset_leaf_presets, resolve_leaf_preset


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    reset_leaf_presets()
    register_deep_research_leaf_presets()
    yield
    reset_leaf_presets()


def _ctx() -> SubagentSpawnContext:
    return SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
        tenant_id=uuid.uuid4(),
    )


def _request(run_id: str, leaf_id: str = "item-0") -> LeafRequest:
    return LeafRequest(
        run_id=run_id,
        step_id="explore",
        leaf=SimpleNamespace(name="deep_research_explorer", type="explorer", max_tool_rounds=8),
        task="Explore worker topic RWA market size for question X.",
        tenant_id=str(uuid.uuid4()),
        leaf_id=leaf_id,
    )


_GOOD_PAGE = (
    "Title: RWA Market Report 2026\n\n"
    "Tokenised treasury products grew to $4.2B by May 2026 according to the "
    "official registry data. The market doubled within twelve months and the "
    "number of issuers reached 38 across five jurisdictions."
)


def _result(sources: list[dict]) -> SubagentResult:
    return SubagentResult(
        name="deep_research_explorer",
        type="explorer",
        status="completed",
        content="## Findings\n- treasuries grew to $4.2B [https://data.gov/rwa]",
        tokens_used=900,
        sources=sources,
    )


def test_explorer_preset_registers_capability_surface():
    preset = resolve_leaf_preset("deep_research_explorer")
    assert preset is not None
    assert preset.allowed_tools == RESEARCH_WORKER_ALLOWED_TOOLS
    assert "deep_research_start" in preset.excluded_tools  # no recursive DR
    assert "write_file" in preset.excluded_tools  # LLM never writes artifacts
    assert preset.disable_tools is False
    assert preset.options["max_sources"] == 8
    assert preset.options["max_source_chars"] == 12000
    assert preset.post_process is not None


async def test_explorer_post_process_refines_raw_sources_into_ledger_shard():
    preset = resolve_leaf_preset("deep_research_explorer")
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    request = _request(run_id)
    raw_sources = [
        {"url": "https://data.gov/rwa", "tool_name": "web_fetch", "content": _GOOD_PAGE},
        {
            "url": "https://bad.example.com/file.pdf",
            "tool_name": "web_fetch",
            # Long enough to clear the usable-length gate — ONLY the RC2
            # binary/PDF guard can refuse this one.
            "content": "%PDF-1.4 /FlateDecode " + "obj stream endstream xref trailer startxref " * 8,
        },
        {"url": "https://err.example.com", "tool_name": "web_fetch", "content": "❌ web_fetch failed"},
    ]
    from app.runtime.workflow_engine import LeafOutcome

    outcome = await preset.post_process(
        request, ctx, _result(raw_sources), LeafOutcome(ok=True, output={"text": "digest"}, tokens_used=900)
    )

    shard = run_artifact_dir(ctx.parent_agent_id, run_id) / "shards" / "item-0"
    rows = [json.loads(line) for line in (shard / "sources.jsonl").read_text().splitlines()]
    assert len(rows) == 1, "binary/PDF and error pages must be refused"
    record = rows[0]
    assert record["url"] == "https://data.gov/rwa"
    assert record["evidence_tier"] == "tier1"  # .gov → REGULATORY → tier1
    assert record["publisher"] == "data.gov"
    assert record["source_id"].startswith("src_")
    assert (shard / "claims.jsonl").exists()

    assert outcome.ok
    assert outcome.output["source_count"] == 1
    assert outcome.output["source_ids"] == [record["source_id"]]
    assert outcome.output["ledger_shard"] == str(shard)


async def test_explorer_shards_do_not_collide_across_fanout_leaves():
    preset = resolve_leaf_preset("deep_research_explorer")
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    from app.runtime.workflow_engine import LeafOutcome

    for leaf_id, url in (("item-0", "https://a.gov/x"), ("item-1", "https://b.gov/y")):
        await preset.post_process(
            _request(run_id, leaf_id),
            ctx,
            _result([{"url": url, "tool_name": "web_fetch", "content": _GOOD_PAGE}]),
            LeafOutcome(ok=True, output={"text": "d"}, tokens_used=1),
        )

    base = run_artifact_dir(ctx.parent_agent_id, run_id) / "shards"
    shard0 = [json.loads(line) for line in (base / "item-0" / "sources.jsonl").read_text().splitlines()]
    shard1 = [json.loads(line) for line in (base / "item-1" / "sources.jsonl").read_text().splitlines()]
    assert [row["url"] for row in shard0] == ["https://a.gov/x"]
    assert [row["url"] for row in shard1] == ["https://b.gov/y"]
    assert shard0[0]["source_id"] != shard1[0]["source_id"]


async def test_full_explorer_flow_via_leaf_executor():
    """End to end through the wrapper: spec carries the preset surface, the
    spawn's captured sources flow into the shard, the outcome reports them."""
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    captured: list = []

    async def spawn(spawn_ctx, spec, task, *, budget=None):
        captured.append({"spec": spec, "budget": budget})
        return SubagentHandle(
            name=spec.name,
            trace_id="tr",
            depth=1,
            result=_result([{"url": "https://data.gov/rwa", "tool_name": "web_fetch", "content": _GOOD_PAGE}]),
        )

    executor = build_subagent_leaf_executor(ctx, spawn=spawn)
    outcome = await executor(_request(run_id))

    spec = captured[0]["spec"]
    assert spec.allowed_tools == RESEARCH_WORKER_ALLOWED_TOOLS
    assert "SUB-AGENT ROLE" in spec.system_prompt
    budget = captured[0]["budget"]
    assert budget.max_sources == 8 and budget.max_source_chars == 12000
    assert outcome.ok
    assert outcome.output["source_count"] == 1
    assert (run_artifact_dir(ctx.parent_agent_id, run_id) / "shards" / "item-0" / "sources.jsonl").exists()
