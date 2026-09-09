"""Maintained regressions for the settlement retry lane's fairness contract.

Seventh correction (CC6 B1): the lane's ordering must be a real rotation —
the chronic rank advances with every failed attempt — and continuously
arriving fresh debt must leave a reserved share of the attempt budget for
chronic candidates. Per-invocation READS are bounded by a durable
session-scoped scan cursor that wraps, and a replaced pending receipt must
preserve the rotation counter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_terminal_runs(
    owner_sessionmaker,
    seed: dict,
    *,
    count: int,
    receipt: dict | None,
    base: datetime,
    prefix: str,
) -> list[uuid.UUID]:
    from app.models.runtime_task import RuntimeTask

    ids: list[uuid.UUID] = []
    async with owner_sessionmaker() as db:
        for index in range(count):
            run_id = uuid.uuid4()
            ids.append(run_id)
            metadata: dict = {}
            if receipt is not None:
                metadata["session_v2_token_settlement"] = dict(receipt)
            db.add(
                RuntimeTask(
                    id=run_id,
                    task_type="web_chat_turn",
                    status="completed",
                    parent_agent_id=seed["agent_id"],
                    child_agent_id=seed["agent_id"],
                    tenant_id=seed["tenant_id"],
                    parent_session_id=str(seed["session_id"]),
                    child_session_id=str(seed["session_id"]),
                    root_user_id=seed["user_id"],
                    root_session_id=str(seed["session_id"]),
                    prompt=f"{prefix} {index}",
                    created_at=base + timedelta(seconds=index),
                    completed_at=base + timedelta(seconds=index),
                    metadata_json=metadata,
                )
            )
        await db.commit()
    return ids


def test_chronic_rank_is_a_rotation_not_a_single_promotion() -> None:
    """Every failed chronic attempt must advance the ordering position."""

    import app.services.web_chat_runtime as runtime

    now = datetime.now(UTC)
    rid = uuid.uuid4()
    ranks = {
        "no_receipt": runtime._settlement_candidate_sort_key({}, created_at=now, run_id=rid)[0],
        "receipt_no_counter": runtime._settlement_candidate_sort_key({"pending": True}, created_at=now, run_id=rid)[0],
        "first_chronic": runtime._settlement_candidate_sort_key(
            {"pending": True, "retry_attempts": 2}, created_at=now, run_id=rid
        )[0],
        "many_attempts": runtime._settlement_candidate_sort_key(
            {"pending": True, "retry_attempts": 97}, created_at=now, run_id=rid
        )[0],
    }
    assert ranks["first_chronic"] > ranks["receipt_no_counter"], ranks
    assert ranks["many_attempts"] > ranks["first_chronic"], (
        "the chronic ordering position must keep advancing with the durable attempt "
        f"counter, otherwise a full budget of permanent failures freezes the lane: {ranks}"
    )


async def test_healthy_chronic_candidate_rotates_behind_permanent_failures(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """>32 permanently failing chronic items; a newer healthy chronic item is reached.

    The healthy item's durable retry counter is seeded HIGHER than every
    filler's, so its ordering key sorts strictly behind the whole permanent-
    failure budget: reaching it requires the rotation itself, not first
    position (CC7 F9 — the previous seeding made it sort first).
    """

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    run_a = seed["run_id"]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_a)
        task.status = "completed"
        task.completed_at = task.completed_at or datetime.now(UTC)
        metadata = dict(task.metadata_json or {})
        entry = dict(metadata.get("session_v2_token_settlement") or {})
        entry.update({"pending": True, "suppressed": 1, "retry_attempts": 7})
        metadata["session_v2_token_settlement"] = entry
        task.metadata_json = metadata
        await db.commit()

    chronic_ids = set(
        await _seed_terminal_runs(
            owner_sessionmaker,
            seed,
            count=40,
            receipt={"pending": True, "retry_attempts": 4, "error": "settlement_unavailable"},
            base=datetime.now(UTC) - timedelta(days=3),
            prefix="chronic filler",
        )
    )
    filler_rank = runtime._settlement_candidate_sort_key(
        {"pending": True, "retry_attempts": 4}, created_at=datetime.now(UTC), run_id=uuid.uuid4()
    )[0]
    healthy_rank = runtime._settlement_candidate_sort_key(
        {"pending": True, "retry_attempts": 7}, created_at=datetime.now(UTC), run_id=run_a
    )[0]
    assert healthy_rank > filler_rank, "seeding must place the healthy candidate BEHIND the fillers"

    attempted: list[uuid.UUID] = []
    real_settle = runtime._settle_completed_run_token_usage

    async def _selective_settle(run_uuid, *, passes: int = 1):
        attempted.append(run_uuid)
        if run_uuid in chronic_ids:
            raise RuntimeError("maintained: permanently unavailable settlement")
        return await real_settle(run_uuid, passes=passes)

    monkeypatch.setattr(runtime, "_settle_completed_run_token_usage", _selective_settle)

    for _ in range(6):
        await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])

    run_a_attempts = sum(1 for value in attempted if value == run_a)
    assert run_a_attempts >= 1, (
        "a chronic-but-settleable candidate behind a full budget of permanent chronic "
        f"failures was never attempted across 6 invocations ({len(attempted)} attempts, "
        f"{len(set(attempted))} distinct runs)"
    )
    assert len(set(attempted)) > 32, (
        "the same oldest chronic failures consumed every invocation's budget: "
        f"only {len(set(attempted))} distinct runs were attempted"
    )


async def test_continuous_fresh_arrivals_leave_a_chronic_reservation(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """Fresh waves arrive every turn; chronic debt must still be revisited."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    run_a = seed["run_id"]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_a)
        metadata = dict(task.metadata_json or {})
        entry = dict(metadata.get("session_v2_token_settlement") or {})
        task.status = "completed"
        task.completed_at = task.completed_at or datetime.now(UTC)
        entry.update({"pending": True, "suppressed": 1, "retry_attempts": 3})
        metadata["session_v2_token_settlement"] = entry
        task.metadata_json = metadata
        await db.commit()

    attempted: list[uuid.UUID] = []
    real_settle = runtime._settle_completed_run_token_usage
    fresh_ids: set[uuid.UUID] = set()

    async def _selective_settle(run_uuid, *, passes: int = 1):
        attempted.append(run_uuid)
        if run_uuid in fresh_ids:
            raise RuntimeError("maintained: fresh arrival cannot settle")
        return await real_settle(run_uuid, passes=passes)

    monkeypatch.setattr(runtime, "_settle_completed_run_token_usage", _selective_settle)

    base = datetime.now(UTC) - timedelta(days=1)
    for wave in range(4):
        fresh_ids.update(
            await _seed_terminal_runs(
                owner_sessionmaker,
                seed,
                count=40,
                receipt=None,
                base=base + timedelta(hours=wave + 1),
                prefix=f"fresh wave {wave}",
            )
        )
        await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])

    run_a_attempts = sum(1 for value in attempted if value == run_a)
    assert run_a_attempts >= 1, (
        "continuously arriving fresh debt consumed the whole attempt budget on every "
        f"invocation; the older suppressed charge was never re-attempted ({len(attempted)} attempts)"
    )


