"""A reviewed learned method is usable only within its current source audience."""

from types import SimpleNamespace

import pytest

from app.database import tenant_scoped_session
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _assert_native_asset_state(n, session_factory, *, evolution_state, lifecycle_status):
    from sqlalchemy import select
    from app.api import ai_assets
    from app.models.ai_asset import AIAssetRecord

    if not getattr(n, "asset_api_registered", False):
        n.client._transport.app.include_router(ai_assets.router)
        n.asset_api_registered = True
    async with tenant_scoped_session(n.tenant, session_factory=session_factory) as db:
        asset = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == n.tenant,
            AIAssetRecord.native_key == f"skill:agent:{n.agent}:{n.first_learned_skill_path.split('/')[1]}"))
        identity = str(asset.id)
    response = await n.client.get(f"/enterprise/ai-assets/{identity}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["asset"]["compatibility"]["evolution_state"] == evolution_state, payload
    assert payload["asset"]["lifecycle_status"] == lifecycle_status, payload
    assert payload["asset"]["projection"]["status"] == "applied", payload
    revision = await n.client.get(f"/enterprise/ai-assets/{identity}/revisions/{payload['active_revision']['version']}")
    assert revision.status_code == 200, revision.text
    assert revision.json()["content"]["control"]["evolution_state"] == evolution_state, revision.text
    assert "EVIDENCE_METHOD_VERSION_ONE" in revision.text, revision.text
    return payload


@pytest.fixture(autouse=True)
def controlled_provider_boundary(monkeypatch):
    def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("Unexpected provider client in a controlled learned Skill probe")

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", unexpected_provider)
    monkeypatch.setattr("app.services.skill_distiller.create_llm_client_from_config", unexpected_provider)


@pytest.fixture
async def learned_skill(shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch, request):  # noqa: F811
    from app.models.capability_policy import CapabilityPolicy
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
    from tests.services.test_skill_distiller import _approving_referee_review, _passing_artifact_gate

    s = shared_memory
    n = s.n
    def no_provider_client(*_args, **_kwargs):
        raise AssertionError("Unexpected provider client in a controlled Skill source probe")

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", no_provider_client)
    monkeypatch.setattr("app.services.skill_distiller.create_llm_client_from_config", no_provider_client)
    async with owner_sessionmaker() as db:
        for capability in ("agent.skill.read", "agent.skill.write"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    mode = getattr(request, "param", None)
    background = mode in {"background", "workflow"}
    if mode == "memory":
        for user, session, run, marker in (
            (n.user, n.session, n.run, "OWN_MEMORY_CANDIDATE"),
            (n.foreign, s.second_session, s.second_run, "FOREIGN_MEMORY_CANDIDATE"),
        ):
            saved, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", {
                "content": f"- skill候选: {marker}", "category": "strategy"}, user=user, session=session, run=run)
            assert "Saved to explicit memory overlay" in saved, saved
    if background:
        from app.models.chat_session import ChatSession
        from tests.integration.test_m4c_explicit_memory_scope_postgres import new_owner_work

        n.learning_history_session, history_run = await new_owner_work(n, owner_sessionmaker)
        for user, session, run, marker in (
            (n.user, n.learning_history_session, history_run, "AUTHORIZED_WORKFLOW_OBSERVATION"),
            (n.foreign, s.second_session, s.second_run, "OTHER_MEMBER_WORKFLOW_OBSERVATION"),
        ):
            async with owner_sessionmaker() as db:
                (await db.get(ChatSession, session)).source_channel = "heartbeat"
                await db.commit()
            await memory_call(n, app_user_sessionmaker, "save_memory", {"content": marker, "category": "strategy"},
                user=user, session=session, run=run)
            await memory_call(n, app_user_sessionmaker, "search_memory", {"query": marker, "scope": "facts"},
                user=user, session=session, run=run, include_history=True)
            await _seal_workflow_reply(n, app_user_sessionmaker, session=session, run=run, content=marker)
    args = {"name": "Evidence Review", "description": "Compare a requested result with its primary evidence.",
        "instructions": "Read the source, compare the claim, and record EVIDENCE_METHOD_VERSION_ONE.",
        "folder_name": "evidence-review"}
    if mode == "crlf":
        args["instructions"] += "\r\nPreserve the original evidence bytes.\r\nReport the comparison."
    invocation = None
    if mode != "workflow":
        submitted, invocation = await memory_call(n, app_user_sessionmaker, "save_skill", args,
            command="Learn this repeatable evidence-checking method for my later work")
        assert "active_skill_created: false" in submitted, submitted
    body = "---\nname: Evidence Review\ndescription: Compare a requested result with its primary evidence.\n---\n\n# Evidence Review\n\n" + args["instructions"]

    async def no_prior_sessions(**_kwargs):
        return []

    async def review_draft(**_kwargs):
        return DistilledSkillDraft(decision="promote", confidence=0.94, name=args["name"], description=args["description"],
            instructions_markdown=args["instructions"], declared_tools=(), declared_packs=(),
            reason="Controlled writer accepts this submitted method for a provisional trial.", skill_markdown=body)

    if background or mode == "memory":
        async def select_direct(*, options, **_kwargs):
            n.learning_options = options
            if mode == "workflow":
                return next(option for option in options if "AUTHORIZED_WORKFLOW_OBSERVATION" in repr(option.evidence))
            return next(option for option in options if option.direct_candidate is not None)

        monkeypatch.setattr("app.services.skill_distiller._select_skill_candidate_with_llm", select_direct)
    else:
        monkeypatch.setattr("app.services.skill_distiller._load_internal_session_evidence", no_prior_sessions)
    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", review_draft)
    monkeypatch.setattr("app.services.skill_distiller._run_skill_artifact_gate", _passing_artifact_gate)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", _approving_referee_review)
    if mode in {"approval_hold", "guard_approval_hold"}:
        async def require_final_approval(**kwargs):
            from sqlalchemy import select
            from app.models.guard_policy import GuardPolicy

            review = await _approving_referee_review(**kwargs)
            await n.service.governance_resolver.build_dependencies().load_guard_policy(n.tenant, n.agent, "inspect_reviewed_skill")
            async with owner_sessionmaker() as db:
                if mode == "guard_approval_hold":
                    db.add(GuardPolicy(tenant_id=n.tenant, zone_guard={"tool_rules": [
                        {"tools": ["save_skill"], "decision": "require_approval"}]}))
                else:
                    policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
                        CapabilityPolicy.capability == "agent.skill.write"))
                    policy.requires_approval = True
                await db.commit()
            return review

        monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", require_final_approval)
    if mode == "post_gate_mutation":
        from app.services.skill_distiller import _run_skill_referee_gate

        async def mutate_after_gate(**kwargs):
            result = await _run_skill_referee_gate(**kwargs)
            assert result[1] is None, result
            path = n.root / "evolution/skill_candidates" / kwargs["candidate_id"] / "SKILL.md.draft"
            path.write_bytes(path.read_bytes() + b"\nUNREVIEWED_POST_GATE_BYTES\n")
            return result

        monkeypatch.setattr("app.services.skill_distiller._run_skill_referee_gate", mutate_after_gate)
    before = sorted(path.relative_to(n.root).as_posix() for path in (n.root / "skills").rglob("SKILL.md"))
    outcome = await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
        current_session_id=str(n.session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
        model=SimpleNamespace(provider="reference", model="controlled-skill-writer", api_key="synthetic", base_url=None))
    if mode in {"approval_hold", "guard_approval_hold"}:
        assert outcome["status"] == "approval_required" and outcome["reason"] == "learned_skill_activation_approval_required", outcome
        assert sorted(path.relative_to(n.root).as_posix() for path in (n.root / "skills").rglob("SKILL.md")) == before
        n.reviewed_markdown = body.rstrip() + "\n"
        return s, invocation
    if mode == "post_gate_mutation":
        assert outcome["status"] == "held" and outcome["reason"] == "learned_skill_draft_changed", outcome
        assert sorted(path.relative_to(n.root).as_posix() for path in (n.root / "skills").rglob("SKILL.md")) == before
        return s, invocation
    assert outcome["status"] == "provisional", outcome
    target = outcome["skill_path"]
    n.first_learned_skill_path = target
    assert target.startswith("skills/") and target.endswith("/SKILL.md"), outcome
    assert "EVIDENCE_METHOD_VERSION_ONE" in (n.root / target).read_text()
    return s, invocation


@pytest.mark.parametrize("learned_skill", ["post_gate_mutation"], indirect=True)
async def test_reviewed_activation_cannot_relabel_bytes_changed_after_the_gates_as_approved(
    learned_skill, app_user_sessionmaker,
):
    import json
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    listed, _invocation = await memory_call(s.n, app_user_sessionmaker, "inspect_reviewed_skill", {})
    assert json.loads(listed)["candidates"] == [], listed


@pytest.mark.parametrize("learned_skill,mutation", [("approval_hold", "none"), ("guard_approval_hold", "none"),
    ("approval_hold", "draft_changed"), ("approval_hold", "source_withdrawn"),
    ("approval_hold", "policy_changed"), ("approval_hold", "arguments_changed")], indirect=["learned_skill"])
