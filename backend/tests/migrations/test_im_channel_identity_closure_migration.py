from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "im_channel_identity_closure_0718.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("im_channel_identity_closure_0718", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_im_identity_migration_has_single_head_parent_and_explicit_authority_columns():
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "im_channel_identity_closure_0718"
    assert module.down_revision == "collaboration_runtime_closure_0717"
    assert "self_identity_user_id" in source
    assert "self_identity_verified_at" in source
    assert "principal_type" in source
    assert "ck_session_commands_principal_type" in source
    assert "uq_session_commands_idempotency" in source
    assert "sc.principal_type = 'external_principal'" in source
    assert "principal_id = cs.external_principal_id" in source
    assert "self_identity_user_id IS NULL" in source
    assert "channel_type = 'wechat_personal'" in source
    assert "self_identity_verified_at IS NOT NULL" in source


def test_legacy_wechat_backfill_requires_existing_verified_principal_binding():
    module = _load_module()
    sql = module.build_verified_wechat_identity_backfill_sql()

    assert "p.channel_config_id = cc.id" in sql
    assert "p.provider = 'wechat_personal'" in sql
    assert "p.status = 'active'" in sql
    assert "p.linked_user_id IS NOT NULL" in sql
    assert "p.subject_id = NULLIF(cc.extra_config ->> 'ilink_user_id', '')" in sql
    assert "COUNT(DISTINCT p.linked_user_id) = 1" in sql
    assert "COALESCE(a.owner_user_id, a.creator_id)" not in sql