async def test_reads_are_bounded_by_the_durable_scan_cursor(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """Per-invocation reads are page-bounded; the cursor still reaches late debt."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    import app.services.web_chat_runtime as runtime

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    page_rows = 10
    monkeypatch.setattr(runtime, "_PENDING_SESSION_SETTLEMENT_PAGE_ROWS", page_rows)

    # 90 fully settled historical rows, then one real candidate at the end.
    await _seed_terminal_runs(
        owner_sessionmaker,
        seed,
        count=90,
        receipt={"pending": False, "settled": 1},
        base=datetime.now(UTC) - timedelta(days=2),
        prefix="settled history",
    )
    late_ids = await _seed_terminal_runs(
        owner_sessionmaker,
        seed,
        count=1,
        receipt=None,
        base=datetime.now(UTC) + timedelta(seconds=5),
        prefix="late candidate",
    )
    late_run = late_ids[0]

    scanned_per_invocation: list[int] = []
    real_key = runtime._settlement_candidate_sort_key

    def counting_key(entry, *, created_at, run_id):
        scanned_per_invocation[-1] += 1
        return real_key(entry, created_at=created_at, run_id=run_id)

    attempted: list[uuid.UUID] = []
    real_settle = runtime._settle_completed_run_token_usage

    async def _settle_spy(run_uuid, *, passes: int = 1):
        attempted.append(run_uuid)
        return await real_settle(run_uuid, passes=passes)

    monkeypatch.setattr(runtime, "_settlement_candidate_sort_key", counting_key)
    monkeypatch.setattr(runtime, "_settle_completed_run_token_usage", _settle_spy)

    max_pages = runtime._PENDING_SESSION_SETTLEMENT_MAX_PAGES
    for invocation in range(12):
        scanned_per_invocation.append(0)
        await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
        if late_run in attempted:
            break
    assert late_run in attempted, (
        "the bounded scan never reached the newest debt: the durable cursor must wrap so "
        "later invocations cover the whole session history"
    )
    assert all(rows <= max_pages * page_rows for rows in scanned_per_invocation), (
        f"an invocation read more than the page bound: {scanned_per_invocation}"
    )


async def test_cursor_interruption_keeps_the_lane_correct(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """A corrupt/lost cursor costs one redundant pass, never a lost charge."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    import app.services.web_chat_runtime as runtime
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    # Seed the cursor with an unusable value (interrupted/corrupt write shape).
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, seed["session_id"])
        metadata = dict(session.transcript_metadata_json or {})
        metadata[runtime._SETTLEMENT_SCAN_CURSOR_METADATA_KEY] = {"last_created_at": "not-a-date"}
        session.transcript_metadata_json = metadata
        await db.commit()

    real_settle = runtime._settle_completed_run_token_usage
    attempted: list[uuid.UUID] = []

    async def _settle_spy(run_uuid, *, passes: int = 1):
        attempted.append(run_uuid)
        return await real_settle(run_uuid, passes=passes)

    monkeypatch.setattr(runtime, "_settle_completed_run_token_usage", _settle_spy)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        task.status = "completed"
        task.completed_at = task.completed_at or datetime.now(UTC)
        await db.commit()
    await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
    # The seed run (the only candidate) was still discovered despite the corrupt cursor.
    assert seed["run_id"] in attempted


