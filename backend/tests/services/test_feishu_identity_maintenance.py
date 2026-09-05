from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select


def _row_snapshot(instance) -> dict:
    return {attr.key: getattr(instance, attr.key) for attr in sa_inspect(instance).mapper.column_attrs}


def test_build_feishu_p2p_conv_id_prefers_user_id():
    from app.services.feishu_identity_maintenance import build_feishu_p2p_conv_id

    assert build_feishu_p2p_conv_id("u_123", "ou_456") == "feishu_p2p_u_123"
    assert build_feishu_p2p_conv_id(None, "ou_456") == "feishu_p2p_ou_456"


def test_choose_canonical_feishu_user_prefers_stable_user_id_real_email_then_oldest():
    from app.services.feishu_identity_maintenance import choose_canonical_feishu_user

    older = datetime(2025, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2025, 2, 1, tzinfo=timezone.utc)

    duplicate_with_fake_email = SimpleNamespace(
        id="dup-fake",
        email="dup@feishu.local",
        feishu_user_id="u_123",
        created_at=older,
    )
    canonical = SimpleNamespace(
        id="canonical",
        email="real@company.com",
        feishu_user_id="u_123",
        created_at=newer,
    )
    weak_candidate = SimpleNamespace(
        id="weak",
        email="weak@company.com",
        feishu_user_id=None,
        created_at=older,
    )

    picked = choose_canonical_feishu_user([duplicate_with_fake_email, canonical, weak_candidate])

    assert picked is canonical


@pytest.mark.asyncio
async def test_find_or_create_feishu_chat_session_uses_canonical_user_id_and_legacy_open_id(monkeypatch):
    from app.services import feishu_identity_maintenance

    agent_id = uuid4()
    user_id = uuid4()
    db = object()
    session = SimpleNamespace(id=uuid4())
    captured = {}

    async def _fake_find_or_create_channel_session(**kwargs):
        captured.update(kwargs)
        return session

    monkeypatch.setattr(
        "app.services.channel_session.find_or_create_channel_session",
        _fake_find_or_create_channel_session,
    )

    result = await feishu_identity_maintenance.find_or_create_feishu_chat_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        provider_user_id=" u_staff_123 ",
        provider_open_id=" ou_app_scoped ",
        first_message_title="[Agent → Example User B]",
    )

    assert result is session
    assert captured["db"] is db
    assert captured["agent_id"] is agent_id
    assert captured["user_id"] is user_id
    assert captured["source_channel"] == "feishu"
    assert captured["external_conv_id"] == "feishu_p2p_u_staff_123"
    assert captured["legacy_external_conv_ids"] == ["feishu_p2p_ou_app_scoped"]
    assert captured["first_message_title"] == "[Agent → Example User B]"


# --- Real-PostgreSQL tenant-boundary regressions --------------------------------
#
# These helpers run as ops maintenance under an explicit audited RLS bypass
# (app/scripts/cleanup_duplicate_feishu_users.py), so RLS cannot be the tenant
# boundary: the queries themselves must keep provider identities scoped to the
# owning tenant. Provider user ids are not globally unique in the Hive schema.


def _feishu_user(*, user_id: uuid.UUID, tenant_id, feishu_user_id: str, email: str, display_name: str):
    from app.models.user import User

    return User(
        id=user_id,
        username=f"fm-{user_id.hex[:10]}",
        email=email,
        password_hash="x",
        display_name=display_name,
        tenant_id=tenant_id,
        feishu_user_id=feishu_user_id,
    )


def _tenant_row(tenant_id, name: str):
    from app.models.tenant import Tenant

    return Tenant(id=tenant_id, name=name, slug=f"fm-{tenant_id.hex[:12]}")