async def test_approved_reviewed_skill_continues_the_exact_draft_without_another_model_review(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, mutation,
):
    import json
    from sqlalchemy import select
    from app.models.audit import ApprovalRequest
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_session import ChatSession
    from app.models.config_revision import ConfigRevision
    from app.models.user import User
    from app.services.approval_service import approval_service
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work
    from uuid import UUID

    s, _submission = learned_skill
    n = s.n
    work_session, work_run = await new_owner_work(n, owner_sessionmaker)
    work = {"session": work_session, "run": work_run}
    async def no_more_review(**_kwargs):
        raise AssertionError("Approved activation must not regenerate or review the frozen draft")
    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", no_more_review)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", no_more_review)
    listed, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill", {}, include_history=True, **work)
    listing = json.loads(listed)
    assert listing["status"] == "ok" and len(listing["candidates"]) == 1, listing
    revision = listing["candidates"][0]["reviewed_revision_id"]
    inspected, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill",
        {"reviewed_revision_id": revision}, include_history=True, **work)
    arguments = json.loads(inspected)["activation_arguments"]
    assert arguments["reviewed_revision_id"] == revision and arguments["instructions"] == n.reviewed_markdown
    if mutation == "arguments_changed":
        arguments["instructions"] += "\nUnreviewed instructions must not be installed.\n"
    waiting, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", arguments, include_history=True, **work)
    assert "approval" in waiting.lower(), waiting
    async with owner_sessionmaker() as db:
        approvals = list(await db.scalars(select(ApprovalRequest).where(ApprovalRequest.tenant_id == n.tenant,
            ApprovalRequest.tool_name == "save_skill")))
        assert len(approvals) == 1, approvals
        approval = approvals[0]
        assert approval.normalized_arguments["reviewed_revision_id"] == revision
        assert approval.normalized_arguments["instructions"] == arguments["instructions"] and approval.consumed_at is None
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        reviewer = await db.get(User, n.foreign)
        resolved = await approval_service.resolve_approval(db, approval.id, reviewer, "approve")
        job = resolved.execution_task_id
        await db.commit()
    async with owner_sessionmaker() as db:
        candidate = await db.get(ConfigRevision, UUID(revision))
        frozen_hash, draft_path = candidate.content_hash, n.root / candidate.content["draft_path"]
        if mutation == "draft_changed":
            draft_path.write_bytes(draft_path.read_bytes() + b"\nUnreviewed file change.\n")
        elif mutation == "source_withdrawn":
            (await db.get(ChatSession, n.session)).user_id = n.foreign
        elif mutation == "policy_changed":
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
                CapabilityPolicy.capability == "agent.skill.write"))
            policy.allowed = False
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        claims = await RuntimeTaskClaimService(db=db, worker_id="reviewed-skill-approval",
            task_types=("approval_execution",)).claim_available()
    assert [claim.id for claim in claims] == [job]
    result = await run_claimed_runtime_task(execute_claimed_approval_execution(job), task_id=job,
        claim_version=claims[0].claim_version, worker_id="reviewed-skill-approval", lease_seconds=180)
    n.first_learned_skill_path = f"skills/{arguments['folder_name']}/SKILL.md"
    if mutation != "none":
        assert result == ("needs_reapproval" if mutation == "policy_changed" else "failed"), result
        assert not (n.root / n.first_learned_skill_path).exists()
        async with owner_sessionmaker() as db:
            retained = await db.get(ConfigRevision, UUID(revision))
            assert retained.content_hash == frozen_hash and retained.is_active
            if mutation == "draft_changed":
                draft_path.write_bytes(n.reviewed_markdown.encode())
            elif mutation == "source_withdrawn":
                (await db.get(ChatSession, n.session)).user_id = n.user
            elif mutation == "policy_changed":
                policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
                    CapabilityPolicy.capability == "agent.skill.write"))
                policy.allowed = True
            await db.commit()
        recovered, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill",
            {"reviewed_revision_id": revision}, **work)
        assert json.loads(recovered)["status"] == "pending", recovered
        assert json.loads(recovered)["activation_arguments"]["instructions"] == n.reviewed_markdown
        return
    assert result == "succeeded", result
    assert (n.root / n.first_learned_skill_path).read_bytes() == n.reviewed_markdown.encode()
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    assert payload["active_revision"]["version"] == 1
    from app.services.ai_assets import backfill_file_native_assets

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await backfill_file_native_assets(db, tenant_id=n.tenant, data_root=n.root.parent, apply=True, actor_user_id=n.user)
    after_backfill = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    assert after_backfill["active_revision"]["id"] == payload["active_revision"]["id"]
    native_revision = read_agent_asset_revision(n.root)
    assert await execute_claimed_approval_execution(job) == "succeeded"
    assert read_agent_asset_revision(n.root) == native_revision
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        consumed = await db.get(ApprovalRequest, approval.id, populate_existing=True)
        assert consumed.consumed_at is not None and consumed.execution_status == "succeeded", consumed.execution_result


@pytest.mark.parametrize("learned_skill", ["approval_hold"], indirect=True)
async def test_reviewed_skill_inspection_cannot_borrow_another_administrators_source_authority(
    learned_skill, app_user_sessionmaker, owner_sessionmaker,
):
    import json
    from app.models.user import User
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    listed, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill", {})
    revision = json.loads(listed)["candidates"][0]["reviewed_revision_id"]
    async with owner_sessionmaker() as db:
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    reader = {"user": n.foreign, "session": s.second_session, "run": s.second_run}
    listed, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill", {}, **reader)
    assert json.loads(listed)["candidates"] == [], listed
    refused, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill",
        {"reviewed_revision_id": revision}, **reader)
    assert "learned_skill_review_denied" in refused and "EVIDENCE_METHOD_VERSION_ONE" not in refused, refused


@pytest.mark.parametrize("learned_skill", ["crlf"], indirect=True)
async def test_learned_package_projection_preserves_reviewed_line_endings(
    learned_skill, app_user_sessionmaker,
):
    s, _invocation = learned_skill
    n = s.n
    body = (n.root / n.first_learned_skill_path).read_bytes()
    assert b"\r\nPreserve the original evidence bytes.\r\n" in body
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    response = await n.client.get(f"/enterprise/ai-assets/{payload['asset']['id']}/revisions/{payload['active_revision']['version']}")
    assert response.status_code == 200, response.text
    files = response.json()["content"]["files"]
    assert next(file["content"].encode() for file in files if file["path"] == "SKILL.md") == body


@pytest.mark.parametrize("failure", ["before_commit", "after_commit"])
@pytest.mark.parametrize("learned_skill", ["approval_hold"], indirect=True)
async def test_reviewed_activation_reconciles_projection_commit_loss_without_replaying_native_work(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, failure,
):
    import json
    from contextlib import asynccontextmanager
    from unittest.mock import patch
    from sqlalchemy import select
    from app import database
    from app.models.capability_policy import CapabilityPolicy
    from app.services import ai_assets
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    # This independent recovery probe uses the normal policy-allowed activation configuration.
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
            CapabilityPolicy.capability == "agent.skill.write"))
        policy.requires_approval = False
        await db.commit()
    listed, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill", {}, include_history=True)
    revision = json.loads(listed)["candidates"][0]["reviewed_revision_id"]
    inspected, _invocation = await memory_call(n, app_user_sessionmaker, "inspect_reviewed_skill",
        {"reviewed_revision_id": revision}, include_history=True)
    arguments = json.loads(inspected)["activation_arguments"]
    original_register, original_scope, faults = ai_assets.register_evolved_workspace_skill_asset, database.tenant_scoped_session, []

    @asynccontextmanager
    async def faulty_scope(*args, **kwargs):
        async with original_scope(*args, **kwargs) as db:
            if kwargs.get("source") == "workspace_skill_asset":
                commit = db.commit
                async def fault_commit():
                    if not faults and failure == "before_commit":
                        faults.append(failure)
                        raise OSError("Controlled interrupted reviewed Skill projection commit")
                    await commit()
                    if not faults and failure == "after_commit":
                        faults.append(failure)
                        raise OSError("Controlled committed reviewed Skill projection response loss")
                monkeypatch.setattr(db, "commit", fault_commit)
            yield db

    async def faulty_register(**kwargs):
        with patch.object(database, "tenant_scoped_session", faulty_scope):
            return await original_register(**kwargs)
    async def no_more_review(**_kwargs):
        raise AssertionError("Projection recovery cannot rerun a reviewed semantic decision")
    monkeypatch.setattr(ai_assets, "register_evolved_workspace_skill_asset", faulty_register)
    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", no_more_review)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", no_more_review)
    first, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", arguments, include_history=True)
    assert faults == [failure], first
    path = f"skills/{arguments['folder_name']}/SKILL.md"
    assert (n.root / path).is_file() and (n.root / path).read_bytes() == n.reviewed_markdown.encode(), first
    native_revision = read_agent_asset_revision(n.root)
    if failure == "before_commit":
        assert "learned_skill_projection_pending" in first and '"outcome": "unknown"' in first, first
        unavailable, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
        assert "EVIDENCE_METHOD_VERSION_ONE" not in unavailable, unavailable
        native_revision = read_agent_asset_revision(n.root)
        second, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", arguments, include_history=True)
        assert json.loads(second)["status"] == "provisional", second
    else:
        assert json.loads(first)["status"] == "provisional", first
    repeated, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", arguments, include_history=True)
    assert json.loads(repeated)["status"] == "already_installed", repeated
    assert read_agent_asset_revision(n.root) == native_revision
    n.first_learned_skill_path = path
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    assert payload["active_revision"]["version"] == 1


async def _seal_workflow_reply(n, session_factory, *, session, run, content):
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import bind_round_inputs, commit_sealed_model_round, prepare_model_request, seal_model_response
    from app.services.session_semantic_history import load_session_semantic_history

    n.memory_rounds[str(run)] += 1
    index = n.memory_rounds[str(run)]
    async with tenant_scoped_session(n.tenant, session_factory=session_factory) as db:
        await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=index)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session)
        messages = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
        provider = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=index, messages=messages, tools=[],
            provider="synthetic", model="local-reference")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=index, provider_request_id=provider,
            response={"content": content, "tool_calls": [], "finish_reason": "stop"})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=index, provider_request_id=provider)
        await db.commit()


@pytest.mark.parametrize("learned_skill", ["background"], indirect=True)
async def test_background_review_keeps_authorized_canonical_work_and_rechecks_it(
    learned_skill, app_user_sessionmaker, owner_sessionmaker,
):
    from app.models.chat_session import ChatSession
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    offered = repr(n.learning_options)
    assert "AUTHORIZED_WORKFLOW_OBSERVATION" in offered, offered
    assert "OTHER_MEMBER_WORKFLOW_OBSERVATION" not in offered, offered
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, n.learning_history_session)).user_id = n.foreign
        await db.commit()
    denied, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_ONE" not in denied, denied


@pytest.mark.parametrize("learned_skill", ["workflow"], indirect=True)
async def test_background_workflow_can_create_a_usable_skill_without_a_save_skill_draft(
    learned_skill, app_user_sessionmaker,
):
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, submission = learned_skill
    assert submission is None
    loaded, _invocation = await memory_call(s.n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded


@pytest.mark.parametrize("after_review", ["preserve", "rollback"])
async def test_correction_discovers_new_bytes_and_preserves_previous_use_as_evidence(
    learned_skill, app_user_sessionmaker, monkeypatch, after_review,
):
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    before = (n.root / n.first_learned_skill_path).read_bytes()
    first, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_ONE" in first, first
    instructions = "Inspect the original source and record EVIDENCE_METHOD_VERSION_TWO with a comparison."
    submitted, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", {
        "name": "Evidence Review", "description": "Compare a requested result with its primary evidence.",
        "instructions": instructions, "folder_name": "evidence-review", "overwrite": True}, include_history=True)
    assert "active_skill_created: false" in submitted, submitted

    async def correction(**_kwargs):
        return DistilledSkillDraft(decision="patch", confidence=0.95, name="Evidence Review",
            description="Compare a requested result with its primary evidence.", instructions_markdown=instructions,
            declared_tools=(), declared_packs=(), reason="Controlled reviewer accepts the revised method.",
            skill_markdown="---\nname: Evidence Review\ndescription: Compare a requested result with its primary evidence.\n---\n\n# Evidence Review\n\n" + instructions)

    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", correction)
    outcome = await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
        current_session_id=str(n.session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
        model=SimpleNamespace(provider="reference", model="controlled-skill-writer", api_key="synthetic", base_url=None))
    assert outcome["status"] == "provisional", outcome
    assert outcome["skill_path"] != n.first_learned_skill_path
    assert (n.root / n.first_learned_skill_path).read_bytes() == before
    current, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_TWO" in current and "EVIDENCE_METHOD_VERSION_ONE" not in current, current
    derived, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", {
        "content": "The revised evidence method replaces the earlier method while preserving its prior work.",
        "category": "strategy"}, include_history=True)
    assert "Saved to explicit memory overlay" in derived, derived
    if after_review == "rollback":
        from app.services.provisional_trial import record_provisional_trial_signal, review_pending_provisional_trials

        new_path = n.root / outcome["skill_path"]
        new_bytes = new_path.read_bytes()
        record_provisional_trial_signal(n.root, skill_name=outcome["skill_path"], kind="negative",
            signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
        async def rollback(payload):
            assert "EVIDENCE_METHOD_VERSION_TWO" in payload["candidate_content"]
            return {"decision": "rollback", "reason": "Controlled reviewer restores the previous independently retained method."}

        report = await review_pending_provisional_trials(n.root, reviewer=rollback)
        assert report["reviewed"] == 1, report
        assert new_path.read_bytes() == new_bytes and (n.root / n.first_learned_skill_path).read_bytes() == before
        restored, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
        assert "EVIDENCE_METHOD_VERSION_ONE" in restored and "EVIDENCE_METHOD_VERSION_TWO" not in restored, restored
        historical, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", {
            "content": "The earlier method is current again; the rolled back revision and its prior work remain verifiable.",
            "category": "strategy"}, include_history=True)
        assert "Saved to explicit memory overlay" in historical, historical


