"""Webhook receiver endpoint for external trigger integration.

Provides a public POST endpoint that external services (GitHub, Grafana, etc.)
can send events to, which triggers the corresponding agent.
"""

import hashlib
import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select

from app.core.rate_limiter import check_rate_limit
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.trigger import AgentTrigger
from app.services.tenant_resolver import resolve_tenant_for_agent

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

DEFAULT_RATE_LIMIT = 5  # max hits per minute per token
MAX_PAYLOAD_SIZE = 65536  # 64KB max payload


@router.post("/t/{token}")
async def receive_webhook(token: str, request: Request):
    """Receive a webhook POST from an external service.

    Public endpoint — no authentication required.
    Security is provided by:
    - Unique, unguessable URL token
    - Optional HMAC signature verification
    - Rate limiting (Redis-backed, per token)
    - Payload size limit (64KB)
    """
    # Distributed rate limiting via Redis (replaces in-memory _rate_hits)
    key = f"ratelimit:webhook:{token[:16]}"
    if not await check_rate_limit(key, 60, 60):  # hard ceiling: 60/min
        logger.warning("Webhook hard rate limit exceeded for token %s...", token[:8])
        return JSONResponse({"ok": True}, status_code=429)

    # Payload size check
    body = await request.body()
    if len(body) > MAX_PAYLOAD_SIZE:
        logger.warning(f"Webhook payload too large for token {token[:8]}...: {len(body)} bytes")
        return JSONResponse({"ok": True}, status_code=413)

    # Phase A — cross-tenant token resolution. A public webhook only carries an
    # unguessable token; the owning tenant is unknown until the trigger is found,
    # so the lookup must run under an audited bypass (the resolved row's agent
    # then yields the tenant we re-pin to for all writes below).
    async with (
        async_session() as lookup_db,
        enter_rls_bypass(
            lookup_db, reason="public webhook token resolution (tenant unknown until trigger found)"
        ) as bypass_db,
    ):
        result = await bypass_db.execute(
            select(AgentTrigger).where(
                AgentTrigger.type == "webhook",
                AgentTrigger.is_enabled,
                AgentTrigger.config["token"].astext == token,
            )
        )
        target = result.scalar_one_or_none()

    if not target:
        # Return 200 OK to avoid leaking whether the token exists
        return JSONResponse({"ok": True})

    # Capture plain values before the bypass session detaches the ORM object.
    target_agent_id = target.agent_id
    target_id = target.id
    target_name = target.name
    target_config = target.config or {}

    # Phase B — re-pin to the resolved agent's tenant for the Agent read,
    # rate-limit audit, and trigger config update.
    target_tenant_id = await resolve_tenant_for_agent(target_agent_id)
    async with tenant_scoped_session(target_tenant_id) as db:
        # Per-agent rate limit check (Redis-backed)
        from app.models.agent import Agent

        agent_result = await db.execute(select(Agent).where(Agent.id == target_agent_id))
        agent_obj = agent_result.scalar_one_or_none()
        agent_rate_limit = (agent_obj.webhook_rate_limit if agent_obj else None) or DEFAULT_RATE_LIMIT
        agent_key = f"ratelimit:webhook_agent:{target_agent_id}"
        if not await check_rate_limit(agent_key, agent_rate_limit, 60):
            logger.warning("Webhook per-agent rate limit (%d/min) for token %s...", agent_rate_limit, token[:8])
            try:
                from app.models.audit import AuditLog

                db.add(
                    AuditLog(
                        agent_id=target_agent_id,
                        action="webhook_rate_limited",
                        details={
                            "trigger_name": target_name,
                            "limit": agent_rate_limit,
                            "token_prefix": token[:8],
                        },
                    )
                )
                await db.commit()
            except Exception as e:
                logger.error("Failed to log webhook rate limit audit: %s", e)
            return JSONResponse({"ok": True}, status_code=429)

        cfg = target_config

        # HMAC signature verification (optional)
        secret = cfg.get("secret")
        if secret:
            sig_header = request.headers.get("x-hub-signature-256", "")
            expected_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig_header, expected_sig):
                logger.warning(f"Webhook signature mismatch for trigger {target_name}")
                # Still return 200 to not leak info
                return JSONResponse({"ok": True})

        # Parse payload
        try:
            payload_str = body.decode("utf-8")
            # Try to pretty-format JSON for readability
            try:
                payload_obj = json.loads(payload_str)
                payload_str = json.dumps(payload_obj, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                logger.debug(
                    "Webhook payload is not valid JSON for trigger token %s..., keeping as raw string", token[:8]
                )
        except Exception:
            payload_str = repr(body[:2000])

        # Store payload and set pending flag
        if cfg.get("_webhook_pending"):
            logger.warning(
                "[Webhook] Overwriting pending payload for trigger %s — previous event may be lost", target_name
            )
        if len(payload_str) > 8000:
            logger.warning("Webhook payload truncated for trigger %s: %d->8000 chars", target_name, len(payload_str))
        new_config = {**cfg, "_webhook_pending": True, "_webhook_payload": payload_str[:8000]}
        from sqlalchemy import update

        await db.execute(update(AgentTrigger).where(AgentTrigger.id == target_id).values(config=new_config))
        await db.commit()

        logger.info(f"Webhook received for trigger {target_name} (agent {target_agent_id})")

    return JSONResponse({"ok": True})
