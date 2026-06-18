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


def _patch_plan_service_must_not_be_called(monkeypatch, handler):
    class FakePlanService:
        async def ensure_awaiting_plan_from_fill(self, **_kwargs):
            raise AssertionError("Deep Research confirmation card must not create a Plan Mode ledger row")

    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: FakePlanService(), raising=False)


@pytest.mark.asyncio
async def test_run_without_user_confirmation_returns_confirmation_card(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_run(**_kwargs):
        raise AssertionError("the research workflow must not start before plan confirmation")

    monkeypatch.setattr(handler, "start_deep_research_workflow_run", fail_run)
    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_run)
    _patch_plan_service_must_not_be_called(monkeypatch, handler)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "standard"})

    payload = json.loads(await handler.deep_research_run(req))

    assert payload["ok"] is False
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert payload["clarifying_questions"]
    assert payload["worker_topics"]
    assert "plan_json" not in payload
    assert "plan_id" not in payload
    assert "plan_markdown" not in payload


@pytest.mark.asyncio
async def test_start_without_user_confirmation_returns_confirmation_card(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no workflow run should start before plan confirmation")

    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_create)
    _patch_plan_service_must_not_be_called(monkeypatch, handler)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "full"})

    payload = json.loads(await handler.deep_research_start(req))

    assert payload["ok"] is False
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert payload["clarifying_questions"]
    assert "plan_id" not in payload


@pytest.mark.asyncio
async def test_start_without_user_confirmation_returns_confirmable_payload_without_plan_mode_row(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no workflow run should start before plan confirmation")

    class FakePlanService:
        def __init__(self):
            self.calls: list[dict] = []

        async def ensure_awaiting_plan_from_fill(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("Deep Research confirmation card must not create a Plan Mode ledger row")

    fake_plan_service = FakePlanService()
    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_create)
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: fake_plan_service, raising=False)
    req = _request(tmp_path)
    req.arguments.update(
        {
            "question": "Research the RWA launchpad opportunity",
            "depth": "full",
            "output_format": "docx",
        }
    )

    payload = json.loads(await handler.deep_research_start(req))

    assert payload["ok"] is False
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert "plan_id" not in payload
    assert "plan_version" not in payload
    assert "plan_hash" not in payload
    assert "plan_markdown_path" not in payload
    assert "plan_markdown" not in payload
    assert "plan_json" not in payload
    assert payload["worker_topics"]
    assert fake_plan_service.calls == []


@pytest.mark.asyncio
async def test_confirmation_card_forbids_agent_self_confirmation(tmp_path, monkeypatch):
    """RC14: the confirmation card is a USER confirmation hard-gate. An agent must not read preference
    memory and set plan_confirmed=true on the user's behalf (observed: agent self-confirmed a full
    RWA run from memory, skipping the user). The guidance must forbid self-confirming and
    require the user's explicit approval in a new message; preference memory may only prefill params."""
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no runtime task before plan confirmation")

    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_create)
    _patch_plan_service_must_not_be_called(monkeypatch, handler)
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
    from app.tools.handlers import deep_research as handler

    async def fake_workflow_run(*, request, agent_id, user_id, workspace, plan_id=None, run_id=None, **_kwargs):
        return {
            "workflow_run_id": "confirmed",
            "status": "completed",
            "report_path": str(workspace / "report.md"),
            "workspace_artifact_dir": str(workspace),
        }

    monkeypatch.setattr(handler, "start_deep_research_workflow_run", fake_workflow_run)
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

    # Keep the background run pending so the in-flight key persists.
    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", lambda **_k: None)

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
async def test_workflow_args_use_confirmed_worker_topics(tmp_path):
    """DR-6b 等价锚：用户确认的 worker topics 必须原样成为 fanout items —
    workflow 形态里 explore 步的 items_from=args.worker_topics。"""
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.workflow_definition import deep_research_workflow_args

    args = deep_research_workflow_args(
        ResearchRequest(
            question="Audit RWA custody claims.",
            mode="source_ledger_audit",
            plan_confirmed=True,
            worker_topics=["confirmed lane alpha", "confirmed lane beta"],
        )
    )
    assert args["worker_topics"] == ["confirmed lane alpha", "confirmed lane beta"]