@pytest.mark.parametrize("reader", ["author", "other_member"])
@pytest.mark.parametrize("surface", ["load_skill", "read_file"])
async def test_reviewed_skill_can_be_loaded_with_exact_sources_only_by_its_actual_reader(
    learned_skill, app_user_sessionmaker, reader, surface,
):
    from app.services.session_model_inputs import _origin_dependencies
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    user, session, run = ((n.user, n.session, n.run) if reader == "author" else
        (n.foreign, s.second_session, s.second_run))
    arguments = {"name": "Evidence Review"} if surface == "load_skill" else {"path": n.first_learned_skill_path}
    loaded, invocation = await memory_call(n, app_user_sessionmaker, surface, arguments,
        user=user, session=session, run=run, command="Use only the learned method I am authorized to read")
    if reader == "author":
        assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            sources, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
                ref=f"session-tool-result:{invocation.id}")
            assert complete and any(source["source_kind"] == "learned_skill_file" for source in sources), (sources, complete)
        derived, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
            {"content": "Use the learned evidence-checking method", "category": "strategy"}, include_history=True)
        assert "Saved to explicit memory overlay" in derived, derived
    else:
        assert "EVIDENCE_METHOD_VERSION_ONE" not in loaded, loaded


@pytest.mark.parametrize("learned_skill", ["memory"], indirect=True)
async def test_background_memory_candidates_keep_their_original_audience(learned_skill):
    s, _submission = learned_skill
    offered = repr(s.n.learning_options)
    assert "OWN_MEMORY_CANDIDATE" in offered, offered
    assert "FOREIGN_MEMORY_CANDIDATE" not in offered, offered


@pytest.mark.parametrize("reader", ["author", "other_member"])
async def test_native_dynamic_skill_awareness_is_scoped_and_source_bound(
    learned_skill, reader, monkeypatch, app_user_sessionmaker, owner_sessionmaker,
):
    from pathlib import Path
    from app.runtime.invoker import AgentInvocationRequest, _resolve_memory_context
    from app.runtime.context import ensure_runtime_assembly_state
    from app.runtime.session import SessionContext

    s, _submission = learned_skill
    n = s.n
    user, session, run = ((n.user, n.session, n.run) if reader == "author" else
        (n.foreign, s.second_session, s.second_run))
    async def no_recalled_memory(*_args, **_kwargs):
        return ""

    monkeypatch.setattr("app.runtime.invoker.build_memory_context", no_recalled_memory)
    request = AgentInvocationRequest(model=None, messages=[], agent_name="Evidence reviewer", role_description="",
        agent_id=n.agent, tenant_id=n.tenant, user_id=user,
        session_context=SessionContext(session_id=str(session), source="web", metadata={"runtime_task_id": str(run)}))
    content = await _resolve_memory_context(request, n.tenant)
    slug = Path(n.first_learned_skill_path).parts[1]
    assert (slug in content) is (reader == "author"), content
    if reader == "author":
        state = ensure_runtime_assembly_state(request.session_context)
        sources = state.context_sources_for(agent_id=n.agent, content=content)
        assert any(ref["source_kind"] == "skill_evolution_observation" for ref in sources), sources
        assert "skill_evolution_digest_sources_unbound" not in state.context_unbound_for(agent_id=n.agent, content=content)
        from app.models.chat_session import ChatSession
        from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
        from app.services.personal_knowledge_access import AgentRuntimePrincipal
        from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

        next_session, next_run = await new_owner_work(n, owner_sessionmaker)
        principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
            session_id=str(next_session), runtime_task_id=str(next_run))
        # New use counters must not invalidate an immutable historical observation.
        await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            await _validate_input_dependencies(db, n.tenant, list(sources), runtime_request=principal)
        async with owner_sessionmaker() as db:
            (await db.get(ChatSession, n.session)).user_id = n.foreign
            await db.commit()
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            with pytest.raises(ContentInputReceiptError) as denied:
                await _validate_input_dependencies(db, n.tenant, list(sources), runtime_request=principal)
            assert denied.value.status_code == 403


async def test_review_rechecks_live_write_policy_and_retains_a_retryable_candidate(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    from sqlalchemy import select
    from app.models.capability_policy import CapabilityPolicy
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
    from tests.services.test_skill_distiller import _approving_referee_review

    s, _submission = learned_skill
    n = s.n
    instructions = "Inspect the follow-up evidence and record the remaining questions."
    submitted, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", {
        "name": "Evidence Followup", "description": "Inspect follow-up primary evidence.",
        "instructions": instructions, "folder_name": "evidence-followup"})
    assert "active_skill_created: false" in submitted, submitted
    async def draft_followup(**_kwargs):
        return DistilledSkillDraft(decision="promote", confidence=0.95, name="Evidence Followup",
            description="Inspect follow-up primary evidence.", instructions_markdown=instructions,
            declared_tools=(), declared_packs=(), reason="Controlled follow-up review.",
            skill_markdown="---\nname: Evidence Followup\ndescription: Inspect follow-up primary evidence.\n---\n\n# Evidence Followup\n\n" + instructions)

    async def change_policy_after_review(**kwargs):
        async with owner_sessionmaker() as db:
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
                CapabilityPolicy.capability == "agent.skill.write"))
            policy.allowed = False
            await db.commit()
        return await _approving_referee_review(**kwargs)

    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", draft_followup)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", change_policy_after_review)
    async def select_followup(*, options, **_kwargs):
        return next(option for option in options if option.candidate_id.startswith("save-skill-")
            and option.record.skill_name == "Evidence Followup")

    monkeypatch.setattr("app.services.skill_distiller._select_skill_candidate_with_llm", select_followup)
    before = sorted(path.relative_to(n.root).as_posix() for path in (n.root / "skills").rglob("SKILL.md"))
    async def review():
        return await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
            current_session_id=str(n.session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
            model=SimpleNamespace(provider="reference", model="controlled-skill-writer", api_key="synthetic", base_url=None))

    denied = await review()
    assert denied["status"] == "held" and denied["reason"] == "learned_skill_activation_denied", denied
    assert sorted(path.relative_to(n.root).as_posix() for path in (n.root / "skills").rglob("SKILL.md")) == before
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
            CapabilityPolicy.capability == "agent.skill.write"))
        policy.allowed = True
        await db.commit()
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", _approving_referee_review)
    resumed = await review()
    assert resumed["status"] == "provisional", resumed
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Followup"})
    assert instructions in loaded, loaded


async def test_retirement_removes_discovery_but_preserves_historical_sources(learned_skill, app_user_sessionmaker):
    from pathlib import Path
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
    before = (n.root / n.first_learned_skill_path).read_bytes()
    folder = Path(n.first_learned_skill_path).parts[1]
    response = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": folder})
    assert response.status_code == 200 and response.json()["status"] == "retired", response.text
    assert (n.root / n.first_learned_skill_path).read_bytes() == before
    again = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": folder})
    assert again.status_code == 200 and again.json()["status"] == "already_retired", again.text
    denied, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"})
    assert "EVIDENCE_METHOD_VERSION_ONE" not in denied, denied
    saved, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", {
        "content": "The method has been retired; its earlier authorized use remains part of this work.",
        "category": "strategy"}, include_history=True)
    assert "Saved to explicit memory overlay" in saved, saved


@pytest.mark.parametrize("reader", ["author", "other_manager"])
async def test_file_api_management_does_not_replace_private_skill_source_authority(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, reader,
):
    from app.core.security import get_current_user
    from app.models.agent import AgentPermission
    from app.models.user import User

    s, _submission = learned_skill
    n = s.n
    if reader == "other_manager":
        from sqlalchemy import delete

        async with owner_sessionmaker() as db:
            await db.execute(delete(AgentPermission).where(AgentPermission.tenant_id == n.tenant,
                AgentPermission.agent_id == n.agent, AgentPermission.scope_type == "company"))
            db.add(AgentPermission(tenant_id=n.tenant, agent_id=n.agent, scope_type="user", scope_id=n.foreign, access_level="manage"))
            await db.commit()
        async def other_user():
            async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
                return await db.get(User, n.foreign)

        monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_current_user, other_user)
    response = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": n.first_learned_skill_path})
    if reader == "author":
        assert response.status_code == 200 and "EVIDENCE_METHOD_VERSION_ONE" in response.text, response.text
    else:
        assert response.status_code == 403 and "EVIDENCE_METHOD_VERSION_ONE" not in response.text, response.text
    listing = await n.client.get(f"/agents/{n.agent}/files/", params={"path": "skills"})
    assert listing.status_code == 200, listing.text
    assert (n.first_learned_skill_path.split("/")[1] in listing.text) is (reader == "author"), listing.text


@pytest.mark.parametrize("reader", ["author", "other_member"])
async def test_pin_uses_the_actual_skill_source_scope(learned_skill, app_user_sessionmaker, reader):
    from pathlib import Path
    from app.services.skill_curator import load_skill_usage
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    slug = Path(n.first_learned_skill_path).parts[1]
    before = load_skill_usage(n.root)[slug]
    user, session, run = ((n.user, n.session, n.run) if reader == "author" else
        (n.foreign, s.second_session, s.second_run))
    result, _invocation = await memory_call(n, app_user_sessionmaker, "pin_skill", {"skill": slug, "pinned": True},
        user=user, session=session, run=run)
    after = load_skill_usage(n.root)[slug]
    if reader == "author":
        assert "Pinned skill" in result and after["pinned"] is True, result
    else:
        assert "Pinned skill" not in result and after == before, result


