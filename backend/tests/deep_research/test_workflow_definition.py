from __future__ import annotations

import asyncio
import uuid

import pytest

from app.config import get_settings
from app.runtime.workflow_admission import AdmissionLimits, admit_workflow
from app.runtime.workflow_compiler import compile_workflow
from app.services.deep_research.schemas import ResearchRequest
from app.services.deep_research.workflow_definition import (
    DEEP_RESEARCH_WORKFLOW_NAME,
    build_deep_research_workflow_definition,
    deep_research_workflow_args,
)


def test_deep_research_v1_definition_compiles_to_plan_fanout_critic_synthesize() -> None:
    definition = build_deep_research_workflow_definition()

    compiled = compile_workflow(
        definition,
        known_leaves={
            "deep_research_planner",
            "deep_research_explorer",
            "deep_research_synthesizer",
            "deep_research_critic",
        },
    )

    assert definition["name"] == DEEP_RESEARCH_WORKFLOW_NAME
    # DR-3 (P-Q2 保真): the adversarial critic runs BEFORE synthesis.
    assert [step.id for step in compiled.definition.steps] == ["plan", "explore", "critic", "synthesize"]
    assert compiled.leaf_names == {
        "deep_research_planner",
        "deep_research_explorer",
        "deep_research_synthesizer",
        "deep_research_critic",
    }


def test_deep_research_v1_args_are_admitted_and_fallback_topics_are_bounded() -> None:
    request = ResearchRequest.from_arguments(
        {
            "question": "Map the current enterprise AI workflow market",
            "depth": "full",
            "scope": "US SaaS",
            "output_format": "markdown",
            "plan_confirmed": True,
        }
    )
    args = deep_research_workflow_args(request)
    compiled = compile_workflow(build_deep_research_workflow_definition())

    admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))

    assert args["worker_topics"] == ["Map the current enterprise AI workflow market"]
    assert admission.budget_tokens > 0


# ── real-PG registration contract (§9 P14) ────────────────────────


@pytest.fixture()
async def pg_tenant_id(owner_sessionmaker):
    from app.database import tenant_scoped_session
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="dr-wf", slug=f"dr-{tid.hex[:10]}"))
    return tid


@pytest.mark.usefixtures("migrated_pg_url")
async def test_ensure_definition_is_idempotent_on_real_pg(pg_tenant_id, owner_sessionmaker):
    """First call registers + activates deep_research.v1; the second call must
    return the SAME record instead of stacking a new version."""
    from app.services.deep_research.workflow_definition import ensure_deep_research_workflow_definition

    first = await ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker)
    second = await ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker)

    assert first.status == "active"
    assert second.id == first.id
    assert second.definition_version == first.definition_version


@pytest.mark.usefixtures("migrated_pg_url")
async def test_concurrent_ensure_does_not_stack_duplicate_versions(pg_tenant_id, owner_sessionmaker):
    """Two concurrent ensures race create_draft (same-name auto-next-version
    never hits the unique constraint) — without serialisation they register
    v1 AND v2 both active. The advisory lock must collapse that to one."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.workflow import WorkflowDefinitionRecord
    from app.services.deep_research.workflow_definition import (
        DEEP_RESEARCH_WORKFLOW_NAME,
        ensure_deep_research_workflow_definition,
    )

    records = await asyncio.gather(
        ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker),
        ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker),
    )
    assert records[0].id == records[1].id

    async with tenant_scoped_session(str(pg_tenant_id), session_factory=owner_sessionmaker) as session:
        rows = (
            (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(
                        WorkflowDefinitionRecord.tenant_id == pg_tenant_id,
                        WorkflowDefinitionRecord.name == DEEP_RESEARCH_WORKFLOW_NAME,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        f"expected exactly one registered record, got versions {[r.definition_version for r in rows]}"
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_ensure_definition_never_reuses_other_tenant_record_under_owner_dsn(
    pg_tenant_id,
    owner_sessionmaker,
):
    """The ensure query must be tenant-explicit, not RLS-only. Owner/superuser
    test DSNs can see every tenant; without a tenant predicate tenant B would
    incorrectly reuse tenant A's active deep_research.v1 record."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.tenant import Tenant
    from app.models.workflow import WorkflowDefinitionRecord
    from app.services.deep_research.workflow_definition import ensure_deep_research_workflow_definition

    other_tenant = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=other_tenant, name="dr-wf-other", slug=f"dr-{other_tenant.hex[:10]}"))

    first = await ensure_deep_research_workflow_definition(
        tenant_id=pg_tenant_id,
        session_factory=owner_sessionmaker,
    )
    second = await ensure_deep_research_workflow_definition(
        tenant_id=other_tenant,
        session_factory=owner_sessionmaker,
    )

    assert first.tenant_id == pg_tenant_id
    assert second.tenant_id == other_tenant
    assert second.id != first.id

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = (
            (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(WorkflowDefinitionRecord.name == DEEP_RESEARCH_WORKFLOW_NAME)
                )
            )
            .scalars()
            .all()
        )
    assert {row.tenant_id for row in rows} >= {pg_tenant_id, other_tenant}