async def test_parent_upgrade_backfills_only_existing_verified_wechat_binding(
    revision_parent_migrated_pg_url: str,
) -> None:
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.external_principal import ExternalPrincipal
    from app.models.session_v2 import SessionCommand
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    (
        tenant_id,
        owner_id,
        identity_user_id,
        verified_agent_id,
        unbound_agent_id,
        admin_assigned_agent_id,
        legacy_feishu_agent_id,
    ) = (uuid4() for _ in range(7))
    verified_config_id, unbound_config_id, admin_assigned_config_id, legacy_feishu_config_id = (
        uuid4() for _ in range(4)
    )
    verified_principal_id, unbound_principal_id, admin_assigned_principal_id, legacy_feishu_principal_id = (
        uuid4() for _ in range(4)
    )
    legacy_session_id, legacy_command_id = uuid4(), uuid4()
    try:
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="IM Identity Upgrade", slug=f"im-upgrade-{tenant_id.hex[:8]}"))
            db.add_all(
                [
                    User(
                        id=owner_id,
                        username=f"im-upgrade-owner-{owner_id.hex[:8]}",
                        email=f"{owner_id.hex[:8]}@im-upgrade.test",
                        password_hash="x",
                        display_name="IM Upgrade Owner",
                        tenant_id=tenant_id,
                    ),
                    User(
                        id=identity_user_id,
                        username=f"im-upgrade-identity-{identity_user_id.hex[:8]}",
                        email=f"{identity_user_id.hex[:8]}@im-upgrade.test",
                        password_hash="x",
                        display_name="IM Identity User",
                        tenant_id=tenant_id,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    Agent(
                        id=verified_agent_id,
                        tenant_id=tenant_id,
                        name="Verified WeChat",
                        creator_id=owner_id,
                    ),
                    Agent(
                        id=unbound_agent_id,
                        tenant_id=tenant_id,
                        name="Unbound WeChat",
                        creator_id=owner_id,
                    ),
                    Agent(
                        id=admin_assigned_agent_id,
                        tenant_id=tenant_id,
                        name="Admin-assigned WeChat",
                        creator_id=owner_id,
                    ),
                    Agent(
                        id=legacy_feishu_agent_id,
                        tenant_id=tenant_id,
                        name="Legacy Feishu",
                        creator_id=owner_id,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ChannelConfig(
                        id=verified_config_id,
                        tenant_id=tenant_id,
                        agent_id=verified_agent_id,
                        channel_type="wechat_personal",
                        is_configured=True,
                        is_connected=True,
                        extra_config={"ilink_user_id": "verified-subject"},
                    ),
                    ChannelConfig(
                        id=unbound_config_id,
                        tenant_id=tenant_id,
                        agent_id=unbound_agent_id,
                        channel_type="wechat_personal",
                        is_configured=True,
                        is_connected=True,
                        extra_config={"ilink_user_id": "unbound-subject"},
                    ),
                    ChannelConfig(
                        id=admin_assigned_config_id,
                        tenant_id=tenant_id,
                        agent_id=admin_assigned_agent_id,
                        channel_type="wechat_personal",
                        is_configured=True,
                        is_connected=True,
                        extra_config={"ilink_user_id": "admin-assigned-subject"},
                    ),
                    ChannelConfig(
                        id=legacy_feishu_config_id,
                        tenant_id=tenant_id,
                        agent_id=legacy_feishu_agent_id,
                        channel_type="feishu",
                        is_configured=True,
                        is_connected=True,
                        extra_config={"connection_mode": "websocket"},
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ExternalPrincipal(
                        id=verified_principal_id,
                        tenant_id=tenant_id,
                        provider="wechat_personal",
                        installation_ref=str(verified_config_id),
                        channel_config_id=verified_config_id,
                        subject_id="verified-subject",
                        display_name="Verified",
                        profile_json={"identity_source": "authenticated_channel_connect"},
                        linked_user_id=identity_user_id,
                        linked_at=datetime.now(UTC),
                        binding_method="wechat_qr",
                        binding_verified_at=datetime.now(UTC),
                    ),
                    ExternalPrincipal(
                        id=unbound_principal_id,
                        tenant_id=tenant_id,
                        provider="wechat_personal",
                        installation_ref=str(unbound_config_id),
                        channel_config_id=unbound_config_id,
                        subject_id="unbound-subject",
                        display_name="Unbound",
                        linked_user_id=None,
                    ),
                    ExternalPrincipal(
                        id=admin_assigned_principal_id,
                        tenant_id=tenant_id,
                        provider="wechat_personal",
                        installation_ref=str(admin_assigned_config_id),
                        channel_config_id=admin_assigned_config_id,
                        subject_id="admin-assigned-subject",
                        display_name="Admin assigned",
                        linked_user_id=identity_user_id,
                        linked_at=datetime.now(UTC),
                        binding_method="wechat_qr",
                        binding_verified_at=datetime.now(UTC),
                    ),
                    ExternalPrincipal(
                        id=legacy_feishu_principal_id,
                        tenant_id=tenant_id,
                        provider="feishu",
                        installation_ref=str(legacy_feishu_config_id),
                        channel_config_id=legacy_feishu_config_id,
                        subject_id="ou_legacy_feishu",
                        display_name="Legacy Feishu",
                        linked_user_id=identity_user_id,
                        linked_at=datetime.now(UTC),
                        binding_method="feishu_qr",
                        binding_verified_at=datetime.now(UTC),
                    ),
                ]
            )
            await db.flush()
            db.add(
                ChatSession(
                    id=legacy_session_id,
                    tenant_id=tenant_id,
                    agent_id=verified_agent_id,
                    user_id=identity_user_id,
                    external_principal_id=verified_principal_id,
                    title="Legacy bound channel command",
                    source_channel="wechat_personal",
                    external_conv_id="wechat_p2p_verified-subject",
                    session_kind="human_chat",
                    actor_type="external_principal",
                    runtime_source="channel_chat",
                    visibility_scope="direct_user",
                    listed_surface="chat",
                )
            )
            await db.flush()
            db.add(
                SessionCommand(
                    id=legacy_command_id,
                    tenant_id=tenant_id,
                    principal_type="user",
                    principal_id=identity_user_id,
                    session_id=legacy_session_id,
                    namespace="human_input",
                    idempotency_key="legacy-channel-event",
                    command_kind="submit_human_input",
                    request_hash="0" * 64,
                    target_hash="1" * 64,
                    request_json={"content": "legacy"},
                    target_json={"session_id": str(legacy_session_id)},
                    status="accepted",
                    receipt_ref=f"session-command:{legacy_command_id}",
                )
            )
            await db.commit()

        _alembic_downgrade(revision_parent_migrated_pg_url, "collaboration_runtime_closure_0717")
        # Reconstruct the exact pre-proof production state after the safe
        # downgrade removed Feishu authority. These rows have a linked User but
        # no typed provider proof columns yet.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE external_principals SET linked_user_id = :user_id, linked_at = NOW() "
                    "WHERE id = :principal_id"
                ),
                {"user_id": identity_user_id, "principal_id": legacy_feishu_principal_id},
            )
            await connection.execute(
                text(
                    "UPDATE channel_configs SET is_configured = TRUE, is_connected = TRUE, "
                    "extra_config = '{\"connection_mode\":\"websocket\"}'::json WHERE id = :config_id"
                ),
                {"config_id": legacy_feishu_config_id},
            )
        _alembic_upgrade(revision_parent_migrated_pg_url, "head")

        async with session_factory() as db:
            verified = await db.get(ChannelConfig, verified_config_id)
            unbound = await db.get(ChannelConfig, unbound_config_id)
            assert verified is not None
            assert verified.self_identity_user_id == identity_user_id
            assert verified.self_identity_verified_at is not None
            assert unbound is not None
            assert unbound.self_identity_user_id is None
            assert unbound.self_identity_verified_at is None
            admin_assigned = await db.get(ChannelConfig, admin_assigned_config_id)
            admin_assigned_principal = await db.get(ExternalPrincipal, admin_assigned_principal_id)
            assert admin_assigned is not None
            assert admin_assigned.self_identity_user_id is None
            assert admin_assigned.is_connected is False
            assert admin_assigned_principal is not None
            assert admin_assigned_principal.linked_user_id is None
            assert admin_assigned_principal.binding_method is None
            legacy_feishu = await db.get(ChannelConfig, legacy_feishu_config_id)
            legacy_feishu_principal = await db.get(ExternalPrincipal, legacy_feishu_principal_id)
            assert legacy_feishu is not None
            assert legacy_feishu.is_configured is False
            assert legacy_feishu.is_connected is False
            assert legacy_feishu.extra_config["connection_status"] == "identity_rebind_required"
            assert legacy_feishu.extra_config["identity_status"] == "rebind_required"
            assert legacy_feishu_principal is not None
            assert legacy_feishu_principal.linked_user_id is None
            assert legacy_feishu_principal.binding_method is None
            migrated_command = await db.get(SessionCommand, legacy_command_id)
            assert migrated_command is not None
            assert migrated_command.principal_type == "external_principal"
            assert migrated_command.principal_id == verified_principal_id

            legacy_session = await db.get(ChatSession, legacy_session_id)
            assert legacy_session is not None
            legacy_session.user_id = None
            await db.flush()
            identity_user = await db.get(User, identity_user_id)
            assert identity_user is not None
            await db.delete(identity_user)
            await db.commit()

        async with session_factory() as db:
            verified_after_user_delete = await db.get(ChannelConfig, verified_config_id)
            assert verified_after_user_delete is not None
            assert verified_after_user_delete.self_identity_user_id is None
            assert verified_after_user_delete.self_identity_verified_at is not None
    finally:
        _alembic_upgrade(revision_parent_migrated_pg_url, "head")
        await engine.dispose()
