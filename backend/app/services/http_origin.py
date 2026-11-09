"""Compare HTTP targets by scheme, host and effective port."""

from urllib.parse import urlsplit

import httpx


def http_origin(url: str) -> tuple[str, str, int | None]:
    try:
        parsed = httpx.URL(str(url))
    except httpx.InvalidURL as exc:
        raise ValueError("Invalid HTTP origin") from exc
    # Match the transport's IDNA handling while still rejecting invalid ports.
    port = urlsplit(str(parsed)).port
    return parsed.scheme, parsed.host, port if port is not None else {"http": 80, "https": 443}.get(parsed.scheme)
