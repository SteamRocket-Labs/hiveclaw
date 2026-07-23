from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _ModelDB:
    def __init__(self, model: object) -> None:
        self.model = model
        self.executed: list[object] = []

    async def execute(self, statement: object) -> _ScalarResult:
        self.executed.append(statement)
        return _ScalarResult(self.model)


class _Request:
    def __init__(self, body: dict, *, trace_id: str = "trace-llm-proxy") -> None:
        self._body = body
        self.state = SimpleNamespace(trace_id=trace_id)

    async def json(self) -> dict:
        return self._body


class _JSONResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _StreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeHTTPClient:
    def __init__(self, *, json_payload: dict | None = None, stream_lines: list[str] | None = None) -> None:
        self.json_payload = json_payload
        self.stream_lines = stream_lines
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> _JSONResponse:
        self.requests.append({"url": url, "headers": headers, "json": json})
        assert self.json_payload is not None
        return _JSONResponse(self.json_payload)

    def stream(self, method: str, url: str, *, headers: dict, json: dict) -> _StreamResponse:
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})
        assert self.stream_lines is not None
        return _StreamResponse(self.stream_lines)


def _llm_model():
    return SimpleNamespace(
        model="gpt-test",
        provider="openai",
        api_key="provider-secret",
        base_url="https://provider.test/v1",
        enabled=True,
    )


def _user():
    return SimpleNamespace(id=uuid4(), tenant_id=uuid4(), role="member")


@pytest.mark.asyncio
async def test_proxy_rejects_exhausted_quota_before_opening_upstream(monkeypatch):
    from app.api import llm_proxy
    from app.services.quota_guard import QuotaExceeded

    user = _user()
    upstream_opened = False

    async def reject_quota(*_args, **_kwargs):
        raise QuotaExceeded("Daily token limit reached (100/100).", quota_type="tokens_daily")

    class _ForbiddenHTTPClient:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal upstream_opened
            upstream_opened = True

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", reject_quota, raising=False)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", _ForbiddenHTTPClient)

    with pytest.raises(HTTPException) as exc_info:
        await llm_proxy.proxy_chat_completions(
            _Request({"model": "gpt-test", "messages": [], "stream": False}),
            current_user=user,
            db=_ModelDB(_llm_model()),
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == {
        "code": "token_quota_exceeded",
        "quota_type": "tokens_daily",
        "message": "Daily token limit reached (100/100).",
    }
    assert upstream_opened is False


@pytest.mark.asyncio
async def test_nonstream_proxy_checks_distributed_limit_and_persists_provider_usage(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    client = _FakeHTTPClient(
        json_payload={
            "id": "completion-1",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        }
    )
    quota_calls: list[tuple[object, dict[str, object]]] = []
    rate_calls: list[tuple[str, int, int]] = []
    usage_calls: list[dict[str, object]] = []

    async def capture_quota(user_id, **kwargs):
        quota_calls.append((user_id, kwargs))

    async def capture_rate(key: str, limit: int, window_seconds: int):
        rate_calls.append((key, limit, window_seconds))

    async def capture_usage(**kwargs):
        usage_calls.append(kwargs)

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", capture_quota, raising=False)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", capture_rate, raising=False)
    monkeypatch.setattr(llm_proxy, "record_autonomous_llm_token_usage", capture_usage, raising=False)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", lambda **_kwargs: client)

    result = await llm_proxy.proxy_chat_completions(
        _Request(
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "work"}],
                "stream": False,
            }
        ),
        current_user=user,
        db=_ModelDB(_llm_model()),
    )

    assert result["id"] == "completion-1"
    assert quota_calls == [(user.id, {"tenant_id": user.tenant_id})]
    assert rate_calls == [(f"ratelimit:llm_proxy:{user.tenant_id}:{user.id}", 60, 60)]
    assert len(usage_calls) == 1
    assert usage_calls[0]["source"] == "desktop_llm_proxy"
    assert usage_calls[0]["tenant_id"] == user.tenant_id
    assert usage_calls[0]["user_id"] == user.id
    assert usage_calls[0]["provider"] == "openai"
    assert usage_calls[0]["model"] == "gpt-test"
    assert usage_calls[0]["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "total_tokens": 16,
    }
    assert usage_calls[0]["raise_on_error"] is True
    assert usage_calls[0]["metadata"] == {
        "request_id": "trace-llm-proxy",
        "route": "llm_proxy.chat_completions",
        "stream": False,
        "usage_source": "provider",
    }


