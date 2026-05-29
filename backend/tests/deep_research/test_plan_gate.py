from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


def _request(tmp_path: Path, *, agent_id: uuid.UUID | None = None):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="deep_research_run",
        arguments={},
        context=ToolExecutionContext(
            agent_id=agent_id or uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=tmp_path,
        ),
    )


@pytest.mark.asyncio
async def test_run_without_plan_confirmed_returns_needs_plan(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_run(**_kwargs):
        raise AssertionError("run_deep_research must not execute before plan confirmation")

    monkeypatch.setattr(handler, "run_deep_research", fail_run)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "standard"})

    payload = json.loads(await handler.deep_research_run(req))

    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert payload["clarifying_questions"]
    assert payload["worker_topics"]
    assert "lanes" in payload["plan"]


@pytest.mark.asyncio
async def test_start_without_plan_confirmed_returns_needs_plan(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no runtime task should be created before plan confirmation")

    monkeypatch.setattr(handler, "create_runtime_task_record", fail_create)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "full"})

    payload = json.loads(await handler.deep_research_start(req))

    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert payload["clarifying_questions"]


@pytest.mark.asyncio
async def test_needs_plan_forbids_agent_self_confirmation(tmp_path, monkeypatch):
    """RC14: the plan gate is a USER confirmation hard-gate. An agent must not read preference
    memory and set plan_confirmed=true on the user's behalf (observed: agent self-confirmed a full
    RWA run from memory, skipping the user). The needs_plan guidance must forbid self-confirming and
    require the user's explicit approval in a new message; preference memory may only prefill params."""
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no runtime task before plan confirmation")

    monkeypatch.setattr(handler, "create_runtime_task_record", fail_create)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "full"})

    payload = json.loads(await handler.deep_research_start(req))
    guidance = (payload["next_action"] + " " + payload["summary"]).lower()

    assert "never self-confirm" in guidance
    assert "not approval" in guidance
    assert "wait" in guidance  # must wait for the user's reply, not self-confirm in the same turn
    assert "preference" in guidance and "prefill" in guidance  # memory only prefills params


@pytest.mark.asyncio
async def test_run_with_plan_confirmed_executes(tmp_path, monkeypatch):
    from app.services.deep_research.schemas import ResearchRun
    from app.tools.handlers import deep_research as handler

    async def fake_run(*, request, agent_id, user_id, workspace, runtime_task_id=None):
        artifact_dir = workspace / "runtime_artifacts" / "deep_research" / "confirmed"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "report.md").write_text("# Report\n", encoding="utf-8")
        (artifact_dir / "sources.jsonl").write_text("{}", encoding="utf-8")
        (artifact_dir / "claims.jsonl").write_text("{}", encoding="utf-8")
        return ResearchRun(
            run_id="confirmed",
            status="completed",
            summary="done",
            artifact_dir=artifact_dir.as_posix(),
            report_path=(artifact_dir / "report.md").as_posix(),
            sources_path=(artifact_dir / "sources.jsonl").as_posix(),
            claims_path=(artifact_dir / "claims.jsonl").as_posix(),
            quality_gates={"attribution": "passed"},
        )

    monkeypatch.setattr(handler, "run_deep_research", fake_run)
    req = _request(tmp_path)
    req.arguments.update(
        {"question": "Research RWA", "depth": "standard", "plan_confirmed": True, "worker_topics": ["lane A"]}
    )

    payload = json.loads(await handler.deep_research_run(req))

    assert payload["ok"] is True
    assert payload["status"] == "completed"