@pytest.mark.asyncio
async def test_reconcile_feishu_identity_state_never_merges_users_across_tenants(owner_sessionmaker):
    """Two tenants sharing one feishu_user_id keep both Users: merged == 0."""
    from app.database import enter_rls_bypass
    from app.services.feishu_identity_maintenance import reconcile_feishu_identity_state

    tenant_a, tenant_b = uuid4(), uuid4()
    user_a, user_b, tenantless = uuid4(), uuid4(), uuid4()
    shared_feishu_user_id = f"u_shared_{uuid4().hex[:8]}"

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                _tenant_row(tenant_a, "Feishu maintenance tenant A"),
                _tenant_row(tenant_b, "Feishu maintenance tenant B"),
                _feishu_user(
                    user_id=user_a,
                    tenant_id=tenant_a,
                    feishu_user_id=shared_feishu_user_id,
                    email=f"fm-a-{user_a.hex[:10]}@test.local",
                    display_name="Tenant A Feishu User",
                ),
                _feishu_user(
                    user_id=user_b,
                    tenant_id=tenant_b,
                    feishu_user_id=shared_feishu_user_id,
                    email=f"fm-b-{user_b.hex[:10]}@test.local",
                    display_name="Tenant B Feishu User",
                ),
                _feishu_user(
                    user_id=tenantless,
                    tenant_id=None,
                    feishu_user_id=shared_feishu_user_id,
                    email=f"fm-plat-{tenantless.hex[:10]}@test.local",
                    display_name="Tenantless Platform Feishu User",
                ),
            ]
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test: feishu maintenance cross-tenant isolation") as bdb:
            stats = await reconcile_feishu_identity_state(bdb)
            await bdb.commit()

    assert stats["merged_users"] == 0

    from app.models.user import User

    async with owner_sessionmaker() as db:
        for user_id, tenant_id in ((user_a, tenant_a), (user_b, tenant_b)):
            user = await db.get(User, user_id)
            assert user is not None, f"user {user_id} must survive cross-tenant maintenance"
            assert user.tenant_id == tenant_id
            assert user.feishu_user_id == shared_feishu_user_id
        platform_user = await db.get(User, tenantless)
        assert platform_user is not None
        assert platform_user.tenant_id is None


