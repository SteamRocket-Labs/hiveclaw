"""CCPlus V1 latency-hiding exclusion contract (U6-a1-exclusion).

A1 SILENT ABSENCE — D-05 (StreamingToolExecutor, mid-stream tool execution) and
D-23 (memory/skill prefetch overlap + async tool-use summary) — are CC-LOCAL
latency-HIDING optimizations, NOT correctness requirements. Hive deliberately
defers them as MAY-adopt Codex-class engineering deltas, to be revisited as a
Hive-native optimization (see
``docs/ccplus-v1-latency-hiding-exclusion-2026-06-24.md``).

These tests turn that silent gap into an EXPLICIT, TRACKED exclusion contract.
Reconciliation §5 (line 168) mandates a renamed contract/exclusion test so the
absence is proven intentional rather than overlooked. The acceptance selector

    pytest -k "streaming_tool_executor or latency_hiding or skill_prefetch or memory_prefetch"

must COLLECT the test functions below, hence the function names embed those tokens.

Revert sensitivity (the wiring being proven is the exclusion ruling itself, whose
live consumer is the actual kernel runtime path ``AgentKernel.handle`` + the
``on_chunk`` streaming callback):

  * If someone reverts the exclusion by implementing ``StreamingToolExecutor`` /
    rewriting the hot loop to do mid-stream execution, the symbol-absence and the
    buffered-async (coroutine, not async-generator) assertions fail.
  * If the exclusion doc is deleted, the doc assertions fail.
"""

from __future__ import annotations

import inspect
from dataclasses import fields as dataclass_fields
from pathlib import Path

import app.kernel.engine as kernel_engine
from app.kernel.contracts import InvocationRequest, InvocationResult
from app.kernel.engine import AgentKernel

# Repo layout: this file lives at backend/tests/kernel/, so the repo root is
# parents[3] and the CCPlus V1 docs live in the repo-root docs/ directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXCLUSION_DOC = _REPO_ROOT / "docs" / "ccplus-v1-latency-hiding-exclusion-2026-06-24.md"


def test_kernel_loop_is_buffered_async_not_streaming_tool_executor() -> None:
    """D-05: the live loop is buffered generate-then-execute, not mid-stream.

    ``AgentKernel.handle`` is the real runtime entry every invocation flows
    through. The exclusion ruling depends on it being a *buffered* async method
    that returns an ``InvocationResult`` -- NOT an async generator that could
    interleave tool execution into the model stream (which is what a real
    ``StreamingToolExecutor`` rewrite would require). These assertions fail the
    moment that rewrite lands, making the exclusion revert-sensitive.
    """
    handle = AgentKernel.handle

    # Buffered async coroutine, not an async generator (no mid-stream interleave).
    assert inspect.iscoroutinefunction(handle), (
        "AgentKernel.handle must be a buffered async coroutine; the latency-hiding "
        "exclusion assumes round-based generate-then-execute."
    )
    assert not inspect.isasyncgenfunction(handle), (
        "AgentKernel.handle must NOT be an async generator. An async generator "
        "would be the signature of mid-stream StreamingToolExecutor execution, "
        "which the D-05 exclusion deliberately defers."
    )

    # The buffered loop returns a completed InvocationResult, not a stream.
    # engine.py uses ``from __future__ import annotations`` (PEP 563), so the
    # annotation is stored as the string name; compare against that form.
    return_annotation = inspect.signature(handle).return_annotation
    assert return_annotation in (InvocationResult, "InvocationResult"), (
        f"AgentKernel.handle must return InvocationResult, got {return_annotation!r}"
    )

    # The deliberate absence: no StreamingToolExecutor symbol exists to import.
    assert not hasattr(kernel_engine, "StreamingToolExecutor"), (
        "StreamingToolExecutor is deliberately absent (D-05 exclusion). If this "
        "symbol reappears, the exclusion ruling has been reverted without updating "
        "the contract."
    )