@pytest.mark.asyncio
async def test_dedup_blocks_second_concurrent_start(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def noop_create(**kwargs):
        return kwargs["task_id"]

    async def noop_plan(**_kwargs):
        return {}

    async def noop_progress(**_kwargs):
        return {}

    monkeypatch.setattr(handler, "create_runtime_task_record", noop_create)
    monkeypatch.setattr(handler, "record_long_task_plan", noop_plan)
    monkeypatch.setattr(handler, "record_long_task_progress", noop_progress)
    # Keep the background task pending so the in-flight key persists.
    monkeypatch.setattr(handler, "_schedule_deep_research_background", lambda *_a, **_k: None)

    agent_id = uuid.uuid4()
    question = "Dedup this exact question about RWA"

    first = json.loads(await handler.deep_research_start(_make_start_req(tmp_path, agent_id, question)))
    second = json.loads(await handler.deep_research_start(_make_start_req(tmp_path, agent_id, question)))

    assert first["ok"] is True and first["status"] == "running"
    assert second.get("deduped") is True
    assert second["task_id"] == first["task_id"]

    # cleanup the in-flight guard so we don't leak into other tests
    handler._INFLIGHT_DEEP_RESEARCH.pop((str(agent_id), question.casefold()), None)


def _make_start_req(tmp_path, agent_id, question):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="deep_research_start",
        arguments={"question": question, "depth": "standard", "plan_confirmed": True},
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=tmp_path,
        ),
    )


class _TopicRecordingRunner:
    def __init__(self):
        self.topics: list[str] = []

    async def run(self, topic: str, *, request, cancel_event=None):
        from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

        self.topics.append(topic)
        idx = len(self.topics)
        return WorkerResult(
            topic=topic,
            intermediate_report=f"Digest {idx}: integrated evidence, 35% growth, 18 controls in 2026.",
            sources=[
                SourceRecord(
                    source_id="",
                    url=f"https://w{idx}.example/s",
                    title=f"src {idx}",
                    publisher=f"w{idx}.example",
                    source_type=SourceType.PRIMARY,
                    content=f"Title: s{idx}\nIssuer discloses 35% growth, 12 jurisdictions, 18 controls in 2026.",
                    fetch_tool="web_fetch",
                )
            ],
            status="ok",
        )


class _MiniReasoner:
    async def refine_plan(self, request, plan):
        return plan

    async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results, **_kw):
        ids = list(ledger.sources)
        second = ids[1] if len(ids) > 1 else ids[0]
        return f"""# Confirmed-topic Audit

## Executive Thesis

Integrated evidence across the fetched sources from 2024, 2025, and 2026 supports the 35%
growth claim over 12 jurisdictions, paired with 18 transfer-control checks and 7 reporting
checkpoints in 2026. Converging signals rather than a single bullish source carry the thesis,
so confidence is moderate while jurisdiction-specific legal interpretation still needs review.
Sources: {ids[0]}, {second}.

## Method And Source Standard

Primary disclosures outrank secondary commentary; tier1 sources carry the argument and search
snippets were never cited as evidence. Every source id in the prose resolves to a fetched page
in the ledger before completion, and weakly graded sources cannot solely support a key claim.

## Findings

- The 35% growth claim holds across 12 jurisdictions in 2026, corroborated across independent
  sources rather than cherry-picked from one. Sources: {ids[0]}, {second}.
- The product workflow exposes 18 transfer-control checks and 7 reporting checkpoints, which makes
  it materially more complex than a simple token sale. Sources: {ids[0]}.

## Contradictions And Gaps

- The strongest counter-argument is that the 35% figure may be self-reported; an independent audit
  is still missing and the secondary-liquidity claim remains inferred. Sources: {second}.

## Source Ledger

- `{ids[0]}` issuer disclosure
- `{second}` regulator filing
"""


@pytest.mark.asyncio
async def test_orchestrator_uses_confirmed_worker_topics(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def _no_linear(tool_name: str, arguments: dict) -> str:
        raise AssertionError("linear path must not run")

    runner = _TopicRecordingRunner()
    result = await DeepResearchOrchestrator(_no_linear, reasoner=_MiniReasoner(), worker_runner=runner).run(
        ResearchRequest(
            question="Audit RWA custody claims.",
            mode="source_ledger_audit",
            depth="quick",
            max_rounds=1,
            max_sources=3,
            concurrency=3,
            plan_confirmed=True,
            worker_topics=["confirmed lane alpha", "confirmed lane beta"],
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "completed"
    # The orchestrator must use the user-confirmed topics verbatim, not planner-generated ones.
    assert set(runner.topics) == {"confirmed lane alpha", "confirmed lane beta"}
