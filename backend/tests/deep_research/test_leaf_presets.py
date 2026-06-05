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


# ── DR-3: planner / critic / synthesizer presets ──────────────────


_PASSING_REPORT_TEMPLATE = """# RWA Pre-IPO Launchpad Deep Research

## Executive Thesis

The investable opportunity is not generic RWA tokenization; it is the narrower ability to combine issuer diligence,
transfer restrictions, compliant distribution, and secondary liquidity into one launchpad workflow. Comparable
platforms — Republic Forge, CartaX, Securitize, and INX Securities — already exceeded $4.2B in cumulative volume
across 28 issuances by Q4 2026. Evidence from {s0} and {s1} supports the market infrastructure
requirement, while {s2} shows the liquidity and compliance constraints (SEC Reg D 506(c),
MAS Securities Act exemption, SFC Type 1 license) that decide whether the product can scale beyond 17 jurisdictions.

## Key Findings

1. A Pre-IPO RWA launchpad is structurally closer to a regulated issuance and liquidity workflow than a DeFi token
   sale. Republic Forge and CartaX took 18-24 months to clear US Reg D before live trading. Sources: {s0}, {s1}.
2. The main adoption wedge is institutional familiarity with tokenized funds (BlackRock BUIDL grew from $250M in Q1
   2026 to $1.7B by Q4 2026) and private-market access via Carta. Sources: {s2}, {fake}.
3. The defensible product surface is a bundled due-diligence, issuance, compliance, and reporting system. Of the top
   12 platforms, only 3 (Securitize, Republic Forge, INX Securities) closed the full loop in 2026. Sources: {s0}.

## Strategic Implications

- Build compliance and disclosure as first-class workflow primitives, not static documents.
- Benchmark against Securitize, Ondo Finance, Republic Forge, and INX Securities.

## Contradictions And Gaps

- The source set supports a directional thesis but not jurisdiction-specific legal advice across SEC, MAS, and SFC
  regimes; the next pass should compare transfer restrictions across the 5 most relevant product categories.

## Source Ledger

- `{s0}` — tier1
- `{s1}` — tier1
- `{s2}` — tier1
"""


def _seed_request_json(ctx, run_id: str, **overrides) -> None:
    import json as _json

    from app.services.deep_research.schemas import to_jsonable

    payload = {
        "question": "RWA Pre-IPO launchpad landscape",
        "worker_topics": ["market", "regulation"],
        "output_language": "en",
        **overrides,
    }
    root = run_artifact_dir(ctx.parent_agent_id, run_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "request.json").write_text(_json.dumps(to_jsonable(payload), ensure_ascii=False), encoding="utf-8")


async def _seed_explorer_shards(ctx, run_id: str) -> list[str]:
    """Two explorer leaves worth of shards (digest + sources) via the real post_process."""
    from app.runtime.workflow_engine import LeafOutcome

    preset = resolve_leaf_preset("deep_research_explorer")
    source_ids: list[str] = []
    for leaf_id, url in (("item-0", "https://a.gov/market"), ("item-1", "https://b.gov/regulation")):
        outcome = await preset.post_process(
            _request(run_id, leaf_id),
            ctx,
            _result([{"url": url, "tool_name": "web_fetch", "content": _GOOD_PAGE}]),
            LeafOutcome(ok=True, output={"text": f"## Findings for {leaf_id}\n- grounded fact"}, tokens_used=10),
        )
        source_ids.extend(outcome.output["source_ids"])
    return source_ids


def test_all_deep_research_presets_registered_with_rc11_surfaces():
    for name in (
        "deep_research_planner",
        "deep_research_explorer",
        "deep_research_synthesizer",
        "deep_research_critic",
    ):
        assert resolve_leaf_preset(name) is not None, name
    # RC11: every non-browsing DR leaf runs with ZERO tools exposed.
    assert resolve_leaf_preset("deep_research_planner").disable_tools is True
    assert resolve_leaf_preset("deep_research_synthesizer").disable_tools is True
    assert resolve_leaf_preset("deep_research_critic").disable_tools is True
    assert resolve_leaf_preset("deep_research_explorer").disable_tools is False