def test_no_streaming_tool_executor_symbol_in_kernel_sources() -> None:
    """D-05: scan the real kernel sources to document the deliberate absence.

    A grep-equivalent over the live engine/contracts source. Re-introducing the
    symbol (the canonical revert of the exclusion) makes this fail, so the
    exclusion is tracked rather than silently re-openable.
    """
    engine_src = Path(inspect.getfile(kernel_engine)).read_text(encoding="utf-8")
    contracts_src = Path(inspect.getfile(InvocationRequest)).read_text(encoding="utf-8")

    assert "StreamingToolExecutor" not in engine_src, (
        "StreamingToolExecutor must not exist in app/kernel/engine.py — its absence "
        "is the D-05 latency-hiding exclusion."
    )
    assert "StreamingToolExecutor" not in contracts_src, (
        "StreamingToolExecutor must not exist in app/kernel/contracts.py — its "
        "absence is the D-05 latency-hiding exclusion."
    )


def test_latency_hiding_streaming_ux_uses_callback_not_mid_stream_execution() -> None:
    """D-05/D-23: streaming UX is delivered via the on_chunk callback.

    The exclusion's correctness claim is that the GOAL -- streaming UX -- is met
    WITHOUT mid-stream tool execution, because model text streams incrementally
    through the ``on_chunk`` callback on the live ``InvocationRequest``. If that
    callback is removed (forcing a streaming rewrite), this fails.
    """
    field_names = {f.name for f in dataclass_fields(InvocationRequest)}
    assert "on_chunk" in field_names, (
        "InvocationRequest.on_chunk is the buffered-async streaming-UX callback "
        "that makes mid-stream StreamingToolExecutor unnecessary for V1."
    )


def test_skill_prefetch_and_memory_prefetch_overlap_deliberately_excluded() -> None:
    """D-23: prefetch overlap + async tool-use summary are excluded, documented.

    There is no prefetch-overlap scheduler symbol on the kernel; the doc records
    why (latency-only Codex-class delta). Asserting the doc carries the D-23
    binding keeps the skill/memory prefetch exclusion explicit and tracked.
    """
    # No latency-hiding prefetch-overlap scheduler is wired onto the kernel engine.
    for symbol in ("PrefetchScheduler", "ToolUseSummaryPrefetcher", "StreamingPrefetch"):
        assert not hasattr(kernel_engine, symbol), (
            f"{symbol} must be absent — D-23 prefetch/overlap latency hiding is deliberately deferred for V1."
        )

    assert _EXCLUSION_DOC.is_file(), "D-23 exclusion ruling doc must exist."
    doc_text = _EXCLUSION_DOC.read_text(encoding="utf-8")
    assert "D-23" in doc_text
    assert "prefetch" in doc_text
    assert "memory" in doc_text and "skill" in doc_text


def test_latency_hiding_exclusion_doc_binds_to_north_star() -> None:
    """The exclusion is a North-Star-bound ruling, not a silent gap.

    Asserts the ruling doc exists and binds D-05/D-23 to the CCPlus North Star as
    MAY-adopt Codex engineering deltas explicitly excluded for V1.
    """
    assert _EXCLUSION_DOC.is_file(), (
        "docs/ccplus-v1-latency-hiding-exclusion-2026-06-24.md must exist — the "
        "exclusion ruling is the artifact that converts the A1 silent absence into "
        "an explicit, tracked exclusion."
    )
    doc_text = _EXCLUSION_DOC.read_text(encoding="utf-8")

    # Both excluded deltas are named.
    assert "D-05" in doc_text
    assert "StreamingToolExecutor" in doc_text

    # North-Star binding: MAY-adopt Codex engineering delta, explicitly excluded.
    assert "MAY-adopt" in doc_text
    assert "Codex" in doc_text
    assert "North Star" in doc_text or "北极星" in doc_text

    # It is framed as an exclusion ruling, not a silent gap.
    lowered = doc_text.lower()
    assert "exclusion ruling" in lowered or "排除裁决" in doc_text
    assert "silent gap" in lowered or "沉默缺口" in doc_text
