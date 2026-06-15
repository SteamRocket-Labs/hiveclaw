"""Shared subprocess environment construction for agent-executed commands."""

from __future__ import annotations

import os
from pathlib import Path

from app.services.code_execution.env_policy import sanitize_agent_execution_env


def build_agent_subprocess_env(*, home: Path) -> dict[str, str]:
    """Build a minimal environment for agent-controlled subprocesses.

    This intentionally does not inherit platform credentials such as
    DATABASE_URL, JWT_SECRET_KEY, SECRETS_MASTER_KEY, or provider API keys.
    Explicit capability-specific credentials must be injected by the caller,
    not inherited from the backend process.
    """
    env, _evidence = sanitize_agent_execution_env(os.environ, home=home, require_home=True)
    return env
