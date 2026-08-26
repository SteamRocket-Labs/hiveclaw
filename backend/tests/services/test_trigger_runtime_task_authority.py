"""Trigger RuntimeTask authority attribution — per-field unanimous-batch.

WHY THIS FILE EXISTS
--------------------
The trigger daemon creates the fired trigger's RuntimeTask ledger row with
``root_user_id``/``root_session_id`` NULL even though the trigger's own
``config`` carries the trusted authority (``created_by`` and
``root_session_id``/``confirmed_plan_session_id``, stamped by the manual-run
and tool surfaces). The runtime-task REST listing and autonomy overview filter
by ``root_user_id == current_user`` FIRST, so the executing trigger is
invisible to the very user who created it — only the operator override can
see it (fresh_2041/fresh_2102 evidence: completed trigger task with NULL
root_user_id, owner list empty).

The fix attributes the ledger row from the trigger resources themselves, but
each authority field is attributed INDEPENDENTLY and only when every trigger
in the fired batch carries the SAME non-empty value for that field (owner,
root session). Mixed or legacy fields stay NULL — mis-attributing a batch to
one user would be worse than leaving it unattributed, and any missing field
still fails the normal RuntimeTask authority check.
"""

from __future__ import annotations

import uuid

from types import SimpleNamespace

from app.services.trigger_daemon import batch_trigger_authority


def _trigger(config: dict | None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), config=config, name="t", type="once")


OWNER_A = uuid.uuid4()
OWNER_B = uuid.uuid4()
SESSION_1 = uuid.uuid4()
SESSION_2 = uuid.uuid4()


def test_unanimous_batch_attributes_owner_and_session() -> None:
    triggers = [
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_1)}),
        _trigger({"created_by": str(OWNER_A), "confirmed_plan_session_id": str(SESSION_1)}),
    ]
    root_user_id, root_session_id = batch_trigger_authority(triggers)
    assert root_user_id == OWNER_A
    assert root_session_id == SESSION_1


def test_mixed_owners_never_mis_attribute() -> None:
    triggers = [
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_1)}),
        _trigger({"created_by": str(OWNER_B), "root_session_id": str(SESSION_1)}),
    ]
    root_user_id, root_session_id = batch_trigger_authority(triggers)
    assert root_user_id is None
    # The session is still unanimous and non-empty, so it may be attributed.
    assert root_session_id == SESSION_1


def test_missing_owner_on_any_trigger_keeps_owner_none() -> None:
    triggers = [
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_1)}),
        _trigger({"root_session_id": str(SESSION_1)}),
    ]
    root_user_id, _ = batch_trigger_authority(triggers)
    assert root_user_id is None


def test_mixed_sessions_keep_session_none_but_owner_stands() -> None:
    triggers = [
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_1)}),
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_2)}),
    ]
    root_user_id, root_session_id = batch_trigger_authority(triggers)
    assert root_user_id == OWNER_A
    assert root_session_id is None


def test_legacy_batch_stays_fully_unattributed() -> None:
    triggers = [_trigger({"cron_expr": "* * * * *"}), _trigger(None)]
    root_user_id, root_session_id = batch_trigger_authority(triggers)
    assert root_user_id is None
    assert root_session_id is None


def test_empty_batch_stays_unattributed() -> None:
    assert batch_trigger_authority([]) == (None, None)


# ---------------------------------------------------------------------------
# Wiring regressions: the daemon's ledger-row creation must actually PASS the
# unanimous authority through (pure helper tests cannot prove the wiring).
# Budget/tenant dependencies are monkeypatched; the real
# _create_trigger_runtime_task body runs and create_runtime_task_record's
# kwargs are captured. No operator override is involved anywhere.
# ---------------------------------------------------------------------------


async def _captured_authority(monkeypatch, triggers) -> dict:
    import app.services.trigger_daemon as daemon

    captured: dict = {}

    async def fake_create_runtime_task_record(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    async def fake_resolve_tenant(agent_id):
        return None  # skip the budget branch; creation must not require it

    monkeypatch.setattr(daemon, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    persisted = await daemon._create_trigger_runtime_task(uuid.uuid4(), triggers)
    assert persisted is not None
    return captured


async def test_wiring_passes_unanimous_owner_and_session(monkeypatch) -> None:
    triggers = [
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_1)}),
        _trigger({"created_by": str(OWNER_A), "confirmed_plan_session_id": str(SESSION_1)}),
    ]
    captured = await _captured_authority(monkeypatch, triggers)
    assert captured["task_type"] == "trigger"
    assert captured["root_user_id"] == OWNER_A
    assert captured["root_session_id"] == str(SESSION_1)
    assert captured["delegation_chain"] == [f"agent:{captured['parent_agent_id']}"]


async def test_wiring_never_mis_attributes_mixed_owners(monkeypatch) -> None:
    triggers = [
        _trigger({"created_by": str(OWNER_A), "root_session_id": str(SESSION_1)}),
        _trigger({"created_by": str(OWNER_B), "root_session_id": str(SESSION_1)}),
    ]
    captured = await _captured_authority(monkeypatch, triggers)
    assert captured["root_user_id"] is None
    assert captured["root_session_id"] == str(SESSION_1)
    assert captured["delegation_chain"] == [f"agent:{captured['parent_agent_id']}"]


async def test_wiring_keeps_legacy_batch_unattributed(monkeypatch) -> None:
    captured = await _captured_authority(monkeypatch, [_trigger({"cron_expr": "* * * * *"}), _trigger(None)])
    assert captured["root_user_id"] is None
    assert captured["root_session_id"] is None
    assert captured["delegation_chain"] == [f"agent:{captured['parent_agent_id']}"]
