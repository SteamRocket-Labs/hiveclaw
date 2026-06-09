from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

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


def _patch_fake_plan_service(monkeypatch, handler, tmp_path: Path):
    from app.services import plan_mode_core

    plan_id = uuid.uuid4()
    plan_json = {
        "schema": plan_mode_core.PLAN_SCHEMA,
        "title": "Deep Research plan",
        "intent_type": "in_session_execution",
        "objective": "Run approved Deep Research.",
        "motivation": "User requested Deep Research.",
        "steps": [{"order": 1, "description": "Confirm plan."}],
        "success_criteria": ["Source-grounded report."],
        "wake_policy": {"type": "none"},
        "required_capabilities": ["deep_research_start"],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": ["Long-running research task"]},
        "estimated_cost": {"tokens_per_run": "medium", "expected_duration": "several minutes"},
        "stop_conditions": ["User rejects the plan."],
        "handoff": {"target": "deep_research", "create_objective": False, "create_trigger": False, "payload": {}},
        "deep_research": {"output_format": "markdown", "worker_topics": ["topic"]},
    }
    plan = SimpleNamespace(
        id=plan_id,
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash=plan_mode_core.compute_plan_hash(plan_json),
        plan_json=plan_json,
        plan_markdown_path=str(tmp_path / "plans" / f"{plan_id}.md"),
    )

    class FakePlanService:
        async def ensure_awaiting_plan_from_fill(self, **_kwargs):
            return plan

    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: FakePlanService(), raising=False)
    return plan


@pytest.mark.asyncio
async def test_run_without_plan_confirmed_returns_needs_plan(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_run(**_kwargs):
        raise AssertionError("the research workflow must not start before plan confirmation")

    monkeypatch.setattr(handler, "start_deep_research_workflow_run", fail_run)
    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_run)
    _patch_fake_plan_service(monkeypatch, handler, tmp_path)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "standard"})

    payload = json.loads(await handler.deep_research_run(req))

    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert payload["clarifying_questions"]
    assert payload["worker_topics"]
    assert "plan" not in payload
    assert "plan_json" not in payload
    assert payload["plan_markdown"]
    assert payload["plan_id"]


@pytest.mark.asyncio
async def test_start_without_plan_confirmed_returns_needs_plan(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no workflow run should start before plan confirmation")

    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_create)
    _patch_fake_plan_service(monkeypatch, handler, tmp_path)
    req = _request(tmp_path)
    req.arguments.update({"question": "Research the RWA launchpad opportunity", "depth": "full"})

    payload = json.loads(await handler.deep_research_start(req))

    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert payload["clarifying_questions"]
    assert payload["plan_id"]


@pytest.mark.asyncio
async def test_start_without_plan_confirmed_returns_confirmable_plan_card_payload(tmp_path, monkeypatch):
    from app.tools.handlers import deep_research as handler
    from app.services import plan_mode_core

    async def fail_create(**_kwargs):
        raise AssertionError("no workflow run should start before plan confirmation")

    plan_id = uuid.uuid4()
    plan_json = {
        "schema": plan_mode_core.PLAN_SCHEMA,
        "title": "Deep Research: RWA launchpad opportunity",
        "intent_type": "in_session_execution",
        "objective": "Research the RWA launchpad opportunity.",
        "motivation": "User asked for source-ledger-backed Deep Research.",
        "steps": [{"order": 1, "description": "Confirm the research plan with the user."}],
        "success_criteria": ["Final report is source-grounded and preserves report.md as canonical output."],
        "wake_policy": {"type": "none"},
        "required_capabilities": ["deep_research_start", "office_document_create"],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": ["Long-running research task"]},
        "estimated_cost": {"tokens_per_run": "high", "expected_duration": "several minutes"},
        "stop_conditions": ["User rejects the plan."],
        "handoff": {
            "target": "deep_research",
            "create_objective": False,
            "create_trigger": False,
            "payload": {
                "question": "Research the RWA launchpad opportunity",
                "depth": "full",
                "output_format": "docx",
                "plan_confirmed": True,
                "worker_topics": ["official evidence"],
            },
        },
        "deep_research": {
            "output_format": "docx",
            "worker_topics": ["official evidence"],
        },
    }
    plan_markdown_path = tmp_path / "plans" / f"{plan_id}.md"
    plan_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    plan_markdown_path.write_text(
        "# Deep Research Markdown Plan\n\n- Confirm the RWA research lanes before execution.\n",
        encoding="utf-8",
    )
    plan = SimpleNamespace(
        id=plan_id,
        status="awaiting_confirmation",
        plan_version=3,
        plan_hash=plan_mode_core.compute_plan_hash(plan_json),
        plan_json=plan_json,
        plan_markdown_path=str(plan_markdown_path),
    )

    class FakePlanService:
        def __init__(self):
            self.calls: list[dict] = []

        async def ensure_awaiting_plan_from_fill(self, **kwargs):
            self.calls.append(kwargs)
            return plan

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
    assert payload["status"] == "needs_plan"
    assert payload["plan_id"] == str(plan_id)
    assert payload["plan_version"] == 3
    assert payload["plan_hash"] == plan.plan_hash
    assert payload["plan_markdown_path"] == str(plan_markdown_path)
    assert payload["plan_markdown"].startswith("# Deep Research Markdown Plan")
    assert "plan_json" not in payload
    assert payload["worker_topics"] == ["official evidence"]
    assert fake_plan_service.calls, "Deep Research needs_plan must materialize a real Plan Mode ledger row"
    assert fake_plan_service.calls[0]["intent_type"] == "in_session_execution"
    assert fake_plan_service.calls[0]["fill"]["handoff"]["target"] == "deep_research"
    assert fake_plan_service.calls[0]["fill"]["handoff"]["payload"]["plan_confirmed"] is True


@pytest.mark.asyncio
async def test_needs_plan_forbids_agent_self_confirmation(tmp_path, monkeypatch):
    """RC14: the plan gate is a USER confirmation hard-gate. An agent must not read preference
    memory and set plan_confirmed=true on the user's behalf (observed: agent self-confirmed a full
    RWA run from memory, skipping the user). The needs_plan guidance must forbid self-confirming and
    require the user's explicit approval in a new message; preference memory may only prefill params."""
    from app.tools.handlers import deep_research as handler

    async def fail_create(**_kwargs):
        raise AssertionError("no runtime task before plan confirmation")

    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fail_create)
    _patch_fake_plan_service(monkeypatch, handler, tmp_path)
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
