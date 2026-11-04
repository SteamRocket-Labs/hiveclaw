"""Re-register configured Telegram webhooks with deterministic secrets.

Run from backend: python -m scripts.reregister_telegram_webhooks [--apply]
The default only lists configuration and agent IDs. Use the instance's database,
PUBLIC_BASE_URL and secret-provider configuration; no credentials are printed.
"""
import argparse
import asyncio

from sqlalchemy import select

from app.api.telegram import _register_telegram_webhook
from app.config import get_settings
from app.database import async_session, enter_rls_bypass
from app.models.channel_config import ChannelConfig
from app.services.secrets_provider import init_secrets_provider


async def run(*, apply: bool = False) -> None:
    settings = get_settings()
    if apply:
        init_secrets_provider(
            settings.SECRETS_MASTER_KEY or None,
            previous_master_keys=tuple(key.strip() for key in settings.SECRETS_MASTER_KEY_PREVIOUS.split(",") if key.strip()),
        )
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="Operator Telegram webhook secret migration"):
            # Dry-run never reads or decrypts credential columns.
            columns = [ChannelConfig.id, ChannelConfig.agent_id]
            if apply:
                columns.append(ChannelConfig.app_secret)
            rows = (await db.execute(select(*columns).where(
                ChannelConfig.channel_type == "telegram", ChannelConfig.is_configured.is_(True),
            ))).all()
            for row in rows:
                print(f"config={row.id} agent={row.agent_id}")
                if apply:
                    if not row.app_secret:
                        print("  Skipped: bot token is not configured")
                        continue
                    await _register_telegram_webhook(row.app_secret, row.agent_id)
    if not apply:
        print(f"Dry run: {len(rows)} configured bot(s). Use --apply to re-register.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Re-register webhooks with Telegram")
    args = parser.parse_args(argv)
    asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    main()
