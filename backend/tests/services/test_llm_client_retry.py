from __future__ import annotations

import httpx
import pytest


class _FakePostClient:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def post(self, url: str, *, json: dict, headers: dict) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self.responses:
            raise AssertionError("no response prepared")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeStreamResponse:
    status_code = 429
    headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    async def aiter_bytes(self):
        yield b'{"error":{"type":"rate_limit_error"}}'


class _FakeStreamClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeStreamResponse()


def _response(status_code: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(status_code, text=f"status {status_code}", headers=headers)


@pytest.mark.asyncio
async def test_post_with_status_retries_uses_ten_rate_limit_rejections_and_exponential_jitter(monkeypatch) -> None:
    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm_client.random, "uniform", lambda _start, _end: 0.0)
    client = _FakePostClient([_response(429) for _ in range(9)] + [_response(200)])

    result = await llm_client._post_with_status_retries(
        client,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={"model": "x"},
        headers={"Authorization": "Bearer test"},
    )

    assert result.status_code == 200
    assert len(client.calls) == 10
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30, 30]


@pytest.mark.asyncio
async def test_post_with_status_retries_respects_smaller_caller_attempt_budget(monkeypatch) -> None:
    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm_client.random, "uniform", lambda _start, _end: 0.0)
    client = _FakePostClient([_response(429) for _ in range(10)])

    result = await llm_client._post_with_status_retries(
        client,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={"model": "selector"},
        headers={},
        max_retries=3,
    )

    assert result.status_code == 429
    assert len(client.calls) == 3
    assert sleeps == [1, 2]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, "rejected"),
        (401, "rejected"),
        (402, "rejected"),
        (403, "rejected"),
        (404, "rejected"),
        (413, "rejected"),
        (422, "rejected"),
        (429, "rejected"),
        (408, "unknown"),
        (409, "unknown"),
        (500, "unknown"),
        (503, "unknown"),
        (529, "unknown"),
    ],
)
def test_http_status_delivery_state_uses_authoritative_rejection_allowlist(status_code, expected) -> None:
    from app.services.llm_client import delivery_state_from_http_status

    assert delivery_state_from_http_status(status_code) == expected


@pytest.mark.asyncio
async def test_post_with_status_retries_does_not_replay_ambiguous_server_failure(monkeypatch) -> None:
    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    client = _FakePostClient([_response(529), _response(200)])

    result = await llm_client._post_with_status_retries(
        client,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={},
        headers={},
    )

    assert result.status_code == 529
    assert len(client.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_post_with_status_retries_honors_retry_after_without_retrying_auth(monkeypatch) -> None:
    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)

    retryable = _FakePostClient([_response(429, retry_after="7"), _response(200)])
    result = await llm_client._post_with_status_retries(
        retryable,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={},
        headers={},
    )
    assert result.status_code == 200
    assert sleeps == [7]

    auth = _FakePostClient([_response(401)])
    result = await llm_client._post_with_status_retries(
        auth,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={},
        headers={},
    )
    assert result.status_code == 401
    assert len(auth.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_class_name",
    ["OpenAICompatibleClient", "OpenAIResponsesClient", "GeminiClient", "AnthropicClient"],
)
async def test_non_stream_clients_honor_caller_bounded_http_attempts_without_leaking_transport_hint(
    monkeypatch,
    client_class_name,
) -> None:
    """Advisory intelligence lanes may spend a smaller, explicit retry budget.

    The budget is a transport/lifecycle invariant, not a provider payload field,
    and must work equally across every non-stream provider protocol.
    """

    import app.services.llm_client as llm_client

    observed: list[dict] = []

    async def fake_post(_client, _url, *, payload, headers, max_retries):
        observed.append({"payload": payload, "headers": headers, "max_retries": max_retries})
        return httpx.Response(429, text='{"error":{"type":"rate_limit_error"}}')

    async def fake_get_client():
        return object()

    monkeypatch.setattr(llm_client, "_post_with_status_retries", fake_post)
    client_class = getattr(llm_client, client_class_name)
    client = client_class(api_key="test-key", model="test-model")
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    with pytest.raises(llm_client.LLMError) as caught:
        await client.complete(
            [llm_client.LLMMessage(role="user", content="select authorized memory")],
            _http_max_attempts=3,
        )

    assert caught.value.http_status == 429
    assert len(observed) == 1
    assert observed[0]["max_retries"] == 3
    assert "_http_max_attempts" not in observed[0]["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_class_name",
    ["OpenAICompatibleClient", "GeminiClient", "AnthropicClient"],
)
async def test_native_stream_clients_honor_caller_bounded_http_attempts_without_leaking_transport_hint(
    monkeypatch,
    client_class_name,
) -> None:
    """A terminal-critical projection may make one provider attempt only.

    The mechanical lifecycle budget must apply equally to every native stream
    protocol and must never become a provider payload field.
    """

    import app.services.llm_client as llm_client

    async def fake_sleep(_seconds: float) -> None:
        return None

    fake_http = _FakeStreamClient()

    async def fake_get_client():
        return fake_http

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    client_class = getattr(llm_client, client_class_name)
    client = client_class(api_key="test-key", model="test-model")
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    with pytest.raises(llm_client.LLMError) as caught:
        await client.stream(
            [llm_client.LLMMessage(role="user", content="update derived session summary")],
            _http_max_attempts=1,
        )

    assert caught.value.http_status == 429
    assert len(fake_http.calls) == 1
    assert "_http_max_attempts" not in fake_http.calls[0]["json"]