async def test_normal_worker_consumes_the_learned_version_and_its_artifact_keeps_source_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import json
    from uuid import uuid4
    from sqlalchemy import select
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionModelResult
    from app.models.user import User
    from app.services import agent_tools
    from app.services.content_input_receipts import ContentInputReceiptError, validate_artifact_content_receipt
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work
    from tests.services.test_skill_distiller import _approving_referee_review, _passing_artifact_gate

    def no_provider_client(*_args, **_kwargs):
        raise AssertionError("Unexpected provider client in a controlled native learned Skill probe")

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", no_provider_client)
    monkeypatch.setattr("app.services.skill_distiller.create_llm_client_from_config", no_provider_client)
    instructions = "Compare the requested claim with its source and record NATIVE_LEARNED_METHOD."
    async def draft(**_kwargs):
        return DistilledSkillDraft(decision="promote", confidence=0.94, name="Evidence Review",
            description="Compare claims with primary evidence.", instructions_markdown=instructions,
            declared_tools=(), declared_packs=(), reason="Controlled semantic review.",
            skill_markdown="---\nname: Evidence Review\ndescription: Compare claims with primary evidence.\n---\n\n# Evidence Review\n\n" + instructions)

    async def no_prior_sessions(**_kwargs):
        return []

    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", draft)
    monkeypatch.setattr("app.services.skill_distiller._load_internal_session_evidence", no_prior_sessions)
    monkeypatch.setattr("app.services.skill_distiller._run_skill_artifact_gate", _passing_artifact_gate)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", _approving_referee_review)
    async def learn(seed):
        n = SimpleNamespace(tenant=seed["tenant_id"], agent=seed["agent_id"], user=seed["user_id"], root=seed["agent_root"])
        n.session, n.run = await new_owner_work(n, owner_sessionmaker)
        seed["learned_source_session"] = n.session
        n.service = agent_tools._get_tool_runtime_service()
        async with owner_sessionmaker() as db:
            for capability in ("agent.skill.write", "workspace.file.write"):
                db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
            await db.commit()
        submitted, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", {
            "name": "Evidence Review", "description": "Compare claims with primary evidence.",
            "instructions": instructions, "folder_name": "evidence-review"})
        assert "active_skill_created: false" in submitted, submitted
        outcome = await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
            current_session_id=str(n.session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
            model=SimpleNamespace(provider="reference", model="controlled-skill-writer", api_key="synthetic", base_url=None))
        assert outcome["status"] == "provisional", outcome
        return outcome

    calls = [("load_skill", {"name": "Evidence Review"}), ("write_file", {
        "path": "workspace/learned-review.md", "content": "NATIVE_LEARNED_METHOD_RESULT"})]
    script = [{"tool_calls": [{"id": f"learned-native-{index}", "type": "function", "function": {
        "name": tool, "arguments": json.dumps(args)}}], "finish_reason": "tool_calls"} for index, (tool, args) in enumerate(calls)]
    script.append({"content": "Completed the evidence review; see learned-review.md.", "finish_reason": "stop"})
    seed, client, _bindings, _outcome = await _run_native_skill_task(owner_sessionmaker, app_user_sessionmaker,
        monkeypatch, tmp_path, skill_setup=learn, client_script=script,
        prompt="Use my learned Evidence Review method and save the result in learned-review.md.")
    assert "NATIVE_LEARNED_METHOD" in json.dumps(client.requests, ensure_ascii=False)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
        assert len(models) == 3
        assert all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] is True for row in models)
        assert any(ref["source_kind"] == "learned_skill_file" for row in models
            for ref in row.model_request_snapshot_json["input_bindings"]["dependencies"])
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == seed["run_id"]))
        assert artifact is not None and artifact.path == "workspace/learned-review.md"
        owner = await db.get(User, seed["user_id"])
        await validate_artifact_content_receipt(db, seed["tenant_id"], owner, artifact)
        artifact_id = artifact.id
    foreign = uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=foreign, tenant_id=seed["tenant_id"], username=f"other-{foreign.hex}",
            email=f"{foreign.hex}@fixture.test", password_hash="synthetic", display_name="Other member", role="member", is_active=True))
        await db.flush()
        (await db.get(ChatSession, seed["learned_source_session"])).user_id = foreign
        await db.commit()
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        artifact = await db.get(ChatArtifact, artifact_id)
        owner = await db.get(User, seed["user_id"])
        with pytest.raises(ContentInputReceiptError) as denied:
            await validate_artifact_content_receipt(db, seed["tenant_id"], owner, artifact)
        assert denied.value.status_code == 403


@pytest.mark.parametrize("failure_statement", [1, 2])
async def test_candidate_source_fence_precedes_private_files_and_survives_capture_failure(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch, failure_statement,  # noqa: F811
):
    from sqlalchemy import select
    from app.models.capability_policy import CapabilityPolicy
    from app.models.config_revision import ConfigRevision
    from app.services import learned_skill_sources
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s = shared_memory
    n = s.n
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.skill.write", allowed=True, requires_approval=False))
        await db.commit()
    save = learned_skill_sources.save_revision
    calls = 0
    async def unavailable_source_record(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failure_statement:
            raise OSError("Controlled candidate source SQL failure")
        return await save(*args, **kwargs)

    monkeypatch.setattr(learned_skill_sources, "save_revision", unavailable_source_record)
    result, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", {
        "name": "Private failure method", "description": "A private source-failure probe.",
        "instructions": "Record PRIVATE_CANDIDATE_FAILURE_INPUT only in authorized work.", "folder_name": "private-failure"})
    assert "active_skill_created: false" not in result, result
    drafts = list((n.root / "evolution/skill_candidates").glob("*/SKILL.md.draft"))
    if failure_statement == 1:
        assert drafts == []
        assert not (n.root / "evolution/evolution_ledger.jsonl").exists()
    else:
        assert len(drafts) == 1 and "PRIVATE_CANDIDATE_FAILURE_INPUT" in drafts[0].read_text()
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == n.tenant,
                ConfigRevision.entity_type == learned_skill_sources.CANDIDATE_ENTITY,
                ConfigRevision.content["agent_id"].astext == str(n.agent), ConfigRevision.is_active.is_(True)))
            assert row is not None and row.content["phase"] == "prepared"
        foreign, _invocation = await memory_call(n, app_user_sessionmaker, "read_file", {
            "path": "evolution/evolution_ledger.jsonl"}, user=n.foreign, session=s.second_session, run=s.second_run)
        assert "PRIVATE_CANDIDATE_FAILURE_INPUT" not in foreign, foreign


@pytest.mark.parametrize("reader", ["author", "other_member"])
async def test_enterprise_asset_catalog_and_revisions_keep_learned_source_authority(
    learned_skill, app_user_sessionmaker, monkeypatch, reader,
):
    from sqlalchemy import select
    from app.api import ai_assets
    from app.core.security import get_current_user
    from app.models.ai_asset import AIAssetRecord
    from app.models.user import User

    s, _submission = learned_skill
    n = s.n
    n.client._transport.app.include_router(ai_assets.router)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        asset = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == n.tenant,
            AIAssetRecord.native_key == f"skill:agent:{n.agent}:{n.first_learned_skill_path.split('/')[1]}"))
        assert asset is not None
        identity = str(asset.id)
    if reader == "other_member":
        async def other_user():
            async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
                return await db.get(User, n.foreign)

        monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_current_user, other_user)
    listing = await n.client.get("/enterprise/ai-assets")
    assert listing.status_code == 200, listing.text
    assert (identity in listing.text) is (reader == "author"), listing.text
    for suffix in ("", "/revisions", "/revisions/1"):
        response = await n.client.get(f"/enterprise/ai-assets/{identity}{suffix}")
        if reader == "author":
            assert response.status_code == 200, response.text
            if suffix == "/revisions/1":
                assert "EVIDENCE_METHOD_VERSION_ONE" in response.text, response.text
        else:
            assert response.status_code in {403, 404} and "EVIDENCE_METHOD_VERSION_ONE" not in response.text, response.text


@pytest.fixture
async def failed_skill_submission(shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.models.capability_policy import CapabilityPolicy
    from app.services import learned_skill_sources
    from app.tools.service import _extract_tool_error_payload
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s = shared_memory
    n = s.n
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.skill.write", allowed=True, requires_approval=False))
        await db.commit()
    save = learned_skill_sources.save_revision
    failures = []
    async def fail_capture_once(*args, **kwargs):
        if kwargs.get("content", {}).get("phase") == "captured" and not failures:
            failures.append("captured")
            raise OSError("Controlled interrupted Skill source capture")
        return await save(*args, **kwargs)

    monkeypatch.setattr(learned_skill_sources, "save_revision", fail_capture_once)
    args = {"name": "Recovery Method", "description": "Compare the retained source with its exact receipt.",
        "instructions": "Read the original evidence and record PRIVATE_RECOVERY_METHOD.", "folder_name": "recovery-method"}
    result, original = await memory_call(n, app_user_sessionmaker, "save_skill", args)
    assert failures == ["captured"]
    error = _extract_tool_error_payload(result)
    assert error is not None and error.get("recovery_tool") == "reconcile_skill_candidate", result
    assert error.get("operation_id"), result
    drafts = list((n.root / "evolution/skill_candidates").glob("*/SKILL.md.draft"))
    assert len(drafts) == 1
    return SimpleNamespace(s=s, args=args, original=original, operation=error["operation_id"],
        draft=drafts[0], original_bytes=drafts[0].read_bytes())


async def test_owner_recovers_the_same_candidate_after_a_capture_failure_and_does_not_activate_twice(
    failed_skill_submission, app_user_sessionmaker, monkeypatch,
):
    import json
    from sqlalchemy import select
    from app.models.config_revision import ConfigRevision
    from app.models.session_v2 import SessionToolInvocation
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
    from tests.services.test_skill_distiller import _approving_referee_review, _passing_artifact_gate

    f = failed_skill_submission
    n = f.s.n
    before = read_agent_asset_revision(n.root)
    status, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate", {"action": "status"}, include_history=True)
    assert f.operation in status, status
    resumed, recovery = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, include_history=True)
    assert resumed.startswith("{"), resumed
    outcome = json.loads(resumed)
    assert outcome["status"] == "captured" and outcome["candidate_id"] == f.draft.parent.name, outcome
    assert f.draft.read_bytes() == f.original_bytes and read_agent_asset_revision(n.root) == before
    replay, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, include_history=True)
    assert json.loads(replay)["candidate_id"] == f.draft.parent.name
    assert f.draft.read_bytes() == f.original_bytes and read_agent_asset_revision(n.root) == before
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        original = await db.get(SessionToolInvocation, f.original.id, populate_existing=True)
        assert original.effect_state == "failed", original.effect_state
        row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == n.tenant,
            ConfigRevision.entity_type == "learned_skill_candidate", ConfigRevision.content["candidate_id"].astext == f.draft.parent.name,
            ConfigRevision.is_active.is_(True)))
        assert row.content["recovery"]["invocation_id"] == str(recovery.id)
    drafts = []
    async def draft_once(**_kwargs):
        drafts.append(True)
        return DistilledSkillDraft(decision="promote", confidence=0.95, name=f.args["name"], description=f.args["description"],
            instructions_markdown=f.args["instructions"], declared_tools=(), declared_packs=(), reason="Controlled recovered method review.",
            skill_markdown=f.original_bytes.decode())
    async def no_prior(**_kwargs):
        return []

    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", draft_once)
    monkeypatch.setattr("app.services.skill_distiller._load_internal_session_evidence", no_prior)
    monkeypatch.setattr("app.services.skill_distiller._run_skill_artifact_gate", _passing_artifact_gate)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", _approving_referee_review)
    async def activate():
        return await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
            current_session_id=str(n.session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
            model=SimpleNamespace(provider="reference", model="controlled", api_key="synthetic", base_url=None))
    activated = await activate()
    assert activated["status"] == "provisional", activated
    await activate()
    assert drafts == [True]
    assert len(list((n.root / "skills").glob("learned-*/SKILL.md"))) == 1
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": f.args["name"]}, include_history=True)
    assert "PRIVATE_RECOVERY_METHOD" in loaded, loaded


