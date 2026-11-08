"""T3 admits only private Web packages, without blocking unrelated explicit input."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database import pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission
from app.models.user import User
from tests.integration.test_a4sf_round2_postgres import _commit, _member
from tests.memory.test_t3_consolidation_platform_gate import (
    _accepted_review_for, _entry_patch, _write_explicit_overlay_entry, _write_reviewed_t2_package,
)
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _audience(db, seed, other, kind):
    agent = await db.get(Agent, seed.agent_id)
    session = await db.get(ChatSession, seed.session_id)
    if kind == "business_team":
        from app.core.permissions import check_agent_access
        from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
        from app.services.business_teams import affiliate_agent

        team = BusinessTeam(tenant_id=seed.tenant_id, name="Shared team", created_by_user_id=seed.user_id)
        db.add(team)
        await db.flush()
        for user_id in (seed.user_id, other):
            db.add(BusinessTeamMembership(tenant_id=seed.tenant_id, business_team_id=team.id, user_id=user_id))
        await db.flush()
        await affiliate_agent(db, await db.get(User, seed.user_id), team.id, seed.agent_id)
        await db.flush()
        assert not (await db.scalars(select(AgentPermission).where(AgentPermission.agent_id == seed.agent_id))).all()
        assert (await check_agent_access(db, await db.get(User, other), seed.agent_id))[1] == "use"
    elif kind.startswith("resource_"):
        action = kind.removeprefix("resource_")
        row = ResourcePermission(tenant_id=seed.tenant_id, principal_type="user",
            principal_id=seed.user_id if action == "owner" else other,
            resource_type="agent", resource_id=seed.agent_id,
            actions=[action if action in {"read", "execute", "manage"} else "read"], effect="allow")
        if action == "revoked":
            row.revoked_at = datetime.now(UTC)
        elif action == "expired":
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif action == "deny":
            row.effect = "deny"
        db.add(row)
    elif kind.startswith("collaboration_"):
        from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
        from app.services.a2a_collaboration_policy import resolve_a2a_collaboration_policy

        peer = Agent(tenant_id=seed.tenant_id, creator_id=other, owner_user_id=other,
            name="Other member's bot", status="idle")
        group = AgentCollaborationGroup(tenant_id=seed.tenant_id, name="Shared edge", created_by_user_id=seed.user_id)
        db.add_all([peer, group])
        await db.flush()
        agent.status = "idle"
        for row in (agent, peer):
            db.add(AgentCollaborationGroupMember(tenant_id=seed.tenant_id, group_id=group.id,
                agent_id=row.id, agent_owner_user_id=row.owner_user_id,
                status="revoked" if kind == "collaboration_revoked" else "active"))
        if kind == "collaboration_expired":
            group.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.flush()
        decision = await resolve_a2a_collaboration_policy(db, peer, agent, action="delegate")
        assert decision.allowed is (kind == "collaboration_active")
    elif kind == "company_read":
        db.add(AgentPermission(tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            scope_type="company", access_level="read"))
    elif kind == "admin_only":
        (await db.get(User, other)).role = "org_admin"
    elif kind == "dingtalk_group":
        session.source_channel = "dingtalk"
        session.delivery_target_json = {"conversation_type": "2", "conversation_id": "group"}
    elif kind == "peer":
        session.peer_agent_id = seed.agent_id
    elif kind == "agent_owner":
        session.visibility_scope = "agent_owner"
    elif kind == "missing_channel":
        session.source_channel = ""


@pytest.mark.parametrize("audience,allowed", [
    ("owner_only", True), ("business_team", False), ("company_read", False),
    ("resource_read", False), ("resource_execute", False), ("resource_manage", False),
    ("resource_owner", True), ("resource_revoked", True), ("resource_expired", True), ("resource_deny", True),
    ("admin_only", True), ("collaboration_active", False), ("collaboration_revoked", True),
    ("collaboration_expired", True), ("dingtalk_group", False), ("peer", False),
    ("agent_owner", False), ("missing_channel", False),
])
async def test_private_consolidation_sees_every_audience(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, audience, allowed,
):
    from app.memory.profile_plane import load_resident_memory
    from app.memory.t3_consolidation import stage_pending_t3_consolidation_job
    from app.memory.t3_platform_gate import file_sha256

    seed = await _seed_waiting_batch(owner_sessionmaker)
    other = uuid.uuid4()
    async with owner_sessionmaker() as db:
        _member(db, seed.tenant_id, other)
        await db.flush()
        await _audience(db, seed, other, audience)
        await db.commit()
    package = _write_reviewed_t2_package(tmp_path, seed.agent_id, str(seed.session_id))
    staged = stage_pending_t3_consolidation_job(agent_id=seed.agent_id, data_root=tmp_path)
    assert staged.status == "staged"
    target = tmp_path / str(seed.agent_id) / "memory/self/self.md"
    ref = f"t2://session/{seed.session_id}/segment/seg-1"
    marker = "PRIVATE_OWNER_METHOD"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        result = await _commit(agent_id=seed.agent_id, data_root=tmp_path, job_id=staged.job_id,
            revised_patch_md=_entry_patch("memory/self/self.md", file_sha256(target), "method",
                f"### Private method\n<!-- id: method -->\n{marker}", refs=[ref]),
            review_md=_accepted_review_for(ref))
    assert result.status == ("committed" if allowed else "held"), result
    assert (marker in load_resident_memory(agent_id=seed.agent_id, data_root=tmp_path, include_overlay=False).text) is allowed
    if not allowed:
        manifest = json.loads((package / "manifest.json").read_text())
        assert manifest["package_status"] == "reviewed" and "t3_absorbed_by" not in manifest


@pytest.mark.parametrize("shared", [True, False])
async def test_heartbeat_filters_each_package_before_models_and_does_not_repeat(
    owner_sessionmaker, monkeypatch, tmp_path, shared,
):
    from app.memory.t3_platform_gate import file_sha256
    from app.services import heartbeat_t3_core as core

    seed = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        if shared:
            db.add(AgentPermission(tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                scope_type="company", access_level="use"))
            held_session_id = seed.session_id
        else:
            held_session_id = uuid.uuid4()
            db.add(ChatSession(id=held_session_id, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                user_id=seed.user_id, source_channel="dingtalk", audience_kind="private",
                delivery_target_json={"conversation_type": "2"}))
        await db.commit()
    held = _write_reviewed_t2_package(tmp_path, seed.agent_id, str(held_session_id))
    before = (held / "manifest.json").read_bytes()
    if shared:
        entry_id = _write_explicit_overlay_entry(tmp_path, seed.agent_id)
        wanted_ref = f"explicit://memory/{entry_id}"
    else:
        qualified = _write_reviewed_t2_package(tmp_path, seed.agent_id, str(seed.session_id))
        wanted_ref = f"t2://session/{seed.session_id}/segment/seg-1"
    target = tmp_path / str(seed.agent_id) / "memory/self/self.md"
    calls, bundles = [], []

    async def consolidate(**kwargs):
        calls.append("consolidate")
        bundles.append(json.loads((kwargs["job_dir"] / "source_bundle.json").read_text()))
        return {"consolidation_pitch_md": "# Accepted method", "revised_patch_md": _entry_patch(
            "memory/self/self.md", file_sha256(target), "method",
            "### Method\n<!-- id: method -->\nKeep the qualified instruction.", refs=[wanted_ref])}

    async def review(**_kwargs):
        calls.append("review")
        return {"review_md": _accepted_review_for(wanted_ref)}

    monkeypatch.setattr(core, "_call_consolidator", consolidate)
    monkeypatch.setattr(core, "_call_memory_gate_review", review)
    first = await core.run_heartbeat_t3_core(agent_id=seed.agent_id, tenant_id=seed.tenant_id,
        data_root=tmp_path, model=object())
    assert first.status == "committed", first
    assert "Keep the qualified instruction." in target.read_text()
    assert [p["ref"] for p in bundles[0]["source_packages"]] == ([] if shared else [wanted_ref])
    assert bool(bundles[0]["explicit_overlay_entries"]) is shared
    assert (held / "manifest.json").read_bytes() == before
    if not shared:
        assert json.loads((qualified / "manifest.json").read_text())["t3_absorbed_by"] == first.job_id
    second = await core.run_heartbeat_t3_core(agent_id=seed.agent_id, tenant_id=seed.tenant_id,
        data_root=tmp_path, model=object())
    assert second.status == "skipped", second
    assert calls == ["consolidate", "review"]


@pytest.mark.parametrize("failure", ["json", "shape", "db"])
async def test_unavailable_audience_holds_instead_of_raising(owner_sessionmaker, monkeypatch, tmp_path, failure):
    seed = await _seed_waiting_batch(owner_sessionmaker)
    _write_reviewed_t2_package(tmp_path, seed.agent_id, str(seed.session_id))
    job = tmp_path / str(seed.agent_id) / "memory/.staging/t3_jobs/invalid-audience"
    job.mkdir(parents=True)
    if failure != "db":
        (job / "source_bundle.json").write_text("{" if failure == "json" else '{"source_packages": [{}]}')
    else:
        @asynccontextmanager
        async def unavailable(*_args, **_kwargs):
            raise SQLAlchemyError("Synthetic database outage")
            yield

        monkeypatch.setattr("app.database.tenant_scoped_session", unavailable)
    ref = f"t2://session/{seed.session_id}/segment/seg-1"
    result = await _commit(agent_id=seed.agent_id, tenant_id=seed.tenant_id, data_root=tmp_path,
        job_id=job.name, revised_patch_md=_entry_patch("memory/self/self.md", "0" * 64, "method",
            "### Method\n<!-- id: method -->\nMust stay uncommitted.", refs=[ref]),
        review_md=_accepted_review_for(ref))
    assert result.status == "held", result
    target = tmp_path / str(seed.agent_id) / "memory/self/self.md"
    assert not target.exists() or "Must stay uncommitted." not in target.read_text()
