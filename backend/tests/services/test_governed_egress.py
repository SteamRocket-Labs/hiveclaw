from __future__ import annotations

import asyncio
import gzip
import json
import uuid

import httpcore
import httpx
import pytest

from app.services.governed_egress import (
    EgressLimits,
    GovernedEgressError,
    PinnedPublicNetworkBackend,
    fetch_public_http,
    validate_public_http_url,
)


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _extract_tool_error(result: str) -> dict:
    start_marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(start_marker) + len(start_marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


async def _public_resolver(hostname: str, port: int) -> tuple[str, ...]:
    assert hostname in {"example.com", "other.example.com"}
    assert port in {80, 443}
    return (PUBLIC_V4, PUBLIC_V6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:secret@example.com/private",
        "https://example.com\n@169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8008/api/health",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/internal",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1%25eth0]/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://example.com:0/",
        "http://example.com:65536/",
    ],
)
async def test_validate_public_http_url_rejects_non_public_or_ambiguous_targets(url: str):
    with pytest.raises(GovernedEgressError) as exc_info:
        await validate_public_http_url(url, resolver=_public_resolver)

    assert exc_info.value.code == "network_target_denied"
    assert str(exc_info.value).startswith("network_target_denied:")


@pytest.mark.asyncio
async def test_validate_public_http_url_rejects_mixed_public_private_dns_answers():
    async def mixed_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return (PUBLIC_V4, "10.8.0.4")

    with pytest.raises(GovernedEgressError) as exc_info:
        await validate_public_http_url("https://example.com/report", resolver=mixed_resolver)

    assert exc_info.value.code == "network_target_denied"
    assert "DNS" in exc_info.value.reason


@pytest.mark.asyncio
async def test_validate_public_http_url_fails_closed_on_unexpected_resolver_error():
    async def broken_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        raise RuntimeError("resolver backend unavailable")

    with pytest.raises(GovernedEgressError) as exc_info:
        await validate_public_http_url("https://example.com", resolver=broken_resolver)

    assert exc_info.value.code == "network_target_denied"
    assert exc_info.value.reason == "DNS resolution failed closed"


@pytest.mark.asyncio
async def test_validate_public_http_url_returns_canonical_target_and_all_pins():
    target = await validate_public_http_url(
        "HTTPS://Example.COM.:443/a/../report?q=1#ignored",
        resolver=_public_resolver,
    )

    assert target.url == "https://example.com/a/../report?q=1"
    assert target.hostname == "example.com"
    assert target.port == 443
    assert target.resolved_ips == (PUBLIC_V4, PUBLIC_V6)


class _FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, peer: str):
        self.peer = peer
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info: str):
        if info == "server_addr":
            return (self.peer, 443)
        return None


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, peer: str | None = None):
        self.hosts: list[str] = []
        self.peer = peer

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        return _FakeStream(self.peer or host)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("governed public HTTP must never use a Unix socket")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_pinned_backend_connects_to_validated_ip_without_second_dns_lookup():
    raw_backend = _RecordingBackend()
    backend = PinnedPublicNetworkBackend(raw_backend)
    backend.register("example.com", 443, (PUBLIC_V4, PUBLIC_V6))

    stream = await backend.connect_tcp("example.com", 443)

    assert raw_backend.hosts == [PUBLIC_V4]
    assert stream.get_extra_info("server_addr")[0] == PUBLIC_V4


@pytest.mark.asyncio
async def test_pinned_backend_fails_closed_when_socket_peer_is_not_the_selected_pin():
    raw_backend = _RecordingBackend(peer="10.0.0.9")
    backend = PinnedPublicNetworkBackend(raw_backend)
    backend.register("example.com", 443, (PUBLIC_V4,))

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443)


def _mock_client_factory(handler):
    def factory(_backend: PinnedPublicNetworkBackend) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    return factory


@pytest.mark.asyncio
async def test_fetch_public_http_revalidates_every_redirect_before_sending_next_request():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(GovernedEgressError) as exc_info:
        await fetch_public_http(
            "https://example.com/start",
            resolver=_public_resolver,
            client_factory=_mock_client_factory(handler),
        )

    assert exc_info.value.code == "network_target_denied"
    assert requests == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_fetch_public_http_rechecks_dns_on_redirect_and_blocks_rebinding():
    dns_answers = iter(((PUBLIC_V4,), ("10.0.0.7",)))
    requests: list[str] = []

    async def rebinding_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return next(dns_answers)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/next"})

    with pytest.raises(GovernedEgressError) as exc_info:
        await fetch_public_http(
            "https://example.com/start",
            resolver=rebinding_resolver,
            client_factory=_mock_client_factory(handler),
        )

    assert exc_info.value.code == "network_target_denied"
    assert requests == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_fetch_public_http_strips_sensitive_headers_on_cross_origin_redirect():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "https://other.example.com/final"})
        return httpx.Response(200, text="public result")

    response = await fetch_public_http(
        "https://example.com/start",
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "Proxy-Authorization": "Basic secret",
            "User-Agent": "Hive test",
        },
        resolver=_public_resolver,
        client_factory=_mock_client_factory(handler),
    )

    assert response.status_code == 200
    assert response.text == "public result"
    assert seen_headers[0]["authorization"] == "Bearer secret"
    assert "authorization" not in seen_headers[1]
    assert "cookie" not in seen_headers[1]
    assert "proxy-authorization" not in seen_headers[1]
    assert seen_headers[1]["user-agent"] == "Hive test"