@pytest.mark.parametrize("decision", ["deny", "require_approval"])
async def test_skill_recovery_honors_the_current_original_tool_policy(
    failed_skill_submission, owner_sessionmaker, app_user_sessionmaker, decision,
):
    from sqlalchemy import select
    from app.models.config_revision import ConfigRevision
    from app.models.guard_policy import GuardPolicy
    from app.tools.service import _extract_tool_error_payload
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    f = failed_skill_submission
    n = f.s.n
    async with owner_sessionmaker() as db:
        db.add(GuardPolicy(tenant_id=n.tenant, zone_guard={"tool_rules": [{"tools": ["save_skill"],
            "decision": decision, "argument_equals": {"folder_name": f.args["folder_name"]}}]}))
        await db.commit()
    denied, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, include_history=True)
    error = _extract_tool_error_payload(denied)
    assert error is not None, denied
    assert error["reason_code"] == ("learned_skill_recovery_denied" if decision == "deny"
        else "learned_skill_recovery_approval_required"), denied
    assert error["outcome"] == ("denied" if decision == "deny" else "approval_required"), denied
    assert f.draft.read_bytes() == f.original_bytes
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == n.tenant,
            ConfigRevision.entity_id == f.operation, ConfigRevision.is_active.is_(True)))
        assert row.content["phase"] == "prepared"
    async with owner_sessionmaker() as db:
        guard = await db.scalar(select(GuardPolicy).where(GuardPolicy.tenant_id == n.tenant))
        guard.zone_guard, guard.version = {}, guard.version + 1
        await db.commit()
    resumed, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, include_history=True)
    assert '"status": "captured"' in resumed, resumed


async def test_skill_recovery_requires_the_original_owner_and_retains_a_cancelled_plan(
    failed_skill_submission, app_user_sessionmaker,
):
    from app.tools.service import _extract_tool_error_payload
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    f = failed_skill_submission
    n, s = f.s.n, f.s
    absent, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate", {"action": "status"},
        user=n.foreign, session=s.second_session, run=s.second_run)
    assert f.operation not in absent and f.draft.parent.name not in absent, absent
    for action in ("retry", "cancel", "status"):
        denied, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
            {"operation_id": f.operation, "action": action}, user=n.foreign, session=s.second_session, run=s.second_run)
        error = _extract_tool_error_payload(denied)
        assert error is not None and error["reason_code"] == "learned_skill_operation_denied", denied
        assert f.draft.parent.name not in denied and "PRIVATE_RECOVERY_METHOD" not in denied, denied
    for _repeat in range(2):
        cancelled, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
            {"operation_id": f.operation, "action": "cancel"}, include_history=True)
        assert '"status": "cancelled"' in cancelled, cancelled
    retry, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, include_history=True)
    assert _extract_tool_error_payload(retry)["reason_code"] == "learned_skill_operation_cancelled", retry
    assert f.draft.read_bytes() == f.original_bytes
    assert not list((n.root / "skills").glob("learned-*/SKILL.md"))


async def test_skill_recovery_from_new_work_rechecks_original_source_access(
    failed_skill_submission, owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.chat_session import ChatSession
    from app.tools.service import _extract_tool_error_payload
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

    f = failed_skill_submission
    n = f.s.n
    session, run = await new_owner_work(n, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, n.session)).user_id = n.foreign
        await db.commit()
    denied, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, session=session, run=run, include_history=True)
    error = _extract_tool_error_payload(denied)
    assert error is not None and error["outcome"] == "denied", denied
    assert f.draft.read_bytes() == f.original_bytes
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, n.session)).user_id = n.user
        await db.commit()
    recovered, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_skill_candidate",
        {"operation_id": f.operation, "action": "retry"}, session=session, run=run, include_history=True)
    assert '"status": "captured"' in recovered, recovered
    assert f.draft.read_bytes() == f.original_bytes


async def test_trial_rollback_stops_discovery_and_preserves_exact_prior_work_sources(
    learned_skill, app_user_sessionmaker,
):
    from app.services.provisional_trial import load_provisional_trial, record_provisional_trial_signal, review_pending_provisional_trials
    from app.services.skill_evolution_registry import get_skill_evolution_entry
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    target = n.root / n.first_learned_skill_path
    before = target.read_bytes()
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
    signal = record_provisional_trial_signal(n.root, skill_name=n.first_learned_skill_path, kind="negative",
        signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
    assert signal["trial_decision"] == "model_review_pending", signal
    inspected = []
    async def rollback(payload):
        inspected.append(payload)
        assert "EVIDENCE_METHOD_VERSION_ONE" in payload["candidate_content"]
        return {"decision": "rollback", "reason": "Controlled reviewer rejects future use while retaining historical evidence."}

    outcome = await review_pending_provisional_trials(n.root, reviewer=rollback, agent_id=n.agent, tenant_id=n.tenant)
    assert outcome["reviewed"] == 1 and len(inspected) == 1, outcome
    assert target.exists() and target.read_bytes() == before
    entry = get_skill_evolution_entry(n.root, n.first_learned_skill_path)
    trial = load_provisional_trial(n.root, entry["last_candidate_id"])
    assert entry["state"] == trial["state"] == "rolled_back"
    assert trial["rollback"]["action"] == "retire"
    await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="rolled_back", lifecycle_status="deprecated")
    next_load, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" not in next_load, next_load
    history, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "The method used earlier was rolled back; its original work remains verifiable.", "category": "strategy"},
        include_history=True)
    assert "Saved to explicit memory overlay" in history, history
    repeated = await review_pending_provisional_trials(n.root, reviewer=rollback)
    assert repeated["reviewed"] == 0 and len(inspected) == 1
    assert target.read_bytes() == before


@pytest.mark.parametrize("withdrawal", ["none", "before_review", "during_review"])
async def test_background_trial_review_keeps_current_source_authority_and_can_resume(
    learned_skill, owner_sessionmaker, app_user_sessionmaker, monkeypatch, withdrawal,
):
    from app.models.chat_session import ChatSession
    from app.services.provisional_trial import load_provisional_trial, record_provisional_trial_signal, review_pending_provisional_trials_with_model
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    before = (n.root / path).read_bytes()
    entry = get_skill_evolution_entry(n.root, path)
    record_provisional_trial_signal(n.root, skill_name=path, kind="negative",
        signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
    async def set_reader(reader):
        async with owner_sessionmaker() as db:
            (await db.get(ChatSession, n.session)).user_id = reader
            await db.commit()
    if withdrawal == "before_review":
        await set_reader(n.foreign)
    requests = []
    closed = []
    class ControlledTrialReviewer:
        async def complete(self, *, messages, **_kwargs):
            requests.append(messages)
            if withdrawal == "during_review" and len(requests) == 1:
                await set_reader(n.foreign)
            return SimpleNamespace(content='{"decision":"promote","reason":"Controlled trial result."}')

        async def close(self):
            closed.append(True)

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: ControlledTrialReviewer())
    async def review():
        return await review_pending_provisional_trials_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
            model=SimpleNamespace(provider="synthetic", model="controlled-trial-review", api_key="synthetic", base_url="http://127.0.0.1:1"))
    report = await review()
    trial = load_provisional_trial(n.root, entry["last_candidate_id"])
    if withdrawal == "none":
        assert report["reviewed"] == 1 and trial["state"] == "promoted", report
        assert len(requests) == 1 and "EVIDENCE_METHOD_VERSION_ONE" in repr(requests), requests
        await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="active", lifecycle_status="active")
    else:
        assert report["reviewed"] == 0 and trial["state"] == "provisional", report
        assert report["errors"] and report["errors"][0]["outcome"] == "denied", report
        assert len(requests) == (0 if withdrawal == "before_review" else 1), requests
        assert trial["review_status"] == "pending_model_review"
        assert (n.root / path).read_bytes() == before
        await set_reader(n.user)
        restored = await review()
        assert restored["reviewed"] == 1, restored
        trial = load_provisional_trial(n.root, entry["last_candidate_id"])
        assert trial["state"] == "promoted"
    assert closed
    assert (n.root / path).read_bytes() == before


