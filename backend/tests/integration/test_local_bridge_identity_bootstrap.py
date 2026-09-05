"""Local bridge device-flow boundaries under the production app_rls role.

WHY THIS FILE EXISTS
--------------------
Two strict-RLS false greens were falsified on fresh real-PostgreSQL runs of
J-14 (DB ``…six_codex_fresh_1720`` and the anonymous CLI repro):

1. ``POST /api/local-bridge/pairing/init`` is the ANONYMOUS device-flow entry
   (no Hive JWT by product contract —
   docs/local-agent-bridge-first-pass-2026-06-22.md). ``create_pairing_session``
   inserted with ``tenant_id=None``, which the tenant-NOT-NULL + forced-RLS
   schema rejects: ``InsufficientPrivilegeError ... policy for
   local_agent_bridge_pairing_sessions``. The previous harness masked this by
   calling init through an owner-authenticated context.
2. ``ensure_default_local_agent_for_pairing`` created the default local Agent
   with a bare ``db.add(agent); await db.flush()``. ``participants`` is a
   derived global identity table whose strict-RLS WITH CHECK requires the
   referenced agent row to be tenant-visible while ``agents.participant_id``
   requires the Participant — the circular bootstrap that
   ``agent_identity_lifecycle.ensure_agent_identity`` documents and solves as
   an audited boundary (already canonical for desktop/HR creation paths).

Each test isolates exactly one boundary. No fake masks the RLS layer; the
app_rls role runs with the same GUC discipline as the API process.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

from sqlalchemy import select

from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID
from app.models.agent import Agent
from app.models.local_bridge import LocalAgentBridgePairingSession
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_bridge_service as bridge_service
from app.services.local_bridge_service import hash_secret, normalize_user_code, utcnow


@contextlib.asynccontextmanager
async def _anonymous_tenant_context():
    """Force the request tenant contextvar to None inside the block.

    pin_rls_tenant_context sets the contextvar; without this reset, a later
    create_pairing_session in the same task would silently take the bound
    branch instead of the anonymous one.
    """
    from app.database import reset_current_tenant, set_current_tenant

    token = set_current_tenant(None)
    try:
        yield
    finally:
        reset_current_tenant(token)


async def _seed_principals(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Bridge Tenant", slug=f"bridge-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"bridge-{suffix}",
                email=f"bridge-{suffix}@example.test",
                password_hash="x",
                display_name="Bridge Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.commit()
    return tenant_id, user_id


async def test_anonymous_pairing_init_holds_unbound_pending_in_quarantine_scope(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Boundary 1: the anonymous device-flow init must persist durably.

    No JWT, no tenant GUC — the pairing is held in the platform quarantine
    scope (the code-only holding scope seeded by tenant_null_semantics_0712,
    hidden from tenant listings) with explicit unbound metadata and NO
    user/agent authority; the request body never carries tenant/user/agent.
    """
    request = SimpleNamespace(
        device_name="Anon Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"anon-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect", "local_agent:receive", "local_agent:report"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload = await bridge_service.create_pairing_session(db, request, base_url="http://test")

    assert payload["user_code"] and payload["device_code"]
    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash
                    == hash_secret(normalize_user_code(payload["user_code"]))
                )
            )
        ).scalar_one()
        assert pairing.tenant_id == TENANT_SCOPE_QUARANTINE_ID
        assert pairing.user_id is None
        assert pairing.agent_id is None
        assert pairing.status == "pending"
        assert pairing.metadata_json.get("tenant_binding") == "unbound_pending_pairing"
        assert pairing.expires_at > utcnow()


