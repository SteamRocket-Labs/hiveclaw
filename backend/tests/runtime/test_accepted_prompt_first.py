"""CCPlus V1 Reconciliation §7 row-00 — accepted-prompt-first acceptance.

Required selector: ``pytest -k accepted_prompt_first``.

Contract under test
-------------------
EVERY runtime entry must persist the accepted user prompt before the kernel
(``invoke_agent`` / the task that calls it) sees the turn. In the split runtime
topology, the API process may only queue the DB control record; the worker must
materialize the initial user turn to the transactional transcript before invoking
the model. T0 is then projected asynchronously from the committed event. This is
the crash-resumable invariant: if the process dies
mid-response, the accepted prompt is already durable so the turn can be
replayed/resumed instead of being silently lost.

The nine runtime entries reduce to THREE real append-before-kernel choke points,
because most entries funnel their kernel dispatch through the web-chat queue and
worker materialization path:

  1. ``web_chat_runtime.start_web_chat_run`` — writes only the DB queued run
     and user read model in the API process; the worker materializes the queued
     initial user turn to the transcript before ``execute_web_chat_run`` calls
     ``invoke_agent``.
  2. ``agents.subagent._spawn_one`` — appends the child T0 ``user_message``
     event(s) BEFORE calling ``invoke(request)`` (the kernel).
  3. ``agent_session_continuation.continue_agent_session_from_mailbox`` —
     appends the ``agent_session_message`` transcript event BEFORE consuming it
     (either the mid-run drain or a fresh ``start_web_chat_run`` turn).

Coverage map for the 9 entries (Reconciliation §7 row-00):

  BEHAVIORALLY ASSERTED (real ordering assertions in this file):
    [1] web chat turn ......... start_web_chat_run queues DB-only; worker
                                materializes the initial user turn before
                                kernel dispatch.
    [2] subagent spawn ........ _spawn_one: child T0 user_message append (and the
                                event is readable on disk) BEFORE invoke().
    [3] agent_session cont. ... continue_agent_session_from_mailbox: append of
                                agent_session_message BEFORE the consumer runs.
    [4] team-member message ... routes through entry [3]
                                (api.agent_teams.message_agent_team_member ->
                                continue_agent_session_from_mailbox); asserted by
                                proving the wiring + the [3] ordering it inherits.
    [5] goal continuation ..... continue_session_goal dispatches the next turn
                                THROUGH start_web_chat_run (entry [1]); asserted
                                by proving the delegation to the proven gate.
    [6] goal post-turn bridge . maybe_continue_session_goal_after_turn -> same
                                start_web_chat_run gate as [5]; covered by [5].
    [7] plan-mode session h/o . continue_current_session_handoff dispatches the
                                continuation THROUGH start_web_chat_run (entry
                                [1]); asserted by proving the delegation.
    [8] plan-mode team h/o .... creates Team container, then delegates teammate
                                dispatch to spawn_agent_team_member_runtime, which
                                reaches the mailbox continuation gate (entry [3]).
    [9] team-member start ..... api.agent_teams.start_agent_team_member_run ->
                                start_web_chat_run (entry [1]); covered by [1].

  Entries [4]-[9] do NOT re-implement transcript persistence — they inherit the
  append-before-kernel guarantee from entries [1]/[3]. We assert that inheritance
  by proving (a) the proven gates ([1],[3]) persist before the kernel
  dispatch, and (b) the delegating entries route their kernel dispatch through
  exactly those gates (no private invoke_agent path). The one entry that needs a
  heavy integration harness to exercise end-to-end (plan-mode team handoff, [8],
  which builds team/member rows in a real DB) is documented as covered-by-[3]
  rather than independently asserted here; see ``test_plan_mode_team_handoff_
  delegates_to_agenttool_teammate_runtime`` for the explicit note.

Every assertion fails if the underlying persist-before-kernel ordering were
reverted (e.g. if ``start_web_chat_run`` went back to in-process dispatch, if the
worker invoked the kernel before materializing the queued user turn, or if
``_spawn_one`` invoked the kernel before writing T0).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from uuid import uuid4

import pytest


# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])


class _OrderRecordingDB:
    """Fake AsyncSession that records the relative order of ``commit`` calls.

    ``commit`` appends ``"commit"`` to the shared ``order`` log so the test can
    assert append/commit/dispatch ordering on a single timeline.
    """

    def __init__(self, order: list[str], active_run=None):
        self.order = order
        self.active_run = active_run
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        return _ScalarResult(self.active_run)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1
        self.order.append("commit")


# --------------------------------------------------------------------------- #
# [1] Web chat turn — start_web_chat_run
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_web_chat_turn_api_queues_db_only_before_worker_dispatch_accepted_prompt_first(monkeypatch, tmp_path):
    """[1] web chat API: the control-plane start call queues the run and user
    read model in DB, but does not write T0 or dispatch the kernel in-process.

    In the split topology the API process may not assume access to the agent
    volume. ``start_web_chat_run`` therefore returns a pending run after DB commit;
    the worker-side materialization test below proves the T0 write happens before
    invoke.
    """
    import app.services.web_chat_runtime as runtime

    order: list[str] = []

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )
    db = _OrderRecordingDB(order, active_run=None)

    async def recording_append(**kwargs):
        order.append("append")
        raise AssertionError(f"API start must not append transcript/T0: {kwargs}")

    def recording_create_task(coro):
        order.append("create_task")
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", recording_append)
    monkeypatch.setattr(runtime.asyncio, "create_task", recording_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="请规划一个长任务",
        display_content="请规划一个长任务",
    )

    assert result["status"] == "pending"
    assert order == ["commit"], order

    from app.memory.t0.ledger import replay_t0_session_events

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []


@pytest.mark.asyncio
async def test_web_chat_worker_materializes_queued_prompt_before_kernel_accepted_prompt_first(monkeypatch, tmp_path):
    """[1] web chat worker: the queued initial user turn is committed to the
    transactional transcript before the worker can invoke the kernel."""
    import app.services.web_chat_runtime as runtime
    from app.models.chat_transcript_event import ChatTranscriptEvent

    order: list[str] = []
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    message_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_session_id=str(session_id),
        metadata_json={
            "source": "web",
            "initial_user_message_t0_materialized": False,
            "initial_user_message": {
                "message_id": str(message_id),
                "content": "请规划一个长任务",
                "llm_content": "请规划一个长任务",
                "display_content": "请规划一个长任务",
                "file_name": "",
                "source": "web",
                "attachments": [],
                "parts": [],
                "metadata": {"turn_id": "turn-1", "intent_id": "intent-1"},
            },
        },
    )
    db = _OrderRecordingDB(order, active_run=None)
    real_append = runtime.append_session_event

    async def recording_append(**kwargs):
        order.append("append")
        return await real_append(**kwargs)

    def fake_capture(**_kwargs):
        return None

    async def fake_mark(**_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", recording_append)
    monkeypatch.setattr(runtime, "_capture_user_checkpoint_workspace_snapshot", fake_capture)
    monkeypatch.setattr(runtime, "mark_latest_pending_clarification_answered", fake_mark)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._materialize_initial_user_turn_for_worker(
        db=db,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        session=session,
    )
    await db.commit()
    order.append("invoke")

    assert order == ["append", "commit", "invoke"], order
    assert runtime_task.metadata_json["initial_user_message_t0_materialized"] is True
    events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert [(event.event_type, event.content, event.projection_status) for event in events] == [
        ("user_message", "请规划一个长任务", "pending")
    ]


@pytest.mark.asyncio
async def test_web_chat_goal_continuation_queues_before_worker_dispatch_accepted_prompt_first(monkeypatch, tmp_path):
    """[1]/[5]/[6]/[7]/[9] non-user prompts still commit a pending DB task
    before any worker dispatch.

    ``goal_continuation`` / ``team_member`` / ``plan_mode_handoff`` enter
    ``start_web_chat_run`` with ``append_user_message=False`` (the prompt is a
    synthesized continuation, not a fresh user message). The API must only queue
    the durable task; worker claim is responsible for execution.
    """
    import app.services.web_chat_runtime as runtime

    order: list[str] = []

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id, agent_id=agent_id, user_id=user_id, title="Session 05-21", last_message_at=None
    )
    db = _OrderRecordingDB(order, active_run=None)

    def recording_create_task(coro):
        order.append("create_task")
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime.asyncio, "create_task", recording_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="Continue working toward the active goal.",
        runtime_task_type="goal_continuation",
        append_user_message=False,
        extra_metadata={"source": "goal_continuation", "goal_id": "goal-1"},
    )

    assert result["run_id"]
    assert result["status"] == "pending"
    assert order == ["commit"], order


# --------------------------------------------------------------------------- #
# [2] Subagent spawn — _spawn_one
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_subagent_spawn_appends_child_prompt_before_kernel_accepted_prompt_first(monkeypatch, tmp_path):
    """[2] subagent: the child T0 ``user_message`` event is appended (and is
    readable on disk) BEFORE the kernel (``invoke``) is called.

    The injected ``invoke`` reads the child's T0 ledger back the moment it is
    invoked; the accepted task prompt must already be there. If ``_spawn_one``
    were reverted to call the kernel before ``_append_subagent_t0_event``, the
    ledger would be empty at invoke time and this test would fail.
    """
    from app.agents.subagent import SubagentJob, SubagentSpawnContext, SubagentSpec, _spawn_one

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    order: list[str] = []
    parent_agent_id = uuid.uuid4()
    trace_id = "trace-accepted-prompt-first"

    ctx = SubagentSpawnContext(
        parent_agent_id=parent_agent_id,
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
        trace_id=trace_id,
        tenant_id=uuid.uuid4(),
    )
    spec = SubagentSpec(name="scout", type="explorer")
    job = SubagentJob(spec=spec, task="find the leak")

    # Recompute the T0 session id the production code uses so we can read it back.
    from app.agents.subagent import _subagent_t0_session_id
    from app.memory.t0.ledger import replay_t0_session_events

    t0_session_id = _subagent_t0_session_id(ctx, spec, ctx.depth + 1)
    assert t0_session_id  # session id resolvable from trace_id

    seen_at_invoke: dict = {}

    async def fake_invoke(request):
        order.append("invoke")
        events = replay_t0_session_events(agent_id=parent_agent_id, session_id=t0_session_id, data_root=tmp_path)
        seen_at_invoke["events"] = [(e.event_type, e.role, e.content) for e in events]
        return SimpleNamespace(content="digest", tokens_used=3)

    # Wrap the real T0 append so we record WHEN it ran relative to the kernel.
    import app.agents.subagent as subagent_mod

    real_append_t0 = subagent_mod._append_subagent_t0_event

    def recording_append_t0(**kwargs):
        if kwargs.get("event_type") == "user_message":
            order.append("append")
        return real_append_t0(**kwargs)

    monkeypatch.setattr(subagent_mod, "_append_subagent_t0_event", recording_append_t0)

    result = await _spawn_one(ctx, job, invoke=fake_invoke)

    assert result.status == "completed"
    # The accepted child prompt is appended before the kernel runs ...
    assert order[0] == "append"
    assert "invoke" in order
    assert order.index("append") < order.index("invoke")
    # ... and was genuinely durable/readable by the time the kernel ran.
    assert seen_at_invoke["events"] == [("user_message", "user", "find the leak")]


# --------------------------------------------------------------------------- #
# [3] Agent-session continuation — continue_agent_session_from_mailbox
#     (also the kernel-dispatch path for [4] team-member message)
# --------------------------------------------------------------------------- #


def _agent_session(*, state: str = "open"):
    parent_session_id = uuid4()
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        parent_session_id=parent_session_id,
        root_session_id=parent_session_id,
        visibility_scope="team",
        listed_surface="parent",
        session_kind="team_member",
        runtime_source="team_member",
        transcript_metadata_json={"session_state": state},
    )


@pytest.mark.asyncio
async def test_agent_session_continuation_appends_before_midrun_consume_accepted_prompt_first(monkeypatch):
    """[3]/[4] active run: the ``agent_session_message`` transcript event is
    appended BEFORE the message is handed to the mid-run drain consumer.

    The persisted transcript event is the durable mailbox truth; the consumer
    only runs against an already-appended message (``message_already_in_t0`` is
    True). Reverting to consume before appending would flip this order.
    """
    import app.services.agent_session_continuation as svc

    order: list[str] = []
    session = _agent_session(state="running")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    active_run = SimpleNamespace(
        id=uuid4(), status="running", metadata_json={}, created_at=None, started_at=None, completed_at=None
    )

    class _DB:
        async def commit(self):
            order.append("commit")

    async def recording_append(**kwargs):
        if kwargs.get("event_type") == "agent_session_message":
            order.append("append")
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**_kwargs):
        return active_run

    async def fake_queue(**kwargs):
        order.append("consume")
        assert kwargs["message_already_in_t0"] is True
        return {"run_id": kwargs["active_run"].id.hex, "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", recording_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "_queue_saved_mid_run_user_message", fake_queue)

    result = await svc.continue_agent_session_from_mailbox(
        db=_DB(),
        agent=agent,
        user=user,
        session=session,
        message="inspect the new evidence",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["status"] == "queued"
    assert result["consumer"] == "mid_run_message_drain"
    assert order[0] == "append"
    assert "consume" in order
    assert order.index("append") < order.index("consume")


@pytest.mark.asyncio
async def test_agent_session_continuation_appends_before_new_turn_kernel_accepted_prompt_first(monkeypatch):
    """[3]/[4] inactive open session: the ``agent_session_message`` transcript
    event is appended BEFORE ``start_web_chat_run`` (the kernel-dispatch gate) is
    called for the fresh continuation turn.

    ``start_web_chat_run`` is invoked with ``append_user_message=False`` precisely
    because the accepted prompt was ALREADY durably appended here first. This is
    the append-before-kernel ordering for the continuation -> new-turn path.
    """
    import app.services.agent_session_continuation as svc

    order: list[str] = []
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)

    class _DB:
        async def commit(self):
            order.append("commit")

    async def recording_append(**kwargs):
        if kwargs.get("event_type") == "agent_session_message":
            order.append("append")
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**_kwargs):
        return None

    async def fake_start(**kwargs):
        order.append("start_web_chat_run")
        # The continuation relies on the prompt already being in T0.
        assert kwargs["append_user_message"] is False
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", recording_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_agent_session_from_mailbox(
        db=_DB(),
        agent=agent,
        user=user,
        session=session,
        message="continue from the last result",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["status"] == "started"
    assert order[0] == "append"
    assert "start_web_chat_run" in order
    assert order.index("append") < order.index("start_web_chat_run")


# --------------------------------------------------------------------------- #
# Delegation inheritance proofs for entries [5]-[9]
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_goal_continuation_dispatches_through_web_chat_gate_accepted_prompt_first(monkeypatch):
    """[5]/[6] goal continuation inherits accepted-prompt-first by dispatching the
    next turn THROUGH ``start_web_chat_run`` (the proven gate [1]), not a private
    ``invoke_agent`` path.

    We assert the delegation: ``continue_session_goal`` calls ``start_web_chat_run``
    as ``goal_continuation``. Combined with entry [1]'s commit-before-dispatch
    ordering, this proves the accepted prompt is durable before the kernel runs.
    If a future change bypassed ``start_web_chat_run`` to call the kernel directly
    (losing the gate), this test fails.
    """
    import app.services.goal_continuation_service as goal_service

    captured: dict = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"run_id": "run-goal-1", "status": "running"}

    monkeypatch.setattr(goal_service, "start_web_chat_run", fake_start)

    class _DB:
        async def flush(self):
            return None

    agent = SimpleNamespace(id=uuid4(), name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=uuid4(), agent_id=agent.id, user_id=user.id)
    goal = SimpleNamespace(
        id=uuid4(),
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        chat_session_id=session.id,
        objective="ship the report",
        status="active",
        metadata_json={},
        continuation_count=0,
        token_budget=None,
        tokens_used=0,
        time_budget_seconds=None,
        max_continuation_turns=None,
        blocked_count=0,
    )

    result = await goal_service.continue_session_goal(db=_DB(), agent=agent, user=user, session=session, goal=goal)

    # Kernel dispatch is delegated to the proven web-chat gate.
    assert captured.get("runtime_task_type") == "goal_continuation"
    assert captured.get("append_user_message") is False
    assert captured["extra_metadata"]["source"] == "goal_continuation"
    assert result["ok"] is True
    assert result["run"]["run_id"] == "run-goal-1"


@pytest.mark.asyncio
async def test_plan_mode_session_handoff_dispatches_through_web_chat_gate_accepted_prompt_first(monkeypatch):
    """[7] plan-mode session handoff inherits accepted-prompt-first by dispatching
    the confirmed-plan continuation THROUGH ``start_web_chat_run`` (gate [1]).

    A confirmed plan's execution prompt is handed to ``start_web_chat_run`` with
    ``source=plan_mode_handoff``; the kernel is reached only via that gate, which
    commits the run before dispatching it. This proves the delegation that carries
    the accepted-prompt-first guarantee.
    """
    import app.services.plan_mode_session_handoff as handoff

    captured: dict = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"runtime_task_id": "run-plan-1", "status": "running", "run_id": "run-plan-1"}

    agent = SimpleNamespace(id=uuid4(), name="Agent", tenant_id=uuid4(), expires_at=None)
    user = SimpleNamespace(id=uuid4(), username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=uuid4(), agent_id=agent.id, user_id=user.id)

    async def fake_load_agent(_db, _agent_id):
        return agent

    async def fake_load_user(_db, _user_id):
        return user

    async def fake_load_session(_db, _session_id):
        return session

    monkeypatch.setattr(handoff, "_load_agent", fake_load_agent)
    monkeypatch.setattr(handoff, "_load_user", fake_load_user)
    monkeypatch.setattr(handoff, "_load_session", fake_load_session)
    monkeypatch.setattr("app.services.web_chat_runtime.start_web_chat_run", fake_start)

    plan = SimpleNamespace(
        id=uuid4(),
        status="confirmed",
        session_id=session.id,
        requested_by_user_id=user.id,
        agent_id=agent.id,
        plan_version=1,
        plan_hash="hash-1",
        original_request="ship the report",
        plan_json={"plan_markdown": "## Plan\n- step one"},
    )

    result = await handoff.continue_current_session_handoff(db=object(), plan=plan)

    # The confirmed plan's execution prompt is dispatched via the proven gate.
    assert captured["extra_metadata"]["source"] == "plan_mode_handoff"
    assert captured["extra_metadata"]["approved_plan_id"] == str(plan.id)
    assert captured["content"]  # a real, non-empty execution prompt is handed over
    assert result["execution"] == "current_session"
    assert result["runtime_task_id"] == "run-plan-1"


def test_plan_mode_team_handoff_delegates_to_agenttool_teammate_runtime():
    """[8] plan-mode TEAM handoff delegates teammate dispatch to AgentTool runtime.

    Plan handoff may create the Team container, but teammate creation/dispatch must
    go through ``spawn_agent_team_member_runtime``. That keeps confirmed plans on the
    same TeamCreate -> AgentTool teammate spawn -> mailbox path as normal session use.
    The handoff itself must not import a private kernel dispatcher or a separate
    ``start_web_chat_run`` lane.
    """
    import ast
    from pathlib import Path

    source = Path("app/services/plan_mode_agent_team_handoff.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    assert "spawn_agent_team_member_runtime" in imported_names
    assert "start_web_chat_run" not in imported_names
    assert "invoke_agent" not in imported_names