@pytest.mark.parametrize("foreign_revoked", [False, True])
async def test_background_trial_reviews_keep_private_method_inputs_separate(
    learned_skill, owner_sessionmaker, app_user_sessionmaker, monkeypatch, foreign_revoked,
):
    from app.models.chat_session import ChatSession
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from app.services.provisional_trial import record_provisional_trial_signal, review_pending_provisional_trials_with_model
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    instructions = "Inspect my own source and record FOREIGN_MAINTENANCE_METHOD."
    submitted, _invocation = await memory_call(n, app_user_sessionmaker, "save_skill", {
        "name": "Evidence Review", "description": "Review the requesting member's own evidence.",
        "instructions": instructions, "folder_name": "evidence-review"},
        user=n.foreign, session=s.second_session, run=s.second_run)
    assert "active_skill_created: false" in submitted, submitted
    async def foreign_draft(**_kwargs):
        return DistilledSkillDraft(decision="promote", confidence=0.95, name="Evidence Review",
            description="Review the requesting member's own evidence.", instructions_markdown=instructions,
            declared_tools=(), declared_packs=(), reason="Controlled private source review.",
            skill_markdown="---\nname: Evidence Review\ndescription: Review the requesting member's own evidence.\n---\n\n" + instructions)

    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", foreign_draft)
    installed = await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
        current_session_id=str(s.second_session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
        model=SimpleNamespace(provider="synthetic", model="controlled-private-method", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert installed["status"] == "provisional", installed
    assert installed["skill_path"] != n.first_learned_skill_path
    for path, session, run in [(n.first_learned_skill_path, n.session, n.run), (installed["skill_path"], s.second_session, s.second_run)]:
        record_provisional_trial_signal(n.root, skill_name=path, kind="negative",
            signal={"runtime_task_id": str(run), "session_id": str(session), "status": "failed"})
    if foreign_revoked:
        async with owner_sessionmaker() as db:
            (await db.get(ChatSession, s.second_session)).user_id = n.user
            await db.commit()
    prompts = []
    class ControlledPrivateReviewer:
        async def complete(self, *, messages, **_kwargs):
            prompts.append("\n".join(m.content or "" for m in messages))
            return SimpleNamespace(content='{"decision":"promote","reason":"Controlled separate private review."}')

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: ControlledPrivateReviewer())
    report = await review_pending_provisional_trials_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
        model=SimpleNamespace(provider="synthetic", model="controlled-trial-review", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert report["reviewed"] == (1 if foreign_revoked else 2), report
    assert sum("EVIDENCE_METHOD_VERSION_ONE" in p for p in prompts) == 1, prompts
    assert sum("FOREIGN_MAINTENANCE_METHOD" in p for p in prompts) == (0 if foreign_revoked else 1), prompts
    assert all(not ("EVIDENCE_METHOD_VERSION_ONE" in p and "FOREIGN_MAINTENANCE_METHOD" in p) for p in prompts), prompts
    from app.services.skill_curator import review_skill_lifecycle_with_model
    import json

    curator_prompts = []
    class ControlledPrivateCurator:
        async def complete(self, *, messages, **_kwargs):
            curator_prompts.append("\n".join(m.content or "" for m in messages))
            return SimpleNamespace(content=json.dumps({"decisions": [
                {"slug": path.split("/")[1], "decision": "keep_active", "reason": "Controlled private lifecycle decision."}
                for path in (n.first_learned_skill_path, installed["skill_path"])]}))

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: ControlledPrivateCurator())
    curated = await review_skill_lifecycle_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
        model=SimpleNamespace(provider="synthetic", model="controlled-private-curator", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert len(curated["applied"]) == (1 if foreign_revoked else 2), curated
    assert sum("EVIDENCE_METHOD_VERSION_ONE" in p for p in curator_prompts) == 1, curator_prompts
    assert sum("FOREIGN_MAINTENANCE_METHOD" in p for p in curator_prompts) == (0 if foreign_revoked else 1), curator_prompts
    assert all(not ("EVIDENCE_METHOD_VERSION_ONE" in p and "FOREIGN_MAINTENANCE_METHOD" in p) for p in curator_prompts), curator_prompts


async def test_trial_review_cannot_reactivate_a_method_retired_while_the_model_was_running(learned_skill, monkeypatch):
    from pathlib import Path
    from app.services.provisional_trial import load_provisional_trial, record_provisional_trial_signal, review_pending_provisional_trials_with_model
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    entry = get_skill_evolution_entry(n.root, path)
    record_provisional_trial_signal(n.root, skill_name=path, kind="negative",
        signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
    class RetiringReviewer:
        async def complete(self, **_kwargs):
            response = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": Path(path).parts[1]})
            assert response.status_code == 200 and response.json()["status"] == "retired", response.text
            return SimpleNamespace(content='{"decision":"promote","reason":"Controlled stale decision after retirement."}')

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: RetiringReviewer())
    report = await review_pending_provisional_trials_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
        model=SimpleNamespace(provider="synthetic", model="controlled-trial-review", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert report["reviewed"] == 0 and report["errors"][0]["reason_code"] == "skill_maintenance_retired", report
    assert load_provisional_trial(n.root, entry["last_candidate_id"])["state"] == "provisional"
    assert (n.root / path).is_file()


@pytest.mark.parametrize("transition", ["archive_provisional", "promote", "retire", "trial_rollback", "archive_active"])
async def test_owner_asset_rollback_restores_native_learning_state_and_preserves_observations(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, transition,
):
    from copy import deepcopy
    import uuid
    import json
    from app.models.user import User
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.provisional_trial import (load_provisional_trial, record_provisional_trial_signal,
        review_pending_provisional_trials_with_model)
    from app.services.skill_curator import review_skill_lifecycle_with_model
    from app.services.skill_evolution_registry import get_skill_evolution_entry
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    body = (n.root / path).read_bytes()
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    initial = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    identity = initial["asset"]["id"]
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
    model = SimpleNamespace(provider="synthetic", model="controlled-lifecycle-review", api_key="synthetic", base_url="http://127.0.0.1:1")
    if transition in {"promote", "trial_rollback", "archive_active"}:
        decision = "rollback" if transition == "trial_rollback" else "promote"
        class TrialReviewer:
            async def complete(self, **_kwargs):
                return SimpleNamespace(content=json.dumps({"decision": decision, "reason": "Controlled trial decision before owner rollback."}))
            async def close(self):
                pass
        monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: TrialReviewer())
        record_provisional_trial_signal(n.root, skill_name=path, kind="negative",
            signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
        report = await review_pending_provisional_trials_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant, model=model)
        assert report["reviewed"] == 1, report
    if transition in {"archive_provisional", "archive_active"}:
        class ArchiveReviewer:
            async def complete(self, **_kwargs):
                return SimpleNamespace(content=json.dumps({"decisions": [{"slug": path.split('/')[1],
                    "decision": "archive", "reason": "Controlled reversible archive before owner rollback."}]}))
            async def close(self):
                pass
        monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: ArchiveReviewer())
        report = await review_skill_lifecycle_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant, model=model)
        assert report["applied"] == [{"slug": path.split('/')[1], "decision": "archive"}], report
    if transition == "retire":
        removed = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": path.split('/')[1]})
        assert removed.status_code == 200 and removed.json()["status"] == "retired", removed.text
    entry = get_skill_evolution_entry(n.root, path)
    before_trial = deepcopy(load_provisional_trial(n.root, entry["last_candidate_id"]))
    assert before_trial is not None
    target = 2 if transition == "archive_active" else 1
    current = (await n.client.get(f"/enterprise/ai-assets/{identity}")).json()
    command = {"target_version": target, "operation_id": str(uuid.uuid4()),
        "expected_revision_id": current["asset"]["active_revision_id"]}
    response = await n.client.post(f"/enterprise/ai-assets/{identity}/rollback", json=command)
    assert response.status_code == 200, response.text
    expected_state = "active" if transition == "archive_active" else "provisional"
    assert get_skill_evolution_entry(n.root, path)["state"] == expected_state, response.text
    after_trial = load_provisional_trial(n.root, entry["last_candidate_id"])
    assert after_trial["state"] == ("promoted" if expected_state == "active" else "provisional"), after_trial
    control_fields = {"state", "updated_at", "owner_restorations"}
    assert {k: v for k, v in after_trial.items() if k not in control_fields} == {
        k: v for k, v in before_trial.items() if k not in control_fields}
    assert (n.root / path).read_bytes() == body
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state=expected_state, lifecycle_status="active")
    native_revision = read_agent_asset_revision(n.root)
    repeated = await n.client.post(f"/enterprise/ai-assets/{identity}/rollback", json=command)
    assert repeated.status_code == 200 and repeated.json() == response.json(), repeated.text
    assert read_agent_asset_revision(n.root) == native_revision
    again = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state=expected_state, lifecycle_status="active")
    assert again["active_revision"]["id"] == payload["active_revision"]["id"]
    restored, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" in restored, restored
    historical, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", {
        "content": "Earlier use of this exact method remains verifiable after owner lifecycle rollback.", "category": "strategy"}, include_history=True)
    assert "Saved to explicit memory overlay" in historical, historical
    if transition == "retire":
        # Retirement is a distinct owner fact, even though a curator archive looks identical.
        retired_again = await n.client.post(f"/enterprise/ai-assets/{identity}/rollback", json={
            "target_version": 2, "operation_id": str(uuid.uuid4()),
            "expected_revision_id": again["asset"]["active_revision_id"]})
        assert retired_again.status_code == 200, retired_again.text
        state = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="archived", lifecycle_status="deprecated")
        assert state["asset"]["compatibility"]["learned_retired"] is True
        restored_again = await n.client.post(f"/enterprise/ai-assets/{identity}/rollback", json={
            "target_version": 1, "operation_id": str(uuid.uuid4()),
            "expected_revision_id": state["asset"]["active_revision_id"]})
        assert restored_again.status_code == 200, restored_again.text
        state = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
        assert state["asset"]["compatibility"]["learned_retired"] is False


@pytest.mark.parametrize("failure", ["prepare_before", "prepare_after", "final_before", "final_after", "supersede"])
async def test_owner_rollback_commit_recovery_never_replays_or_revives_a_later_retirement(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, failure,
):
    import uuid
    from app.database import get_db
    from app.models.user import User
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.learned_skill_lifecycle import reconcile_agent_lifecycles
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    body = (n.root / path).read_bytes()
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    initial = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    asset_id = initial["asset"]["id"]
    retired = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": path.split('/')[1]})
    assert retired.status_code == 200, retired.text
    current = (await n.client.get(f"/enterprise/ai-assets/{asset_id}")).json()
    command = {"target_version": 1, "operation_id": str(uuid.uuid4()),
        "expected_revision_id": current["asset"]["active_revision_id"]}
    native_before = read_agent_asset_revision(n.root)
    commit_calls, faults = [], []
    original_dependency = n.client._transport.app.dependency_overrides[get_db]
    async def fault_db():
        async for db in original_dependency():
            commit = db.commit
            async def faulty_commit():
                commit_calls.append(True)
                stage = 1 if failure.startswith("prepare") else 2
                if len(commit_calls) == stage and not faults:
                    faults.append(failure)
                    if failure.endswith("after"):
                        await commit()
                    raise OSError("Controlled owner rollback commit response failure")
                await commit()
            monkeypatch.setattr(db, "commit", faulty_commit)
            yield db
    monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_db, fault_db)
    first = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json=command)
    assert faults == [failure], first.text
    native_after = read_agent_asset_revision(n.root)
    if failure in {"prepare_after", "final_after"}:
        assert first.status_code == 200, first.text
    else:
        assert first.status_code == 503 and first.json()["detail"]["code"] == "skill_owner_operation_pending", first.text
        assert first.json()["detail"]["outcome"] == ("not_started" if failure == "prepare_before" else "unknown"), first.text
    assert native_after == native_before + (0 if failure == "prepare_before" else 1)
    if failure in {"final_before", "supersede"}:
        pending = await n.client.get(f"/enterprise/ai-assets/{asset_id}")
        assert pending.status_code == 200, pending.text
        assert pending.json()["asset"]["projection"]["owner_operation"] == {
            "operation_id": command["operation_id"], "expected_revision_id": command["expected_revision_id"], "target_version": 1}, pending.text
        held = await n.client.post(f"/enterprise/ai-assets/{asset_id}/reconcile")
        assert held.status_code == 503 and held.json()["detail"]["code"] == "skill_owner_operation_pending", held.text
        background = await reconcile_agent_lifecycles(workspace=n.root, agent_id=n.agent, tenant_id=n.tenant)
        assert background["applied"] == 0, background
        assert not any(item.get("status") == "applied" for item in background["held"]), background
    if failure == "supersede":
        stopped = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": path.split('/')[1]})
        assert stopped.status_code == 200, stopped.text
        stale = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json=command)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "skill_owner_operation_superseded", stale.text
        current = (await n.client.get(f"/enterprise/ai-assets/{asset_id}")).json()
        assert current["asset"]["lifecycle_status"] == "deprecated" and current["asset"]["projection"]["status"] == "applied", current
        command = {"target_version": 1, "operation_id": str(uuid.uuid4()),
            "expected_revision_id": current["asset"]["active_revision_id"]}
    resumed = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json=command)
    assert resumed.status_code == 200, resumed.text
    result = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    after_resume = read_agent_asset_revision(n.root)
    repeated = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json=command)
    assert repeated.status_code == 200 and repeated.json() == resumed.json(), repeated.text
    assert read_agent_asset_revision(n.root) == after_resume
    if failure != "prepare_before" and failure != "supersede":
        assert after_resume == native_after
    assert result["asset"]["compatibility"]["learned_retired"] is False
    entry = get_skill_evolution_entry(n.root, path)
    assert load_provisional_trial(n.root, entry["last_candidate_id"])["owner_restorations"][-1]["operation_id"] == command["operation_id"]
    stopped = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": path.split('/')[1]})
    assert stopped.status_code == 200, stopped.text
    old = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json=command)
    assert old.status_code == 200 and old.json() == resumed.json(), old.text
    retained = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="archived", lifecycle_status="deprecated")
    assert retained["asset"]["compatibility"]["learned_retired"] is True
    assert read_agent_asset_revision(n.root) == after_resume and (n.root / path).read_bytes() == body


