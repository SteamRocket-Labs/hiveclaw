"""Shared subprocess environment construction for agent-executed commands."""

from __future__ import annotations

import os
from pathlib import Path


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


def build_agent_subprocess_env(*, home: Path) -> dict[str, str]:
    """Build a minimal environment for agent-controlled subprocesses.

    This intentionally does not inherit platform credentials such as
    DATABASE_URL, JWT_SECRET_KEY, SECRETS_MASTER_KEY, or provider API keys.
    Explicit capability-specific credentials must be injected by the caller,
    not inherited from the backend process.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SUBPROCESS_ENV_ALLOWLIST and value is not None
    }
    env["PATH"] = env.get("PATH") or _DEFAULT_PATH
    env["HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env
