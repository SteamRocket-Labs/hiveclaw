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

    # The service queries by exposing the lookup criteria through attributes
    # on the statement object we hand it. We model that by storing the most
    # recent filter the service requested on the fake itself.
    async def execute(self, stmt):
        plan_id = getattr(stmt, "_plan_lookup_id", None)
        agent_id = getattr(stmt, "_plan_lookup_agent_id", None)
        if plan_id is not None:
            match = next((r for r in self.rows if str(r.id) == str(plan_id)), None)
            return _ScalarOneResult(match)
        if agent_id is not None:
            matches = [r for r in self.rows if str(r.agent_id) == str(agent_id)]
            return _ScalarsResult(matches)
        return _ScalarsResult(list(self.rows))


@pytest.fixture()
def patched_service(monkeypatch, tmp_path):
    """Return (service, session) with async_session + AGENT_DATA_DIR patched."""
    from app.services import plan_mode_service as mod

    session = _PlanSession()
    monkeypatch.setattr(mod, "async_session", lambda: session)
    monkeypatch.setattr(mod, "_agent_data_dir", lambda: tmp_path)

    service = mod.PlanModeService()
    return service, session, tmp_path


# ---------------------------------------------------------------------------
# create_plan_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_plan_request_persists_draft(patched_service):
    service, session, _ = patched_service
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
    service, session, _ = patched_service
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
    service, session, data_dir = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="每天 9 点帮我整理新闻",
        intent_type="autonomous_wake",
    )

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
    service, session, data_dir = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="x",
        intent_type="long_task",
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
async def test_generate_plan_unknown_plan_raises(patched_service):
    service, _, _ = patched_service
    with pytest.raises(LookupError):
        await service.generate_plan(plan_id=uuid4(), fill={})


# ---------------------------------------------------------------------------
# revise_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revise_plan_supersedes_old_and_bumps_version(patched_service):
    service, session, _ = patched_service
    agent_id = uuid4()

    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        original_request="x",
        intent_type="long_task",
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


# ---------------------------------------------------------------------------
# confirm_plan
# ---------------------------------------------------------------------------


async def _make_awaiting(service, *, agent_id=None, requester=None):
    agent_id = agent_id or uuid4()
    requester = requester or uuid4()
    draft = await service.create_plan_request(
        agent_id=agent_id,
        requested_by_user_id=requester,
        original_request="每天 9 点帮我整理新闻",
        intent_type="autonomous_wake",
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
    service, session, data_dir = patched_service
    plan, requester = await _make_awaiting(service)
    confirmer = uuid4()

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

    # Markdown re-rendered with confirmation reflected in frontmatter.
    md = (data_dir / str(plan.agent_id) / "plans" / f"{plan.id}.md").read_text(encoding="utf-8")
    assert "status: confirmed" in md
    assert f"confirmed_by: {confirmer}" in md


@pytest.mark.asyncio
async def test_confirm_plan_allows_requesting_user_to_confirm(patched_service):
    service, session, _ = patched_service
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
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError) as exc:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=uuid4(),
            plan_version=plan.plan_version + 1,
            plan_hash=plan.plan_hash,
        )
    assert exc.value.error_code == "version_mismatch"
    assert plan.status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_confirm_plan_hash_mismatch_conflicts(patched_service):
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError) as exc:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=uuid4(),
            plan_version=plan.plan_version,
            plan_hash="sha256:tampered",
        )
    assert exc.value.error_code == "hash_mismatch"
    assert plan.status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_confirmed_plan_cannot_be_reconfirmed(patched_service):
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)
    await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=uuid4(),
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    from app.services.plan_mode_service import PlanConflictError

    with pytest.raises(PlanConflictError) as exc:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=uuid4(),
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
        )
    assert exc.value.error_code == "not_confirmable"


# ---------------------------------------------------------------------------
# reject_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_plan_sets_rejected(patched_service):
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)
    rejecter = uuid4()

    result = await service.reject_plan(
        plan_id=plan.id,
        rejecting_user_id=rejecter,
        reason="not needed",
    )

    assert result.status == "rejected"
    assert result.rejected_by_user_id == rejecter
    assert result.rejected_at is not None


