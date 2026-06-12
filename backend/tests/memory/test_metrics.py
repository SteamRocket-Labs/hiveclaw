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
    record_prompt_cache_metrics,
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


def test_prompt_cache_metrics_snapshot_and_prometheus_export() -> None:
    from app.memory.metrics import render_prometheus
    from app.services.prompt_cache import extract_cache_metrics

    record_prompt_cache_metrics(
        extract_cache_metrics(
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 50,
            },
            provider="anthropic",
        )
    )

    snap = snapshot()
    assert snap["prompt_cache_observations_total"]["anthropic:hit"] == 1
    assert snap["prompt_cache_read_tokens_total"]["anthropic"] == 800
    assert snap["prompt_cache_write_tokens_total"]["anthropic"] == 50
    assert snap["prompt_cache_input_tokens_total"]["anthropic"] == 1000
    assert snap["prompt_cache_hit_rate"]["anthropic"] == 0.8

    text = render_prometheus()
    assert 'hive_prompt_cache_read_tokens_total{provider="anthropic"} 800' in text
    assert 'hive_prompt_cache_hit_rate{provider="anthropic"} 0.8' in text


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


# ── P0-2c: extraction pipeline counters ──────────────────────────────


class TestExtractionMetrics:
    def setup_method(self):
        from app.memory.metrics import reset_all
        reset_all()

    def test_record_enqueue_increments_per_source(self):
        from app.memory.metrics import record_extract_enqueue, snapshot

        record_extract_enqueue("web")
        record_extract_enqueue("web")
        record_extract_enqueue("trigger")

        snap = snapshot()
        assert snap["extract_enqueue_total"] == {"web": 2, "trigger": 1}

    def test_record_enqueue_failure_buckets_by_reason(self):
        from app.memory.metrics import record_extract_enqueue_failure, snapshot

        record_extract_enqueue_failure("web", "OSError")
        record_extract_enqueue_failure("web", "OSError")
        record_extract_enqueue_failure("web", "PermissionError")

        snap = snapshot()
        assert snap["extract_enqueue_failure_total"] == {
            "web:OSError": 2,
            "web:PermissionError": 1,
        }

    def test_record_task_success_and_failure(self):
        from app.memory.metrics import (
            record_extract_task_failure,
            record_extract_task_success,
            snapshot,
        )

        record_extract_task_success("web")
        record_extract_task_success("trigger")
        record_extract_task_failure("web", "RuntimeError")

        snap = snapshot()
        assert snap["extract_task_success_total"] == {"web": 1, "trigger": 1}
        assert snap["extract_task_failure_total"] == {"web:RuntimeError": 1}

    def test_record_drain_timeout(self):
        from app.memory.metrics import record_extract_drain_timeout, snapshot

        record_extract_drain_timeout()  # default source
        record_extract_drain_timeout("session_close")

        snap = snapshot()
        assert snap["extract_drain_timeout_total"] == {"session_close": 2}

    def test_record_replay_outcome_aggregates(self):
        from app.memory.metrics import record_extract_replay_outcome, snapshot

        record_extract_replay_outcome(scheduled=3, skipped_stale=1, failed=0)
        record_extract_replay_outcome(scheduled=2, skipped_stale=0, failed=1)

        snap = snapshot()
        assert snap["extract_replay"] == {
            "scheduled": 5,
            "skipped_stale": 1,
            "failed": 1,
        }

    def test_reset_all_clears_extraction_counters(self):
        from app.memory.metrics import (
            record_extract_enqueue,
            record_extract_replay_outcome,
            record_extract_task_failure,
            reset_all,
            snapshot,
        )

        record_extract_enqueue("web")
        record_extract_task_failure("web", "OSError")
        record_extract_replay_outcome(scheduled=5, skipped_stale=2, failed=1)

        reset_all()
        snap = snapshot()
        assert snap["extract_enqueue_total"] == {}
        assert snap["extract_task_failure_total"] == {}
        assert snap["extract_replay"] == {"scheduled": 0, "skipped_stale": 0, "failed": 0}

    def test_snapshot_includes_extraction_keys_even_when_empty(self):
        from app.memory.metrics import reset_all, snapshot

        reset_all()
        snap = snapshot()
        # All P0-2c keys must be present (operator dashboards may rely on them).
        for key in (
            "extract_enqueue_total",
            "extract_enqueue_failure_total",
            "extract_task_success_total",
            "extract_task_failure_total",
            "extract_drain_timeout_total",
            "extract_replay",
        ):
            assert key in snap
        assert snap["extract_replay"] == {"scheduled": 0, "skipped_stale": 0, "failed": 0}


