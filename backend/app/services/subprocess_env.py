"""Shared subprocess environment construction for agent-executed commands."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_SUBPROCESS_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NPM_CONFIG_REGISTRY",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
    }
)

_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_URL_ENV_KEYS = frozenset({"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "NPM_CONFIG_REGISTRY"})


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


def build_agent_subprocess_env(*, home: Path) -> dict[str, str]:
    """Build a minimal environment for agent-controlled subprocesses.

    This intentionally does not inherit platform credentials such as
    DATABASE_URL, JWT_SECRET_KEY, SECRETS_MASTER_KEY, or provider API keys.
    Explicit capability-specific credentials must be injected by the caller,
    not inherited from the backend process.
    """
    env = {
        key: _strip_url_credentials(value) if key in _URL_ENV_KEYS else value
        for key, value in os.environ.items()
        if key in _SUBPROCESS_ENV_ALLOWLIST and value is not None
    }
    env["PATH"] = env.get("PATH") or _DEFAULT_PATH
    env["HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env