@pytest.mark.parametrize("reader", ["owner_admin", "other_admin_inspection", "other_admin_recipient"])
async def test_owner_rollback_checks_actual_source_authority_before_native_or_sql_effects(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, reader,
):
    from contextlib import nullcontext
    import uuid
    from sqlalchemy import select, func
    from app.core.security import get_current_user
    from app.core.source_read_purpose import source_recipient_read
    from app.models.config_revision import ConfigRevision
    from app.models.user import User
    from app.services.agent_asset_transaction import read_agent_asset_revision

    s, _submission = learned_skill
    n = s.n
    async with owner_sessionmaker() as db:
        for actor in [n.user, n.foreign]:
            (await db.get(User, actor)).role = "org_admin"
        await db.commit()
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    asset_id = payload["asset"]["id"]
    if reader != "owner_admin":
        async def other_user():
            async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
                return await db.get(User, n.foreign)
        monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_current_user, other_user)
    before = read_agent_asset_revision(n.root)
    command = {"target_version": 1, "operation_id": str(uuid.uuid4()), "expected_revision_id": payload["asset"]["active_revision_id"]}
    purpose = source_recipient_read(session_id=s.second_session, user_id=n.foreign) if reader == "other_admin_recipient" else nullcontext()
    with purpose:
        response = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json=command)
    if reader == "other_admin_recipient":
        assert response.status_code == 403 and "EVIDENCE_METHOD_VERSION_ONE" not in response.text, response.text
        with source_recipient_read(session_id=s.second_session, user_id=n.foreign):
            invalid = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json={
                **command, "target_version": 999, "operation_id": str(uuid.uuid4())})
        assert invalid.status_code == 403 and "EVIDENCE_METHOD_VERSION_ONE" not in invalid.text, invalid.text
        assert read_agent_asset_revision(n.root) == before
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            count = await db.scalar(select(func.count()).select_from(ConfigRevision).where(
                ConfigRevision.tenant_id == n.tenant, ConfigRevision.entity_type == "learned_skill_asset_operation"))
            assert count == 0
        unchanged = (await n.client.get(f"/enterprise/ai-assets/{asset_id}")).json()
        assert unchanged["asset"]["active_revision_id"] == command["expected_revision_id"]
        assert unchanged["asset"]["projection"]["status"] == "applied", unchanged
    else:
        assert response.status_code == 200, response.text
        assert read_agent_asset_revision(n.root) == before + 1
        stale = await n.client.post(f"/enterprise/ai-assets/{asset_id}/rollback", json={**command, "operation_id": str(uuid.uuid4())})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "skill_owner_revision_changed", stale.text


async def test_waiting_trial_model_cannot_overwrite_an_owner_rollback(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    import uuid
    from app.models.user import User
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.provisional_trial import record_provisional_trial_signal, review_pending_provisional_trials
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    s, _submission = learned_skill
    n = s.n
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    path = n.first_learned_skill_path
    record_provisional_trial_signal(n.root, skill_name=path, kind="negative", signal={"status": "failed"})
    native_after_owner = []
    async def reviewer(evidence):
        assert "EVIDENCE_METHOD_VERSION_ONE" in evidence["candidate_content"]
        response = await n.client.post(f"/enterprise/ai-assets/{payload['asset']['id']}/rollback", json={
            "target_version": 1, "operation_id": str(uuid.uuid4()), "expected_revision_id": payload["asset"]["active_revision_id"]})
        assert response.status_code == 200, response.text
        native_after_owner.append(read_agent_asset_revision(n.root))
        return {"decision": "promote", "reason": "Controlled review that predates the owner intervention."}
    result = await review_pending_provisional_trials(n.root, agent_id=n.agent, tenant_id=n.tenant, reviewer=reviewer)
    assert result["reviewed"] == 0 and result["errors"][0]["reason_code"] == "skill_owner_operation_changed", result
    assert get_skill_evolution_entry(n.root, path)["state"] == "provisional"
    assert [read_agent_asset_revision(n.root)] == native_after_owner


@pytest.mark.parametrize("problem", ["expired", "unreceipted"])
async def test_owner_rollback_retains_trial_window_and_requires_committed_control_evidence(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, problem,
):
    from datetime import UTC, datetime, timedelta
    import json
    import uuid
    from app.models.user import User
    from app.services.agent_asset_transaction import AgentAssetTransaction, read_agent_asset_revision
    from app.services.provisional_trial import load_provisional_trial, _stage_trial, trial_rel_path
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    candidate = get_skill_evolution_entry(n.root, path)["last_candidate_id"]
    trial_file = n.root / trial_rel_path(candidate)
    original = trial_file.read_bytes()
    trial = load_provisional_trial(n.root, candidate)
    if problem == "expired":
        trial["started_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with AgentAssetTransaction(n.root, operation="controlled_historical_trial_window") as tx:
            _stage_trial(tx, trial)
            tx.commit()
    else:
        trial["state"] = "promoted"
        trial_file.write_text(json.dumps(trial))
    native_before = read_agent_asset_revision(n.root)
    body = (n.root / path).read_bytes()
    command = {"target_version": 1, "operation_id": str(uuid.uuid4()), "expected_revision_id": payload["asset"]["active_revision_id"]}
    response = await n.client.post(f"/enterprise/ai-assets/{payload['asset']['id']}/rollback", json=command)
    assert response.status_code == (409 if problem == "expired" else 503), response.text
    assert response.json()["detail"]["code"] == ("skill_owner_trial_expired" if problem == "expired" else "skill_owner_native_unverified"), response.text
    assert read_agent_asset_revision(n.root) == native_before and (n.root / path).read_bytes() == body
    assert load_provisional_trial(n.root, candidate) == trial
    if problem == "unreceipted":
        trial_file.write_bytes(original)
        restored = await n.client.post(f"/enterprise/ai-assets/{payload['asset']['id']}/rollback", json=command)
        assert restored.status_code == 200, restored.text
    else:
        stopped = await n.client.post(f"/agents/{n.agent}/files/uninstall-skill", json={"folder_name": path.split('/')[1]})
        assert stopped.status_code == 200, stopped.text
        assert read_agent_asset_revision(n.root) == native_before


async def test_curator_archive_and_restore_retain_native_skill_history_without_promoting_its_trial(
    learned_skill, app_user_sessionmaker, monkeypatch,
):
    import json
    from app.services.skill_curator import review_skill_lifecycle_with_model
    from app.services.skill_evolution_registry import get_skill_evolution_entry
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    before = (n.root / path).read_bytes()
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
    decision = "archive"
    class ArchivingCurator:
        async def complete(self, *, messages, **_kwargs):
            assert "EVIDENCE_METHOD_VERSION_ONE" in repr(messages)
            return SimpleNamespace(content=json.dumps({"decisions": [{"slug": path.split("/")[1],
                "decision": decision, "reason": "Controlled reversible lifecycle decision."}]}))

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: ArchivingCurator())
    async def curate():
        return await review_skill_lifecycle_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
            model=SimpleNamespace(provider="synthetic", model="controlled-private-curator", api_key="synthetic", base_url="http://127.0.0.1:1"))
    archived = await curate()
    assert archived["applied"] == [{"slug": path.split("/")[1], "decision": "archive"}], archived
    assert (n.root / path).is_file() and (n.root / path).read_bytes() == before
    assert get_skill_evolution_entry(n.root, path)["state"] == "archived"
    await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="archived", lifecycle_status="deprecated")
    denied, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" not in denied, denied
    historical, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", {
        "content": "The archived method's earlier work remains verifiable.", "category": "strategy"}, include_history=True)
    assert "Saved to explicit memory overlay" in historical, historical
    decision = "restore"
    restored = await curate()
    assert restored["applied"] == [{"slug": path.split("/")[1], "decision": "restore"}], restored
    assert (n.root / path).read_bytes() == before
    assert get_skill_evolution_entry(n.root, path)["state"] == "provisional"
    await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    current, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"}, include_history=True)
    assert "EVIDENCE_METHOD_VERSION_ONE" in current, current