async def test_pending_receipt_replacement_preserves_rotation_counter(owner_sessionmaker, tmp_path) -> None:
    """A suppressed receipt rewrite must not erase the durable rotation fact."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        metadata = dict(task.metadata_json or {})
        metadata["session_v2_token_settlement"] = {
            "pending": True,
            "suppressed": 1,
            "retry_attempts": 5,
        }
        task.metadata_json = metadata
        await db.commit()

    await runtime._persist_completed_run_settlement_receipt(seed["run_id"], seed["tenant_id"], {"suppressed": 1})

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        entry: dict[str, Any] = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert bool(entry.get("pending")) is True
    assert int(entry.get("retry_attempts") or 0) == 5, (
        f"the rotation counter was erased by the pending receipt rewrite: {entry}"
    )


async def test_scan_cursor_is_not_pinned_by_a_rank_outlier_at_the_window_head(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Enumeration progress must not wait on historic attempt counts (CC7 F4).

    The read window's OLDEST candidate carries a huge durable retry counter
    (a long-lived session's accumulated failures), all other window
    candidates carry low counters, and a healthy settleable candidate sits
    OUTSIDE the first read window. The durable cursor must advance with
    enumeration alone — the outlier delays its own ATTEMPT (rank ordering),
    never the scan position — so later invocations enumerate past the window
    and reach the outside debt.
    """

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    page_rows = 10
    monkeypatch.setattr(runtime, "_PENDING_SESSION_SETTLEMENT_PAGE_ROWS", page_rows)

    # 40 permanent-failure candidates fill the whole 4-page read window; the
    # OLDEST one is the rank outlier.
    window_ids = await _seed_terminal_runs(
        owner_sessionmaker,
        seed,
        count=40,
        receipt={"pending": True, "retry_attempts": 3, "error": "settlement_unavailable"},
        base=datetime.now(UTC) - timedelta(days=2),
        prefix="window filler",
    )
    async with owner_sessionmaker() as db:
        outlier = await db.get(RuntimeTask, window_ids[0])
        metadata = dict(outlier.metadata_json or {})
        entry = dict(metadata.get("session_v2_token_settlement") or {})
        entry["retry_attempts"] = 500
        metadata["session_v2_token_settlement"] = entry
        outlier.metadata_json = metadata
        await db.commit()

    healthy_ids = await _seed_terminal_runs(
        owner_sessionmaker,
        seed,
        count=1,
        receipt=None,
        base=datetime.now(UTC) + timedelta(seconds=5),
        prefix="healthy outside window",
    )
    healthy_run = healthy_ids[0]
    chronic_ids = set(window_ids)

    attempted: list[uuid.UUID] = []
    real_settle = runtime._settle_completed_run_token_usage

    async def _selective_settle(run_uuid, *, passes: int = 1):
        attempted.append(run_uuid)
        if run_uuid in chronic_ids:
            raise RuntimeError("maintained: permanently unavailable settlement")
        return await real_settle(run_uuid, passes=passes)

    monkeypatch.setattr(runtime, "_settle_completed_run_token_usage", _selective_settle)

    cursor_values: list[object] = []
    real_persist = runtime._persist_settlement_scan_cursor

    async def _cursor_spy(tenant_id, *, session_id, cursor):
        cursor_values.append(cursor)
        return await real_persist(tenant_id, session_id=session_id, cursor=cursor)

    monkeypatch.setattr(runtime, "_persist_settlement_scan_cursor", _cursor_spy)

    for _ in range(20):
        await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
        if healthy_run in attempted:
            break

    assert healthy_run in attempted, (
        "debt outside the read window was never enumerated: the durable cursor stayed "
        f"pinned by the window-head rank outlier (cursor values seen: {cursor_values!r}; "
        f"{len(set(attempted))} distinct runs attempted)"
    )
    assert len(set(map(str, cursor_values))) > 1, (
        f"the scan cursor never advanced across invocations: {cursor_values!r}"
    )
    # The durable cursor recorded real progress along the way (a wrap at the
    # end of history legitimately clears the key back to the oldest rows).
    assert any(value is not None for value in cursor_values), cursor_values
