"""Tests for memory backend metrics counters."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app.memory.metrics import (
    LatencyWindow,
    RecallTimer,
    consecutive_failures,
    record_recall,
    record_recall_error,
    record_sync,
    record_sync_failure,
    reset_all,
    snapshot,
    time_since_last_failure,
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_all()
    yield
    reset_all()


# ── Counters ──────────────────────────────────────────────────


def test_record_recall_increments_total() -> None:
    record_recall("hindsight", "t1", 10.5, empty=False)
    record_recall("hindsight", "t1", 20.0, empty=True)
    snap = snapshot()
    assert snap["recall_total"]["hindsight:t1"] == 2
    assert snap["recall_empty_total"]["hindsight:t1"] == 1


def test_record_recall_error_increments_counter_and_failure_streak() -> None:
    record_recall_error("hindsight", "t1", "ConnectError")
    record_recall_error("hindsight", "t1", "ConnectError")
    record_recall_error("hindsight", "t1", "HTTPStatusError")

    snap = snapshot()
    assert snap["recall_error_total"]["hindsight:t1:ConnectError"] == 2
    assert snap["recall_error_total"]["hindsight:t1:HTTPStatusError"] == 1
    assert consecutive_failures("t1") == 3


def test_successful_recall_resets_failure_streak() -> None:
    record_recall_error("hindsight", "t1", "ConnectError")
    record_recall_error("hindsight", "t1", "ConnectError")
    assert consecutive_failures("t1") == 2

    record_recall("hindsight", "t1", 5.0, empty=False)
    assert consecutive_failures("t1") == 0


def test_time_since_last_failure_returns_none_when_never_failed() -> None:
    assert time_since_last_failure("t1") is None


def test_time_since_last_failure_is_positive_after_failure() -> None:
    record_recall_error("hindsight", "t1", "X")
    elapsed = time_since_last_failure("t1")
    assert elapsed is not None
    assert 0.0 <= elapsed < 1.0


def test_sync_counters() -> None:
    record_sync("t1", 5)
    record_sync("t1", 3)
    record_sync("t2", 7)
    record_sync_failure("t1")

    snap = snapshot()
    assert snap["sync_items_total"] == {"t1": 8, "t2": 7}
    assert snap["sync_failure_total"] == {"t1": 1}


def test_reset_all_clears_everything() -> None:
    record_recall("hindsight", "t1", 10.0, empty=False)
    record_recall_error("hindsight", "t1", "X")
    record_sync("t1", 5)
    reset_all()
    snap = snapshot()
    assert snap["recall_total"] == {}
    assert snap["recall_error_total"] == {}
    assert snap["sync_items_total"] == {}
    assert consecutive_failures("t1") == 0


# ── LatencyWindow ─────────────────────────────────────────────


def test_latency_window_empty_snapshot() -> None:
    w = LatencyWindow()
    assert w.snapshot() == {"count": 0}


def test_latency_window_percentiles() -> None:
    w = LatencyWindow()
    for i in range(1, 101):  # 1..100
        w.observe(float(i))
    snap = w.snapshot()
    assert snap["count"] == 100
    assert snap["p50"] == 51.0  # sorted[100//2]
    assert snap["p95"] == 96.0  # sorted[int(100*0.95)] = 95
    assert snap["p99"] == 100.0  # sorted[int(100*0.99)] = 99
    assert snap["max"] == 100.0


def test_latency_window_respects_max_samples() -> None:
    w = LatencyWindow(max_samples=10)
    for i in range(50):
        w.observe(float(i))
    snap = w.snapshot()
    assert snap["count"] == 10
    # Should keep the 10 most-recent: 40..49
    assert snap["max"] == 49.0


# ── RecallTimer ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_timer_records_success() -> None:
    async with RecallTimer("hindsight", "t1") as t:
        await asyncio.sleep(0.01)
        t.observed_results = 3
    snap = snapshot()
    assert snap["recall_total"]["hindsight:t1"] == 1
    assert snap["recall_empty_total"].get("hindsight:t1", 0) == 0
    lat = snap["recall_latency_ms"]["hindsight:t1"]
    assert lat["count"] == 1
    assert lat["max"] >= 10.0  # slept 10ms


@pytest.mark.asyncio
async def test_recall_timer_records_empty_result() -> None:
    async with RecallTimer("hindsight", "t1") as t:
        t.observed_results = 0
    snap = snapshot()
    assert snap["recall_empty_total"]["hindsight:t1"] == 1


@pytest.mark.asyncio
async def test_recall_timer_records_none_result_as_empty() -> None:
    async with RecallTimer("hindsight", "t1") as t:
        pass  # observed_results stays None
        assert t is not None
    snap = snapshot()
    assert snap["recall_empty_total"]["hindsight:t1"] == 1


@pytest.mark.asyncio
async def test_recall_timer_records_error_on_exception() -> None:
    with pytest.raises(ValueError):
        async with RecallTimer("hindsight", "t1"):
            raise ValueError("boom")

    snap = snapshot()
    assert snap["recall_error_total"]["hindsight:t1:ValueError"] == 1
    assert consecutive_failures("t1") == 1


@pytest.mark.asyncio
async def test_recall_timer_records_error_on_explicit_reason() -> None:
    async with RecallTimer("hindsight", "t1") as t:
        t.error_reason = "HTTPStatusError"
    snap = snapshot()
    assert snap["recall_error_total"]["hindsight:t1:HTTPStatusError"] == 1
    assert consecutive_failures("t1") == 1