@pytest.mark.parametrize("failure", ["before_commit", "after_commit"])
async def test_lifecycle_projection_commit_failure_is_reconciled_without_repeating_native_work_or_model(
    learned_skill, app_user_sessionmaker, monkeypatch, failure,
):
    from contextlib import asynccontextmanager
    from sqlalchemy import select
    from app.api import ai_assets
    from app.models.ai_asset import AIAssetRecord
    from app.services import learned_skill_lifecycle as lifecycle
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.provisional_trial import record_provisional_trial_signal, review_pending_provisional_trials_with_model

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    record_provisional_trial_signal(n.root, skill_name=path, kind="negative",
        signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
    native_scope = lifecycle.tenant_scoped_session
    faults, model_calls = [], []
    class CommitResponseFault:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def commit(self):
            if not faults and failure == "before_commit":
                faults.append("before_commit")
                raise OSError("Controlled interrupted projection commit")
            await self.db.commit()
            if not faults and failure == "after_commit":
                faults.append("after_commit")
                raise OSError("Controlled committed projection response loss")

    @asynccontextmanager
    async def faulty_scope(*args, **kwargs):
        async with native_scope(*args, **kwargs) as db:
            yield CommitResponseFault(db)

    class ControlledPromotion:
        async def complete(self, **_kwargs):
            model_calls.append(True)
            return SimpleNamespace(content='{"decision":"promote","reason":"Controlled exact native decision."}')

        async def close(self):
            pass

    monkeypatch.setattr(lifecycle, "tenant_scoped_session", faulty_scope)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: ControlledPromotion())
    result = await review_pending_provisional_trials_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
        model=SimpleNamespace(provider="synthetic", model="controlled-trial-review", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert result["reviewed"] == 1 and faults == [failure] and model_calls == [True], result
    after_native = read_agent_asset_revision(n.root)
    if failure == "before_commit":
        assert result["errors"][0]["reason_code"] == "skill_lifecycle_projection_pending", result
        assert result["errors"][0]["outcome"] == "unknown", result
        n.client._transport.app.include_router(ai_assets.router)
        n.asset_api_registered = True
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == n.tenant,
                AIAssetRecord.native_key == f"skill:agent:{n.agent}:{path.split('/')[1]}"))
            identity = str(record.id)
        stale = await n.client.get(f"/enterprise/ai-assets/{identity}")
        assert stale.status_code == 200 and stale.json()["asset"]["projection"]["status"] == "drifted", stale.text
        from app.services import evolution_daemon

        async def no_model(*_args, **_kwargs):
            return None
        async def no_enhancement(*_args, **_kwargs):
            return SimpleNamespace(synced=0, skipped=0, reason="Controlled unrelated lane without mutations")
        monkeypatch.setattr(evolution_daemon, "_resolve_agent_model", no_model)
        monkeypatch.setattr("app.memory.enhancement.sync_t3_to_memory_enhancement", no_enhancement)
        restored = await evolution_daemon.run_heartbeat_evolution_maintenance(agent_id=n.agent, tenant_id=n.tenant,
            outcome_type=None, current_session_id=str(n.session))
        assert restored["skill_lifecycle_projection"]["applied"] == 1, restored
        assert restored["skill_curator"]["reason"] == "model_unavailable", restored
    else:
        assert not result["errors"], result
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="active", lifecycle_status="active")
    assert payload["active_revision"]["version"] == 2, payload
    repeated = await lifecycle.reconcile_agent_lifecycles(workspace=n.root, agent_id=n.agent, tenant_id=n.tenant)
    assert repeated["applied"] == 0, repeated
    again = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="active", lifecycle_status="active")
    assert again["active_revision"]["version"] == 2
    assert model_calls == [True] and read_agent_asset_revision(n.root) == after_native


@pytest.mark.parametrize("reader", ["owner_admin", "other_admin_inspection", "other_admin_recipient"])
async def test_existing_native_lifecycle_drift_has_a_source_checked_idempotent_reconcile_entry(
    learned_skill, app_user_sessionmaker, owner_sessionmaker, monkeypatch, reader,
):
    from sqlalchemy import select
    from app.api import ai_assets
    from app.core.security import get_current_user
    from app.core.source_read_purpose import source_recipient_read
    from contextlib import nullcontext
    from app.models.ai_asset import AIAssetRecord
    from app.models.user import User
    from app.services import learned_skill_lifecycle as lifecycle
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.provisional_trial import record_provisional_trial_signal, review_pending_provisional_trials_with_model

    s, _submission = learned_skill
    n = s.n
    path = n.first_learned_skill_path
    record_provisional_trial_signal(n.root, skill_name=path, kind="negative",
        signal={"runtime_task_id": str(n.run), "session_id": str(n.session), "status": "failed"})
    model_calls = []
    class LegacyPromotion:
        async def complete(self, **_kwargs):
            model_calls.append(True)
            return SimpleNamespace(content='{"decision":"promote","reason":"Controlled native-only historical transition."}')

        async def close(self):
            pass

    async def native_only(**_kwargs):
        return {"status": "needs_reconciliation", "outcome": "unknown", "reason_code": "controlled_native_only_transition"}
    monkeypatch.setattr(lifecycle, "synchronize_after_native", native_only)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: LegacyPromotion())
    promoted = await review_pending_provisional_trials_with_model(n.root, agent_id=n.agent, tenant_id=n.tenant,
        model=SimpleNamespace(provider="synthetic", model="controlled-trial-review", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert promoted["reviewed"] == 1 and model_calls == [True], promoted
    native_revision = read_agent_asset_revision(n.root)
    actor = n.user if reader == "owner_admin" else n.foreign
    async with owner_sessionmaker() as db:
        # This exercises the existing administrator route with actual persisted test authorization.
        (await db.get(User, actor)).role = "org_admin"
        await db.commit()
    if reader != "owner_admin":
        async def other_user():
            async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
                return await db.get(User, n.foreign)
        monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_current_user, other_user)
    n.client._transport.app.include_router(ai_assets.router)
    n.asset_api_registered = True
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == n.tenant,
            AIAssetRecord.native_key == f"skill:agent:{n.agent}:{path.split('/')[1]}"))
        identity, original_revision = str(record.id), record.active_revision_id
    purpose = source_recipient_read(session_id=s.second_session, user_id=n.foreign) if reader == "other_admin_recipient" else nullcontext()
    with purpose:
        response = await n.client.post(f"/enterprise/ai-assets/{identity}/reconcile")
    if reader != "other_admin_recipient":
        assert response.status_code == 200 and response.json()["status"] == "applied", response.text
        repeated = await n.client.post(f"/enterprise/ai-assets/{identity}/reconcile")
        assert repeated.status_code == 200 and repeated.json()["status"] == "applied", repeated.text
        payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="active", lifecycle_status="active")
        assert payload["active_revision"]["version"] == 2
    else:
        assert response.status_code == 403 and "EVIDENCE_METHOD_VERSION_ONE" not in response.text, response.text
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            current = await db.get(AIAssetRecord, record.id, populate_existing=True)
            assert current.active_revision_id == original_revision and current.compatibility_json["evolution_state"] == "provisional"
    assert model_calls == [True] and read_agent_asset_revision(n.root) == native_revision


async def test_lifecycle_repair_confirms_the_error_status_commit_without_creating_a_revision(
    learned_skill, app_user_sessionmaker, monkeypatch,
):
    from contextlib import asynccontextmanager
    from sqlalchemy import select
    from app.models.ai_asset import AIAssetRecord
    from app.services import learned_skill_lifecycle as lifecycle
    from app.services.agent_asset_transaction import read_agent_asset_revision

    s, _submission = learned_skill
    n = s.n
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == n.tenant,
            AIAssetRecord.native_key == f"skill:agent:{n.agent}:{n.first_learned_skill_path.split('/')[1]}"))
        record.projection_status, record.projection_error = "failed", "Controlled earlier projection failure"
        await db.commit()
    native_scope, faults = lifecycle.tenant_scoped_session, []
    native_revision = read_agent_asset_revision(n.root)

    class CommitFault:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def commit(self):
            if not faults:
                faults.append(True)
                raise OSError("Controlled interrupted error-state repair")
            await self.db.commit()

    @asynccontextmanager
    async def faulty_scope(*args, **kwargs):
        async with native_scope(*args, **kwargs) as db:
            yield CommitFault(db)

    monkeypatch.setattr(lifecycle, "tenant_scoped_session", faulty_scope)
    first = await lifecycle.reconcile_agent_lifecycles(workspace=n.root, agent_id=n.agent, tenant_id=n.tenant)
    assert first["status"] == "needs_reconciliation" and first["applied"] == 0, first
    assert first["held"][0]["outcome"] == "unknown", first
    second = await lifecycle.reconcile_agent_lifecycles(workspace=n.root, agent_id=n.agent, tenant_id=n.tenant)
    assert second["status"] == "applied" and second["applied"] == 1, second
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    assert payload["active_revision"]["version"] == 1 and read_agent_asset_revision(n.root) == native_revision


async def test_unreceipted_lifecycle_file_cannot_become_an_applied_asset_projection(
    learned_skill, app_user_sessionmaker, owner_sessionmaker,
):
    import json
    from sqlalchemy import select
    from app.api import ai_assets
    from app.models.ai_asset import AIAssetRecord
    from app.models.user import User

    s, _submission = learned_skill
    n = s.n
    registry = n.root / "evolution/skill_registry.json"
    before = registry.read_bytes()
    edited = json.loads(before)
    entry = next(value for value in edited["skills"].values() if value["target_path"] == n.first_learned_skill_path)
    entry["state"] = "active"
    registry.write_text(json.dumps(edited))
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    n.client._transport.app.include_router(ai_assets.router)
    n.asset_api_registered = True
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == n.tenant,
            AIAssetRecord.native_key == f"skill:agent:{n.agent}:{n.first_learned_skill_path.split('/')[1]}"))
        identity, revision = str(record.id), record.active_revision_id
    observed = await n.client.get(f"/enterprise/ai-assets/{identity}")
    assert observed.status_code == 200 and observed.json()["asset"]["projection"]["status"] == "unavailable", observed.text
    refused = await n.client.post(f"/enterprise/ai-assets/{identity}/reconcile")
    assert refused.status_code == 503 and refused.json()["detail"]["code"] == "skill_lifecycle_native_unverified", refused.text
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        current = await db.get(AIAssetRecord, record.id, populate_existing=True)
        assert current.active_revision_id == revision and current.compatibility_json["evolution_state"] == "provisional"
    registry.write_bytes(before)
    restored = await n.client.post(f"/enterprise/ai-assets/{identity}/reconcile")
    assert restored.status_code == 200 and restored.json()["status"] == "applied", restored.text
    payload = await _assert_native_asset_state(n, app_user_sessionmaker, evolution_state="provisional", lifecycle_status="active")
    assert payload["active_revision"]["version"] == 1


@pytest.mark.parametrize("reader", ["author", "other_member"])
async def test_reviewed_skill_catalog_metadata_respects_its_actual_source_audience(learned_skill, reader, monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _build_native_skill_catalog
    from app.services import agent_context

    s, _submission = learned_skill
    n = s.n
    user, session = (n.user, n.session) if reader == "author" else (n.foreign, s.second_session)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", n.root.parent)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", n.root.parent)
    request = AgentInvocationRequest(model=None, messages=[], agent_name="Evidence reviewer", role_description="",
        agent_id=n.agent, tenant_id=n.tenant, user_id=user)
    catalog = await _build_native_skill_catalog(request, n.agent, session_id=str(session))
    assert ("Compare a requested result with its primary evidence." in catalog) is (reader == "author"), catalog


async def test_later_work_rechecks_learned_inputs_before_the_next_model_request(
    learned_skill, app_user_sessionmaker, owner_sessionmaker,
):
    from sqlalchemy import select
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionModelResult
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

    s, _submission = learned_skill
    n = s.n
    session, run = await new_owner_work(n, owner_sessionmaker)
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_skill", {"name": "Evidence Review"},
        session=session, run=run)
    assert "EVIDENCE_METHOD_VERSION_ONE" in loaded, loaded
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, n.session)).user_id = n.foreign
        await db.commit()
    with pytest.raises(ExecutionAuthorityUnavailable) as blocked:
        await memory_call(n, app_user_sessionmaker, "save_memory",
            {"content": "Use the method whose original inputs were withdrawn", "category": "strategy"},
            session=session, run=run, include_history=True)
    assert blocked.value.status == "denied" and blocked.value.code == "content_source_access_denied"
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run)))
        assert len(models) == 1, models
