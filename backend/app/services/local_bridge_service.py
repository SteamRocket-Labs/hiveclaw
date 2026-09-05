"""Local Agent Bridge pairing, token, and auth helpers."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID, TENANT_SCOPE_QUARANTINE_SLUG
from app.database import enter_rls_bypass, get_current_tenant_id, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.local_agent_channel import LocalAgentChannel
from app.models.local_bridge import LocalAgentBridgeConnection, LocalAgentBridgePairingSession
from app.models.tenant import Tenant
from app.models.user import User

BRIDGE_TOKEN_PREFIX = "hb_"
DEFAULT_PAIRING_EXPIRES_SECONDS = 15 * 60
DEFAULT_PAIRING_POLL_INTERVAL_SECONDS = 3
DEFAULT_BRIDGE_TOKEN_TTL_DAYS = 30
DEFAULT_SCOPES = (
    "local_agent:connect",
    "local_agent:receive",
    "local_agent:send",
    "local_agent:report",
    "presence:write",
    "files:upload",
)
HIVE_CONNECT_PRODUCT_NAME = "Hive Connect"
HIVE_CONNECT_SKILL_NAME = "hive-connect"
HIVE_CONNECT_SKILL_REPO_URL_ENV = "HIVE_CONNECT_SKILL_REPO_URL"
HIVE_CONNECT_NPM_PACKAGE_ENV = "HIVE_CONNECT_NPM_PACKAGE"
HIVE_CONNECT_BINARY_NAME = "hive-connect"
HIVE_CONNECT_CLIENT_KIND = "hive-connect"
LOCAL_AGENT_PRESENCE_ONLINE_TTL_SECONDS = 90
LOCAL_AGENT_POLICY_SEED = "local_agent_action_gov_0712"
LOCAL_AGENT_POLICY_DEFAULTS: tuple[tuple[str, tuple[bool, bool]], ...] = (
    ("local_agent.execute", (True, True)),
    ("local_agent.file_download", (True, True)),
    ("local_agent.file_upload", (True, True)),
    ("local_agent.event_stream", (True, False)),
    ("local_agent.result_report", (True, False)),
)


async def _ensure_local_agent_capability_policies(db: AsyncSession, *, agent: Agent) -> None:
    """Seed only missing per-agent policies; never overwrite owner/admin choices."""

    capabilities = tuple(capability for capability, _decision in LOCAL_AGENT_POLICY_DEFAULTS)
    existing = (
        (
            await db.execute(
                select(CapabilityPolicy).where(
                    CapabilityPolicy.tenant_id == agent.tenant_id,
                    CapabilityPolicy.agent_id == agent.id,
                    CapabilityPolicy.capability.in_(capabilities),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_names = {policy.capability for policy in existing}
    for capability, (allowed, requires_approval) in LOCAL_AGENT_POLICY_DEFAULTS:
        if capability in existing_names:
            continue
        db.add(
            CapabilityPolicy(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                capability=capability,
                allowed=allowed,
                requires_approval=requires_approval,
                conditions={
                    "seeded_by": LOCAL_AGENT_POLICY_SEED,
                    "action_default": "require_owner_approval" if requires_approval else "protocol_receipt",
                },
            )
        )
    await db.flush()


async def _return_seeded_local_agent(db: AsyncSession, agent: Agent) -> Agent:
    await _ensure_local_agent_capability_policies(db, agent=agent)
    return agent


def hive_connect_install_guide(*, base_url: str | None = None) -> dict[str, Any]:
    """Return the product-owned local runtime install guide for Hub surfaces."""

    skill_repo_url = os.getenv(HIVE_CONNECT_SKILL_REPO_URL_ENV, "").strip()
    npm_package = os.getenv(HIVE_CONNECT_NPM_PACKAGE_ENV, "").strip()
    sources_configured = bool(skill_repo_url and npm_package)
    install_skill = f"npx skills add {skill_repo_url} --skill {HIVE_CONNECT_SKILL_NAME}" if sources_configured else ""
    install_cli = f"npm install -g {npm_package}" if sources_configured else ""
    login = f"{HIVE_CONNECT_BINARY_NAME} login" if sources_configured else ""
    status = f"{HIVE_CONNECT_BINARY_NAME} status" if sources_configured else ""
    run = (
        f"{HIVE_CONNECT_BINARY_NAME} daemon install --config ~/.hive-connect/config.toml --force"
        if sources_configured
        else ""
    )
    user_prompt = "帮我安装 Hive Connect skill，并连接到 Hive。"
    if sources_configured:
        daemon_status = f"{HIVE_CONNECT_BINARY_NAME} daemon status"
        instructions = [
            user_prompt,
            "请按下面流程自动完成：",
            f"1. 执行 {install_skill} 安装 Hive Connect skill。",
            f"2. 按 skill 执行 {install_cli} 安装本地 CLI。",
            f"3. 执行 {login}。",
            "4. 浏览器打开 Hive 后登录；Hive 会自动完成本地 Agent 认证，不需要复制任何一次性码。",
            f"5. 执行 {run}，安装并启动后台常驻服务。",
            f"6. 执行 {daemon_status}，确认后台服务正在运行。",
            f"7. 可选：执行 {status}，确认本机仍保留 Hive 登录绑定（这不代表在线）。",
            "8. 回到 Hive 页面查看本地 Agent 在线标记；如果离线，重新执行第 5-6 步，不要重复 login。",
        ]
    else:
        instructions = [
            user_prompt,
            "请按下面流程自动完成：",
            "1. Hive Connect 安装源不可用（unavailable）；请停止并联系 Hive 管理员，配置已批准的 "
            "HIVE_CONNECT_SKILL_REPO_URL 与 HIVE_CONNECT_NPM_PACKAGE。不要猜测仓库或 package 名称。",
        ]
    return {
        "product_name": HIVE_CONNECT_PRODUCT_NAME,
        "skill_repo_url": skill_repo_url,
        "skill_name": HIVE_CONNECT_SKILL_NAME,
        "npm_package": npm_package,
        "binary_name": HIVE_CONNECT_BINARY_NAME,
        "install_skill_command": install_skill,
        "install_cli_command": install_cli,
        "login_command": login,
        "status_command": status,
        "run_command": run,
        "user_prompt": user_prompt,
        "instructions": instructions,
    }


@dataclass(frozen=True)
class BridgeAuthContext:
    """Resolved runtime identity for one local bridge connection."""

    connection_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None
    user_id: uuid.UUID
    scopes: tuple[str, ...]
    client_kind: str
    device_name: str


async def require_local_agent_capability_policy(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    capability: str,
) -> CapabilityPolicy:
    """Resolve the live tenant/Agent policy for one bridge side effect."""

    if agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Local bridge connection is not bound to an Agent",
        )
    policies = (
        (
            await db.execute(
                select(CapabilityPolicy).where(
                    CapabilityPolicy.tenant_id == tenant_id,
                    or_(CapabilityPolicy.agent_id.is_(None), CapabilityPolicy.agent_id == agent_id),
                    CapabilityPolicy.capability == capability,
                )
            )
        )
        .scalars()
        .all()
    )
    agent_policy = next((row for row in policies if row.agent_id == agent_id), None)
    tenant_policy = next((row for row in policies if row.agent_id is None), None)
    policy = agent_policy or tenant_policy
    if policy is None or not policy.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Local Agent capability denied by live policy: {capability}",
        )
    return policy


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_user_code(user_code: str) -> str:
    return user_code.strip().upper().replace(" ", "")


def generate_user_code() -> str:
    # Avoid ambiguous characters where possible; still short enough to read.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"HIVE-{suffix[:4]}-{suffix[4:]}"


def generate_device_code() -> str:
    return secrets.token_urlsafe(48)


def generate_bridge_token() -> str:
    return f"{BRIDGE_TOKEN_PREFIX}{secrets.token_urlsafe(48)}"


def normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> list[str]:
    requested = [str(scope).strip() for scope in (scopes or DEFAULT_SCOPES) if str(scope).strip()]
    allowed = set(DEFAULT_SCOPES)
    normalized = [scope for scope in dict.fromkeys(requested) if scope in allowed]
    return normalized or list(DEFAULT_SCOPES)


def _verification_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/local-bridge/activate"


def _pairing_public_payload(
    *,
    session: LocalAgentBridgePairingSession,
    device_code: str,
    user_code: str,
    base_url: str,
) -> dict[str, Any]:
    verification_uri = _verification_uri(base_url)
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?user_code={user_code}",
        "expires_in": DEFAULT_PAIRING_EXPIRES_SECONDS,
        "interval": DEFAULT_PAIRING_POLL_INTERVAL_SECONDS,
        "pairing_id": str(session.id),
    }


async def create_pairing_session(db: AsyncSession, request: Any, *, base_url: str) -> dict[str, Any]:
    """Create an unbound device-flow pairing session for a local CLI.

    The device flow starts WITHOUT a Hive JWT (docs/local-agent-bridge-first-
    pass): tenant/user/agent authority is bound only by browser approval, and
    the request body never carries tenant/user/agent. With no tenant context
    pinned, the genuinely unbound pending pairing is held in the platform
    quarantine scope (the code-only holding scope seeded by
    tenant_null_semantics_0712 and hidden from tenant listings) under an
    audited RLS bypass; approval rebinds it to the authenticated tenant/user.
    """

    user_code = generate_user_code()
    device_code = generate_device_code()
    current_tenant = get_current_tenant_id()
    unbound = not current_tenant
    session = LocalAgentBridgePairingSession(
        tenant_id=uuid.UUID(current_tenant) if current_tenant else TENANT_SCOPE_QUARANTINE_ID,
        pairing_code_hash=hash_secret(normalize_user_code(user_code)),
        device_code_hash=hash_secret(device_code),
        device_name=str(request.device_name or "Local Agent").strip()[:255],
        client_kind=str(request.client_kind or HIVE_CONNECT_CLIENT_KIND).strip()[:64],
        device_fingerprint=str(request.device_fingerprint or "unknown").strip()[:255],
        scopes=normalize_scopes(getattr(request, "scopes", None)),
        status="pending",
        metadata_json=(
            {
                "tenant_binding": "unbound_pending_pairing",
                "holding_scope": TENANT_SCOPE_QUARANTINE_SLUG,
            }
            if unbound
            else {}
        ),
        expires_at=utcnow() + timedelta(seconds=DEFAULT_PAIRING_EXPIRES_SECONDS),
    )
    if unbound:
        # The core insert below writes the row directly, so the ORM id
        # default must be materialized explicitly first.
        session.id = uuid.uuid4()
    if unbound:
        # Scanner-visible writes: the audited grant must truthfully expose
        # that this scope inserts the quarantine Tenant seed and the pairing
        # row — explicit core statements, never an invisible ORM flush.
        # Import the PostgreSQL dialect insert under the bare name so the
        # rls_bypass_manifest scanner fingerprints the actual write shape
        # (insert:Tenant / insert:LocalAgentBridgePairingSession).
        from sqlalchemy.dialects.postgresql import insert

        async with enter_rls_bypass(
            db,
            reason="anonymous local bridge pairing init (unbound pending holding scope)",
        ) as bypass_db:
            # Freshly created databases have no quarantine scope row until a
            # genuinely unbound row needs it (the 0712 migration seeds it only
            # when residual NULL-tenant rows existed). Values mirror the
            # migration seed exactly.
            await bypass_db.execute(
                insert(Tenant)
                .values(
                    id=TENANT_SCOPE_QUARANTINE_ID,
                    name="Tenant Scope Quarantine",
                    slug=TENANT_SCOPE_QUARANTINE_SLUG,
                    im_provider="web_only",
                    is_active=False,
                    min_heartbeat_interval_minutes=45,
                    timezone="UTC",
                    default_max_triggers=20,
                    min_poll_interval_floor=5,
                    max_webhook_rate_ceiling=5,
                    tokens_used_today=0,
                    tokens_used_month=0,
                    tokens_used_total=0,
                    sync_version=1,
                )
                .on_conflict_do_nothing(index_elements=[Tenant.id])
            )
            await bypass_db.execute(
                insert(LocalAgentBridgePairingSession).values(
                    id=session.id,
                    tenant_id=session.tenant_id,
                    pairing_code_hash=session.pairing_code_hash,
                    device_code_hash=session.device_code_hash,
                    device_name=session.device_name,
                    client_kind=session.client_kind,
                    device_fingerprint=session.device_fingerprint,
                    scopes=list(session.scopes or []),
                    status="pending",
                    metadata_json=dict(session.metadata_json or {}),
                    expires_at=session.expires_at,
                )
            )
            session = (
                (
                    await bypass_db.execute(
                        select(LocalAgentBridgePairingSession).where(LocalAgentBridgePairingSession.id == session.id)
                    )
                )
                .scalars()
                .one()
            )
            await bypass_db.commit()
    else:
        db.add(session)
        await db.flush()
        await db.commit()
        await db.refresh(session)
    return _pairing_public_payload(session=session, device_code=device_code, user_code=user_code, base_url=base_url)


async def _load_pairing_by_user_code(db: AsyncSession, user_code: str) -> LocalAgentBridgePairingSession:
    """Load a pairing for a MUTATION path, holding the row lock to commit.

    The FOR UPDATE fence makes concurrent approvals/exchanges serialize on
    the row: the loser re-reads the winner's terminal state instead of
    racing a stale pending snapshot. The lock survives the audited bypass
    scope until the caller's commit/rollback.
    """
    async with enter_rls_bypass(db, reason="local bridge user-code pairing lookup"):
        result = await db.execute(
            select(LocalAgentBridgePairingSession)
            .where(LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code)))
            .with_for_update()
        )
    pairing = result.scalar_one_or_none()
    if pairing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pairing request not found")
    return pairing


async def _load_pairing_by_device_code(db: AsyncSession, device_code: str) -> LocalAgentBridgePairingSession:
    """Load a pairing for a MUTATION path (exchange), holding the row lock.

    Same FOR UPDATE fence as the user-code loader: a concurrent exchange
    re-reads the winner's claimed state instead of issuing a second token.
    """
    async with enter_rls_bypass(db, reason="local bridge device-code pairing lookup"):
        result = await db.execute(
            select(LocalAgentBridgePairingSession)
            .where(LocalAgentBridgePairingSession.device_code_hash == hash_secret(device_code))
            .with_for_update()
        )
    pairing = result.scalar_one_or_none()
    if pairing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pairing request not found")
    return pairing


def _ensure_pairing_not_expired(pairing: LocalAgentBridgePairingSession) -> None:
    if pairing.expires_at <= utcnow():
        pairing.status = "expired"
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Pairing request expired")


async def _pairing_identity_is_live(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> bool:
    """Revalidate the live tenant/user membership a pairing would bind to.

    An approved pairing records a past owner decision, not live authority:
    the exact active-user / active-tenant / membership join is re-read as a
    fresh READ COMMITTED statement under the audited bypass (before any
    tenant GUC is pinned, so a quarantine-held pairing can be checked too).
    The freshness guarantee is caller-specific — see the ordering notes at
    the approve-time binding gate and the exchange pre-effect fence.
    """
    async with enter_rls_bypass(db, reason="local bridge pairing live identity check"):
        identity = await db.execute(
            select(User.id)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.id == user_id,
                User.is_active.is_(True),
                User.tenant_id == tenant_id,
                Tenant.id == tenant_id,
                Tenant.is_active.is_(True),
            )
        )
    return identity.scalar_one_or_none() is not None


def _local_agent_name_from_pairing(pairing: LocalAgentBridgePairingSession) -> str:
    raw_name = str(pairing.device_name or "").strip()
    return (raw_name or "Local Agent")[:100]


async def ensure_default_local_agent_for_pairing(
    db: AsyncSession,
    *,
    user_code: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Agent:
    """Return the real Hive Agent identity for a user-level local bridge login."""

    pairing = await _load_pairing_by_user_code(db, user_code)
    _ensure_pairing_not_expired(pairing)
    if pairing.status not in {"pending", "approved"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Pairing is {pairing.status}")
    # An approved/claimed pairing's binding is immutable: a different
    # principal holding the short user code must not read the bound agent,
    # re-approve, or trigger an attacker-side Agent creation. The guard runs
    # BEFORE any existing-agent return or new-Agent creation.
    if pairing.status in {"approved", "claimed"} and (
        str(pairing.tenant_id or "") != str(tenant_id) or str(pairing.user_id or "") != str(user_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "pairing_already_bound", "status": pairing.status},
        )

    if pairing.agent_id:
        existing = await db.get(Agent, pairing.agent_id)
        if existing is not None:
            return await _return_seeded_local_agent(db, existing)

    if pairing.device_fingerprint and pairing.device_fingerprint != "unknown":
        result = await db.execute(
            select(LocalAgentBridgeConnection)
            .where(
                LocalAgentBridgeConnection.user_id == user_id,
                LocalAgentBridgeConnection.tenant_id == tenant_id,
                LocalAgentBridgeConnection.device_fingerprint == pairing.device_fingerprint,
                LocalAgentBridgeConnection.agent_id.is_not(None),
                LocalAgentBridgeConnection.status == "active",
            )
            .order_by(LocalAgentBridgeConnection.created_at.desc(), LocalAgentBridgeConnection.id.desc())
            .limit(1)
        )
        connection = result.scalar_one_or_none()
        if connection and connection.agent_id:
            existing = await db.get(Agent, connection.agent_id)
            if existing is not None and getattr(existing, "deleted_at", None) is None:
                return await _return_seeded_local_agent(db, existing)

    agent_name = _local_agent_name_from_pairing(pairing)
    result = await db.execute(
        select(Agent)
        .where(
            Agent.creator_id == user_id,
            Agent.tenant_id == tenant_id,
            Agent.agent_type == "local_agent",
            Agent.name == agent_name,
            Agent.deleted_at.is_(None),
        )
        .order_by(Agent.created_at.desc(), Agent.id.desc())
        .limit(1)
    )
    existing_agent = result.scalar_one_or_none()
    if existing_agent is not None:
        return await _return_seeded_local_agent(db, existing_agent)

    local_agent = Agent(
        name=agent_name,
        role_description="Local runtime connected through Hive Connect.",
        welcome_message="I am your local agent endpoint connected through Hive Connect.",
        creator_id=user_id,
        sponsor_user_id=user_id,
        owner_user_id=user_id,
        tenant_id=tenant_id,
        agent_type="local_agent",
        agent_class="internal_tenant",
        security_zone="standard",
        status="running",
    )
    db.add(local_agent)
    # participants is a derived global identity table whose strict-RLS WITH
    # CHECK requires the referenced agent row while agents.participant_id
    # requires the Participant — the circular bootstrap ensure_agent_identity
    # documents and solves as an audited boundary (canonical for desktop/HR
    # creation paths; a raw flush here 500'd under app_rls).
    from app.services.agent_identity_lifecycle import ensure_agent_identity

    await ensure_agent_identity(
        db,
        local_agent,
        rls_bypass_reason="local bridge agent identity bootstrap",
        rls_bypass_actor_id=str(user_id),
    )
    from app.services.ai_assets import register_agent_asset

    await register_agent_asset(
        db,
        local_agent,
        change_source="create",
        actor_user_id=user_id,
        change_message="Hive Connect local Agent created",
    )
    return await _return_seeded_local_agent(db, local_agent)


async def approve_pairing_session(
    db: AsyncSession,
    *,
    user_code: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pairing = await _load_pairing_by_user_code(db, user_code)
    _ensure_pairing_not_expired(pairing)
    if pairing.status == "claimed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "pairing_already_claimed", "status": "claimed"},
        )
    if pairing.status == "approved":
        # Approved bindings are immutable. The exact same server-derived
        # tenant/user/agent re-approval is idempotent; anything else is a
        # typed refusal with NO mutation.
        same_binding = (
            str(pairing.tenant_id or "") == str(tenant_id)
            and str(pairing.user_id or "") == str(user_id)
            and (agent_id is None or pairing.agent_id is None or str(pairing.agent_id) == str(agent_id))
        )
        if not same_binding:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "pairing_already_bound", "status": "approved"},
            )
        return {
            "status": "approved",
            "pairing_id": str(pairing.id),
            "agent_id": str(pairing.agent_id) if pairing.agent_id else None,
            "tenant_id": str(pairing.tenant_id),
            "user_id": str(pairing.user_id),
        }
    if pairing.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Pairing is {pairing.status}")

    # Rebind the (possibly quarantine-held) pending pairing to the
    # authenticated tenant/user. Scanner-visible write: the audited grant
    # truthfully exposes the UPDATE.
    from sqlalchemy import update

    prior_metadata = dict(pairing.metadata_json or {})
    initial_holding = prior_metadata.pop("holding_scope", None) or (
        TENANT_SCOPE_QUARANTINE_SLUG
        if prior_metadata.pop("tenant_binding", None) == "unbound_pending_pairing"
        else None
    )
    prior_metadata.pop("tenant_binding", None)
    new_metadata = {
        **prior_metadata,
        **(metadata or {}),
        "tenant_binding": "approved_server_derived",
    }
    if initial_holding:
        new_metadata["initial_holding_scope"] = initial_holding
    async with enter_rls_bypass(db, reason="local bridge pairing approval tenant rebind") as bypass_db:
        claim = await bypass_db.execute(
            update(LocalAgentBridgePairingSession)
            .where(LocalAgentBridgePairingSession.id == pairing.id, LocalAgentBridgePairingSession.status == "pending")
            .values(
                user_id=user_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                status="approved",
                approved_at=utcnow(),
                metadata_json=new_metadata,
            )
        )
        if claim.rowcount != 1:
            # A concurrent approval won the row: roll back this request's
            # earlier uncommitted work (Agent/Participant/asset rows created
            # by ensure_default_local_agent_for_pairing) and fail typed.
            await bypass_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "pairing_already_bound", "status": "approved"},
            )
        # Binding gate, after the rebind UPDATE and before the commit. The
        # UPDATE's FK checks hold FOR KEY SHARE locks on the bound User and
        # Tenant rows until this transaction commits, so a concurrent
        # offboarding/retirement is in exactly one of two states: it already
        # committed (this fresh READ COMMITTED read sees the inactive truth),
        # or it is blocked on those row locks and cannot change liveness
        # until after this commit. The rollback disposes of the pairing
        # rebind plus every Agent/Participant/asset/policy row flushed by
        # ensure_default_local_agent_for_pairing earlier in this request —
        # that whole chain only flushes, so nothing survives as an orphan.
        if not await _pairing_identity_is_live(bypass_db, user_id=user_id, tenant_id=tenant_id):
            await bypass_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "pairing_identity_inactive", "status": "pending"},
            )
        await bypass_db.commit()
    return {
        "status": "approved",
        "pairing_id": str(pairing.id),
        "agent_id": str(agent_id) if agent_id else None,
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
    }


async def reject_pairing_session(
    db: AsyncSession,
    *,
    user_code: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pairing = await _load_pairing_by_user_code(db, user_code)
    _ensure_pairing_not_expired(pairing)
    if pairing.status in {"approved", "claimed"}:
        # Terminal bindings are immutable — reject can never mutate them.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "pairing_binding_terminal", "status": pairing.status},
        )
    if pairing.status == "rejected":
        # Idempotent only for the exact same binding; a different principal
        # cannot re-bind the rejected row to themselves.
        if str(pairing.tenant_id or "") != str(tenant_id) or str(pairing.user_id or "") != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "pairing_already_bound", "status": "rejected"},
            )
        return {"status": "rejected", "pairing_id": str(pairing.id)}
    if pairing.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Pairing is {pairing.status}")
    if agent_id and pairing.agent_id and str(pairing.agent_id) != str(agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pairing request not found")
    # Same truthful metadata contract as approval: the unbound holding claim
    # is replaced by the current server-derived binding, with the holding
    # scope preserved as provenance.
    prior_metadata = dict(pairing.metadata_json or {})
    initial_holding = prior_metadata.pop("holding_scope", None) or (
        TENANT_SCOPE_QUARANTINE_SLUG
        if prior_metadata.pop("tenant_binding", None) == "unbound_pending_pairing"
        else None
    )
    prior_metadata.pop("tenant_binding", None)
    new_metadata = {**prior_metadata, "tenant_binding": "rejected_server_derived"}
    if initial_holding:
        new_metadata["initial_holding_scope"] = initial_holding
    # Scanner-visible write under the audited rebind scope.
    from sqlalchemy import update

    async with enter_rls_bypass(db, reason="local bridge pairing reject tenant rebind") as bypass_db:
        claim = await bypass_db.execute(
            update(LocalAgentBridgePairingSession)
            .where(LocalAgentBridgePairingSession.id == pairing.id, LocalAgentBridgePairingSession.status == "pending")
            .values(
                user_id=user_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                status="rejected",
                rejected_at=utcnow(),
                metadata_json=new_metadata,
            )
        )
        if claim.rowcount != 1:
            await bypass_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "pairing_binding_terminal", "status": "rejected"},
            )
        await bypass_db.commit()
    return {"status": "rejected", "pairing_id": str(pairing.id)}


async def exchange_pairing_session(db: AsyncSession, *, device_code: str) -> dict[str, Any]:
    pairing = await _load_pairing_by_device_code(db, device_code)
    _ensure_pairing_not_expired(pairing)
    if pairing.status == "pending":
        return {"status": "pending", "interval": DEFAULT_PAIRING_POLL_INTERVAL_SECONDS}
    if pairing.status == "rejected":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Pairing request rejected")
    if pairing.status == "claimed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pairing token already claimed")
    if pairing.status != "approved" or not pairing.tenant_id or not pairing.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Pairing is {pairing.status}")

    # Pre-effect fence: an approved pairing records a past owner decision,
    # not live authority, so revalidate the active user/tenant membership
    # before any token or connection mutation. Unlike the approve-time gate
    # this read races a concurrent lifecycle by design — the authoritative
    # serialization here is the pairing row FOR UPDATE loader above plus the
    # connection INSERT's FK locks, and offboarding's revoke UPDATE rejects
    # or revokes whatever commits first.
    if not await _pairing_identity_is_live(db, user_id=pairing.user_id, tenant_id=pairing.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "pairing_identity_inactive", "status": "approved"},
        )

    await pin_rls_tenant_context(db, pairing.tenant_id)
    raw_token = generate_bridge_token()
    token_ttl_days = min(
        90,
        max(
            1,
            int(getattr(get_settings(), "LOCAL_BRIDGE_TOKEN_TTL_DAYS", DEFAULT_BRIDGE_TOKEN_TTL_DAYS)),
        ),
    )
    expires_at = utcnow() + timedelta(days=token_ttl_days)
    connection = LocalAgentBridgeConnection(
        tenant_id=pairing.tenant_id,
        agent_id=pairing.agent_id,
        user_id=pairing.user_id,
        device_name=pairing.device_name,
        client_kind=pairing.client_kind,
        device_fingerprint=pairing.device_fingerprint,
        token_hash=hash_secret(raw_token),
        scopes=normalize_scopes(pairing.scopes),
        status="active",
        expires_at=expires_at,
        metadata_json={"pairing_id": str(pairing.id)},
    )
    db.add(connection)
    await db.flush()
    # Claim exactly once: the status predicate plus rowcount (with the FOR
    # UPDATE loader fence) means a concurrent exchange loses typed and its
    # just-inserted connection row rolls back with the transaction.
    from sqlalchemy import update

    claim = await db.execute(
        update(LocalAgentBridgePairingSession)
        .where(LocalAgentBridgePairingSession.id == pairing.id, LocalAgentBridgePairingSession.status == "approved")
        .values(connection_id=connection.id, status="claimed", claimed_at=utcnow())
    )
    if claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "pairing_already_claimed", "status": "claimed"},
        )
    await db.commit()
    await db.refresh(connection)
    return {
        "status": "active",
        "access_token": raw_token,
        "token_type": "Bearer",
        "connection_id": str(connection.id),
        "tenant_id": str(connection.tenant_id),
        "agent_id": str(connection.agent_id) if connection.agent_id else None,
        "user_id": str(connection.user_id),
        "scopes": list(connection.scopes or []),
        "expires_at": connection.expires_at.isoformat(),
        "expires_in": max(0, int((connection.expires_at - utcnow()).total_seconds())),
    }


def _parse_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or not token.startswith(BRIDGE_TOKEN_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge token")
    return token.strip()


async def resolve_bridge_auth_context(
    db: AsyncSession,
    *,
    authorization: str | None,
    last_seen_ip: str | None = None,
    user_agent: str | None = None,
) -> BridgeAuthContext:
    token = _parse_bearer_token(authorization)
    async with enter_rls_bypass(db, reason="local bridge bearer token lookup"):
        result = await db.execute(
            select(LocalAgentBridgeConnection)
            .join(User, User.id == LocalAgentBridgeConnection.user_id)
            .join(Tenant, Tenant.id == LocalAgentBridgeConnection.tenant_id)
            .where(
                LocalAgentBridgeConnection.token_hash == hash_secret(token),
                LocalAgentBridgeConnection.status == "active",
                User.is_active.is_(True),
                User.tenant_id == LocalAgentBridgeConnection.tenant_id,
                Tenant.is_active.is_(True),
            )
        )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge token")
    await pin_rls_tenant_context(db, connection.tenant_id)
    if connection.expires_at is None or connection.expires_at <= utcnow():
        connection.status = "expired"
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bridge token expired")

    connection.last_seen_at = utcnow()
    connection.last_seen_ip = last_seen_ip
    connection.last_seen_user_agent = user_agent
    return BridgeAuthContext(
        connection_id=connection.id,
        tenant_id=connection.tenant_id,
        agent_id=connection.agent_id,
        user_id=connection.user_id,
        scopes=tuple(connection.scopes or []),
        client_kind=connection.client_kind,
        device_name=connection.device_name,
    )


def _presence_status_for(connection: LocalAgentBridgeConnection, channel: LocalAgentChannel | None) -> str:
    if connection.status != "active":
        return "offline"
    if channel is None:
        return "unknown"
    if channel.status == "online":
        if channel.last_seen_at is None:
            return "unknown"
        if utcnow() - channel.last_seen_at > timedelta(seconds=LOCAL_AGENT_PRESENCE_ONLINE_TTL_SECONDS):
            return "offline"
        return "online"
    if channel.status in {"offline", "stale"}:
        return "offline"
    return "unknown"


def serialize_connection_for_list(
    connection: LocalAgentBridgeConnection,
    *,
    channel: LocalAgentChannel | None = None,
) -> dict[str, Any]:
    presence_status = _presence_status_for(connection, channel)
    return {
        "id": str(connection.id),
        "tenant_id": str(connection.tenant_id),
        "agent_id": str(connection.agent_id) if connection.agent_id else None,
        "user_id": str(connection.user_id),
        "device_name": connection.device_name,
        "client_kind": connection.client_kind,
        "status": connection.status,
        "presence_status": presence_status,
        "presence_last_seen_at": channel.last_seen_at.isoformat() if channel and channel.last_seen_at else None,
        "runtime_kind": channel.runtime_kind if channel and channel.runtime_kind else None,
        "scopes": list(connection.scopes or []),
        "last_seen_at": connection.last_seen_at.isoformat() if connection.last_seen_at else None,
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
        "revoked_at": connection.revoked_at.isoformat() if connection.revoked_at else None,
        "expires_at": (
            connection.expires_at.isoformat() if getattr(connection, "expires_at", None) is not None else None
        ),
    }


async def get_connection_presence(db: AsyncSession, *, connection_id: uuid.UUID) -> dict[str, Any]:
    result = await db.execute(select(LocalAgentBridgeConnection).where(LocalAgentBridgeConnection.id == connection_id))
    connection = result.scalar_one_or_none()
    if connection is None:
        return {
            "presence_status": "unknown",
            "presence_last_seen_at": None,
            "runtime_kind": None,
        }

    channel_result = await db.execute(select(LocalAgentChannel).where(LocalAgentChannel.connection_id == connection_id))
    channel = channel_result.scalar_one_or_none()
    return {
        "presence_status": _presence_status_for(connection, channel),
        "presence_last_seen_at": channel.last_seen_at.isoformat() if channel and channel.last_seen_at else None,
        "runtime_kind": channel.runtime_kind if channel and channel.runtime_kind else None,
    }


async def list_connections(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = select(LocalAgentBridgeConnection).order_by(LocalAgentBridgeConnection.created_at.desc())
    if agent_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.agent_id == agent_id)
    if user_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.user_id == user_id)
    if tenant_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.tenant_id == tenant_id)
    result = await db.execute(stmt)
    connections = result.scalars().all()
    if not connections:
        return []

    connection_ids = [conn.id for conn in connections]
    channel_result = await db.execute(
        select(LocalAgentChannel).where(LocalAgentChannel.connection_id.in_(connection_ids))
    )
    channels_by_connection = {channel.connection_id: channel for channel in channel_result.scalars().all()}
    return [serialize_connection_for_list(conn, channel=channels_by_connection.get(conn.id)) for conn in connections]


async def revoke_connection(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    stmt = select(LocalAgentBridgeConnection).where(LocalAgentBridgeConnection.id == connection_id)
    if agent_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.agent_id == agent_id)
    if user_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.user_id == user_id)
    if tenant_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.tenant_id == tenant_id)
    result = await db.execute(stmt)
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bridge connection not found")
    connection.status = "revoked"
    connection.revoked_at = utcnow()
    await db.commit()
    return {"status": "revoked", "connection_id": str(connection.id)}
