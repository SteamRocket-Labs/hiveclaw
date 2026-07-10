"""Tests for state-first conversation compaction summaries."""

from __future__ import annotations


# ── P0: full history into summary LLM (docs/compaction-cc-alignment.md §3 P0) ──


def test_build_summary_input_keeps_full_history():
    """>40 messages must ALL reach the summary LLM — the [-40:] pruning is removed."""
    from app.services.conversation_summarizer import _build_summary_input

    messages = [{"role": "user", "content": f"message number {i} marker-{i}-end"} for i in range(80)]

    text, dropped = _build_summary_input(messages, provider="openai")

    assert "marker-0-end" in text  # oldest message survives (old code dropped all but last 40)
    assert "marker-79-end" in text
    assert dropped == 0


def test_build_summary_input_relaxed_user_message_cap():
    """Per-message caps are defensive high limits, not routine pruning (was 800 chars)."""
    from app.services.conversation_summarizer import _build_summary_input

    big = "x" * 5000
    text, _ = _build_summary_input([{"role": "user", "content": big}], provider="openai")

    assert big in text


def test_build_summary_input_relaxed_tool_result_cap():
    """Tool results keep up to the defensive cap (was 1500 chars)."""
    from app.services.conversation_summarizer import _build_summary_input

    big = "y" * 6000
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": big},
    ]

    text, _ = _build_summary_input(messages, provider="openai")

    assert big in text


def test_build_summary_input_head_drop_only_when_over_window():
    """Mechanical truncation appears ONLY as over-window fallback: drop oldest first
    (CC truncateHeadForPTLRetry philosophy), keep the most recent messages intact."""
    from app.services.conversation_summarizer import _build_summary_input

    messages = [{"role": "user", "content": f"msg-{i}-mark " + "z" * 1000} for i in range(50)]

    text, dropped = _build_summary_input(messages, provider="openai", max_input_tokens=2000)

    assert dropped > 0
    assert "msg-49-mark" in text  # newest kept
    assert "msg-0-mark" not in text  # oldest dropped


def test_estimate_tokens_counts_cjk_text_without_ascii_underestimate() -> None:
    from app.services.conversation_summarizer import estimate_tokens

    content = "中文预算校准会影响压缩触发和上下文保护" * 100

    assert estimate_tokens([{"role": "user", "content": content}], provider="openai") >= int(len(content) * 0.9)


async def test_llm_summarize_sends_full_history_and_raised_max_tokens(monkeypatch):
    """Integration: _llm_summarize sends full history and max_tokens >= 8000 (was 2500).

    Test Double rationale: LLM client is an external API boundary.
    """
    import app.services.llm_client as llm_client_mod
    from app.services.conversation_summarizer import _llm_summarize

    captured: dict = {}

    class FakeClient:
        async def stream(self, *, messages, max_tokens, temperature):
            captured["max_tokens"] = max_tokens
            captured["user_text"] = messages[-1].content

            class R:
                content = "<summary>ok</summary>"

            return R()

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(llm_client_mod, "create_llm_client", lambda **_: FakeClient())

    messages = [{"role": "user", "content": f"unique-marker-{i}-end"} for i in range(60)]
    out = await _llm_summarize(messages, {"provider": "openai", "model": "gpt-4o", "api_key": "k"})

    assert out == "ok"
    assert captured["max_tokens"] >= 8000
    assert "unique-marker-0-end" in captured["user_text"]  # old code kept only last 40
    assert captured["closed"] is True


# ``_extract_summary`` (mechanical 11-section fallback) and its helpers were
# removed as dead code (B-6) — they had no live caller. Compaction now runs
# only through the LLM summarizer path, so no replacement test is added here.


def test_dead_mechanical_extract_helpers_are_removed():
    """B-6 revert guard: the unused mechanical-compression helpers must stay
    deleted (no live caller). The LLM-path keeper ``_extract_summary_from_response``
    must remain importable."""
    import app.services.conversation_summarizer as cs

    for dead in (
        "_extract_summary",
        "_extract_decisions",
        "_extract_pending",
        "_extract_artifacts",
        "_extract_preferences",
        "_extract_tool_summary",
    ):
        assert not hasattr(cs, dead), f"{dead} was deleted as dead code (B-6); do not reintroduce"

    # Keepers survive.
    assert hasattr(cs, "_extract_summary_from_response")
    assert hasattr(cs, "estimate_tokens")
