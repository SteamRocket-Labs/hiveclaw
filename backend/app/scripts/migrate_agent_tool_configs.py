"""Inspect, encrypt, or rotate AgentTool configs; dry-run by default."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.config import get_settings
from app.database import schema_engine
from app.services.agent_tool_config_storage import (
    inspect_agent_tool_config_rows,
    migrate_agent_tool_config_rows,
)
from app.services.secrets_provider import FernetSecretsProvider


async def run(*, apply: bool) -> dict:
    settings = get_settings()
    previous_keys = tuple(key.strip() for key in settings.SECRETS_MASTER_KEY_PREVIOUS.split(",") if key.strip())
    if apply:
        if not settings.SECRETS_MASTER_KEY:
            raise RuntimeError("SECRETS_MASTER_KEY is required for AgentTool config migration")
        provider = FernetSecretsProvider(
            settings.SECRETS_MASTER_KEY,
            previous_master_keys=previous_keys,
        )
        async with schema_engine.begin() as connection:
            return await connection.run_sync(
                lambda bind: migrate_agent_tool_config_rows(
                    bind,
                    provider=provider,
                    apply=True,
                )
            )

    async with schema_engine.connect() as connection:
        return await connection.run_sync(lambda bind: inspect_agent_tool_config_rows(bind))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Count plaintext AgentTool configs or encrypt/rotate complete config documents in place.")
    )
    parser.add_argument("--apply", action="store_true", help="Encrypt/rotate rows (default: dry-run counts only).")
    parser.add_argument("--confirm", action="store_true", help="Required with --apply.")
    args = parser.parse_args()
    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm")
    report = asyncio.run(run(apply=args.apply))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