async def test_pairing_approval_bootstraps_identity_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Boundary 2: approval creates the local Agent identity under app_rls.

    The tenant/user-bound approval path must create the Agent plus its
    Participant identity pair under the production app_rls role with the
    tenant GUC pinned — through the canonical audited
    ``ensure_agent_identity`` bootstrap, never a raw flush and never an
    un-audited bypass.
    """
    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    # Anonymous device-flow init first (boundary 1 path as setup), so the
    # approval rebind carries the quarantine provenance end to end.
    request = SimpleNamespace(
        device_name="Bound Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"bound-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect", "local_agent:receive", "local_agent:report"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload = await bridge_service.create_pairing_session(db, request, base_url="http://test")
    user_code = payload["user_code"]

    async with app_user_sessionmaker() as db:
        # Pin through the product's session-info mechanism (what get_db does
        # for authenticated requests): a raw SET LOCAL would not survive the
        # audited bypass/commit transitions inside the bootstrap flow.
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, tenant_id)
        agent = await bridge_service.ensure_default_local_agent_for_pairing(
            db,
            user_code=user_code,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        await bridge_service.approve_pairing_session(
            db,
            user_code=user_code,
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent.id,
            metadata={"approval_surface": "regression"},
        )

    assert str(agent.tenant_id) == str(tenant_id)
    assert agent.participant_id is not None
    async with owner_sessionmaker() as db:
        row = await db.get(Agent, agent.id)
        assert row is not None
        assert str(row.tenant_id) == str(tenant_id)
        assert row.agent_type == "local_agent"
        participant = (await db.execute(select(Participant).where(Participant.id == row.participant_id))).scalar_one()
        assert str(participant.ref_id) == str(agent.id)
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        assert pairing.status == "approved"
        assert str(pairing.tenant_id) == str(tenant_id)
        assert str(pairing.user_id) == str(user_id)
        assert str(pairing.agent_id) == str(agent.id)
        # Item 2: the rebind must not leave the unbound claim behind; current
        # binding is approved/server-derived, provenance keeps the holding
        # scope it started from.
        assert pairing.metadata_json.get("tenant_binding") == "approved_server_derived"
        assert pairing.metadata_json.get("initial_holding_scope") == "__hive_scope_quarantine__"


async def test_conflicting_second_approval_cannot_hijack_binding(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """An approved pairing is immutable: only the exact same server-derived
    tenant/user/agent is idempotent; a different principal holding the short
    code gets a typed 409 with NO mutation and NO attacker-side Agent."""
    from fastapi import HTTPException
    from sqlalchemy import func, select as sel

    from app.models.agent import Agent as AgentModel

    tenant_a, user_a = await _seed_principals(owner_sessionmaker)
    # A second, independent principal.
    suffix = uuid.uuid4().hex[:10]
    tenant_b = uuid.uuid4()
    user_b = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_b, name="Bridge Tenant B", slug=f"bridge-b-{suffix}"))
        db.add(
            User(
                id=user_b,
                username=f"bridge-b-{suffix}",
                email=f"bridge-b-{suffix}@example.test",
                password_hash="x",
                display_name="Bridge Attacker",
                tenant_id=tenant_b,
                role="org_admin",
            )
        )
        await db.commit()

    # Anonymous init (boundary 1 path), then first approval binds tenant A.
    request = SimpleNamespace(
        device_name="Contested Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"contested-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect", "local_agent:receive", "local_agent:report"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload = await bridge_service.create_pairing_session(db, request, base_url="http://test")
    user_code = payload["user_code"]

    from app.database import pin_rls_tenant_context

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        agent_a = await bridge_service.ensure_default_local_agent_for_pairing(
            db, user_code=user_code, user_id=user_a, tenant_id=tenant_a
        )
        await bridge_service.approve_pairing_session(
            db, user_code=user_code, user_id=user_a, tenant_id=tenant_a, agent_id=agent_a.id
        )

    # The conflicting second principal: the guard must fire BEFORE creating
    # any attacker-side Agent and BEFORE any mutation.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_b)
        with pytest.raises(HTTPException) as hijack_agent:
            await bridge_service.ensure_default_local_agent_for_pairing(
                db, user_code=user_code, user_id=user_b, tenant_id=tenant_b
            )
        assert hijack_agent.value.status_code == 409
        with pytest.raises(HTTPException) as hijack_approve:
            await bridge_service.approve_pairing_session(
                db, user_code=user_code, user_id=user_b, tenant_id=tenant_b, agent_id=None
            )
        assert hijack_approve.value.status_code == 409

    # Durable proof: first binding unchanged; no tenant-B local Agent and no
    # foreign connection row were created.
    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                sel(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        assert pairing.status == "approved"
        assert str(pairing.tenant_id) == str(tenant_a)
        assert str(pairing.user_id) == str(user_a)
        assert str(pairing.agent_id) == str(agent_a.id)
        tenant_b_agents = (
            await db.execute(
                sel(func.count())
                .select_from(AgentModel)
                .where(AgentModel.tenant_id == tenant_b, AgentModel.agent_type == "local_agent")
            )
        ).scalar_one()
        assert int(tenant_b_agents) == 0
        from app.models.local_bridge import LocalAgentBridgeConnection

        tenant_b_connections = (
            await db.execute(
                sel(func.count())
                .select_from(LocalAgentBridgeConnection)
                .where(LocalAgentBridgeConnection.tenant_id == tenant_b)
            )
        ).scalar_one()
        assert int(tenant_b_connections) == 0

    # Exact-same re-approval stays idempotent (no error, no mutation).
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        result = await bridge_service.approve_pairing_session(
            db, user_code=user_code, user_id=user_a, tenant_id=tenant_a, agent_id=agent_a.id
        )
    assert result["status"] == "approved"


async def test_reject_is_terminal_and_cannot_mutate_bound_pairings(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Reject may only act on a pending pairing; it is terminal/idempotent for
    already-rejected rows and must never mutate an approved/claimed one."""
    from fastapi import HTTPException

    from app.database import pin_rls_tenant_context

    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    request = SimpleNamespace(
        device_name="Rejectable Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"reject-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload = await bridge_service.create_pairing_session(db, request, base_url="http://test")
    user_code = payload["user_code"]

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        agent = await bridge_service.ensure_default_local_agent_for_pairing(
            db, user_code=user_code, user_id=user_id, tenant_id=tenant_id
        )
        await bridge_service.approve_pairing_session(
            db, user_code=user_code, user_id=user_id, tenant_id=tenant_id, agent_id=agent.id
        )

    # Rejecting an APPROVED pairing is a typed refusal with no mutation.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        with pytest.raises(HTTPException) as rejected:
            await bridge_service.reject_pairing_session(db, user_code=user_code, user_id=user_id, tenant_id=tenant_id)
        assert rejected.value.status_code == 409

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        assert pairing.status == "approved"

    # A pending pairing can be rejected; rejecting it again is idempotent.
    request2 = SimpleNamespace(
        device_name="Pending Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"pending-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload2 = await bridge_service.create_pairing_session(db, request2, base_url="http://test")
    user_code2 = payload2["user_code"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        first = await bridge_service.reject_pairing_session(
            db, user_code=user_code2, user_id=user_id, tenant_id=tenant_id
        )
        second = await bridge_service.reject_pairing_session(
            db, user_code=user_code2, user_id=user_id, tenant_id=tenant_id
        )
    assert first["status"] == "rejected" and second["status"] == "rejected"
    async with owner_sessionmaker() as db:
        rejected_row = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code2))
                )
            )
        ).scalar_one()
        assert rejected_row.metadata_json.get("tenant_binding") == "rejected_server_derived"
        assert rejected_row.metadata_json.get("initial_holding_scope") == "__hive_scope_quarantine__"
    # A different principal cannot reject the already-rejected pairing into
    # their own tenant either (terminal + no re-mutation).
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        with pytest.raises(HTTPException) as foreign:
            await bridge_service.reject_pairing_session(
                db, user_code=user_code2, user_id=uuid.uuid4(), tenant_id=uuid.uuid4()
            )
        assert foreign.value.status_code == 409


