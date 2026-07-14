"""Network-fact governance for agent-controlled HTTP fetches.

This module owns only mechanically verifiable network invariants: URL syntax,
public-address resolution, connection pinning, redirects, timeouts, and byte
ceilings. It never judges page meaning or rewrites fetched content.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import ipaddress
import re
import socket
import ssl
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx


AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]
ClientFactory = Callable[["PinnedPublicNetworkBackend"], httpx.AsyncClient]

_SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NUMERIC_HOST_RE = re.compile(r"^[0-9.]+$")
_HEX_HOST_RE = re.compile(r"^(?:0x[0-9a-f]+|(?:0x[0-9a-f]+\.)+[0-9a-fx]+)$", re.IGNORECASE)
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class GovernedEgressError(RuntimeError):
    """Typed, non-semantic failure at the governed network boundary."""

    def __init__(self, code: str, reason: str, *, url: str | None = None):
        super().__init__(f"{code}:{reason}")
        self.code = code
        self.reason = reason
        self.url = url


@dataclass(frozen=True)
class EgressLimits:
    max_redirects: int = 5
    max_wire_bytes: int = 8 * 1024 * 1024
    max_decoded_bytes: int = 16 * 1024 * 1024
    total_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if self.max_wire_bytes <= 0 or self.max_decoded_bytes <= 0:
            raise ValueError("response byte limits must be positive")
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")


@dataclass(frozen=True)
class ValidatedPublicTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    resolved_ips: tuple[str, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.hostname, self.port)


@dataclass(frozen=True)
class GovernedHTTPResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return httpx.Response(self.status_code, headers=self.headers, content=self.content).text

    def json(self) -> Any:
        return httpx.Response(self.status_code, headers=self.headers, content=self.content).json()


def _deny(reason: str, *, url: str | None = None, code: str = "network_target_denied") -> GovernedEgressError:
    return GovernedEgressError(code, reason, url=url)


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    for network in _NAT64_NETWORKS:
        if address in network:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            # Mapped/translated forms are intentionally rejected even when the
            # embedded IPv4 address is public; one canonical address form keeps
            # validation and the eventual peer identity unambiguous.
            return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _parse_public_address(value: str, *, url: str, source: str) -> str:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise _deny(f"{source} returned an invalid IP address", url=url) from exc
    if not _is_public_address(address):
        raise _deny(f"{source} resolved to a non-public network address", url=url)
    return str(address)


def _normalize_http_url(
    value: str,
) -> tuple[str, str, str, int, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    raw = str(value or "")
    if not raw:
        raise _deny("URL is empty", url=raw)
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in raw):
        raise _deny("URL contains whitespace or control characters", url=raw)
    if "\\" in raw:
        raise _deny("URL contains an ambiguous backslash", url=raw)
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise _deny("Only http and https URL schemes are allowed", url=raw)
        if parsed.username is not None or parsed.password is not None:
            raise _deny("URL userinfo is not allowed", url=raw)
        hostname = parsed.hostname
        if not hostname:
            raise _deny("URL hostname is missing", url=raw)
        if "%" in hostname:
            raise _deny("IPv6 zone identifiers and encoded hostnames are not allowed", url=raw)
        explicit_port = parsed.port
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise _deny("URL port is outside the valid range", url=raw)
    except GovernedEgressError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise _deny("URL authority or port is invalid", url=raw) from exc

    hostname = hostname.rstrip(".")
    if not hostname:
        raise _deny("URL hostname is missing", url=raw)

    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal = ipaddress.ip_address(hostname)
        canonical_hostname = str(literal)
    except ValueError:
        if ":" in hostname or _NUMERIC_HOST_RE.fullmatch(hostname) or _HEX_HOST_RE.fullmatch(hostname):
            raise _deny("Ambiguous numeric IP address forms are not allowed", url=raw)
        try:
            canonical_hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise _deny("URL hostname is invalid", url=raw) from exc
        if "." not in canonical_hostname:
            raise _deny("Single-label hostnames are not public fetch targets", url=raw)

    rendered_host = f"[{canonical_hostname}]" if ":" in canonical_hostname else canonical_hostname
    default_port = 443 if scheme == "https" else 80
    netloc = rendered_host if port == default_port else f"{rendered_host}:{port}"
    canonical_url = urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    return canonical_url, scheme, canonical_hostname, port, literal


async def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve all A/AAAA answers without applying semantic policy."""

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        value = str(sockaddr[0])
        if value not in addresses:
            addresses.append(value)
    return tuple(addresses)


async def validate_public_http_url(
    value: str,
    *,
    resolver: AddressResolver | None = None,
) -> ValidatedPublicTarget:
    """Validate one URL and return the complete public address pin set."""

    canonical_url, scheme, hostname, port, literal = _normalize_http_url(value)
    if literal is not None:
        resolved = (_parse_public_address(str(literal), url=canonical_url, source="URL"),)
    else:
        selected_resolver = resolver or resolve_public_addresses
        try:
            answers = await selected_resolver(hostname, port)
        except GovernedEgressError:
            raise
        except Exception as exc:
            raise _deny("DNS resolution failed closed", url=canonical_url) from exc
        if not answers:
            raise _deny("DNS resolution returned no addresses", url=canonical_url)
        pins: list[str] = []
        for answer in answers:
            canonical_ip = _parse_public_address(str(answer), url=canonical_url, source="DNS")
            if canonical_ip not in pins:
                pins.append(canonical_ip)
        resolved = tuple(pins)

    return ValidatedPublicTarget(
        url=canonical_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        resolved_ips=resolved,
    )


class PinnedPublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to IPs produced by the validated resolver pass."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None):
        self._backend = backend or httpcore.AnyIOBackend()
        self._pins: dict[tuple[str, int], tuple[str, ...]] = {}

    def register(self, hostname: str, port: int, resolved_ips: Sequence[str]) -> None:
        pins = tuple(_parse_public_address(str(value), url=hostname, source="Pinned target") for value in resolved_ips)
        if not pins:
            raise _deny("Validated target has no connection pins", url=hostname)
        self._pins[(hostname.lower().rstrip("."), int(port))] = pins

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Sequence[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        pins = self._pins.get((host.lower().rstrip("."), int(port)))
        if not pins:
            raise httpcore.ConnectError(f"No validated public address pins for {host}:{port}")

        last_error: BaseException | None = None
        for pinned_ip in pins:
            try:
                stream = await self._backend.connect_tcp(
                    pinned_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                peer = stream.get_extra_info("server_addr")
                peer_ip = str(peer[0]).split("%", 1)[0] if isinstance(peer, tuple) and peer else ""
                try:
                    canonical_peer = _parse_public_address(peer_ip, url=host, source="Socket peer")
                except GovernedEgressError as exc:
                    await stream.aclose()
                    raise httpcore.ConnectError(exc.reason) from exc
                if canonical_peer != pinned_ip:
                    await stream.aclose()
                    raise httpcore.ConnectError("Socket peer did not match the selected validated address pin")
                return stream
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"Could not connect to validated public target {host}:{port}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Sequence[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed by public HTTP egress")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose httpcore pool uses the pinned network backend."""

    def __init__(self, backend: PinnedPublicNetworkBackend):
        # AsyncHTTPTransport's request/response adapter and exception mapping
        # are reused; only the empty connection pool is replaced before use.
        super().__init__(verify=True, trust_env=False, http1=True, http2=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001 - required httpcore injection seam
            ssl_context=ssl.create_default_context(),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


def _default_client_factory(backend: PinnedPublicNetworkBackend) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_PinnedAsyncHTTPTransport(backend),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(10.0),
    )


def _strip_cross_origin_credentials(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _SENSITIVE_REDIRECT_HEADERS}


async def _read_limited_response(response: httpx.Response, limits: EgressLimits, *, url: str) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limits.max_wire_bytes:
                raise _deny(
                    "HTTP response exceeded the wire-byte ceiling",
                    url=url,
                    code="network_response_too_large",
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    decoded_bytes = 0
    async for chunk in response.aiter_bytes():
        decoded_bytes += len(chunk)
        if decoded_bytes > limits.max_decoded_bytes:
            raise _deny(
                "HTTP response exceeded the decoded-byte ceiling",
                url=url,
                code="network_response_too_large",
            )
        wire_bytes = int(getattr(response, "num_bytes_downloaded", decoded_bytes))
        if wire_bytes > limits.max_wire_bytes:
            raise _deny(
                "HTTP response exceeded the wire-byte ceiling",
                url=url,
                code="network_response_too_large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_public_http(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    resolver: AddressResolver | None = None,
    limits: EgressLimits | None = None,
    client_factory: ClientFactory | None = None,
) -> GovernedHTTPResponse:
    """GET a public URL through DNS-pinned, manually redirected HTTP."""

    policy = limits or EgressLimits()
    backend = PinnedPublicNetworkBackend()
    factory = client_factory or _default_client_factory
    current_headers = dict(headers or {})

    async def execute() -> GovernedHTTPResponse:
        target = await validate_public_http_url(url, resolver=resolver)
        backend.register(target.hostname, target.port, target.resolved_ips)
        redirect_count = 0

        async with factory(backend) as client:
            while True:
                async with client.stream(
                    "GET",
                    target.url,
                    headers=current_headers,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES and response.headers.get("location"):
                        if redirect_count >= policy.max_redirects:
                            raise _deny(
                                "HTTP redirect limit exceeded",
                                url=target.url,
                                code="network_redirect_denied",
                            )
                        redirected_url = urljoin(target.url, response.headers["location"])
                        next_target = await validate_public_http_url(redirected_url, resolver=resolver)
                        if target.scheme == "https" and next_target.scheme != "https":
                            raise _deny(
                                "HTTPS to HTTP redirect downgrade is not allowed",
                                url=redirected_url,
                                code="network_redirect_denied",
                            )
                        if next_target.origin != target.origin:
                            current_headers.clear()
                            current_headers.update(_strip_cross_origin_credentials(headers or {}))
                        backend.register(next_target.hostname, next_target.port, next_target.resolved_ips)
                        target = next_target
                        redirect_count += 1
                        continue

                    content = await _read_limited_response(response, policy, url=target.url)
                    return GovernedHTTPResponse(
                        status_code=response.status_code,
                        headers=httpx.Headers(response.headers),
                        content=content,
                        url=target.url,
                    )

    try:
        async with asyncio.timeout(policy.total_timeout_seconds):
            return await execute()
    except GovernedEgressError:
        raise
    except TimeoutError as exc:
        raise _deny("HTTP request exceeded the total timeout", url=url, code="network_timeout") from exc
