"""Integration tests for :class:`PlanModeService` (imperative shell).

The shell is tested with the project's established hand-rolled async-session
fakes (see e.g. ``tests/services/test_runtime_task_service.py`` and
``test_objective_service.py``). There is no DB engine / Testcontainers harness
in this repo's test suite, so we follow the existing pattern rather than
introducing a new one. The DB-touching surface here is thin CRUD; all the
load-bearing logic lives in the pure ``plan_mode_core`` (separately unit
tested with no fakes at all).

Markdown artifacts are written to a real ``tmp_path`` (filesystem is cheap and
real), so the file side of the shell is exercised for real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# Fake async session mirroring the SQLAlchemy AsyncSession surface we use.
#
# Path-unification cut ④: the isolated RPC planner (DefaultAgentPlanPlanner) was
# removed. ``generate_plan`` now lands a caller-supplied structured ``fill`` as
# the plan_json directly (the agent authors it in main-loop Plan Mode and submits
# via exit_plan_mode), so there is no planner fake any more — tests pass the fill
# straight into ``generate_plan``.
# ---------------------------------------------------------------------------


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _PlanSession:
    """In-memory stand-in for an async session over agent_plan_requests.

    Indexes added rows by ``id`` and answers ``select`` queries by inspecting
    the compiled statement's WHERE criteria via a tiny matcher the service
    provides. To stay decoupled from SQLAlchemy internals, the service is
    expected to fetch by primary key (``get_by_id``) and to list by agent;
    this fake supports exactly those two access shapes used by the service.
    """

    def __init__(self):
        self.rows: list[object] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self.refresh_calls = 0
        # The service sets these hints right before calling execute().
        self._next_lookup_id = None
        self._next_lookup_agent_id = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid4()
        # default timestamps the way the DB server_default would
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)
        value.updated_at = datetime.now(timezone.utc)
        self.rows.append(value)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def refresh(self, _value):
        self.refresh_calls += 1

    # The service queries by exposing the lookup criteria through attributes
    # on the statement object we hand it. We model that by storing the most
    # recent filter the service requested on the fake itself.
    async def execute(self, stmt):
        plan_id = getattr(stmt, "_plan_lookup_id", None)
        agent_id = getattr(stmt, "_plan_lookup_agent_id", None)
        lease_key = getattr(stmt, "_plan_lease_lookup_key", None)
        if lease_key is not None:
            match = next(
                (row for row in self.rows if getattr(row, "execution_idempotency_key", None) == lease_key),
                None,
            )
            return _ScalarOneResult(match)
        if plan_id is not None:
            match = next((r for r in self.rows if str(r.id) == str(plan_id)), None)
            return _ScalarOneResult(match)
        if agent_id is not None:
            matches = [r for r in self.rows if str(r.agent_id) == str(agent_id)]
            return _ScalarsResult(matches)
        return _ScalarsResult(list(self.rows))


def _patch_plan_session(monkeypatch, mod, session) -> None:
    """Route the service's GUC-scoped session + tenant resolvers to the fake.

    RLS stage-2a moved ``PlanModeService`` off the bare ``async_session`` onto
    ``tenant_scoped_session`` plus ``resolve_tenant_for_{plan,agent}`` (the
    audited single-row breakers). The fake ``_PlanSession`` already behaves as an
    async context manager and ignores the harmless GUC ``SET LOCAL`` statements,
    so we hand it back for any tenant id and stub the resolvers to a constant —
    the fake keys rows by id/agent_id, not by tenant.
    """
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *a, **k: session)
    _const_tenant = uuid4()

    async def _fake_resolve(*_a, **_k):
        return _const_tenant

    monkeypatch.setattr(mod, "resolve_tenant_for_plan", _fake_resolve)
    monkeypatch.setattr(mod, "resolve_tenant_for_agent", _fake_resolve)


@pytest.fixture()
def patched_service(monkeypatch, tmp_path):
    """Return (service, session, tmp_path, None) with async_session + AGENT_DATA_DIR
    patched. The 4th tuple slot is a vestigial ``None`` (it used to be the RPC
    planner fake, removed in cut ④); kept so existing ``_planner`` unpacks stay
    valid without churn — no test reads it any more."""
    from app.services import plan_mode_service as mod

    session = _PlanSession()
    _patch_plan_session(monkeypatch, mod, session)
    monkeypatch.setattr(mod, "_agent_data_dir", lambda: tmp_path)

    service = mod.PlanModeService()
    return service, session, tmp_path, None


# ---------------------------------------------------------------------------
# create_plan_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_plan_request_persists_draft(patched_service):
    service, session, _, _planner = patched_service
    agent_id = uuid4()
    user_id = uuid4()

    plan = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=user_id,
        original_request="每天 9 点帮我整理新闻",
        intent_type="autonomous_wake",
        source="web_chat",
        session_id="sess-1",
    )

    assert plan.status == "draft"
    assert plan.agent_id == agent_id
    assert plan.requested_by_user_id == user_id
    assert plan.intent_type == "autonomous_wake"
    assert plan.plan_version == 1
    assert plan.source == "web_chat"
    assert session.commit_calls == 1
    assert len(session.rows) == 1


@pytest.mark.asyncio
async def test_create_plan_request_rejects_unknown_intent(patched_service):
    service, session, _, _planner = patched_service
    with pytest.raises(ValueError, match="intent_type"):
        await service.create_plan_request(
            agent_id=uuid4(),
            requested_by_user_id=uuid4(),
            original_request="x",
            intent_type="bogus",
        )
    assert session.rows == []


# ---------------------------------------------------------------------------
# generate_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_plan_produces_awaiting_confirmation_with_hash_and_markdown(patched_service):
    service, session, data_dir, _ = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="每天 9 点帮我整理新闻",
        intent_type="autonomous_wake",
    )

    # Cut ④: generate_plan lands the caller-supplied fill (the agent authored it
    # in main-loop Plan Mode) directly as plan_json — no RPC planner.
    updated = await service.generate_plan(
        plan_id=draft.id,
        fill={
            "objective": "Produce a useful daily industry brief.",
            "steps": [{"order": 1, "description": "Collect sources", "expected_output": "list"}],
            "success_criteria": ["Brief includes 5-10 updates with links."],
            "stop_conditions": ["User cancels."],
            "wake_policy": {"type": "cron", "timezone": "Asia/Shanghai", "expr": "0 9 * * 1-5"},
            "required_capabilities": ["web_search"],
        },
    )

    assert updated.status == "awaiting_confirmation"
    assert updated.plan_hash and updated.plan_hash.startswith("sha256:")
    assert updated.plan_json["objective"] == "Produce a useful daily industry brief."
    assert updated.plan_json["wake_policy"]["expr"] == "0 9 * * 1-5"
    # Structured-fill landing provenance (single path post-cut ④): the plan is
    # authored by the agent in main-loop Plan Mode — NOT the removed RPC "workflow"
    # planner. The stale "workflow" mislabel was the canonical provenance bug.
    assert updated.metadata_json["author_type"] == "agent"
    assert updated.metadata_json["planner_prompt_version"] == "structured_fill.v1"
    assert updated.plan_json["required_capabilities"] == ["Web 来源核验"]
    # No RPC planner ran → no planner work-ledger artifact is created.
    assert "planner_work_ledger" not in updated.metadata_json
    assert not (data_dir / str(agent_id) / "plans" / f"{updated.id}.work_ledger.json").exists()

    # Markdown artifact actually written to disk at the documented path.
    md_path = data_dir / str(agent_id) / "plans" / f"{updated.id}.md"
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert f"plan_hash: {updated.plan_hash}" in md
    assert "status: awaiting_confirmation" in md
    assert updated.plan_markdown_path == str(md_path)

    # Hash must match the canonical hash of the stored plan_json.
    from app.services.plan_mode_core import compute_plan_hash

    assert updated.plan_hash == compute_plan_hash(updated.plan_json)


@pytest.mark.asyncio
async def test_generate_plan_marks_planning_failed_on_invalid_fill(patched_service):
    service, session, data_dir, _ = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="x",
        intent_type="in_session_execution",
    )

    # objective left blank + malformed step -> schema invalid
    result = await service.generate_plan(
        plan_id=draft.id,
        fill={"steps": [{"description": "no order"}]},
    )

    assert result.status == "planning_failed"
    assert result.plan_hash is None
    # No markdown written for a failed plan.
    assert not (data_dir / str(agent_id) / "plans" / f"{result.id}.md").exists()
    assert result.metadata_json and result.metadata_json.get("planning_errors")


@pytest.mark.asyncio
async def test_generate_plan_repairs_common_agent_output_shape_before_validation(monkeypatch, tmp_path):
    from app.services import plan_mode_service as mod

    session = _PlanSession()
    _patch_plan_session(monkeypatch, mod, session)
    monkeypatch.setattr(mod, "_agent_data_dir", lambda: tmp_path)
    service = mod.PlanModeService()
    agent_id = uuid4()
    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="做一个web3的全景报告",
        intent_type="in_session_execution",
    )

    # A fill with bare steps (no order) + a non-canonical risk level still
    # normalises through _apply_generation before validation.
    result = await service.generate_plan(
        plan_id=draft.id,
        fill={
            "title": "Web3 panorama report",
            "objective": "Produce a structured Web3 panorama report.",
            "motivation": "The user asked for a source-grounded report before execution.",
            "steps": [
                {"description": "Frame scope and constraints."},
                {"description": "Map major Web3 narratives."},
                {"description": "Synthesize findings into a report."},
            ],
            "success_criteria": ["The user sees a complete plan before execution."],
            "stop_conditions": ["The user rejects the plan."],
            "risk_assessment": {"level": "moderate", "reasons": []},
            "required_capabilities": ["Web research"],
        },
    )

    assert result.status == "awaiting_confirmation"
    assert result.plan_hash and result.plan_hash.startswith("sha256:")
    assert [step["order"] for step in result.plan_json["steps"]] == [1, 2, 3]
    assert result.plan_json["risk_assessment"]["level"] == "medium"
    assert result.metadata_json["quality_checks"]["schema_valid"] is True


@pytest.mark.asyncio
async def test_generate_plan_unknown_plan_raises(patched_service):
    service, _, _, _planner = patched_service
    with pytest.raises(LookupError):
        await service.generate_plan(plan_id=uuid4(), fill={})


# ---------------------------------------------------------------------------
# revise_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revise_plan_supersedes_old_and_bumps_version(patched_service):
    service, session, _, _planner = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="x",
        intent_type="in_session_execution",
    )
    v1 = await service.generate_plan(
        plan_id=draft.id,
        fill={
            "objective": "obj1",
            "steps": [{"order": 1, "description": "d"}],
            "success_criteria": ["c"],
            "stop_conditions": ["s"],
        },
    )
    v1_hash = v1.plan_hash

    v2 = await service.revise_plan(
        plan_id=draft.id,
        fill={
            "objective": "obj2 revised",
            "steps": [{"order": 1, "description": "d2"}],
            "success_criteria": ["c2"],
            "stop_conditions": ["s2"],
        },
    )

    # Old row superseded, pointer set.
    assert v1.status == "superseded"
    assert v1.superseded_by_plan_id == v2.id
    # New row is a fresh PlanRequest at version 2, back in planning lineage.
    assert v2.id != v1.id
    assert v2.plan_version == 2
    assert v2.status == "awaiting_confirmation"
    assert v2.plan_hash != v1_hash
    assert v2.plan_json["objective"] == "obj2 revised"


@pytest.mark.asyncio
async def test_supersede_to_draft_creates_fresh_draft_without_generating(patched_service):
    """Cut ③/④: the launcher path needs a superseded draft WITHOUT plan_json so the
    agent can author it via exit_plan_mode. supersede_to_draft must NOT land any
    plan_json itself — it only forks the ledger row."""
    service, session, _, _ = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="原始请求",
        intent_type="in_session_execution",
    )
    v1 = await service.generate_plan(
        plan_id=draft.id,
        fill={
            "objective": "obj1",
            "steps": [{"order": 1, "description": "d"}],
            "success_criteria": ["c"],
            "stop_conditions": ["s"],
        },
    )

    new_draft = await service.supersede_to_draft(plan_id=v1.id)

    # Old row superseded + pointer set; new row is a bare draft at the next version.
    assert v1.status == "superseded"
    assert v1.superseded_by_plan_id == new_draft.id
    assert new_draft.id != v1.id
    assert new_draft.status == "draft"
    assert new_draft.plan_version == 2
    assert new_draft.original_request == "原始请求"
    assert new_draft.metadata_json["revised_from_plan_id"] == str(v1.id)
    # No plan_json authored by supersede itself — only carries the prior title.
    assert "objective" not in (new_draft.plan_json or {})
    assert "steps" not in (new_draft.plan_json or {})


@pytest.mark.asyncio
async def test_supersede_to_draft_unknown_plan_raises(patched_service):
    service, _, _, _planner = patched_service
    with pytest.raises(LookupError):
        await service.supersede_to_draft(plan_id=uuid4())


# ---------------------------------------------------------------------------
# confirm_plan
# ---------------------------------------------------------------------------


async def _make_awaiting(service, *, agent_id=None, requester=None, session_id=None):
    agent_id = agent_id or uuid4()
    requester = requester or uuid4()
    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=requester,
        original_request="每天 9 点帮我整理新闻",
        intent_type="autonomous_wake",
        session_id=session_id,
    )
    plan = await service.generate_plan(
        plan_id=draft.id,
        fill={
            "objective": "obj",
            "steps": [{"order": 1, "description": "d"}],
            "success_criteria": ["c"],
            "stop_conditions": ["s"],
            "wake_policy": {"type": "cron", "timezone": "Asia/Shanghai", "expr": "0 9 * * *"},
        },
    )
    return plan, requester


@pytest.mark.asyncio
async def test_confirm_plan_success_sets_confirmed(patched_service):
    service, session, data_dir, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    confirmer = requester

    result = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=confirmer,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
        reason="Looks good",
    )

    assert result.status == "confirmed"
    assert result.confirmed_by_user_id == confirmer
    assert result.confirmed_at is not None
    assert result.handoff_status == "not_started"

    from app.models.audit import ApprovalRequest

    tickets = [row for row in session.rows if isinstance(row, ApprovalRequest)]
    assert len(tickets) == 1
    assert tickets[0].action_type == "plan_authorization"
    assert tickets[0].requested_by == requester
    assert tickets[0].resolved_by == confirmer
    assert tickets[0].consumed_at is None

    # Markdown re-rendered with confirmation reflected in frontmatter.
    md = (data_dir / str(plan.agent_id) / "plans" / f"{plan.id}.md").read_text(encoding="utf-8")
    assert "status: confirmed" in md
    assert f"confirmed_by: {confirmer}" in md


@pytest.mark.asyncio
async def test_confirm_plan_allows_requesting_user_to_confirm(patched_service):
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)

    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_by_user_id == requester


@pytest.mark.asyncio
async def test_confirm_plan_version_mismatch_conflicts(patched_service):
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError) as exc:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=requester,
            plan_version=plan.plan_version + 1,
            plan_hash=plan.plan_hash,
        )
    assert exc.value.error_code == "version_mismatch"
    assert plan.status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_confirm_plan_ignores_legacy_client_hash_and_uses_server_plan(patched_service):
    service, _session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)

    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash="sha256:tampered",
    )

    assert confirmed.status == "confirmed"
    assert confirmed.plan_hash == plan.plan_hash


@pytest.mark.asyncio
async def test_confirmed_plan_cannot_be_reconfirmed(patched_service):
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError) as exc:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=requester,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
        )
    assert exc.value.error_code == "not_confirmable"


# ---------------------------------------------------------------------------
# reject_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_plan_sets_rejected(patched_service):
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    rejecter = uuid4()
    refresh_calls_before_reject = session.refresh_calls

    result = await service.reject_plan(
        plan_id=plan.id,
        rejecting_user_id=rejecter,
        reason="not needed",
    )

    assert result.status == "rejected"
    assert result.rejected_by_user_id == rejecter
    assert result.rejected_at is not None
    assert session.refresh_calls == refresh_calls_before_reject + 1


@pytest.mark.asyncio
async def test_reject_after_confirm_is_blocked(patched_service):
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError):
        await service.reject_plan(plan_id=plan.id, rejecting_user_id=uuid4(), reason="x")


# ---------------------------------------------------------------------------
# handoff_confirmed_plan  (Phase 4 lands the concrete creates; Phase 1 wires
# the status mechanics + interface only)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ensure_awaiting_plan_from_fill — the single ledger entry for caller/agent-
# authored structured fills (specialized tools, exit_plan_mode). The RPC intercept-
# then-create entry (ensure_awaiting_plan) was removed in path-unification cut ④.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_awaiting_plan_from_fill_creates_and_dedupes_custom_long_task_plan(patched_service):
    service, session, _, _ = patched_service
    agent_id = uuid4()
    signature = "workflow-report-signature"
    fill = {
        "title": "Workflow Report: RWA launchpads",
        "objective": "Run source-ledger-backed research on RWA launchpads.",
        "motivation": "The user requested a formal source-grounded report.",
        "steps": [
            {
                "order": 1,
                "description": "Confirm scope, evidence threshold, output language, and delivery format.",
                "expected_output": "User-approved research plan.",
            },
            {
                "order": 2,
                "description": "Collect and verify sources by lane, then synthesize the final report.",
                "expected_output": "report.md plus optional derived delivery artifact.",
            },
        ],
        "success_criteria": ["Every material claim is source-bound."],
        "wake_policy": {"type": "none"},
        "required_capabilities": ["Web research", "Office artifact generation"],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": ["Long-running research task"]},
        "estimated_cost": {"tokens_per_run": "high", "expected_duration": "several minutes"},
        "stop_conditions": ["User rejects the plan."],
        "handoff": {
            "target": "continue_current_session",
            "create_objective": False,
            "create_trigger": False,
            "payload": {
                "question": "Research RWA launchpads",
                "depth": "full",
                "output_format": "docx",
                "plan_confirmed": True,
                "worker_topics": ["official evidence"],
            },
        },
    }

    first = await service.ensure_awaiting_plan_from_fill(
        agent_id=agent_id,
        intent_type="in_session_execution",
        signature=signature,
        fill=fill,
        original_request="Research RWA launchpads",
        source="tool_runtime",
        metadata_json={"interactive_plan_mode": True},
    )
    rows_after_first = len(session.rows)
    second = await service.ensure_awaiting_plan_from_fill(
        agent_id=agent_id,
        intent_type="in_session_execution",
        signature=signature,
        fill={**fill, "title": "Should not create duplicate"},
        original_request="Research RWA launchpads",
        source="tool_runtime",
        metadata_json={"interactive_plan_mode": True},
    )

    assert first.status == "awaiting_confirmation"
    assert first.intent_type == "in_session_execution"
    assert first.plan_json["handoff"]["target"] == "continue_current_session"
    assert first.metadata_json["intercept_signature"] == signature
    assert first.metadata_json["interactive_plan_mode"] is True
    # Agent-authored via main-loop Plan Mode — not the removed RPC "workflow" planner.
    assert first.metadata_json["author_type"] == "agent"
    assert first.metadata_json["planner_prompt_version"] == "structured_fill.v1"
    assert str(second.id) == str(first.id)
    assert len(session.rows) == rows_after_first


@pytest.mark.asyncio
async def test_ensure_awaiting_plan_from_fill_rejects_internal_tool_script_plan(monkeypatch, tmp_path):
    from app.services import plan_mode_service as mod

    session = _PlanSession()
    _patch_plan_session(monkeypatch, mod, session)
    monkeypatch.setattr(mod, "_agent_data_dir", lambda: tmp_path)
    service = mod.PlanModeService()
    fill = {
        "title": "Bad internal script",
        "objective": "Call load_skill and start_workflow with plan_confirmed=false.",
        "motivation": "The user requested a source-grounded report before execution.",
        "steps": [
            {
                "order": 1,
                "description": "调用 load_skill('web-research')。",
                "expected_output": "A user-approved research plan.",
            },
            {
                "order": 2,
                "description": "Run approved evidence lanes and synthesize the final report.",
                "expected_output": "runtime_artifacts/long_tasks/task/work_ledger.json",
            },
        ],
        "success_criteria": ["The final report is source-grounded and written in Simplified Chinese."],
        "wake_policy": {"type": "none"},
        "required_capabilities": ["Web research", "source-ledger web research"],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": ["Long-running research workflow."]},
        "estimated_cost": {"tokens_per_run": "high", "expected_duration": "about 8-15 minutes"},
        "stop_conditions": ["The user rejects the plan."],
        "handoff": {
            "target": "continue_current_session",
            "create_objective": False,
            "create_trigger": False,
            "payload": {
                "question": "Web3 full landscape",
                "depth": "full",
                "output_format": "markdown",
                "plan_confirmed": True,
                "worker_topics": ["official evidence"],
            },
        },
    }

    plan = await service.ensure_awaiting_plan_from_fill(
        agent_id=uuid4(),
        intent_type="in_session_execution",
        signature="workflow-report:web3",
        fill=fill,
        original_request="做一个web3的全景报告",
        source="web_chat",
        metadata_json={"interactive_plan_mode": True},
    )

    assert plan.status == "planning_failed"
    assert plan.plan_hash is None
    errors = "\n".join(plan.metadata_json["planning_errors"])
    assert "load_skill" in errors
    assert "plan_confirmed" in errors


@pytest.mark.asyncio
async def test_ensure_awaiting_plan_from_fill_allows_hidden_execution_contract(monkeypatch, tmp_path):
    from app.services import plan_mode_service as mod

    session = _PlanSession()
    _patch_plan_session(monkeypatch, mod, session)
    monkeypatch.setattr(mod, "_agent_data_dir", lambda: tmp_path)
    service = mod.PlanModeService()
    contract = {
        "type": "workflow",
        "workflow_ref": "custom_research.v1",
        "args": {
            "question": "Web3 full landscape",
            "internal_tool_note": "start_workflow writes runtime_artifacts/workflow_runs/run/work_ledger.json",
        },
    }
    fill = {
        "title": "Web3 全景研究计划",
        "objective": "生成一份有来源支撑的中文 Web3 全景研究报告。",
        "motivation": "用户需要先确认研究范围、证据标准和交付物。",
        "steps": [
            {
                "order": 1,
                "description": "确认研究范围、覆盖赛道、证据标准和交付格式。",
                "expected_output": "用户确认后的研究范围。",
            },
            {
                "order": 2,
                "description": "按赛道收集来源、交叉核验，并综合成报告。",
                "expected_output": "带引用的中文研究报告。",
            },
        ],
        "success_criteria": ["关键判断均有来源支撑。"],
        "wake_policy": {"type": "none"},
        "required_capabilities": ["Web research", "source verification"],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": ["长任务且需要多来源核验。"]},
        "estimated_cost": {"tokens_per_run": "high", "expected_duration": "about 15-30 minutes"},
        "stop_conditions": ["用户拒绝计划。"],
        "handoff": {"target": "continue_current_session", "create_objective": False, "create_trigger": False},
        "execution_contract": contract,
    }

    plan = await service.ensure_awaiting_plan_from_fill(
        agent_id=uuid4(),
        intent_type="in_session_execution",
        signature="workflow-report:web3:hidden-contract",
        fill=fill,
        original_request="做一个web3的全景报告",
        source="web_chat",
        metadata_json={"interactive_plan_mode": True},
    )

    assert plan.status == "awaiting_confirmation"
    assert plan.plan_json["handoff"]["target"] == "continue_current_session"
    assert plan.plan_json["execution_contract"] == contract
    assert "planning_errors" not in (plan.metadata_json or {})


def test_get_plan_mode_service_returns_shared_singleton():
    from app.services.plan_mode_service import get_plan_mode_service

    assert get_plan_mode_service() is get_plan_mode_service()


@pytest.mark.asyncio
async def test_handoff_requires_confirmed_status(patched_service):
    service, session, _, _planner = patched_service
    plan, _ = await _make_awaiting(service)  # still awaiting_confirmation

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError) as exc:
        await service.handoff_confirmed_plan(plan_id=plan.id)
    assert exc.value.error_code == "not_confirmed"


@pytest.mark.asyncio
async def test_handoff_marks_skipped_when_no_handler_registered(patched_service):
    """Phase 1 has no concrete handoff target wired; the contract is that the
    status stays ``confirmed`` and ``handoff_status`` records the outcome
    (§13) instead of raising or silently succeeding."""
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    result = await service.handoff_confirmed_plan(plan_id=confirmed.id)

    # status must remain confirmed (§7/§13).
    assert result.status == "confirmed"
    assert result.handoff_status == "skipped"
    assert result.handoff_payload is not None
    assert result.handoff_payload.get("reason") == "no_handler_registered"


@pytest.mark.asyncio
async def test_handoff_is_idempotent_after_completion(patched_service):
    """A completed handoff is not re-run (idempotency, §13)."""
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    calls = {"n": 0}

    def handler(db, plan_row):
        assert db is session
        calls["n"] += 1
        return {"created_objective_id": str(uuid4()), "created_trigger_id": str(uuid4())}

    service.register_handoff_handler("scheduled_trigger", handler)

    first = await service.handoff_confirmed_plan(plan_id=confirmed.id)
    assert first.handoff_status == "completed"
    assert calls["n"] == 1
    assert first.handoff_payload["created_objective_id"]

    second = await service.handoff_confirmed_plan(plan_id=confirmed.id)
    assert second.handoff_status == "completed"
    assert calls["n"] == 1  # not re-invoked


@pytest.mark.asyncio
async def test_handoff_records_failure_without_corrupting_confirmed(patched_service):
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    def boom(db, plan_row):
        assert db is session
        raise RuntimeError("downstream create failed")

    service.register_handoff_handler("scheduled_trigger", boom)

    result = await service.handoff_confirmed_plan(plan_id=confirmed.id)

    assert result.status == "confirmed"  # never flips to a failed *status*
    assert result.handoff_status == "failed"
    assert "downstream create failed" in (result.handoff_payload or {}).get("error", "")


@pytest.mark.asyncio
async def test_confirm_and_handoff_plan_confirms_then_runs_handoff(patched_service):
    """Web confirm must not leave a client-side split-brain gap between
    ``confirmed`` and execution handoff. The backend service provides one
    confirm-and-start operation; the legacy confirm/handoff methods remain for
    non-web callers that intentionally need the split."""
    service, session, _, _planner = patched_service
    plan, requester = await _make_awaiting(service)
    confirmer = uuid4()
    calls = {"handoff": 0}

    def handler(db, plan_row):
        assert db is session
        assert plan_row.status == "confirmed"
        calls["handoff"] += 1
        return {"runtime_task_id": "run-123", "execution": "current_session"}

    service.register_handoff_handler("scheduled_trigger", handler)

    result = await service.confirm_and_handoff_plan(
        plan_id=plan.id,
        confirming_user_id=confirmer,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
        reason="Looks good",
        authorization_source="delegated_approver",
    )

    assert result.status == "confirmed"
    assert result.confirmed_by_user_id == confirmer
    assert result.handoff_status == "completed"
    assert result.handoff_payload["runtime_task_id"] == "run-123"
    assert calls["handoff"] == 1


# ---------------------------------------------------------------------------
# read helpers (API support)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_plan_returns_none_for_unknown(patched_service):
    service, _, _, _planner = patched_service
    assert await service.get_plan(uuid4()) is None


@pytest.mark.asyncio
async def test_find_latest_awaiting_plan_for_session_filters_status_and_session(patched_service):
    service, session, _, _planner = patched_service
    agent_id = uuid4()

    older, _ = await _make_awaiting(service, agent_id=agent_id, session_id="wechat-session")
    other_session, _ = await _make_awaiting(service, agent_id=agent_id, session_id="other-session")
    latest, _ = await _make_awaiting(service, agent_id=agent_id, session_id="wechat-session")
    older.created_at = older.created_at.replace(year=2025)
    other_session.created_at = other_session.created_at.replace(year=2027)
    latest.created_at = latest.created_at.replace(year=2026)
    older.status = "confirmed"

    found = await service.find_latest_awaiting_plan_for_session(agent_id=agent_id, session_id="wechat-session")

    assert found is not None
    assert found.id == latest.id
    assert found.status == "awaiting_confirmation"
    assert found.session_id == "wechat-session"
    assert len(session.rows) >= 3


@pytest.mark.asyncio
async def test_list_plans_for_agent_filters_by_agent(patched_service):
    service, _, _, _planner = patched_service
    agent_a = uuid4()
    agent_b = uuid4()
    await service.create_plan_request(
        agent_id=agent_a, requested_by_user_id=uuid4(), original_request="x", intent_type="in_session_execution"
    )
    await service.create_plan_request(
        agent_id=agent_b, requested_by_user_id=uuid4(), original_request="y", intent_type="in_session_execution"
    )

    rows = await service.list_plans_for_agent(agent_a)
    assert len(rows) == 1
    assert rows[0].agent_id == agent_a
