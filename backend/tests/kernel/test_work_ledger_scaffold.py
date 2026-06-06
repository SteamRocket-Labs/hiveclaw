"""切口② contract: Work Ledger as a general-path cognitive scaffold.

docs/agent-task-cognitive-scaffold.md §5.3 Delta-2 / §9 acceptance 2-3:

* the kernel injects a per-round Work Ledger reminder ONLY when the invoker has
  flagged the turn complex (``metadata["work_ledger_enabled"]``) — zero overhead
  on simple Q&A, and suppressed while Plan Mode is active so the two reminders
  never conflict (§8 invariant);
* after a context compaction the kernel re-injects the 5-question reboot
  (where am I / what's open / what's verified / what failed / what's pending)
  from the persisted ledger, so the agent resumes from the next open todo instead
  of repeating a dead end.

The reminder-gating tests target the pure decision function (no kernel mocks).
The reboot test exercises the real ledger service writing to a temp data_root
(no mocked service, per the testing discipline) and reads it back through the
real ``_build_restoration_context``.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


# ── Reminder eligibility gate (pure; throttling moved to the scheduler) ──────
# T-G1: the per-round injection decision lives in kernel/reminder_scheduler.py
# (behavioral throttling pinned in test_runtime_reminder_scheduler.py). These
# tests pin the ELIGIBILITY gate (M7: flag answers "may this run see ledger
# reminders at all") and the load-bearing reminder text.


def test_eligibility_is_false_without_session_context():
    from app.kernel.reminder_scheduler import _ledger_eligible

    assert _ledger_eligible(None) is False


def test_eligibility_is_false_when_flag_absent_or_false():
    from app.kernel.reminder_scheduler import _ledger_eligible
    from app.runtime.session import SessionContext

    # No metadata flag at all → simple Q&A default, not eligible.
    assert _ledger_eligible(SessionContext()) is False

    sc = SessionContext()
    sc.metadata = {"work_ledger_enabled": False}
    assert _ledger_eligible(sc) is False


def test_reminder_text_teaches_the_ledger_tools():
    from app.kernel.reminder_scheduler import _WORK_LEDGER_REMINDER, _ledger_eligible
    from app.runtime.session import SessionContext

    sc = SessionContext()
    sc.metadata = {"work_ledger_enabled": True}
    assert _ledger_eligible(sc) is True
    # The reminder must teach the three 切口① tools as working memory.
    assert "track_todo" in _WORK_LEDGER_REMINDER
    assert "record_finding" in _WORK_LEDGER_REMINDER
    assert "read_ledger" in _WORK_LEDGER_REMINDER
    # And reassert the cognitive≠execution invariant.
    assert "never starts execution" in _WORK_LEDGER_REMINDER


def test_eligibility_suppressed_while_plan_mode_active():
    # §8 invariant: the ledger reminder and the plan-mode reminder coexist
    # without conflict. Planning is read-only / no-execution, so a "track your
    # execution todos" nudge is suppressed there.
    from app.kernel.reminder_scheduler import _ledger_eligible
    from app.runtime.session import PlanModeState, SessionContext

    sc = SessionContext()
    sc.metadata = {"work_ledger_enabled": True}
    sc.plan_mode = PlanModeState(active=True)
    assert _ledger_eligible(sc) is False


# ── Post-compaction reboot injection (real service + temp data_root) ─────────


def _write_complex_ledger(agent_id, data_root):
    """Author a realistic in-progress ledger with the real service primitives."""
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_finding,
        upsert_agent_work_ledger_todo,
    )

    done = upsert_agent_work_ledger_todo(
        agent_id=agent_id, title="Survey the existing migration scripts", data_root=data_root
    )
    upsert_agent_work_ledger_todo(
        agent_id=agent_id, item_id=done["item"]["id"], status="completed", data_root=data_root
    )
    upsert_agent_work_ledger_todo(
        agent_id=agent_id, title="Write the up/down migration", status="in_progress", data_root=data_root
    )
    upsert_agent_work_ledger_todo(
        agent_id=agent_id, title="Run the migration against a temp DB", status="pending", data_root=data_root
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="alembic heads is single — safe to autogenerate",
        trust="verified",
        data_root=data_root,
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="failure",
        summary="autogenerate dropped an unrelated index",
        next_strategy="hand-edit the revision to scope only the new table",
        data_root=data_root,
    )


def test_restoration_injects_ledger_reboot_on_complex_turn(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)
    # _build_restoration_context only proceeds past soul/focus if `parts` is
    # non-empty; soul.md is the anchor that the rest of the context appends to.
    (workspace / "soul.md").write_text("# Identity\nMigration assistant.", encoding="utf-8")

    _write_complex_ledger(agent_id, tmp_path)

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    session = SessionContext()
    session.metadata = {"work_ledger_enabled": True}

    restored = _build_restoration_context(agent_id, session_context=session)

    # The 5-question reboot must be present with the live ledger state.
    assert "Work Ledger — Reboot After Compaction" in restored
    assert "in_progress" not in restored  # rendered as prose, not raw status dumps
    # current phase / open todos / verified findings / failures / pending verify
    assert "Write the up/down migration" in restored  # open required todo
    assert "Run the migration against a temp DB" in restored  # open required todo
    assert "Survey the existing migration scripts" not in restored  # completed → not "open"
    assert "alembic heads is single" in restored  # verified finding
    assert "autogenerate dropped an unrelated index" in restored  # failure → do not repeat
    assert "continue from the next open todo" in restored


def test_restoration_skips_ledger_reboot_when_turn_not_complex(tmp_path, monkeypatch):
    """Simple turns pay zero cost: the ledger file is not even read."""
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)
    (workspace / "soul.md").write_text("# Identity\nMigration assistant.", encoding="utf-8")

    _write_complex_ledger(agent_id, tmp_path)  # ledger exists on disk...

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    session = SessionContext()  # ...but the turn is not flagged complex
    restored = _build_restoration_context(agent_id, session_context=session)

    assert "Work Ledger — Reboot After Compaction" not in restored
    # Soul still restored — only the ledger block is gated off.
    assert "Migration assistant." in restored


def test_restoration_no_ledger_block_when_no_ledger_file(tmp_path, monkeypatch):
    """Complex turn but no ledger written yet → no empty reboot block."""
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)
    (workspace / "soul.md").write_text("# Identity\nHelper.", encoding="utf-8")

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    session = SessionContext()
    session.metadata = {"work_ledger_enabled": True}
    restored = _build_restoration_context(agent_id, session_context=session)

    assert "Work Ledger — Reboot After Compaction" not in restored
    assert "Helper." in restored