@pytest.mark.asyncio
async def test_fetch_public_http_rejects_https_to_http_redirect_downgrade():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://example.com/plain"})

    with pytest.raises(GovernedEgressError) as exc_info:
        await fetch_public_http(
            "https://example.com/start",
            resolver=_public_resolver,
            client_factory=_mock_client_factory(handler),
        )

    assert exc_info.value.code == "network_redirect_denied"


@pytest.mark.asyncio
async def test_fetch_public_http_enforces_redirect_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("hop", "0"))
        return httpx.Response(302, headers={"Location": f"https://example.com/loop?hop={index + 1}"})

    with pytest.raises(GovernedEgressError) as exc_info:
        await fetch_public_http(
            "https://example.com/loop?hop=0",
            resolver=_public_resolver,
            limits=EgressLimits(max_redirects=2),
            client_factory=_mock_client_factory(handler),
        )

    assert exc_info.value.code == "network_redirect_denied"


@pytest.mark.asyncio
async def test_fetch_public_http_enforces_decoded_response_limit_for_compression_bomb():
    compressed = gzip.compress(b"A" * 16_384)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"},
            content=compressed,
        )

    with pytest.raises(GovernedEgressError) as exc_info:
        await fetch_public_http(
            "https://example.com/bomb",
            resolver=_public_resolver,
            limits=EgressLimits(max_wire_bytes=4096, max_decoded_bytes=1024),
            client_factory=_mock_client_factory(handler),
        )

    assert exc_info.value.code == "network_response_too_large"


@pytest.mark.asyncio
async def test_fetch_public_http_enforces_total_wall_clock_timeout():
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, text="too late")

    with pytest.raises(GovernedEgressError) as exc_info:
        await fetch_public_http(
            "https://example.com/slow",
            resolver=_public_resolver,
            limits=EgressLimits(total_timeout_seconds=0.01),
            client_factory=_mock_client_factory(handler),
        )

    assert exc_info.value.code == "network_timeout"


@pytest.mark.asyncio
async def test_web_fetch_returns_typed_deny_without_opening_http_client(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    class _ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("denied target must not construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenClient)

    result = await web_mcp._web_fetch(
        {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}
    )

    payload = _extract_tool_error(result)
    assert payload["error_class"] == "network_target_denied"
    assert payload["retryable"] is False
    assert payload["provider"] == "web_fetch"


@pytest.mark.asyncio
async def test_every_remote_fetch_provider_rejects_private_target_before_forwarding(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def no_config(_name: str) -> dict:
        return {"auth_mode": "keyless"}

    async def configured_xcrawl() -> str:
        return "xcrawl-key"

    async def forbidden_anysearch(*_args, **_kwargs):
        raise AssertionError("private target must not be forwarded to AnySearch")

    class _ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("private target must not reach a provider endpoint")

    monkeypatch.setattr(web_mcp, "_get_tool_config", no_config)
    monkeypatch.setattr(web_mcp, "_get_xcrawl_api_key", configured_xcrawl)
    monkeypatch.setattr(web_mcp, "_call_anysearch_mcp_tool", forbidden_anysearch)
    monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenClient)

    target = "http://127.0.0.1:8008/api/health"
    calls = [
        ("anysearch", web_mcp._anysearch_extract, {"url": target}),
        ("tavily", web_mcp._tavily_extract, {"url": target}),
        ("exa", web_mcp._exa_fetch, {"url": target}),
        ("firecrawl", web_mcp._firecrawl_fetch, {"url": target}),
        ("xcrawl", web_mcp._xcrawl_scrape, {"url": target}),
        ("advanced", web_mcp._advanced_web_fetch, {"url": target, "provider": "anysearch"}),
    ]
    results = [(name, await call(arguments)) for name, call, arguments in calls]

    for name, result in results:
        payload = _extract_tool_error(result)
        assert payload["error_class"] == "network_target_denied", (name, payload)
        assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_personal_knowledge_url_ingest_uses_same_governed_fetch_boundary(monkeypatch, tmp_path):
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    class _ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("private knowledge URL must not construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenClient)
    service = PersonalKnowledgeService(data_root=tmp_path)

    with pytest.raises(GovernedEgressError) as exc_info:
        await service.ingest_url(
            object(),
            tenant_id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            url="http://10.0.0.8/company-secrets",
        )

    assert exc_info.value.code == "network_target_denied"


@pytest.mark.asyncio
async def test_imagekit_url_forwarding_rejects_private_target_before_provider_call(monkeypatch, tmp_path):
    from app.services.agent_tool_domains.image_upload import _upload_image

    async def tool_config(_name: str, *, agent_id):
        return {"private_key": "imagekit-secret"}

    class _ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("private image URL must not be forwarded to ImageKit")

    monkeypatch.setattr("app.services.tool_config_service.resolve_tool_config", tool_config)
    monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenClient)

    result = await _upload_image(
        uuid.uuid4(),
        tmp_path,
        {"url": "http://127.0.0.1:8008/internal.png"},
    )

    assert "network_target_denied" in result
