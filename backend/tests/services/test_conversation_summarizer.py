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


def test_build_summary_input_never_head_drops_before_model_compaction():
    """Provider window pressure is handled by covered chunks, not old-message deletion."""
    from app.services.conversation_summarizer import _build_summary_input

    messages = [{"role": "user", "content": f"msg-{i}-mark " + "z" * 1000} for i in range(50)]

    text, dropped = _build_summary_input(messages, provider="openai", max_input_tokens=2000)

    assert dropped == 0
    assert "msg-49-mark" in text
    assert "msg-0-mark" in text


def test_build_summary_input_keeps_long_message_and_tool_tails() -> None:
    from app.services.conversation_summarizer import _build_summary_input

    user_tail = "DECISIVE_USER_TAIL"
    tool_tail = "DECISIVE_TOOL_TAIL"
    args_tail = "DECISIVE_ARGS_TAIL"
    messages = [
        {"role": "user", "content": "u" * 20_000 + user_tail},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "execute_code", "arguments": "a" * 8_000 + args_tail}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "t" * 25_000 + tool_tail},
    ]

    text, dropped = _build_summary_input(messages, provider="openai", max_input_tokens=2_000)

    assert dropped == 0
    assert user_tail in text
    assert tool_tail in text
    assert args_tail in text


def test_summary_chunks_cover_every_input_byte_with_hash_refs() -> None:
    from app.services.conversation_summarizer import _build_summary_input_chunks

    messages = [{"role": "user", "content": f"message-{index}:" + "x" * 4_000} for index in range(8)]
    chunks = _build_summary_input_chunks(messages, provider="openai", max_input_tokens=2_000)

    assert len(chunks) > 1
    assert all(chunk.coverage_refs for chunk in chunks)
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "message-0:" in joined
    assert "message-7:" in joined
    assert all("sha256:" in ref for chunk in chunks for ref in chunk.coverage_refs)


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


async def test_llm_summarize_forwards_optional_transport_attempt_budget(monkeypatch):
    """Derived projections can bound transport retries without changing summary semantics."""

    import app.services.llm_client as llm_client_mod
    from app.services.conversation_summarizer import _llm_summarize

    captured: dict = {}

    class FakeClient:
        async def stream(self, *, messages, max_tokens, temperature, **kwargs):
            del messages, max_tokens, temperature
            captured.update(kwargs)

            class R:
                content = "<summary>bounded summary</summary>"

            return R()

        async def close(self):
            return None

    monkeypatch.setattr(llm_client_mod, "create_llm_client", lambda **_: FakeClient())

    result = await _llm_summarize(
        [{"role": "user", "content": "terminal-critical derived projection"}],
        {"provider": "openai", "model": "gpt-4o", "api_key": "k"},
        http_max_attempts=1,
    )

    assert result == "bounded summary"
    assert captured == {"_http_max_attempts": 1}


async def test_llm_summarize_hierarchically_reduces_without_oversized_reduce_prompt(monkeypatch):
    import app.services.llm_client as llm_client_mod
    from app.services.conversation_summarizer import _llm_summarize

    calls: list[str] = []

    class FakeClient:
        async def stream(self, *, messages, max_tokens, temperature):
            del max_tokens, temperature
            user_text = messages[-1].content
            calls.append(user_text)
            phase = user_text.split("<compaction_phase>", 1)[1].split("</compaction_phase>", 1)[0]

            class R:
                content = f"<summary>{phase}:" + ("s" * 2500) + "</summary>"

            return R()

        async def close(self):
            return None

    monkeypatch.setattr(llm_client_mod, "create_llm_client", lambda **_: FakeClient())
    messages = [{"role": "user", "content": f"marker-{index}:" + ("x" * 3000)} for index in range(18)]

    result = await _llm_summarize(
        messages,
        {"provider": "openai", "model": "gpt-4o", "api_key": "k", "max_input_tokens": 1000},
    )

    assert result is not None
    assert "<compaction_coverage>" in result
    assert any("reduce_level_" in call for call in calls)
    assert max(len(call) for call in calls) < 15000


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