@pytest.mark.asyncio
async def test_reject_after_confirm_is_blocked(patched_service):
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)
    await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=uuid4(),
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
# ensure_awaiting_plan — intercept-then-create (§9.2). The tool gate / auto-sync
# paths call this to materialise a confirmable plan from the blocked action's
# own arguments, idempotently per (agent, intent, signature).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_awaiting_plan_creates_awaiting_plan_from_tool_args(patched_service):
    service, session, _ = patched_service
    agent_id = uuid4()

    plan = await service.ensure_awaiting_plan(
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={
            "name": "Daily brief",
            "type": "cron",
            "config": {"expr": "0 9 * * 1-5"},
            "reason": "Recurring morning brief.",
        },
        source="tool_runtime",
    )

    assert plan.status == "awaiting_confirmation"
    assert plan.intent_type == "autonomous_wake"
    assert plan.plan_hash and plan.plan_hash.startswith("sha256:")
    assert plan.plan_json["title"] == "Daily brief"
    assert plan.plan_json["wake_policy"]["expr"] == "0 9 * * 1-5"
    # The intercept signature is stored so a repeat call can dedupe on it.
    assert plan.metadata_json.get("intercept_signature")
    # requester is NOT the agent's own confirmation: created with no user, so a
    # later confirm by a real user can never be a self-confirm.
    assert plan.requested_by_user_id is None


@pytest.mark.asyncio
async def test_ensure_awaiting_plan_is_idempotent_for_same_signature(patched_service):
    service, session, _ = patched_service
    agent_id = uuid4()
    args = {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "r"}

    first = await service.ensure_awaiting_plan(
        agent_id=agent_id, action_kind="create_enabled_trigger", tool_name="set_trigger", arguments=args
    )
    rows_after_first = len(session.rows)
    second = await service.ensure_awaiting_plan(
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"config": {"expr": "0 9 * * *"}, "type": "cron", "name": "Daily brief", "reason": "r"},
    )

    assert str(second.id) == str(first.id)  # reused, not a new row
    assert len(session.rows) == rows_after_first  # no duplicate persisted


@pytest.mark.asyncio
async def test_ensure_awaiting_plan_distinct_signature_creates_new_plan(patched_service):
    service, session, _ = patched_service
    agent_id = uuid4()

    first = await service.ensure_awaiting_plan(
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"name": "A", "type": "cron", "config": {"expr": "0 9 * * *"}},
    )
    second = await service.ensure_awaiting_plan(
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        arguments={"name": "B", "type": "cron", "config": {"expr": "0 10 * * *"}},
    )

    assert str(second.id) != str(first.id)


def test_get_plan_mode_service_returns_shared_singleton():
    from app.services.plan_mode_service import get_plan_mode_service

    assert get_plan_mode_service() is get_plan_mode_service()


@pytest.mark.asyncio
async def test_handoff_requires_confirmed_status(patched_service):
    service, session, _ = patched_service
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
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=uuid4(),
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
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=uuid4(),
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    calls = {"n": 0}

    def handler(db, plan_row):
        assert db is session
        calls["n"] += 1
        return {"created_objective_id": str(uuid4()), "created_trigger_id": str(uuid4())}

    service.register_handoff_handler("objective_trigger", handler)

    first = await service.handoff_confirmed_plan(plan_id=confirmed.id)
    assert first.handoff_status == "completed"
    assert calls["n"] == 1
    assert first.handoff_payload["created_objective_id"]

    second = await service.handoff_confirmed_plan(plan_id=confirmed.id)
    assert second.handoff_status == "completed"
    assert calls["n"] == 1  # not re-invoked


@pytest.mark.asyncio
async def test_handoff_records_failure_without_corrupting_confirmed(patched_service):
    service, session, _ = patched_service
    plan, _ = await _make_awaiting(service)
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=uuid4(),
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    def boom(db, plan_row):
        assert db is session
        raise RuntimeError("downstream create failed")

    service.register_handoff_handler("objective_trigger", boom)

    result = await service.handoff_confirmed_plan(plan_id=confirmed.id)

    assert result.status == "confirmed"  # never flips to a failed *status*
    assert result.handoff_status == "failed"
    assert "downstream create failed" in (result.handoff_payload or {}).get("error", "")


# ---------------------------------------------------------------------------
# read helpers (API support)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_plan_returns_none_for_unknown(patched_service):
    service, _, _ = patched_service
    assert await service.get_plan(uuid4()) is None


@pytest.mark.asyncio
async def test_list_plans_for_agent_filters_by_agent(patched_service):
    service, _, _ = patched_service
    agent_a = uuid4()
    agent_b = uuid4()
    await service.create_plan_request(
        agent_id=agent_a, requested_by_user_id=uuid4(), original_request="x", intent_type="long_task"
    )
    await service.create_plan_request(
        agent_id=agent_b, requested_by_user_id=uuid4(), original_request="y", intent_type="long_task"
    )

    rows = await service.list_plans_for_agent(agent_a)
    assert len(rows) == 1
    assert rows[0].agent_id == agent_a