@pytest.mark.asyncio
async def test_responses_stream_fallback_preserves_caller_bounded_http_attempts(monkeypatch) -> None:
    import app.services.llm_client as llm_client

    observed: list[dict] = []

    async def fake_post(_client, _url, *, payload, headers, max_retries):
        observed.append({"payload": payload, "headers": headers, "max_retries": max_retries})
        return httpx.Response(429, text='{"error":{"type":"rate_limit_error"}}')

    async def fake_get_client():
        return object()

    monkeypatch.setattr(llm_client, "_post_with_status_retries", fake_post)
    client = llm_client.OpenAIResponsesClient(api_key="test-key", model="test-model")
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    with pytest.raises(llm_client.LLMError) as caught:
        await client.stream(
            [llm_client.LLMMessage(role="user", content="update derived session summary")],
            _http_max_attempts=1,
        )

    assert caught.value.http_status == 429
    assert observed[0]["max_retries"] == 1
    assert "_http_max_attempts" not in observed[0]["payload"]


@pytest.mark.asyncio
async def test_post_with_status_retries_never_replays_after_a_402_rejection(monkeypatch) -> None:
    """DAY1-PROVIDER-402-CLASSIFICATION-001: an explicit HTTP 402 response is
    an authoritative rejection.  The same provider request is never
    automatically retried after it — and when a 429 came first, the 402
    stops the retry loop immediately (exactly two calls, never a third)."""

    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm_client.random, "uniform", lambda _start, _end: 0.0)

    single = _FakePostClient([_response(402), _response(200)])
    result = await llm_client._post_with_status_retries(
        single,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={},
        headers={},
    )
    assert result.status_code == 402
    assert len(single.calls) == 1
    assert sleeps == []

    after_rate_limit = _FakePostClient([_response(429), _response(402), _response(200)])
    result = await llm_client._post_with_status_retries(
        after_rate_limit,  # type: ignore[arg-type]
        "https://llm.example/v1/chat/completions",
        payload={},
        headers={},
    )
    assert result.status_code == 402
    assert len(after_rate_limit.calls) == 2
    assert sleeps == [1]


@pytest.mark.asyncio
async def test_post_with_status_retries_never_replays_unknown_network_delivery(monkeypatch) -> None:
    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm_client.random, "uniform", lambda _start, _end: 0.0)
    client = _FakePostClient([httpx.ConnectError("boom"), httpx.ReadError("again"), _response(200)])

    with pytest.raises(llm_client.LLMError) as caught:
        await llm_client._post_with_status_retries(
            client,  # type: ignore[arg-type]
            "https://llm.example/v1/chat/completions",
            payload={},
            headers={},
        )

    assert caught.value.delivery_state == "unknown"
    assert len(client.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_openai_compatible_complete_maps_http_402_to_typed_rejected(monkeypatch) -> None:
    """Wiring proof (DAY1-PROVIDER-402-CLASSIFICATION-001): a real HTTP 402
    response through the live ``OpenAICompatibleClient.complete`` path must
    raise ``LLMError`` with typed ``delivery_state='rejected'`` and
    ``http_status=402`` — exactly one provider call, never an ambiguous
    delivery outcome."""

    from types import SimpleNamespace

    import app.services.llm_client as llm_client

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    body = '{"error":{"message":"Insufficient Balance","type":"invalid_request_error"}}'
    fake_post = _FakePostClient([httpx.Response(402, text=body), httpx.Response(200, text="{}")])
    client = llm_client.OpenAICompatibleClient(api_key="test-key", model="gpt-4.1")
    client._client = SimpleNamespace(is_closed=False, post=fake_post.post)

    with pytest.raises(llm_client.LLMError) as caught:
        await client.complete([llm_client.LLMMessage(role="user", content="hello")])

    assert caught.value.delivery_state == "rejected"
    assert caught.value.http_status == 402
    assert len(fake_post.calls) == 1
    assert sleeps == []
