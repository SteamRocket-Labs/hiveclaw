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


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, "rejected"),
        (401, "rejected"),
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