@pytest.mark.asyncio
async def test_merge_duplicate_feishu_users_merges_same_tenant_duplicate_and_moves_references(owner_sessionmaker):
    """A legitimate same-tenant duplicate still merges, references move in-tenant,
    and the globally unique open ID transfers from the placeholder duplicate to
    the real-email canonical survivor."""
    from app.database import enter_rls_bypass
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.identity import ExternalIdentity, IdentityProvider
    from app.models.participant import Participant
    from app.models.user import User

    from app.services.feishu_identity_maintenance import merge_duplicate_feishu_users

    tenant_id = uuid4()
    primary_id, duplicate_id = uuid4(), uuid4()
    agent_id = uuid4()
    feishu_user_id = f"u_dup_{uuid4().hex[:8]}"
    primary_email = f"fm-real-{primary_id.hex[:10]}@company.test"
    duplicate_union_id = f"on_{uuid4().hex[:8]}"
    duplicate_open_id = f"ou_dup_{uuid4().hex[:8]}"

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                _tenant_row(tenant_id, "Feishu maintenance same-tenant"),
                _feishu_user(
                    user_id=primary_id,
                    tenant_id=tenant_id,
                    feishu_user_id=feishu_user_id,
                    email=primary_email,
                    display_name="Same-tenant primary",
                ),
                User(
                    id=duplicate_id,
                    username=f"fm-{duplicate_id.hex[:10]}",
                    email=f"fm-{duplicate_id.hex[:10]}@feishu.local",
                    password_hash="x",
                    display_name="Same-tenant duplicate",
                    tenant_id=tenant_id,
                    feishu_user_id=feishu_user_id,
                    feishu_open_id=duplicate_open_id,
                    feishu_union_id=duplicate_union_id,
                    avatar_url="https://cdn.test.invalid/duplicate-avatar.png",
                ),
            ]
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Same-tenant merge agent", creator_id=primary_id))
        provider = IdentityProvider(
            provider_type="feishu",
            name="Same-tenant feishu provider",
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()
        primary_participant = Participant(
            type="user",
            ref_id=primary_id,
            display_name="Same-tenant primary",
        )
        duplicate_participant = Participant(
            type="user",
            ref_id=duplicate_id,
            display_name="Same-tenant duplicate",
        )
        db.add_all([primary_participant, duplicate_participant])
        await db.flush()
        session_row = ChatSession(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=duplicate_id,
            source_channel="feishu",
            external_conv_id=f"feishu_p2p_{feishu_user_id}",
            participant_id=duplicate_participant.id,
        )
        message_row = ChatMessage(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=duplicate_id,
            role="user",
            content="duplicate-owned message",
            conversation_id="legacy-conv",
            participant_id=duplicate_participant.id,
        )
        identity_row = ExternalIdentity(
            provider_id=provider.id,
            user_id=duplicate_id,
            provider_user_id=feishu_user_id,
        )
        db.add_all([session_row, message_row, identity_row])
        await db.commit()
        session_uuid, message_uuid, identity_uuid = session_row.id, message_row.id, identity_row.id
        primary_participant_id = primary_participant.id

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test: feishu maintenance same-tenant merge") as bdb:
            merged = await merge_duplicate_feishu_users(bdb)
            await bdb.commit()

    assert merged == 1

    async with owner_sessionmaker() as db:
        assert await db.get(User, duplicate_id) is None
        primary = await db.get(User, primary_id)
        assert primary is not None
        assert primary.email == primary_email
        assert primary.feishu_open_id == duplicate_open_id
        assert primary.feishu_union_id == duplicate_union_id
        assert primary.avatar_url == "https://cdn.test.invalid/duplicate-avatar.png"
        moved_session = await db.get(ChatSession, session_uuid)
        moved_message = await db.get(ChatMessage, message_uuid)
        moved_identity = await db.get(ExternalIdentity, identity_uuid)
        assert moved_session.user_id == primary_id
        assert moved_session.participant_id == primary_participant_id
        assert moved_message.user_id == primary_id
        assert moved_message.participant_id == primary_participant_id
        assert moved_identity.user_id == primary_id
        leftover_participant = await db.scalar(
            select(Participant).where(Participant.type == "user", Participant.ref_id == duplicate_id)
        )
        assert leftover_participant is None


@pytest.mark.asyncio
async def test_normalize_feishu_chat_sessions_leaves_foreign_tenant_message_untouched(owner_sessionmaker):
    """Legacy→canonical merge moves only same-tenant messages sharing the conv id string."""
    from app.database import enter_rls_bypass
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.services.feishu_identity_maintenance import normalize_feishu_chat_sessions

    tenant_a, tenant_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    agent_a, agent_b = uuid4(), uuid4()
    feishu_user_id = f"u_norm_{uuid4().hex[:8]}"
    feishu_open_id = f"ou_norm_{uuid4().hex[:8]}"

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                _tenant_row(tenant_a, "Feishu normalization tenant A"),
                _tenant_row(tenant_b, "Feishu normalization tenant B"),
                _feishu_user(
                    user_id=user_a,
                    tenant_id=tenant_a,
                    feishu_user_id=feishu_user_id,
                    email=f"fm-na-{user_a.hex[:10]}@test.local",
                    display_name="Tenant A normalization user",
                ),
                _feishu_user(
                    user_id=user_b,
                    tenant_id=tenant_b,
                    feishu_user_id=None,
                    email=f"fm-nb-{user_b.hex[:10]}@test.local",
                    display_name="Tenant B bystander user",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Agent(id=agent_a, tenant_id=tenant_a, name="Tenant A feishu agent", creator_id=user_a),
                Agent(id=agent_b, tenant_id=tenant_b, name="Tenant B bystander agent", creator_id=user_b),
            ]
        )
        await db.flush()
        legacy_session = ChatSession(
            tenant_id=tenant_a,
            agent_id=agent_a,
            user_id=user_a,
            source_channel="feishu",
            external_conv_id=f"feishu_p2p_{feishu_open_id}",
        )
        db.add(legacy_session)
        await db.flush()
        canonical_session = ChatSession(
            tenant_id=tenant_a,
            agent_id=agent_a,
            user_id=user_a,
            source_channel="feishu",
            external_conv_id=f"feishu_p2p_{feishu_user_id}",
        )
        db.add(canonical_session)
        await db.flush()
        same_tenant_message = ChatMessage(
            tenant_id=tenant_a,
            agent_id=agent_a,
            user_id=user_a,
            role="user",
            content="tenant A message on the legacy session",
            conversation_id=str(legacy_session.id),
        )
        # A valid tenant-B message whose free-string conversation_id happens to
        # equal tenant A's legacy session UUID.
        foreign_message = ChatMessage(
            tenant_id=tenant_b,
            agent_id=agent_b,
            user_id=user_b,
            role="user",
            content="tenant B sentinel message",
            conversation_id=str(legacy_session.id),
        )
        db.add_all([same_tenant_message, foreign_message])
        await db.commit()
        legacy_session_id = legacy_session.id
        canonical_session_id = canonical_session.id
        same_tenant_message_id = same_tenant_message.id
        foreign_message_id = foreign_message.id
        foreign_before = dict(_row_snapshot(foreign_message))

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test: feishu session normalization tenant boundary") as bdb:
            normalized = await normalize_feishu_chat_sessions(bdb)
            await bdb.commit()

    # The helper scans globally; other tests' feishu sessions may also normalize,
    # so the count is only a sanity floor — the row-level outcomes are the proof.
    assert normalized >= 1

    async with owner_sessionmaker() as db:
        foreign_after = await db.get(ChatMessage, foreign_message_id)
        assert _row_snapshot(foreign_after) == foreign_before
        moved_message = await db.get(ChatMessage, same_tenant_message_id)
        assert moved_message.conversation_id == str(canonical_session_id)
        assert moved_message.user_id == user_a
        assert await db.get(ChatSession, legacy_session_id) is None
        assert await db.get(ChatSession, canonical_session_id) is not None


