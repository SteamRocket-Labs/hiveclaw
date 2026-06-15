"""Environment allowlist policy for agent-controlled code execution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_AGENT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

AGENT_EXECUTION_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NPM_CONFIG_REGISTRY",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PYTHONDONTWRITEBYTECODE",
    }
)

URL_ENV_KEYS = frozenset({"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "NPM_CONFIG_REGISTRY"})

_CREDENTIAL_KEY_MARKERS = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "DATABASE_URL",
    "DSN",
    "JWT",
    "KEY",
    "PASSWORD",
    "PRIVATE",
    "REDIS_URL",
    "SECRET",
    "TOKEN",
)


def _strip_url_credentials(value: str) -> str:
    """Remove userinfo from URL-like environment values before agent execution."""
    parts: list[str] = []
    for item in value.split():
        parsed = urlsplit(item)
        if parsed.scheme and parsed.netloc and ("@" in parsed.netloc or parsed.username or parsed.password):
            hostname = parsed.hostname or ""
            if parsed.port is not None:
                hostname = f"{hostname}:{parsed.port}"
            item = urlunsplit((parsed.scheme, hostname, parsed.path, parsed.query, parsed.fragment))
        parts.append(item)
    return " ".join(parts)


def _looks_like_credential_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _CREDENTIAL_KEY_MARKERS)


def sanitize_agent_execution_env(
    env: Mapping[str, str | None],
    *,
    home: str | Path | None = None,
    require_home: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return a safe env plus a value-free evidence report.

    Provider code calls this even when tool handlers already used the safe env
    builder. That second pass is intentional: code execution providers are the
    isolation boundary and must not trust upstream callers.
    """

    sanitized: dict[str, str] = {}
    blocked_keys: list[str] = []
    credential_keys_blocked: list[str] = []
    stripped_url_keys: list[str] = []

    for key, value in env.items():
        if value is None:
            continue
        if key not in AGENT_EXECUTION_ENV_ALLOWLIST:
            blocked_keys.append(key)
            if _looks_like_credential_key(key):
                credential_keys_blocked.append(key)
            continue

        text = str(value)
        cleaned = _strip_url_credentials(text) if key in URL_ENV_KEYS else text
        if cleaned != text:
            stripped_url_keys.append(key)
        sanitized[key] = cleaned

    if home is not None:
        sanitized["HOME"] = str(home)
    sanitized["PATH"] = sanitized.get("PATH") or DEFAULT_AGENT_PATH
    sanitized["PYTHONDONTWRITEBYTECODE"] = "1"

    missing_required_keys: list[str] = []
    if require_home and not sanitized.get("HOME"):
        missing_required_keys.append("HOME")

    evidence: dict[str, Any] = {
        "mode": "allowlist",
        "values_included": False,
        "allowed_keys": sorted(sanitized),
        "blocked_keys": sorted(blocked_keys),
        "credential_keys_blocked": sorted(credential_keys_blocked),
        "url_credentials_stripped_keys": sorted(stripped_url_keys),
        "missing_required_keys": missing_required_keys,
    }
    return sanitized, evidence
