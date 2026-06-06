"""T-G1 — ReminderScheduler: transient injection with behavioral throttling.

docs/runtime-guidance-cc-alignment.md §5 T-G1 (v0.2 拍板):

* **No stacking (M1)**: reminders participate in THIS round's LLM request
  only — they never enter ``api_messages``, so a multi-round run sends each
  reminder at most as often as the scheduler allows, never N copies.
* **Behavioral throttling (CC attachments.ts:254 alignment)**: a reminder
  with ``idle_rounds=N`` fires only after N observed rounds without any of
  its ``observed_tools``; ``cooldown_rounds=M`` enforces a gap between two
  injections. The engine feeds ``observe(tool_names)`` once per round — no
  history scanning.
* **Eligibility stays a gate, frequency moves to behavior (M7)**: the
  work-ledger flag only answers "may this run see ledger reminders at all";
  plan-mode suppression is preserved as-is.
* **reset() re-arms fire-once reminders after compaction (M8)** — the plan
  FULL re-arm, generalized.

Pure-core tests first (no kernel mocks), then kernel integration pinning the
no-stacking behaviour through the fake-client call log.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import PlanModeState, SessionContext

LEDGER_TOOLS = ("track_todo", "record_finding", "read_ledger")


def _ledger_ctx() -> SessionContext:
    sc = SessionContext()
    sc.metadata = {"work_ledger_enabled": True}
    return sc


def _plan_ctx(**plan_kwargs) -> SessionContext:
    sc = SessionContext()
    sc.plan_mode = PlanModeState(active=True, **plan_kwargs)
    return sc


def _round_state(round_i: int = 0, max_rounds: int = 200, **extra) -> dict:
    state = {
        "round_i": round_i,
        "max_rounds": max_rounds,
        "total_tool_calls": 0,
        "failed_tool_calls": 0,
        "context_tokens": 1_000,
    }
    state.update(extra)
    return state


def _scheduler():
    from app.kernel.reminder_scheduler import ReminderScheduler, build_default_reminder_specs

    return ReminderScheduler(build_default_reminder_specs())


def _drive(scheduler, ctx, rounds: int, tools_per_round=()) -> dict[int, list[str]]:
    """Run collect→observe for N rounds; return {round_index: texts}."""
    fired: dict[int, list[str]] = {}
    for i in range(rounds):
        fired[i] = scheduler.collect(ctx, _round_state(round_i=i))
        scheduler.observe(tools_per_round[i] if i < len(tools_per_round) else ())
    return fired


# ── Work ledger: behavioral throttling (CC 10+10) ───────────────────


def test_ledger_reminder_waits_for_idle_rounds():
    """No nag in the first 10 rounds; fires once the model has gone 10
    observed rounds without touching any ledger tool."""
    scheduler = _scheduler()
    ctx = _ledger_ctx()

    fired = _drive(scheduler, ctx, rounds=12)

    ledger_rounds = [i for i, texts in fired.items() if any("track_todo" in t for t in texts)]
    assert ledger_rounds, "ledger reminder never fired"
    assert min(ledger_rounds) >= 10


def test_tool_use_resets_ledger_idle_counter():
    """Using a ledger tool at round 5 restarts the idle window — no reminder
    before round 15."""
    scheduler = _scheduler()
    ctx = _ledger_ctx()
    tools = [()] * 5 + [("track_todo",)] + [()] * 11

    fired = _drive(scheduler, ctx, rounds=17, tools_per_round=tools)

    ledger_rounds = [i for i, texts in fired.items() if any("track_todo" in t for t in texts)]
    assert ledger_rounds, "ledger reminder never fired after counter reset"
    assert min(ledger_rounds) >= 15


def test_ledger_cooldown_between_injections():
    """After one injection the next is at least cooldown_rounds away."""
    scheduler = _scheduler()
    ctx = _ledger_ctx()

    fired = _drive(scheduler, ctx, rounds=25)

    ledger_rounds = [i for i, texts in fired.items() if any("track_todo" in t for t in texts)]
    assert len(ledger_rounds) >= 2, f"expected at least two injections in 25 rounds, got {ledger_rounds}"
    assert ledger_rounds[1] - ledger_rounds[0] >= 10


def test_ledger_eligibility_gate_is_hard():
    """M7: without the work_ledger_enabled flag the reminder NEVER fires —
    eligibility is the gate, behaviour only governs frequency."""
    scheduler = _scheduler()
    ctx = SessionContext()  # no metadata flag

    fired = _drive(scheduler, ctx, rounds=30)

    assert not any("track_todo" in t for texts in fired.values() for t in texts)


def test_ledger_suppressed_while_plan_active():
    """Behavioural invariance: planning is read-only, the execution-todo nudge
    stays suppressed (formerly hard-coded in _work_ledger_reminder_content)."""
    scheduler = _scheduler()
    ctx = _plan_ctx()
    ctx.metadata = {"work_ledger_enabled": True}

    fired = _drive(scheduler, ctx, rounds=30)

    assert not any("track_todo" in t for texts in fired.values() for t in texts)


# ── Plan mode: FULL once → SPARSE on cooldown (CC plan throttle 5) ──


def test_plan_full_fires_once_then_sparse_cooldown():
    from app.kernel.reminder_scheduler import _PLAN_MODE_REMINDER_SPARSE

    scheduler = _scheduler()
    ctx = _plan_ctx()

    fired = _drive(scheduler, ctx, rounds=8)

    full_rounds = [i for i, texts in fired.items() if any("Plan Mode is active" in t for t in texts)]
    sparse_rounds = [i for i, texts in fired.items() if _PLAN_MODE_REMINDER_SPARSE in texts]
    assert full_rounds == [0], f"FULL must fire exactly once, on round 0: {full_rounds}"
    assert sparse_rounds, "SPARSE never fired"
    # CC plan-mode attachment throttle: ≥5 rounds between consecutive plan reminders.
    gaps = [b - a for a, b in zip(full_rounds + sparse_rounds, (full_rounds + sparse_rounds)[1:], strict=False)]
    assert all(gap >= 5 for gap in gaps), f"plan reminders not throttled: {sorted(full_rounds + sparse_rounds)}"


def test_plan_full_rearms_after_reset():
    """M8: compaction calls reset() — the next round re-sends FULL (the old
    _reset_plan_reminder semantics, generalized)."""
    scheduler = _scheduler()
    ctx = _plan_ctx()

    first = scheduler.collect(ctx, _round_state(round_i=0))
    scheduler.observe(())
    assert any("Plan Mode is active" in t for t in first)

    scheduler.reset()

    after_reset = scheduler.collect(ctx, _round_state(round_i=1))
    assert any("Plan Mode is active" in t for t in after_reset)


def test_plan_full_carries_file_hint_when_provisioned():
    """Behavioural migration pin: the plan-file hint still rides the FULL text."""
    scheduler = _scheduler()
    ctx = _plan_ctx(plan_file_path="/tmp/plan.md")

    first = scheduler.collect(ctx, _round_state(round_i=0))

    full = next(t for t in first if "Plan Mode is active" in t)
    assert "/tmp/plan.md" in full


# ── Round pressure: threshold-triggered, carries real data ──────────


def test_round_pressure_fires_at_thresholds_with_data():
    scheduler = _scheduler()
    ctx = SessionContext()
    max_rounds = 10  # thresholds: 80% → 8, final-2 → 8 == same; use 20 for distinct
    max_rounds = 20  # 80% → 16, final-2 → 18

    fired: dict[int, list[str]] = {}
    for i in range(20):
        fired[i] = scheduler.collect(
            ctx,
            _round_state(round_i=i, max_rounds=max_rounds, total_tool_calls=i * 2, context_tokens=5_000),
        )
        scheduler.observe(())

    pressure_rounds = [i for i, texts in fired.items() if any("tool rounds used" in t for t in texts)]
    assert pressure_rounds == [16, 18]
    # data-bearing (B2 invariance): the warning carries concrete numbers.
    warn = next(t for t in fired[16] if "tool rounds used" in t)
    assert "16/20" in warn


# ── Event-driven channel (loop guard) ───────────────────────────────


def test_enqueued_event_drains_once():
    scheduler = _scheduler()
    ctx = SessionContext()

    scheduler.enqueue("LOOP-GUARD: stop repeating yourself")

    first = scheduler.collect(ctx, _round_state(round_i=0))
    scheduler.observe(())
    second = scheduler.collect(ctx, _round_state(round_i=1))

    assert any("LOOP-GUARD" in t for t in first)
    assert not any("LOOP-GUARD" in t for t in second)


# ── Kernel integration: transient means NO stacking (M1) ────────────


class _ToolLoopClient:
    """Fake client: N tool-call rounds then a final text round."""

    def __init__(self, tool_rounds: int) -> None:
        self._remaining = tool_rounds
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self._remaining > 0:
            self._remaining -= 1
            return SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{self._remaining}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "x"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 10},
            )
        return SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 5})

    async def close(self) -> None:
        return None


def _kernel(client, persist_sink: list | None = None, max_rounds: int = 6):
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    def _persist(**kwargs):
        if persist_sink is not None:
            persist_sink.append(kwargs)

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=max_rounds),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            resolve_retrieval_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": "read", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: client,
            execute_tool=lambda *_a, **_k: "file content",
            persist_memory=_persist,
            record_token_usage=lambda *a, **k: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )


def _model():
    return SimpleNamespace(
        provider="openai",
        model="gpt-test",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )


@pytest.mark.asyncio
async def test_api_messages_never_accumulate_reminders():
    """M1 pin: in plan mode, the LAST LLM call's message list contains the
    FULL reminder at most once and the SPARSE reminder at most once — the
    old per-round append stacked one SPARSE per round."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.reminder_scheduler import _PLAN_MODE_REMINDER_SPARSE

    client = _ToolLoopClient(tool_rounds=3)
    kernel = _kernel(client)
    sc = SessionContext()
    sc.plan_mode = PlanModeState(active=True)

    result = await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "plan something"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=sc,
        )
    )

    assert result.content == "done"
    assert len(client.calls) == 4
    last_messages = client.calls[-1]["messages"]
    full_count = sum(1 for m in last_messages if m.role == "system" and "Plan Mode is active" in (m.content or ""))
    sparse_count = sum(1 for m in last_messages if (m.content or "") == _PLAN_MODE_REMINDER_SPARSE)
    assert full_count <= 1, f"FULL reminder stacked {full_count}× in one request"
    assert sparse_count <= 1, f"SPARSE reminder stacked {sparse_count}× in one request"


@pytest.mark.asyncio
async def test_first_round_request_still_carries_full_reminder():
    """Transient ≠ absent: round 1 in plan mode must SEE the FULL reminder."""
    from app.kernel.contracts import InvocationRequest

    client = _ToolLoopClient(tool_rounds=1)
    kernel = _kernel(client)
    sc = SessionContext()
    sc.plan_mode = PlanModeState(active=True)

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "plan"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=sc,
        )
    )

    first_messages = client.calls[0]["messages"]
    assert any(m.role == "system" and "Plan Mode is active" in (m.content or "") for m in first_messages)
