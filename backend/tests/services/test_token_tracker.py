from __future__ import annotations

import pytest


def test_estimate_tokens_from_text_keeps_legacy_ascii_ratio() -> None:
    from app.services.token_tracker import estimate_tokens_from_text

    assert estimate_tokens_from_text("a" * 350) == 100


def test_estimate_tokens_from_text_counts_cjk_near_character_count() -> None:
    from app.services.token_tracker import estimate_tokens_from_chars, estimate_tokens_from_text

    text = "中文预算校准会影响长任务上下文和费用观测" * 20

    assert estimate_tokens_from_text(text) >= int(len(text) * 0.9)
    assert estimate_tokens_from_text(text) > estimate_tokens_from_chars(len(text)) * 3


def test_estimate_tokens_from_text_handles_mixed_cjk_and_ascii() -> None:
    from app.services.token_tracker import estimate_tokens_from_text

    text = ("agent budget " * 20) + ("中文预算" * 20)

    assert estimate_tokens_from_text(text) > int(len(text) / 3.5)
    assert estimate_tokens_from_text(text) < len(text)


def test_extract_usage_tokens_excludes_deepseek_prompt_cache_hits() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "total_tokens": 139482,
                "prompt_tokens": 139154,
                "completion_tokens": 328,
                "prompt_cache_hit_tokens": 133120,
                "prompt_cache_miss_tokens": 6034,
            }
        )
        == 6362
    )


def test_extract_usage_tokens_excludes_openai_cached_prompt_tokens() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "total_tokens": 1000,
                "prompt_tokens": 900,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 700},
            }
        )
        == 300
    )


def test_extract_usage_tokens_counts_anthropic_native_usage_without_double_subtracting_cache_read() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "input_tokens": 900,
                "output_tokens": 100,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 50,
            }
        )
        == 1050
    )


def test_extract_usage_tokens_prefers_deepseek_cache_miss_when_available() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "total_tokens": 20_000,
                "completion_tokens": 123,
                "prompt_cache_hit_tokens": 18_000,
                "prompt_cache_miss_tokens": 1_877,
            }
        )
        == 2_000
    )


@pytest.mark.asyncio
async def test_direct_user_usage_updates_tenant_user_and_append_only_event(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services import token_tracker

    tenant_id = uuid4()
    user_id = uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        tokens_reset_at=None,
        tokens_used_today=3,
        tokens_used_month=4,
        tokens_used_total=5,
    )
    user = SimpleNamespace(
        id=user_id,
        tokens_reset_at=None,
        tokens_used_today=7,
        tokens_used_month=8,
        tokens_used_total=9,
    )

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        def __init__(self):
            self.values = [tenant, user]
            self.added = []
            self.committed = False

        async def execute(self, _statement):
            return _Result(self.values.pop(0))

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            self.committed = True

    db = _DB()

    @asynccontextmanager
    async def fake_tenant_scoped_session(resolved_tenant_id):
        assert resolved_tenant_id == tenant_id
        yield db

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_scoped_session)

    await token_tracker.record_autonomous_llm_token_usage(
        source="desktop_llm_proxy",
        usage={"total_tokens": 12},
        provider="openai",
        model="gpt-test",
        tenant_id=tenant_id,
        user_id=user_id,
        metadata={"request_id": "trace-1"},
        raise_on_error=True,
    )

    assert tenant.tokens_used_today == 15
    assert tenant.tokens_used_month == 16
    assert tenant.tokens_used_total == 17
    assert user.tokens_used_today == 19
    assert user.tokens_used_month == 20
    assert user.tokens_used_total == 21
    assert db.committed is True
    assert len(db.added) == 1
    event = db.added[0]
    assert event.tenant_id == tenant_id
    assert event.user_id == user_id
    assert event.source == "desktop_llm_proxy"
    assert event.tokens == 12


@pytest.mark.asyncio
async def test_direct_usage_strict_mode_does_not_swallow_persistence_failure(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from uuid import uuid4

    from app.services import token_tracker

    @asynccontextmanager
    async def failing_tenant_scoped_session(_tenant_id):
        raise RuntimeError("usage ledger unavailable")
        yield

    monkeypatch.setattr("app.database.tenant_scoped_session", failing_tenant_scoped_session)

    with pytest.raises(RuntimeError, match="usage ledger unavailable"):
        await token_tracker.record_autonomous_llm_token_usage(
            source="desktop_llm_proxy",
            usage={"total_tokens": 12},
            tenant_id=uuid4(),
            user_id=uuid4(),
            raise_on_error=True,
        )
