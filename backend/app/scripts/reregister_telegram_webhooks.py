"""Re-register configured Telegram webhooks with deterministic secrets.

Run from backend: python -m app.scripts.reregister_telegram_webhooks [--apply]
Required environment: DATABASE_URL, SECRETS_MASTER_KEY, SECRET_KEY, and
PUBLIC_BASE_URL (or the public base URL in the admin platform setting).
The default is a dry-run; no credentials are printed.

Run --apply in the production environment first, confirm every row says
registered, and only then deploy. The old code verifies the secret header
whenever present, so this order loses no messages.
"""
import argparse
import asyncio

from sqlalchemy import func, or_, select

from app.api.telegram import _register_telegram_webhook, _resolve_public_base_url
from app.config import get_settings
from app.database import async_session, enter_rls_bypass
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant import Tenant
from app.services.secrets_provider import init_secrets_provider


async def run(*, apply: bool = False) -> bool:
    settings = get_settings()
    if apply:
        if not settings.SECRETS_MASTER_KEY:
            raise RuntimeError("SECRETS_MASTER_KEY is required for webhook registration")
        if not settings.SECRET_KEY or settings.SECRET_KEY == "change-me-in-production":
            raise RuntimeError("A production SECRET_KEY is required for webhook registration")
    public_base = await _resolve_public_base_url()
    print(f"Public base URL: {public_base or '(not configured)'}")
    if apply:
        if not public_base:
            raise RuntimeError("Configure PUBLIC_BASE_URL or the admin platform setting before applying")
        init_secrets_provider(
            settings.SECRETS_MASTER_KEY,
            previous_master_keys=tuple(key.strip() for key in settings.SECRETS_MASTER_KEY_PREVIOUS.split(",") if key.strip()),
        )
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="Operator Telegram webhook secret migration"):
            # Match load_public_agent_channel_config: legacy rows inherit the
            # agent's company; a known company must exist and be active.
            tenant_id = func.coalesce(ChannelConfig.tenant_id, Agent.tenant_id)
            # Dry-run never reads or decrypts credential columns.
            columns = [ChannelConfig.id, ChannelConfig.agent_id]
            if apply:
                columns.append(ChannelConfig.app_secret)
            rows = (await db.execute(
                select(*columns)
                .outerjoin(Agent, Agent.id == ChannelConfig.agent_id)
                .outerjoin(Tenant, Tenant.id == tenant_id)
                .where(
                    ChannelConfig.channel_type == "telegram",
                    ChannelConfig.is_configured.is_(True),
                    or_(tenant_id.is_(None), Tenant.is_active.is_(True)),
                )
            )).all()
    succeeded = True
    for row in rows:
        print(f"config={row.id} agent={row.agent_id}")
        if apply:
            registered = bool(row.app_secret) and await _register_telegram_webhook(
                row.app_secret, row.agent_id, public_base=public_base,
            )
            print("  registered" if registered else "  FAILED")
            succeeded = succeeded and registered
    if not apply:
        print(f"Dry run: {len(rows)} configured bot(s). Use --apply to re-register.")
    return succeeded


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Re-register webhooks with Telegram")
    args = parser.parse_args(argv)
    if not asyncio.run(run(apply=args.apply)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