async def test_racing_approvals_produce_exactly_one_immutable_winner(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Two principals approving the SAME pending pairing concurrently.

    The FOR UPDATE loader fence plus the status-predicated claim UPDATE mean
    exactly one winner binds the pairing; the loser fails typed 409, and any
    Agent/Participant/asset rows the loser created earlier in its request
    roll back — no loser Agent survives anywhere.
    """
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import func, select as sel

    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent as AgentModel
    from app.models.local_bridge import LocalAgentBridgeConnection
    from app.models.participant import Participant as ParticipantModel

    tenant_a, user_a = await _seed_principals(owner_sessionmaker)
    suffix = uuid.uuid4().hex[:10]
    tenant_b = uuid.uuid4()
    user_b = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_b, name="Bridge Race Tenant B", slug=f"bridge-race-b-{suffix}"))
        db.add(
            User(
                id=user_b,
                username=f"bridge-race-b-{suffix}",
                email=f"bridge-race-b-{suffix}@example.test",
                password_hash="x",
                display_name="Bridge Race B",
                tenant_id=tenant_b,
                role="org_admin",
            )
        )
        await db.commit()

    request = SimpleNamespace(
        device_name="Raced Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"raced-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect", "local_agent:receive", "local_agent:report"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload = await bridge_service.create_pairing_session(db, request, base_url="http://test")
    user_code = payload["user_code"]
    device_code = payload["device_code"]

    async def _approve_as(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            agent = await bridge_service.ensure_default_local_agent_for_pairing(
                db, user_code=user_code, user_id=user_id, tenant_id=tenant_id
            )
            await bridge_service.approve_pairing_session(
                db, user_code=user_code, user_id=user_id, tenant_id=tenant_id, agent_id=agent.id
            )
            return str(agent.id)

    outcomes = await asyncio.gather(
        _approve_as(tenant_a, user_a),
        _approve_as(tenant_b, user_b),
        return_exceptions=True,
    )
    winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes
    assert isinstance(losers[0], HTTPException) and losers[0].status_code == 409, losers[0]
    winner_agent_id = winners[0]

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                sel(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        assert pairing.status == "approved"
        assert str(pairing.agent_id) == winner_agent_id
        bound_tenant = uuid.UUID(str(pairing.tenant_id))
        assert bound_tenant in {tenant_a, tenant_b}
        total_local_agents = (
            await db.execute(
                sel(func.count())
                .select_from(AgentModel)
                .where(
                    AgentModel.agent_type == "local_agent",
                    AgentModel.tenant_id.in_([tenant_a, tenant_b]),
                )
            )
        ).scalar_one()
        assert int(total_local_agents) == 1
        # AI asset rollback: exactly ONE agent asset exists across BOTH
        # candidate tenants, bound to the winner's Agent — no loser asset.
        from app.models.ai_asset import AIAssetRecord

        agent_assets = (
            await db.execute(
                sel(func.count())
                .select_from(AIAssetRecord)
                .where(
                    AIAssetRecord.tenant_id.in_([tenant_a, tenant_b]),
                    AIAssetRecord.native_entity_id == uuid.UUID(winner_agent_id),
                )
            )
        ).scalar_one()
        assert int(agent_assets) == 1
        all_candidate_assets = (
            await db.execute(
                sel(func.count()).select_from(AIAssetRecord).where(AIAssetRecord.tenant_id.in_([tenant_a, tenant_b]))
            )
        ).scalar_one()
        assert int(all_candidate_assets) == 1
        # Participant identity leak check ACROSS both candidate agents and
        # tenants — not just ref_id == winner: a loser identity that somehow
        # survived rollback cannot hide.
        agent_participants = (
            await db.execute(
                sel(func.count())
                .select_from(ParticipantModel)
                .where(
                    ParticipantModel.ref_id.in_(
                        sel(AgentModel.id).where(
                            AgentModel.agent_type == "local_agent",
                            AgentModel.tenant_id.in_([tenant_a, tenant_b]),
                        )
                    )
                )
            )
        ).scalar_one()
        assert int(agent_participants) == 1
        connections = (
            await db.execute(
                sel(func.count())
                .select_from(LocalAgentBridgeConnection)
                .where(LocalAgentBridgeConnection.tenant_id.in_([tenant_a, tenant_b]))
            )
        ).scalar_one()
        assert int(connections) == 0

    # The winner may still exchange exactly once (also proves device code).
    async with app_user_sessionmaker() as db:
        claimed = await bridge_service.exchange_pairing_session(db, device_code=device_code)
    assert claimed["status"] == "active"


async def test_nested_pairing_bypass_exit_restores_outer_bypass_on_real_postgresql(
    app_user_sessionmaker,
) -> None:
    """RLS-BYPASS-NESTED-RESTORE-001 regression on real PostgreSQL.

    ``approve_pairing_session`` holds an outer audited bypass while
    ``_pairing_identity_is_live`` enters a nested one on the same session.
    The inner exit must restore the persisted outer BYPASS — the live
    ``current_setting`` GUC and the session-info scope must agree — instead
    of restoring the request ContextVar (``None`` → ``''``), which silently
    un-bypassed the rest of the outer audited scope.
    """
    from sqlalchemy import text

    from app.database import _RLS_TENANT_INFO_KEY, enter_rls_bypass, reset_current_tenant, set_current_tenant

    async def current_tenant_guc(db) -> str | None:
        return (await db.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar_one()

    # Phase 1: anonymous request context (ContextVar None), the exact approve
    # path shape — no persisted scope exists before the outer bypass.
    token = set_current_tenant(None)
    try:
        async with app_user_sessionmaker() as db:
            async with enter_rls_bypass(db, reason="regression outer pairing approval rebind") as outer_db:
                async with enter_rls_bypass(outer_db, reason="regression nested pairing live identity check"):
                    pass
                assert await current_tenant_guc(outer_db) == "BYPASS"
                assert outer_db.sync_session.info[_RLS_TENANT_INFO_KEY] == "BYPASS"
            assert await current_tenant_guc(db) == ""
            assert _RLS_TENANT_INFO_KEY not in db.sync_session.info
            await db.rollback()
    finally:
        reset_current_tenant(token)

    # Phase 2: authenticated request context — a pinned tenant scope exists
    # before the outer bypass; both exits must restore it exactly.
    tenant_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, tenant_id)
        async with enter_rls_bypass(db, reason="regression outer bypass over pinned tenant") as outer_db:
            async with enter_rls_bypass(outer_db, reason="regression nested bypass over pinned tenant"):
                pass
            assert await current_tenant_guc(outer_db) == "BYPASS"
            assert outer_db.sync_session.info[_RLS_TENANT_INFO_KEY] == "BYPASS"
        assert await current_tenant_guc(db) == str(tenant_id)
        assert db.sync_session.info[_RLS_TENANT_INFO_KEY] == str(tenant_id)
        await db.rollback()


async def test_racing_exchanges_produce_exactly_one_active_token(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Two simultaneous device-code exchanges claim the pairing exactly once.

    The FOR UPDATE device-code loader fence plus the status-predicated claim
    mean one exchange returns an active token; the other fails typed 409 and
    its connection row rolls back — exactly one active connection exists.
    """
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import func, select as sel

    from app.database import pin_rls_tenant_context
    from app.models.local_bridge import LocalAgentBridgeConnection

    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    request = SimpleNamespace(
        device_name="Exchanged Local Runner",
        client_kind="hive-connect",
        device_fingerprint=f"exch-{uuid.uuid4().hex[:8]}",
        scopes=["local_agent:connect", "local_agent:receive", "local_agent:report"],
    )
    async with app_user_sessionmaker() as db:
        async with _anonymous_tenant_context():
            payload = await bridge_service.create_pairing_session(db, request, base_url="http://test")
    user_code = payload["user_code"]
    device_code = payload["device_code"]

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        agent = await bridge_service.ensure_default_local_agent_for_pairing(
            db, user_code=user_code, user_id=user_id, tenant_id=tenant_id
        )
        await bridge_service.approve_pairing_session(
            db, user_code=user_code, user_id=user_id, tenant_id=tenant_id, agent_id=agent.id
        )

    racing_sessions = [app_user_sessionmaker(), app_user_sessionmaker()]
    try:
        outcomes = await asyncio.gather(
            bridge_service.exchange_pairing_session(racing_sessions[0], device_code=device_code),
            bridge_service.exchange_pairing_session(racing_sessions[1], device_code=device_code),
            return_exceptions=True,
        )
    finally:
        for racing_session in racing_sessions:
            await racing_session.close()
    actives = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(actives) == 1, outcomes
    assert len(conflicts) == 1, outcomes
    assert isinstance(conflicts[0], HTTPException) and conflicts[0].status_code == 409, conflicts[0]
    assert actives[0]["status"] == "active"
    assert actives[0]["access_token"]

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                sel(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        assert pairing.status == "claimed"
        active_connections = (
            await db.execute(
                sel(func.count())
                .select_from(LocalAgentBridgeConnection)
                .where(
                    LocalAgentBridgeConnection.tenant_id == tenant_id,
                    LocalAgentBridgeConnection.status == "active",
                )
            )
        ).scalar_one()
        assert int(active_connections) == 1