@pytest.mark.asyncio
async def test_merge_user_record_fails_closed_before_mutating_a_cross_tenant_pair(owner_sessionmaker):
    """The pre-mutation guard refuses a mismatched pair with zero partial mutation."""
    from app.database import enter_rls_bypass
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.user import User
    from app.services.feishu_identity_maintenance import _merge_user_record

    tenant_a, tenant_b = uuid4(), uuid4()
    primary_id, duplicate_id = uuid4(), uuid4()
    agent_b = uuid4()

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                _tenant_row(tenant_a, "Feishu guard tenant A"),
                _tenant_row(tenant_b, "Feishu guard tenant B"),
                _feishu_user(
                    user_id=primary_id,
                    tenant_id=tenant_a,
                    feishu_user_id=f"u_guard_{uuid4().hex[:8]}",
                    email=f"fm-ga-{primary_id.hex[:10]}@test.local",
                    display_name="Guard primary",
                ),
                _feishu_user(
                    user_id=duplicate_id,
                    tenant_id=tenant_b,
                    feishu_user_id=f"u_guard_{uuid4().hex[:8]}",
                    email=f"fm-gb-{duplicate_id.hex[:10]}@test.local",
                    display_name="Guard duplicate",
                ),
            ]
        )
        await db.flush()
        db.add(Agent(id=agent_b, tenant_id=tenant_b, name="Guard tenant B agent", creator_id=duplicate_id))
        await db.flush()
        message_row = ChatMessage(
            tenant_id=tenant_b,
            agent_id=agent_b,
            user_id=duplicate_id,
            role="user",
            content="guard sentinel message",
            conversation_id="guard-conv",
        )
        db.add(message_row)
        await db.commit()
        message_uuid = message_row.id

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test: feishu merge guard fails closed") as bdb:
            primary = await bdb.get(User, primary_id)
            duplicate = await bdb.get(User, duplicate_id)
            with pytest.raises(ValueError):
                await _merge_user_record(bdb, primary, duplicate)
            await bdb.rollback()

    async with owner_sessionmaker() as db:
        assert await db.get(User, primary_id) is not None
        assert await db.get(User, duplicate_id) is not None
        unchanged_message = await db.get(ChatMessage, message_uuid)
        assert unchanged_message.user_id == duplicate_id
        assert unchanged_message.content == "guard sentinel message"