# ── DR-4: launch wiring + product-surface parity ───────────────────


async def test_start_workflow_run_writes_request_json_and_returns_real_dirs(tmp_path, monkeypatch):
    """The launch must (1) pre-generate the run id, (2) write request.json to
    the run artifact root BEFORE any leaf executes, (3) register the DR leaf
    presets, and (4) return real artifact/workspace paths — not null."""
    from types import SimpleNamespace

    from app.config import get_settings
    from app.services.deep_research import workflow_definition as wf_def
    from app.services.deep_research.leaf_presets import run_artifact_dir
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.workflow_leaf_presets import reset_leaf_presets, resolve_leaf_preset

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "agents"))
    reset_leaf_presets()

    agent_id = uuid.uuid4()
    tenant = uuid.uuid4()
    captured: dict = {}

    async def fake_resolve(aid, **kwargs):
        return SimpleNamespace(id=aid, tenant_id=tenant, name="A", role_description=""), SimpleNamespace()

    async def fake_ensure(**kwargs):
        return SimpleNamespace(
            name="deep_research.v1",
            definition_version=1,
            definition_hash="hash-1",
            definition_json={"name": "deep_research.v1"},
        )

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        run_id = kwargs["run_id"]
        # request.json must already be on disk when the first leaf would run.
        root = run_artifact_dir(agent_id, run_id)
        captured["request_json_existed_at_launch"] = (root / "request.json").exists()
        (root / "report.md").parent.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_text("# Report\n", encoding="utf-8")
        (root / "final.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(run_id=run_id, outcome=SimpleNamespace(status="completed", reason=None))

    monkeypatch.setattr(wf_def, "resolve_agent_runtime", fake_resolve)
    monkeypatch.setattr(wf_def, "ensure_deep_research_workflow_definition", fake_ensure)
    monkeypatch.setattr(wf_def, "start_ephemeral_workflow_for_agent", fake_launch)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = await wf_def.start_deep_research_workflow_run(
        request=ResearchRequest.from_arguments({"question": "RWA market", "output_language": "en"}),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        workspace=workspace,
    )

    assert captured["request_json_existed_at_launch"] is True
    assert resolve_leaf_preset("deep_research_explorer") is not None, "presets must be registered at launch"
    root = run_artifact_dir(agent_id, captured["run_id"])
    assert payload["workspace_artifact_dir"] == str(root)
    assert payload["report_path"] == str(root / "report.md")
    # Completed run → workspace packet mirrored next to the agent's files.
    assert (workspace / "workspace" / "deep_research_reports" / str(captured["run_id"]) / "report.md").exists()
    reset_leaf_presets()


async def test_completed_run_delivers_report_artifact_to_parent_session(tmp_path, monkeypatch):
    """A-1: a DR run with parent_session_id must surface the published report
    to the parent chat session as a row-free ``artifact_delivery`` event
    pointing at the workspace report path — otherwise a chat-initiated DR
    leaves the user with run/step status but no clickable report."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.config import get_settings
    from app.services.deep_research import workflow_definition as wf_def
    from app.services.deep_research.leaf_presets import run_artifact_dir
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.workflow_leaf_presets import reset_leaf_presets

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "agents"))
    reset_leaf_presets()

    agent_id = uuid.uuid4()
    tenant = uuid.uuid4()
    parent_session_id = uuid.uuid4()

    async def fake_resolve(aid, **kwargs):
        return SimpleNamespace(id=aid, tenant_id=tenant, name="A", role_description=""), SimpleNamespace()

    async def fake_ensure(**kwargs):
        return SimpleNamespace(
            name="deep_research.v1",
            definition_version=1,
            definition_hash="hash-1",
            definition_json={"name": "deep_research.v1"},
        )

    async def fake_launch(**kwargs):
        run_id = kwargs["run_id"]
        root = run_artifact_dir(agent_id, run_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_text("# RWA report\n", encoding="utf-8")
        (root / "final.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(run_id=run_id, outcome=SimpleNamespace(status="completed", reason=None))

    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid.uuid4())

    @asynccontextmanager
    async def fake_session(*args, **kwargs):
        yield SimpleNamespace(commit=_noop_commit)

    monkeypatch.setattr(wf_def, "resolve_agent_runtime", fake_resolve)
    monkeypatch.setattr(wf_def, "ensure_deep_research_workflow_definition", fake_ensure)
    monkeypatch.setattr(wf_def, "start_ephemeral_workflow_for_agent", fake_launch)
    monkeypatch.setattr(wf_def, "tenant_scoped_session", fake_session)
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = await wf_def.start_deep_research_workflow_run(
        request=ResearchRequest.from_arguments({"question": "RWA market", "output_language": "en"}),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        workspace=workspace,
        parent_session_id=parent_session_id,
    )

    assert payload["status"] == "completed"
    assert "append" in captured, "completed DR with parent session must emit an artifact_delivery event"
    event = captured["append"]
    assert event["event_type"] == "artifact_delivery"
    assert event["materialize_chat_message"] is False
    assert str(event["session_id"]) == str(parent_session_id)
    parts = event["parts"]
    assert parts and parts[0]["type"] == "artifact"
    report_paths = [part["path"] for part in parts]
    assert "workspace/deep_research_reports/{}/report.md".format(payload["created_workflow_run_id"]) in report_paths
    assert all(part["source"] == "deep_research" for part in parts)
    reset_leaf_presets()


async def test_completed_run_without_parent_session_emits_no_artifact_event(tmp_path, monkeypatch):
    """A-1 guard: trigger/heartbeat DR (no parent_session_id) must not attempt
    an artifact_delivery event — there is no chat timeline to deliver to."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.config import get_settings
    from app.services.deep_research import workflow_definition as wf_def
    from app.services.deep_research.leaf_presets import run_artifact_dir
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.workflow_leaf_presets import reset_leaf_presets

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "agents"))
    reset_leaf_presets()

    agent_id = uuid.uuid4()
    tenant = uuid.uuid4()

    async def fake_resolve(aid, **kwargs):
        return SimpleNamespace(id=aid, tenant_id=tenant, name="A", role_description=""), SimpleNamespace()

    async def fake_ensure(**kwargs):
        return SimpleNamespace(
            name="deep_research.v1", definition_version=1, definition_hash="h", definition_json={"name": "deep_research.v1"}
        )

    async def fake_launch(**kwargs):
        run_id = kwargs["run_id"]
        root = run_artifact_dir(agent_id, run_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_text("# r\n", encoding="utf-8")
        (root / "final.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(run_id=run_id, outcome=SimpleNamespace(status="completed", reason=None))

    calls: list = []

    @asynccontextmanager
    async def fake_session(*args, **kwargs):
        calls.append(args)
        yield SimpleNamespace(commit=_noop_commit)

    monkeypatch.setattr(wf_def, "resolve_agent_runtime", fake_resolve)
    monkeypatch.setattr(wf_def, "ensure_deep_research_workflow_definition", fake_ensure)
    monkeypatch.setattr(wf_def, "start_ephemeral_workflow_for_agent", fake_launch)
    monkeypatch.setattr(wf_def, "tenant_scoped_session", fake_session)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    await wf_def.start_deep_research_workflow_run(
        request=ResearchRequest.from_arguments({"question": "RWA market"}),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        workspace=workspace,
        parent_session_id=None,
    )
    assert calls == [], "no parent session → no artifact_delivery session must be opened"
    reset_leaf_presets()


async def _noop_commit():
    return None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_runtime_start_run_accepts_injected_run_id(pg_tenant_id, owner_sessionmaker):
    """DR-4 plumbing: the caller may pre-generate the run id so artifacts can
    land under it before execution starts."""
    from app.runtime.workflow_engine import LeafOutcome
    from app.services.workflow_runtime_service import WorkflowRuntimeService

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    injected = uuid.uuid4()

    async def leaf(request):
        return LeafOutcome(ok=True, output={"ok": True}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=pg_tenant_id,
        definition_data={
            "name": "runid-probe",
            "args_schema": {},
            "steps": [{"id": "one", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Do"}],
        },
        args={},
        leaf_executor=leaf,
        run_id=injected,
    )
    assert handle.run_id == injected
    assert handle.outcome.status == "completed"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_full_deep_research_workflow_chain_on_real_engine(
    pg_tenant_id, owner_sessionmaker, tmp_path, monkeypatch
):
    """DR-5 前置：除真 LLM 外全真 — 真 PG journal、真引擎（fanout/retry/
    journal）、真 presets（pre/post 全跑）、fake spawn 喂真实形状语料。
    plan→explore(×2)→critic→synthesize 四步全链跑通且 report.md 完整落盘。"""
    import json as _json
    from types import SimpleNamespace

    from app.agents.subagent import SubagentHandle, SubagentResult, SubagentSpawnContext
    from app.config import get_settings
    from app.services.deep_research.leaf_presets import (
        register_deep_research_leaf_presets,
        run_artifact_dir,
    )
    from app.services.deep_research.schemas import to_jsonable
    from app.services.deep_research.workflow_definition import build_deep_research_workflow_definition
    from app.services.workflow_launch import build_subagent_leaf_executor
    from app.services.workflow_leaf_presets import reset_leaf_presets
    from app.services.workflow_runtime_service import WorkflowRuntimeService

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    reset_leaf_presets()
    register_deep_research_leaf_presets()

    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    root = run_artifact_dir(agent_id, run_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "request.json").write_text(
        _json.dumps(to_jsonable({"question": "RWA market structure", "output_language": "en", "depth": "quick"})),
        encoding="utf-8",
    )

    page = (
        "Title: Tokenised Treasuries 2026\n\nTokenised treasury products grew to $4.2B by May 2026 per the "
        "official registry; issuers reached 38 across five jurisdictions and BlackRock BUIDL holds $1.7B."
    )
    synthesized = """# RWA Market Structure Deep Research

## Executive Thesis

Tokenised treasuries are the anchor of RWA adoption: cumulative volume reached $4.2B by May 2026 with 38 issuers
across five jurisdictions, and BlackRock BUIDL alone grew from $250M to $1.7B within 2026. Evidence from SRC0 and
SRC1 grounds the market-size view, while regulatory posture (SEC, MAS) decides scale beyond the current base.

## Key Findings

1. Treasury products dominate volume: $4.2B cumulative, 38 issuers, five jurisdictions. Sources: SRC0.
2. Institutional wrappers win distribution — BUIDL's $250M→$1.7B trajectory in 2026 shows demand concentrates in
   regulated fund shells offered by BlackRock, Securitize, and Franklin Templeton. Sources: SRC1.
3. Regulatory clarity (SEC no-action posture, MAS sandbox graduations) is the binding constraint on the next 12
   months of issuance growth across the 17 candidate venues. Sources: SRC0, SRC1.

## Strategic Implications

- Prioritise regulated fund wrappers over bespoke SPV tokens; that is where the $1.7B of demand sits.
- Track SEC and MAS posture quarterly; venue count (17) only converts to volume under clear transfer rules.

## Contradictions And Gaps

- Issuer-count growth (38) outpaces audited volume disclosure; third-party attestation coverage remains thin.

## Source Ledger

- `SRC0` — tier1
- `SRC1` — tier1
"""

    async def spawn(ctx: SubagentSpawnContext, spec, task, *, budget=None):
        if spec.name == "deep_research_explorer":
            url = "https://a.gov/treasuries" if "market size" in task else "https://b.gov/funds"
            result = SubagentResult(
                name=spec.name,
                type=spec.type,
                status="completed",
                content=f"## Findings\n- grounded numbers from {url}",
                tokens_used=300,
                sources=[{"url": url, "tool_name": "web_fetch", "content": page}],
            )
        elif spec.name == "deep_research_critic":
            assert spec.disable_tools is True  # RC11 on the real spawn path
            result = SubagentResult(
                name=spec.name,
                type=spec.type,
                status="completed",
                content=_json.dumps({"strongest_counter_argument": "volume concentration in one issuer"}),
                tokens_used=80,
            )
        elif spec.name == "deep_research_synthesizer":
            assert spec.disable_tools is True
            assert "COVERAGE IS MANDATORY" in task  # pre_process rebuilt the real instruction
            merged = [
                _json.loads(line)
                for line in (root / "sources.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            ids = [row["source_id"] for row in merged]
            result = SubagentResult(
                name=spec.name,
                type=spec.type,
                status="completed",
                content=synthesized.replace("SRC0", ids[0]).replace("SRC1", ids[-1]),
                tokens_used=500,
            )
        else:  # planner
            assert spec.disable_tools is True
            result = SubagentResult(
                name=spec.name,
                type=spec.type,
                status="completed",
                content="Strategy: prioritise registries and regulator publications.",
                tokens_used=40,
            )
        return SubagentHandle(name=spec.name, trace_id="tr", depth=1, result=result)

    ctx = SubagentSpawnContext(
        parent_agent_id=agent_id,
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
        tenant_id=pg_tenant_id,
    )
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    handle = await service.start_run(
        tenant_id=pg_tenant_id,
        definition_data=build_deep_research_workflow_definition(),
        args=deep_research_workflow_args(
            ResearchRequest.from_arguments(
                {
                    "question": "RWA market structure",
                    "worker_topics": ["market size", "fund wrappers"],
                    "output_language": "en",
                    "depth": "quick",
                }
            )
        ),
        leaf_executor=build_subagent_leaf_executor(ctx, spawn=spawn),
        run_id=run_id,
        agent_id=agent_id,
    )

    assert handle.outcome.status == "completed", handle.outcome.reason
    loaded = await service.load_run(handle.run_id, tenant_id=pg_tenant_id)
    assert {s.step_id: s.status for s in loaded.steps} == {
        "plan": "done",
        "explore": "done",
        "critic": "done",
        "synthesize": "done",
    }
    report = (root / "report.md").read_text(encoding="utf-8")
    assert "## Footnotes" in report and "[^1]" in report
    assert (root / "final.json").exists()
    assert (root / "devils_advocate.jsonl").exists()
    merged_rows = (root / "sources.jsonl").read_text(encoding="utf-8").splitlines()
    assert len([line for line in merged_rows if line.strip()]) == 2  # both explorer shards merged
    reset_leaf_presets()