@pytest.mark.asyncio
async def test_nonstream_proxy_withholds_success_when_usage_commit_fails(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    client = _FakeHTTPClient(
        json_payload={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"total_tokens": 8},
        }
    )

    async def noop(*_args, **_kwargs):
        return None

    async def fail_usage(**_kwargs):
        raise RuntimeError("usage ledger unavailable")

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", noop)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", noop)
    monkeypatch.setattr(llm_proxy, "record_autonomous_llm_token_usage", fail_usage)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", lambda **_kwargs: client)

    with pytest.raises(HTTPException) as exc_info:
        await llm_proxy.proxy_chat_completions(
            _Request({"model": "gpt-test", "messages": [], "stream": False}),
            current_user=user,
            db=_ModelDB(_llm_model()),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "usage_metering_unavailable"


@pytest.mark.asyncio
async def test_proxy_fails_closed_when_distributed_rate_authority_is_unavailable(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    upstream_opened = False

    async def accept_quota(*_args, **_kwargs):
        return None

    async def fail_rate(*_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    class _ForbiddenHTTPClient:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal upstream_opened
            upstream_opened = True

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", accept_quota)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", fail_rate)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", _ForbiddenHTTPClient)

    with pytest.raises(HTTPException) as exc_info:
        await llm_proxy.proxy_chat_completions(
            _Request({"model": "gpt-test", "messages": [], "stream": False}),
            current_user=user,
            db=_ModelDB(_llm_model()),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "rate_limit_unavailable"
    assert upstream_opened is False


@pytest.mark.asyncio
async def test_stream_proxy_forces_usage_and_commits_meter_before_done(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    usage = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
    lines = [
        'data: {"choices":[{"delta":{"content":"hello"}}]}',
        f"data: {json.dumps({'choices': [], 'usage': usage})}",
        "data: [DONE]",
    ]
    client = _FakeHTTPClient(stream_lines=lines)
    events: list[str] = []

    async def noop(*_args, **_kwargs):
        return None

    async def capture_usage(**kwargs):
        events.append(f"meter:{kwargs['usage']['total_tokens']}")

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", noop, raising=False)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", noop, raising=False)
    monkeypatch.setattr(llm_proxy, "record_autonomous_llm_token_usage", capture_usage, raising=False)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await llm_proxy.proxy_chat_completions(
        _Request(
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "work"}],
                "stream": True,
            }
        ),
        current_user=user,
        db=_ModelDB(_llm_model()),
    )
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
        if "[DONE]" in chunk:
            events.append("done")

    assert client.requests[0]["json"]["stream_options"] == {"include_usage": True}
    assert events == ["meter:13", "done"]
    assert "".join(chunks) == "\n\n".join(lines) + "\n\n"


@pytest.mark.asyncio
async def test_stream_proxy_replaces_done_with_typed_error_when_usage_commit_fails(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    client = _FakeHTTPClient(
        stream_lines=[
            'data: {"choices":[{"delta":{"content":"partial"}}]}',
            'data: {"choices":[],"usage":{"total_tokens":6}}',
            "data: [DONE]",
        ]
    )

    async def noop(*_args, **_kwargs):
        return None

    async def fail_usage(**_kwargs):
        raise RuntimeError("usage ledger unavailable")

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", noop)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", noop)
    monkeypatch.setattr(llm_proxy, "record_autonomous_llm_token_usage", fail_usage)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await llm_proxy.proxy_chat_completions(
        _Request({"model": "gpt-test", "messages": [], "stream": True}),
        current_user=user,
        db=_ModelDB(_llm_model()),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    stream_body = "".join(chunks)

    assert "partial" in stream_body
    assert '"code": "usage_metering_unavailable"' in stream_body
    assert "[DONE]" not in stream_body


@pytest.mark.asyncio
async def test_stream_proxy_uses_observable_estimate_when_provider_omits_usage(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    client = _FakeHTTPClient(
        stream_lines=[
            'data: {"choices":[{"delta":{"content":"你好，世界"}}]}',
            "data: [DONE]",
        ]
    )
    usage_calls: list[dict[str, object]] = []

    async def noop(*_args, **_kwargs):
        return None

    async def capture_usage(**kwargs):
        usage_calls.append(kwargs)

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", noop, raising=False)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", noop, raising=False)
    monkeypatch.setattr(llm_proxy, "record_autonomous_llm_token_usage", capture_usage, raising=False)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await llm_proxy.proxy_chat_completions(
        _Request(
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "请回答"}],
                "stream": True,
            }
        ),
        current_user=user,
        db=_ModelDB(_llm_model()),
    )
    async for _chunk in response.body_iterator:
        pass

    assert len(usage_calls) == 1
    assert usage_calls[0]["usage"]["total_tokens"] > 0
    assert usage_calls[0]["metadata"]["usage_source"] == "estimated_missing_provider_usage"
    assert usage_calls[0]["metadata"]["input_estimated_tokens"] > 0
    assert usage_calls[0]["metadata"]["output_estimated_tokens"] > 0


@pytest.mark.asyncio
async def test_stream_disconnect_still_commits_recoverable_usage_estimate(monkeypatch):
    from app.api import llm_proxy

    user = _user()
    client = _FakeHTTPClient(
        stream_lines=[
            'data: {"choices":[{"delta":{"content":"partial"}}]}',
            'data: {"choices":[{"delta":{"content":"not delivered"}}]}',
            "data: [DONE]",
        ]
    )
    usage_calls: list[dict[str, object]] = []

    async def noop(*_args, **_kwargs):
        return None

    async def capture_usage(**kwargs):
        usage_calls.append(kwargs)

    monkeypatch.setattr(llm_proxy, "check_user_token_quota", noop)
    monkeypatch.setattr(llm_proxy, "rate_limit_or_429", noop)
    monkeypatch.setattr(llm_proxy, "record_autonomous_llm_token_usage", capture_usage)
    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await llm_proxy.proxy_chat_completions(
        _Request(
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "work"}],
                "stream": True,
            }
        ),
        current_user=user,
        db=_ModelDB(_llm_model()),
    )
    iterator = response.body_iterator
    first_chunk = await anext(iterator)
    await iterator.aclose()

    assert "partial" in first_chunk
    assert len(usage_calls) == 1
    assert usage_calls[0]["metadata"]["usage_source"] == "estimated_missing_provider_usage"
    assert usage_calls[0]["usage"]["total_tokens"] > 0