@pytest.mark.parametrize(
    "target_mismatch",
    ["canonical_session_tenant", "canonical_user_tenant"],
)
@pytest.mark.asyncio
async def test_normalize_feishu_chat_sessions_skips_canonical_target_outside_the_legacy_tenant(
    owner_sessionmaker, target_mismatch
):
    """A canonical collision row the DB unique key sees must not re-key a local
    message onto a foreign-tenant session or User, and must not abort the batch."""
    from app.database import enter_rls_bypass
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.services.feishu_identity_maintenance import normalize_feishu_chat_sessions

    tenant_a, tenant_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    agent_a = uuid4()
    feishu_user_id = f"u_skip_{uuid4().hex[:8]}"
    feishu_open_id = f"ou_skip_{uuid4().hex[:8]}"
    legacy_conv_id = f"feishu_p2p_{feishu_open_id}"
    canonical_conv_id = f"feishu_p2p_{feishu_user_id}"

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                _tenant_row(tenant_a, "Feishu skip normalization tenant A"),
                _tenant_row(tenant_b, "Feishu skip normalization tenant B"),
                _feishu_user(
                    user_id=user_a,
                    tenant_id=tenant_a,
                    feishu_user_id=feishu_user_id,
                    email=f"fm-sa-{user_a.hex[:10]}@test.local",
                    display_name="Tenant A skip user",
                ),
                _feishu_user(
                    user_id=user_b,
                    tenant_id=tenant_b,
                    feishu_user_id=None,
                    email=f"fm-sb-{user_b.hex[:10]}@test.local",
                    display_name="Tenant B foreign target user",
                ),
            ]
        )
        await db.flush()
        db.add(Agent(id=agent_a, tenant_id=tenant_a, name="Tenant A skip agent", creator_id=user_a))
        await db.flush()
        legacy_session = ChatSession(
            tenant_id=tenant_a,
            agent_id=agent_a,
            user_id=user_a,
            source_channel="feishu",
            external_conv_id=legacy_conv_id,
        )
        db.add(legacy_session)
        await db.flush()
        # Holds the exact (agent_id, external_conv_id) key the legacy session
        # would rename into — the row uq_chat_sessions_agent_ext_conv still
        # sees even when its tenant or referenced User sits outside tenant A.
        canonical_session = ChatSession(
            tenant_id=tenant_b if target_mismatch == "canonical_session_tenant" else tenant_a,
            agent_id=agent_a,
            user_id=user_b,
            source_channel="feishu",
            external_conv_id=canonical_conv_id,
        )
        db.add(canonical_session)
        await db.flush()
        local_message = ChatMessage(
            tenant_id=tenant_a,
            agent_id=agent_a,
            user_id=user_a,
            role="user",
            content="tenant A message that must stay on the legacy session",
            conversation_id=str(legacy_session.id),
        )
        db.add(local_message)
        await db.commit()
        legacy_session_id = legacy_session.id
        canonical_session_id = canonical_session.id
        local_message_id = local_message.id

    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test: feishu normalization skips foreign canonical target") as bdb:
            normalized = await normalize_feishu_chat_sessions(bdb)
            await bdb.commit()

    # The skipped pair itself contributes nothing to the count; earlier feishu
    # rows in this file have already normalized by file order, and a skipped
    # pair stays skipped on every later pass.
    assert normalized == 0

    from app.models.user import User

    async with owner_sessionmaker() as db:
        unchanged_legacy = await db.get(ChatSession, legacy_session_id)
        assert unchanged_legacy is not None
        assert unchanged_legacy.external_conv_id == legacy_conv_id
        assert unchanged_legacy.user_id == user_a
        assert unchanged_legacy.tenant_id == tenant_a
        unchanged_canonical = await db.get(ChatSession, canonical_session_id)
        assert unchanged_canonical is not None
        assert unchanged_canonical.external_conv_id == canonical_conv_id
        assert unchanged_canonical.tenant_id == (
            tenant_b if target_mismatch == "canonical_session_tenant" else tenant_a
        )
        assert unchanged_canonical.user_id == user_b
        unchanged_message = await db.get(ChatMessage, local_message_id)
        assert unchanged_message.conversation_id == str(legacy_session_id)
        assert unchanged_message.user_id == user_a
        assert unchanged_message.tenant_id == tenant_a
        foreign_user = await db.get(User, user_b)
        assert foreign_user is not None
        assert foreign_user.tenant_id == tenant_b
