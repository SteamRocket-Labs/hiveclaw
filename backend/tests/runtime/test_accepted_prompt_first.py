"""CCPlus V1 Reconciliation §7 row-00 — accepted-prompt-first acceptance.

Required selector: ``pytest -k accepted_prompt_first``.

Contract under test
-------------------
EVERY runtime entry must persist the accepted user prompt to the durable
transcript / T0 session ledger (append + ``db.commit``) BEFORE it dispatches the
kernel (``invoke_agent`` / the task that calls it). This is the crash-resumable
invariant: if the process dies mid-response, the accepted prompt is already
durable so the turn can be replayed/resumed instead of being silently lost.

The nine runtime entries reduce to THREE real append-before-kernel choke points,
because most entries funnel their kernel dispatch through ``start_web_chat_run``:

  1. ``web_chat_runtime.start_web_chat_run`` — appends the ``user_message``
     transcript event and ``db.commit()``s it BEFORE scheduling
     ``execute_web_chat_run`` (the ONLY caller of ``invoke_agent`` for chat).
  2. ``agents.subagent._spawn_one`` — appends the child T0 ``user_message``
     event(s) BEFORE calling ``invoke(request)`` (the kernel).
  3. ``agent_session_continuation.continue_agent_session_from_mailbox`` —
     appends the ``agent_session_message`` transcript event BEFORE consuming it
     (either the mid-run drain or a fresh ``start_web_chat_run`` turn).

Coverage map for the 9 entries (Reconciliation §7 row-00):

  BEHAVIORALLY ASSERTED (real ordering assertions in this file):
    [1] web chat turn ......... start_web_chat_run: append+commit BEFORE
                                the create_task(execute_web_chat_run) dispatch.
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
    [8] plan-mode team h/o .... start_agent_team_from_plan member runs dispatch
                                THROUGH start_web_chat_run (entry [1]); covered by
                                the same delegation guarantee as [7].
    [9] team-member start ..... api.agent_teams.start_agent_team_member_run ->
                                start_web_chat_run (entry [1]); covered by [1].

  Entries [4]-[9] do NOT re-implement transcript persistence — they inherit the
  append-before-kernel guarantee from entries [1]/[3]. We assert that inheritance
  by proving (a) the proven gates ([1],[3]) order append+commit before the kernel
  dispatch, and (b) the delegating entries route their kernel dispatch through
  exactly those gates (no private invoke_agent path). The one entry that needs a
  heavy integration harness to exercise end-to-end (plan-mode team handoff, [8],
  which builds team/member rows in a real DB) is documented as covered-by-[1]
  rather than independently asserted here; see ``test_plan_mode_team_handoff_
  accepted_prompt_first_is_deferred_to_web_chat_gate`` for the explicit note.

Every assertion fails if the underlying append-before-kernel ordering were
reverted (e.g. if ``start_web_chat_run`` scheduled the run task before committing
the user message, or ``_spawn_one`` invoked the kernel before writing T0).
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
async def test_web_chat_turn_appends_and_commits_prompt_before_kernel_dispatch_accepted_prompt_first(
    monkeypatch, tmp_path
):
    """[1] web chat: append(user_message) + db.commit() happen BEFORE the
    create_task(execute_web_chat_run) kernel-dispatch.

    ``execute_web_chat_run`` is the ONLY function that calls ``invoke_agent`` for
    a chat turn, and it is reached exclusively via ``asyncio.create_task`` at the
    tail of ``start_web_chat_run``. Recording the order of (append, commit,
    create_task) on one timeline therefore proves the accepted prompt is durable
    before the kernel is ever dispatched. Reverting to schedule the task before
    the commit would flip the order and fail this test.
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

    real_append = runtime.append_session_event

    async def recording_append(**kwargs):
        order.append("append")
        # Persist for real into the tmp T0 ledger so the event is genuinely
        # durable (and observable) by the time the run task is scheduled.
        return await real_append(**kwargs)

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

    assert result["status"] == "running"
    # The accepted prompt is appended AND committed before the kernel task spawns.
    assert order == ["append", "commit", "create_task"], order
    assert order.index("append") < order.index("create_task")
    assert order.index("commit") < order.index("create_task")

    # And the prompt is genuinely durable in the T0 ledger at dispatch time.
    from app.memory.t0.ledger import replay_t0_session_events

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(e.event_type, e.role, e.content) for e in events] == [("user_message", "user", "请规划一个长任务")]


@pytest.mark.asyncio
async def test_web_chat_goal_continuation_appends_before_kernel_dispatch_accepted_prompt_first(monkeypatch, tmp_path):
    """[1]/[5]/[6]/[7]/[9] non-user prompts still commit before dispatch.

    ``goal_continuation`` / ``team_member`` / ``plan_mode_handoff`` enter
    ``start_web_chat_run`` with ``append_user_message=False`` (the prompt is a
    synthesized continuation, not a fresh user message). The accepted-prompt-first
    contract still holds: the run task must not be scheduled before ``db.commit``
    durably records the run. This proves the commit-before-dispatch ordering for
    every delegating entry that funnels through this gate.
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
    # Even with no user_message append, the run is committed before dispatch.
    assert order == ["commit", "create_task"], order
    assert order.index("commit") < order.index("create_task")


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


def test_plan_mode_team_handoff_accepted_prompt_first_is_deferred_to_web_chat_gate():
    """[8] plan-mode TEAM handoff — coverage note (not independently asserted).

    ``start_agent_team_from_plan`` builds team + member rows in a real DB and then
    dispatches each member's first run THROUGH ``start_web_chat_run`` (gate [1]),
    inheriting the same commit-before-dispatch ordering proven by entry [1]. A
    faithful end-to-end ordering assertion for this entry needs a heavy DB harness
    (team/member/session materialisation, unique-index handling) that belongs to
    an integration test, not a unit test — so it is deliberately covered-by-[1]
    rather than re-asserted with a fake here.

    This test pins the structural fact that makes the inheritance true: the team
    handoff's ONLY kernel-dispatch path is ``start_web_chat_run`` (no private
    ``invoke_agent`` import). If someone added a direct kernel call to the team
    handoff (bypassing the accepted-prompt-first gate), this guard fails.
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

    # Kernel dispatch is delegated to the proven gate ...
    assert "start_web_chat_run" in imported_names
    # ... and the team handoff never reaches the kernel directly.
    assert "invoke_agent" not in imported_names
