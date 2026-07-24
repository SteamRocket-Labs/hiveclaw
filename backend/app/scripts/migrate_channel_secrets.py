"""Inspect, encrypt, or rotate channel credentials; dry-run by default."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.database import schema_engine
from app.services.channel_ingress_inbox import (
    migrate_channel_ingress_exact_secret_rows,
)
from app.services.channel_secret_storage import (
    inspect_channel_ingress_secret_rows,
    inspect_channel_secret_rows,
    inspect_delivery_target_secret_rows,
    migrate_channel_ingress_secret_rows,
    migrate_channel_secret_rows,
    migrate_delivery_target_secret_rows,
)
from app.services.secrets_provider import FernetSecretsProvider
from app.services.secrets_provider import init_secrets_provider


async def run(*, apply: bool) -> dict:
    settings = get_settings()
    previous_keys = tuple(key.strip() for key in settings.SECRETS_MASTER_KEY_PREVIOUS.split(",") if key.strip())
    exact_ingress: dict = {
        "schema": "hive.channel_ingress_exact_secret_backfill.v1",
        "mode": "unavailable",
        "reason": "SECRETS_MASTER_KEY is required to decrypt the exact credential inventory",
    }
    if settings.SECRETS_MASTER_KEY:
        init_secrets_provider(
            settings.SECRETS_MASTER_KEY,
            previous_master_keys=previous_keys,
        )
    if apply:
        if not settings.SECRETS_MASTER_KEY:
            raise RuntimeError("SECRETS_MASTER_KEY is required for channel secret migration")
        provider = FernetSecretsProvider(
            settings.SECRETS_MASTER_KEY,
            previous_master_keys=previous_keys,
        )
        async with schema_engine.begin() as connection:
            channel_configs = await connection.run_sync(
                lambda bind: migrate_channel_secret_rows(
                    bind,
                    provider=provider,
                    apply=True,
                )
            )
            delivery_targets = await connection.run_sync(
                lambda bind: migrate_delivery_target_secret_rows(
                    bind,
                    provider=provider,
                    apply=True,
                )
            )
            channel_ingress = await connection.run_sync(
                lambda bind: migrate_channel_ingress_secret_rows(
                    bind,
                    provider=provider,
                    apply=True,
                )
            )
        session_factory = async_sessionmaker(
            schema_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as db:
            exact_ingress = await migrate_channel_ingress_exact_secret_rows(
                db,
                apply=True,
            )
        return {
            "schema": "hive.channel_secret_migration.v3",
            "mode": "apply",
            "channel_configs": channel_configs,
            "delivery_targets": delivery_targets,
            "channel_ingress": channel_ingress,
            "channel_ingress_exact": exact_ingress,
        }

    async with schema_engine.connect() as connection:
        channel_configs = await connection.run_sync(lambda bind: inspect_channel_secret_rows(bind))
        delivery_targets = await connection.run_sync(lambda bind: inspect_delivery_target_secret_rows(bind))
        channel_ingress = await connection.run_sync(lambda bind: inspect_channel_ingress_secret_rows(bind))
    if settings.SECRETS_MASTER_KEY:
        session_factory = async_sessionmaker(
            schema_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as db:
            exact_ingress = await migrate_channel_ingress_exact_secret_rows(
                db,
                apply=False,
            )
    return {
        "schema": "hive.channel_secret_migration.v3",
        "mode": "dry_run",
        "channel_configs": channel_configs,
        "delivery_targets": delivery_targets,
        "channel_ingress": channel_ingress,
        "channel_ingress_exact": exact_ingress,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Count legacy plaintext channel and delivery-target credentials or encrypt/rotate them in place.")
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