class TestFrozenPrefixMetrics:
    """P1-1b — frozen prompt prefix size sampling and warn/overrun counters."""

    def test_record_sample_populates_window(self):
        from app.memory.metrics import record_frozen_prefix_metering, snapshot

        record_frozen_prefix_metering(chars=1000, tokens=300)
        record_frozen_prefix_metering(chars=2000, tokens=600)

        snap = snapshot()
        assert snap["frozen_prefix_chars"]["count"] == 2
        assert snap["frozen_prefix_tokens"]["count"] == 2
        assert snap["frozen_prefix_chars"]["max"] == 2000.0
        assert snap["frozen_prefix_tokens"]["max"] == 600.0
        assert snap["frozen_prefix_warn_total"] == 0
        assert snap["frozen_prefix_overrun_total"] == 0

    def test_warn_flag_increments_warn_only(self):
        from app.memory.metrics import record_frozen_prefix_metering, snapshot

        record_frozen_prefix_metering(chars=22000, tokens=6300, warn=True)
        snap = snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 0

    def test_overrun_flag_independent_from_warn(self):
        from app.memory.metrics import record_frozen_prefix_metering, snapshot

        record_frozen_prefix_metering(chars=35000, tokens=10000, warn=True, overrun=True)
        snap = snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 1

    def test_reset_all_clears_frozen_prefix(self):
        from app.memory.metrics import (
            record_frozen_prefix_metering,
            reset_all,
            snapshot,
        )

        record_frozen_prefix_metering(chars=1000, tokens=300)
        record_frozen_prefix_metering(chars=22000, tokens=6300, warn=True, overrun=False)

        reset_all()
        snap = snapshot()
        assert snap["frozen_prefix_chars"]["count"] == 0
        assert snap["frozen_prefix_tokens"]["count"] == 0
        assert snap["frozen_prefix_warn_total"] == 0
        assert snap["frozen_prefix_overrun_total"] == 0

    def test_snapshot_includes_frozen_prefix_keys_even_when_empty(self):
        from app.memory.metrics import reset_all, snapshot

        reset_all()
        snap = snapshot()
        for key in (
            "frozen_prefix_chars",
            "frozen_prefix_tokens",
            "frozen_prefix_warn_total",
            "frozen_prefix_overrun_total",
        ):
            assert key in snap
        # Empty windows expose count=0 so dashboards can render without errors.
        assert snap["frozen_prefix_chars"] == {"count": 0}
        assert snap["frozen_prefix_tokens"] == {"count": 0}
        assert snap["frozen_prefix_warn_total"] == 0
        assert snap["frozen_prefix_overrun_total"] == 0


class TestPrometheusExport:
    def test_snapshot_and_prometheus_include_hook_failures(self):
        from app.memory.metrics import record_hook_failure, render_prometheus, snapshot

        record_hook_failure(event="response_complete", source="kernel", reason="RuntimeError")
        record_hook_failure(event="response_complete", source="kernel", reason="RuntimeError")
        record_hook_failure(event="post_tool_use", source="registry", reason="ValueError")

        snap = snapshot()
        assert snap["hook_failure_total"] == {
            "response_complete:kernel:RuntimeError": 2,
            "post_tool_use:registry:ValueError": 1,
        }

        text = render_prometheus()
        assert (
            'hive_memory_hook_failure_total{event="response_complete",source="kernel",reason="RuntimeError"} 2'
            in text
        )
        assert (
            'hive_memory_hook_failure_total{event="post_tool_use",source="registry",reason="ValueError"} 1'
            in text
        )

    def test_prometheus_export_includes_extract_failure_ratio_and_alert(self):
        from app.memory.metrics import (
            record_extract_task_failure,
            record_extract_task_success,
            render_prometheus,
        )

        for _ in range(4):
            record_extract_task_success("web")
        record_extract_task_failure("web", "RuntimeError")

        text = render_prometheus()

        assert "# HELP hive_memory_extract_task_failure_total" in text
        assert '# TYPE hive_memory_extract_failure_ratio gauge' in text
        assert 'hive_memory_extract_task_success_total{source="web"} 4' in text
        assert 'hive_memory_extract_task_failure_total{source="web",reason="RuntimeError"} 1' in text
        assert 'hive_memory_extract_failure_ratio{source="web"} 0.2' in text
        assert 'hive_memory_extract_failure_ratio_high{source="web"} 1' in text

    def test_prometheus_export_escapes_label_values(self):
        from app.memory.metrics import record_recall_error, render_prometheus

        record_recall_error('hi"nd\\sight', "tenant-a", "ConnectError")

        text = render_prometheus()

        assert 'hive_memory_recall_error_total{backend="hi\\"nd\\\\sight",tenant_id="tenant-a",reason="ConnectError"} 1' in text