async def test_explorer_writes_digest_and_meta_for_the_shard_bus():
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    await _seed_explorer_shards(ctx, run_id)
    shard = run_artifact_dir(ctx.parent_agent_id, run_id) / "shards" / "item-0"
    assert "Findings for item-0" in (shard / "digest.md").read_text()
    meta = json.loads((shard / "meta.json").read_text())
    assert meta["leaf_id"] == "item-0"
    assert meta["topic"].startswith("Explore worker topic")


async def test_critic_pre_builds_adversarial_task_and_post_lands_review():
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    _seed_request_json(ctx, run_id)
    await _seed_explorer_shards(ctx, run_id)
    preset = resolve_leaf_preset("deep_research_critic")
    request = _request(run_id, leaf_id=None)
    request.step_id = "critic"
    request.leaf = SimpleNamespace(name="deep_research_critic", type="critic", max_tool_rounds=8)

    task = await preset.pre_process(request, ctx)
    assert "Devil's Advocate" in task
    assert "worker_digests" in task and "Findings for item-0" in task

    from app.runtime.workflow_engine import LeafOutcome

    review = {"cherry_picking": ["only bullish sources"], "strongest_counter_argument": "liquidity is unproven"}
    result = SubagentResult(
        name="deep_research_critic", type="critic", status="completed", content=json.dumps(review), tokens_used=5
    )
    outcome = await preset.post_process(request, ctx, result, LeafOutcome(ok=True, output={"text": result.content}))
    assert outcome.ok
    assert outcome.output["devils_advocate"]["strongest_counter_argument"] == "liquidity is unproven"
    saved = json.loads(
        (run_artifact_dir(ctx.parent_agent_id, run_id) / "devils_advocate.jsonl").read_text().splitlines()[0]
    )
    assert saved["cherry_picking"] == ["only bullish sources"]

    # Non-JSON critique → fail loud so the definition-level retry re-spawns.
    bad = SubagentResult(name="deep_research_critic", type="critic", status="completed", content="not json")
    bad_outcome = await preset.post_process(request, ctx, bad, LeafOutcome(ok=True, output={"text": "not json"}))
    assert bad_outcome.ok is False


async def test_synthesizer_pre_merges_shards_and_builds_digest_instruction():
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    _seed_request_json(ctx, run_id)
    await _seed_explorer_shards(ctx, run_id)
    preset = resolve_leaf_preset("deep_research_synthesizer")
    request = _request(run_id, leaf_id=None)
    request.step_id = "synthesize"

    task = await preset.pre_process(request, ctx)

    assert "COVERAGE IS MANDATORY" in task
    assert "worker_digests" in task
    root = run_artifact_dir(ctx.parent_agent_id, run_id)
    merged = [json.loads(line) for line in (root / "sources.jsonl").read_text().splitlines()]
    assert len(merged) == 2, "both shards must merge into the top-level ledger"


async def test_synthesizer_post_rc12_footnotes_quality_gate_and_artifacts():
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    _seed_request_json(ctx, run_id)
    real_ids = await _seed_explorer_shards(ctx, run_id)
    preset = resolve_leaf_preset("deep_research_synthesizer")
    request = _request(run_id, leaf_id=None)
    request.step_id = "synthesize"
    await preset.pre_process(request, ctx)  # merge + stage synthesis inputs

    from app.runtime.workflow_engine import LeafOutcome

    report = _PASSING_REPORT_TEMPLATE.format(s0=real_ids[0], s1=real_ids[1], s2=real_ids[0], fake="src_fabricated9999")
    result = SubagentResult(
        name="deep_research_synthesizer", type="worker", status="completed", content=report, tokens_used=50
    )
    outcome = await preset.post_process(request, ctx, result, LeafOutcome(ok=True, output={"text": report}))

    assert outcome.ok, outcome.error
    root = run_artifact_dir(ctx.parent_agent_id, run_id)
    saved = (root / "report.md").read_text()
    assert "src_fabricated9999" not in saved, "RC12 must neutralise hallucinated refs"
    assert "[^1]" in saved and "## Footnotes" in saved
    assert outcome.output["quality"] == "passed"
    assert outcome.output["report_path"] == str(root / "report.md")
    assert (root / "final.json").exists()

    # Garbage synthesis → fail loud (definition retry owns the second attempt).
    junk = SubagentResult(name="deep_research_synthesizer", type="worker", status="completed", content="too short")
    junk_outcome = await preset.post_process(request, ctx, junk, LeafOutcome(ok=True, output={"text": "too short"}))
    assert junk_outcome.ok is False


def test_definition_order_critic_before_synthesize_with_retry():
    """I2 保真：对抗评审发生在合成之前（P-Q2），合成步带引擎级 retry。"""
    from app.services.deep_research.workflow_definition import build_deep_research_workflow_definition

    definition = build_deep_research_workflow_definition()
    order = [step["id"] for step in definition["steps"]]
    assert order == ["plan", "explore", "critic", "synthesize"]
    synthesize = definition["steps"][3]
    assert synthesize["retry"] == {"max_attempts": 2}


# ── DR-5b: per-claim adversarial verdicts (CC Verify alignment) ────


async def test_critic_payload_carries_claims_and_demands_verdicts():
    """The critic must receive the claim ledger (with ids) and be instructed
    to return per-claim uphold/refute verdicts — CC's Verify semantics."""
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    _seed_request_json(ctx, run_id)
    await _seed_explorer_shards(ctx, run_id)
    preset = resolve_leaf_preset("deep_research_critic")
    request = _request(run_id, leaf_id=None)
    request.step_id = "critic"

    task = await preset.pre_process(request, ctx)

    assert "claim_verdicts" in task, "instruction must demand per-claim verdicts"
    assert '"claim_id"' in task and "refute" in task


async def test_refuted_claims_are_killed_for_synthesis_but_audit_visible():
    """A claim the critic refutes must (a) never reach the synthesis payload
    and (b) stay in the merged claims.jsonl downgraded to unsupported with the
    refute reason — killed for the report, visible for audit."""
    ctx = _ctx()
    run_id = str(uuid.uuid4())
    _seed_request_json(ctx, run_id)
    await _seed_explorer_shards(ctx, run_id)
    root = run_artifact_dir(ctx.parent_agent_id, run_id)

    # Identify a real claim id from the shards to refute.
    shard_claims = []
    for shard in sorted((root / "shards").iterdir()):
        path = shard / "claims.jsonl"
        if path.exists():
            shard_claims.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    assert shard_claims, "fixture must produce at least one extracted claim"
    doomed = shard_claims[0]["claim_id"]

    (root / "devils_advocate.jsonl").write_text(
        json.dumps(
            {
                "strongest_counter_argument": "registry data is self-reported",
                "claim_verdicts": [{"claim_id": doomed, "verdict": "refute", "reason": "single self-reported source"}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    preset = resolve_leaf_preset("deep_research_synthesizer")
    request = _request(run_id, leaf_id=None)
    request.step_id = "synthesize"
    task = await preset.pre_process(request, ctx)

    # (a) killed for the writer: the doomed claim id is absent from the payload.
    payload_str = task[task.find("{") :]
    payload = (
        json.loads(payload_str[payload_str.find('{"question"') :])
        if '{"question"' in payload_str
        else json.loads(payload_str)
    )
    payload_claim_ids = {claim.get("claim_id") for claim in payload.get("claims", [])}
    assert doomed not in payload_claim_ids

    # (b) audit-visible: merged claims.jsonl keeps it, downgraded + annotated.
    merged_claims = [
        json.loads(line) for line in (root / "claims.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    doomed_rows = [
        row for row in merged_claims if row.get("claim_id") == doomed or "refuted" in str(row.get("notes", ""))
    ]
    assert any(
        row.get("status") == "unsupported" and "refute" in str(row.get("notes", "")).casefold() for row in doomed_rows
    ), f"refuted claim must stay downgraded+annotated, got: {doomed_rows}"
