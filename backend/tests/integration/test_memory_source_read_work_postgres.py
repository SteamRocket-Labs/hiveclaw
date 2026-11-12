"""Measure real memory disclosures with a retained, shared source DAG."""

import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import event, select

from app.database import tenant_scoped_session
from app.memory.explicit_overlay import load_explicit_overlay_entries, prepare_explicit_memory_entry
from app.memory.write_gate import MemoryWriteDecision
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.organization_identity import ContentInputReceipt
from app.models.session_v2 import SessionCommand, SessionToolInvocation
from app.runtime.context_candidates import ContextMessageOrigin
from app.services import memory_service
from app.services.content_input_receipts import record_session_actor_identity
from app.services.explicit_memory_authority import (
    AUTHORITY_SCHEMA, authorized_explicit_entries, memory_ref, source_dependency,
)
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.tools.handlers.memory import _load_scoped_memory, search_memory
from app.tools.runtime import ToolExecutionContext
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_call, group_memory, save_group  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def dependency(kind, identity, version):
    return {"source_kind": kind, "source_id": str(identity), "source_version": version}


@contextmanager
def statement_count(engine):
    statements = []

    def executed(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "after_cursor_execute", executed)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", executed)


@pytest.fixture
async def memory_dag(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.services.agent_context import _strip_primary_heading, agent_identity_content, load_agent_context_resource
    from app.services.agent_directory_sources import observe_directory
    from app.services.config_versioning import canonical_content_hash, save_revision
    from app.services.owner_action_policy import render_owner_action_policy_prompt_section, save_owner_action_policy
    from app.services.skill_seeder import builtin_skill_definitions
    from app.services.skill_source_receipts import _platform_skill_sources
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_semantic_history import _event_payload
    from app.models.chat_transcript_event import ChatTranscriptEvent

    n = native_workspace
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", n.root.parent)
    n.principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), purpose="interactive_session")
    n.context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
        workspace=n.root, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    # Use genuine application Skill bytes, original-owner bootstrap witnesses and
    # real session/tool/actor observations. No source validator is replaced.
    backend = Path(__file__).resolve().parents[2]
    files = sorted((f"skills/{skill['folder_name']}/{file['path']}", file["content"])
        for skill in builtin_skill_definitions(pack_dirs=(backend / "packs",)) if skill.get("is_default")
        for file in skill["files"] if file["path"] == "SKILL.md")[:9]
    assert len(files) == 9
    for path, content in [*files, ("soul.md", "# Soul\n\nHelp the owner complete their work.\n"),
        ("memory/self/self.md", "# Self model\n\nOwner-authored initial state.\n")]:
        target = n.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, n.agent)
        await save_revision(db, tenant_id=n.tenant, entity_type="agent_bootstrap", entity_id=n.agent,
            content={"schema": "hive.agent_bootstrap.v1",
                "identity": agent_identity_content(agent.name, agent.role_description),
                "soul": {"content_sha256": digest((n.root / "soul.md").read_text()),
                    "resource_sha256": digest(_strip_primary_heading((n.root / "soul.md").read_text().strip()).strip())},
                "resident_sources": {"memory/self/self.md": digest((n.root / "memory/self/self.md").read_text())},
                "platform_skill_sources": {path: digest(content) for path, content in files}},
            change_source="user", changed_by_user_id=n.user)
        policy = await save_owner_action_policy(db, agent_id=n.agent, tenant_id=n.tenant,
            actions={"tool.local_read": "full_authority", "tool.local_write": "full_authority",
                "tool.external_effect": "confirm_first"},
            changed_by_user_id=n.user)
        for capability in ("agent.memory.read", "workspace.file.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
        skills = await _platform_skill_sources(db, n.tenant, n.agent)
        shared = [dependency("agent_platform_skill_file", f"{n.agent}\n{path}\n{skills[path][0]}", skills[path][1])
            for path, _content in files]
        shared += [dependency("agent_identity", f"{n.agent}\nidentity",
            canonical_content_hash(agent_identity_content(agent.name, agent.role_description))),
            dependency("owner_action_policy", f"{n.agent}\n{policy.revision_id}",
                digest(render_owner_action_policy_prompt_section(policy))),
            dependency("agent_resident_memory", f"{n.agent}\nmemory/self/self.md",
                digest((n.root / "memory/self/self.md").read_text()))]
    resource = await load_agent_context_resource(agent_id=n.agent, tenant_id=n.tenant, resource_ref="soul")
    assert resource.status == "ok", resource
    shared.append(dependency("agent_context_resource", f"{n.agent}\nsoul", resource.sha256))
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        _content, directory = await observe_directory(db, tenant_id=n.tenant, agent_id=n.agent, runtime_request=n.principal)
        actor, _proof = await record_session_actor_identity(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, user_id=n.user, display_name=None, runtime_task_id=n.run)
        shared += [directory, actor]
        await db.commit()
    raw, invocation = await memory_call(n, app_user_sessionmaker, "read_context_resource", {"ref": "soul"})
    assert "Help the owner" in raw, raw
    async with owner_sessionmaker() as db:
        command = await db.scalar(select(SessionCommand).where(SessionCommand.tenant_id == n.tenant,
            SessionCommand.session_id == n.session, SessionCommand.namespace == "human_input"))
        invocation = await db.get(SessionToolInvocation, invocation.id)
        tool_event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        shared += [dependency("session_command", command.id, command.request_hash),
            dependency("session_tool_invocation", invocation.id, tool_result_source_hash(_event_payload(tool_event)))]
    assert len(shared) == 17
    n.shared = shared
    n.receipts, n.entries = [], []
    # Four retained source receipts and six active memories, 17..21 direct
    # dependencies each; the longest explicit-memory path has three edges.
    parents = [[], [0], [1], [0], [], [0], [1], [2], [0, 3], [0, 1, 2, 3]]
    manifest = []
    async with owner_sessionmaker() as db:
        for index, links in enumerate(parents):
            body = f"Recall fixture release MEMORY_DAG_{index}: retained owner knowledge."
            prepared = prepare_explicit_memory_entry(agent_id=n.agent, data_root=n.root.parent,
                decision=MemoryWriteDecision(original_content=body, content=body, category="project", sensitivity="PL1_public"),
                source_refs=(), target_hint="capabilities", origin="explicit_user_request", entry_id=f"dag-{index}")
            entry = prepared.entry
            entry.path.parent.mkdir(parents=True, exist_ok=True)
            entry.path.write_text(prepared.rendered)
            metadata = {**entry.metadata, "status": "retired" if index < 4 else "active"}
            manifest.append(metadata)
            receipt = ContentInputReceipt(id=uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
                carrier_ref=memory_ref(n.agent, entry.entry_id), revision=1, content_sha256=entry.source_sha256,
                dependencies_json=[*shared, *[source_dependency(n.receipts[parent]) for parent in links]],
                authority_json={"schema": AUTHORITY_SCHEMA, "state": metadata["status"],
                    "tenant_id": str(n.tenant), "agent_id": str(n.agent), "owner_user_id": str(n.user),
                    "audience_kind": "private", "source_session_id": str(n.session), "metadata": entry.metadata})
            db.add(receipt)
            n.receipts.append(receipt)
            n.entries.append(entry)
        await db.commit()
    (n.root / "memory/explicit/manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in manifest))
    n.active = [entry for entry in load_explicit_overlay_entries(n.root.parent, n.agent) if entry.status == "active"]
    assert len(n.active) == 6

    return n


async def resolve(n):
    return await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(n.session),
        runtime_task_id=str(n.run), current_user_id=n.user, return_result=True)


@pytest.mark.parametrize("surface,bound", [("context", 260), ("search", 260), ("load", 150)])
async def test_memory_disclosure_statement_work(memory_dag, app_user_engine, surface, bound):
    n = memory_dag
    with statement_count(app_user_engine) as statements:
        if surface == "context":
            result = await resolve(n)
            content = result.content
            assert "explicit_memory_source_authority" not in result.degraded_components, result
        elif surface == "search":
            content = await search_memory(n.agent, {"query": "Recall fixture release", "scope": "facts"},
                tenant_id=str(n.tenant), execution_context=n.context)
        else:
            content = await _load_scoped_memory(n.agent, {"ids": ["dag-9"]}, n.context)
    print("MEMORY_SQL", surface, len(statements))
    assert "MEMORY_DAG_9" in content, content
    if surface == "search":
        assert all(f"MEMORY_DAG_{i}" in content for i in range(4, 10)), content
    assert len(statements) <= bound, (surface, len(statements), bound)


async def test_prepare_model_request_statement_baseline(memory_dag, app_user_engine, app_user_sessionmaker):
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import prepare_model_request

    n = memory_dag
    context = await resolve(n)
    message = LLMMessage(role="system", content=context.content,
        context_origin=ContextMessageOrigin.capture(kind="memory", role="system", content=context.content,
            sources=context.source_refs, sections=context.sections, unbound=context.source_unbound))
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with statement_count(app_user_engine) as statements:
            request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=2, messages=[message], tools=[],
                provider="synthetic", model="local-reference")
        assert request_id
        print("PREPARE_MODEL_REQUEST_SQL", len(statements))
        await db.rollback()


@pytest.mark.parametrize("surface", ["authorization", "result_recording"])
async def test_one_authorization_walk_validates_each_completed_source_once(memory_dag, monkeypatch, surface):
    from app.services import content_input_receipts
    from app.services.explicit_memory_tool_sources import record_memory_result
    from app.tools.runtime import ToolExecutionRequest

    n, calls = memory_dag, Counter()
    validate = content_input_receipts._validate_input_dependency_batch

    async def observed(db, tenant_id, dependencies, **kwargs):
        await validate(db, tenant_id, dependencies, **kwargs)
        calls.update(tuple(row.values()) for row in dependencies)

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", observed)
    if surface == "authorization":
        issues = []
        allowed = await authorized_explicit_entries(tenant_id=n.tenant, agent_id=n.agent, request=n.principal,
            entries=n.active, issue_observer=issues)
        assert len(allowed) == 6 and not issues, issues
    else:
        await record_memory_result(ToolExecutionRequest("search_memory", {"query": "Recall fixture release"}, n.context),
            content="Authorized fixture memories", entries=list(zip(n.entries[4:], n.receipts[4:])))
    assert max(calls.values()) == 1, calls


@pytest.mark.parametrize("change", ["revoked", "bytes", "skill", "cycle"])
async def test_index_authority_rechecks_current_sources(memory_dag, owner_sessionmaker, monkeypatch, change):
    n = memory_dag

    async def apply_change():
        if change == "bytes":
            n.entries[9].path.write_text(n.entries[9].path.read_text() + "\nChanged after the original owner witness.\n")
        elif change == "skill":
            path = n.shared[0]["source_id"].split("\n")[1]
            (n.root / path).write_text("Changed application Skill bytes after the witness.\n")
        else:
            async with owner_sessionmaker() as db:
                row = await db.get(ContentInputReceipt, n.receipts[9].id)
                if change == "revoked":
                    row.authority_json = {**row.authority_json, "state": "revoked"}
                else:
                    row.dependencies_json = [*row.dependencies_json, source_dependency(row)]
                await db.commit()

    await apply_change()
    result = await resolve(n)
    assert "MEMORY_DAG_9" not in result.content, result.content
    # Revoked entries are deliberately omitted without an availability issue;
    # changed or cyclic sources retain their existing degraded classification.
    assert ("explicit_memory_source_authority" in result.degraded_components) == (change != "revoked"), result
    # A4c in the prompt: entries dropped by the recheck are named by count;
    # a revoked (owner-withdrawn) entry stays silently omitted.
    if change == "revoked":
        assert "记忆暂时无法核实" not in result.content, result.content
    else:
        # bytes/cycle drop the changed entry alone; a changed shared Skill
        # source drops its whole dependent chain (six entries in this fixture).
        expected = 6 if change == "skill" else 1
        assert f"有{expected} 条记忆暂时无法核实，已略过，可继续使用其余内容。" in result.content, result.content


async def test_cyclic_chain_is_not_hidden_by_completed_sibling_walk(memory_dag, owner_sessionmaker):
    n = memory_dag
    async with owner_sessionmaker() as db:
        row = await db.get(ContentInputReceipt, n.receipts[0].id)
        row.dependencies_json = [*row.dependencies_json, source_dependency(n.receipts[7])]
        await db.commit()
    issues = []
    allowed = await authorized_explicit_entries(tenant_id=n.tenant, agent_id=n.agent, request=n.principal,
        entries=n.active, issue_observer=issues)
    assert [entry.entry_id for entry, _receipt in allowed] == ["dag-4"]
    assert {issue["code"] for issue in issues} == {"explicit_memory_source_cycle"}
    assert len(issues) == 5


async def test_final_reader_check_sees_revocation_during_the_walk(memory_dag, owner_sessionmaker, monkeypatch):
    from app.models.user import User
    from app.services import content_input_receipts
    from app.services.content_input_receipts import ContentInputReceiptError

    n, revoked = memory_dag, False
    validate = content_input_receipts._validate_input_dependency_batch

    async def withdraw_after_last_source(db, tenant_id, dependencies, **kwargs):
        nonlocal revoked
        await validate(db, tenant_id, dependencies, **kwargs)
        if dependencies == [n.shared[-1]] and not revoked:
            async with owner_sessionmaker() as writer:
                (await writer.get(User, n.user)).is_active = False
                await writer.commit()
            revoked = True

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", withdraw_after_last_source)
    with pytest.raises(ContentInputReceiptError) as caught:
        await authorized_explicit_entries(tenant_id=n.tenant, agent_id=n.agent, request=n.principal,
            entries=[next(entry for entry in n.active if entry.entry_id == "dag-4")])
    assert revoked
    assert (caught.value.code, caught.value.status_code) == ("content_source_access_denied", 403)


@pytest.mark.parametrize("surface", ["authorization", "result_recording"])
@pytest.mark.parametrize("withdraw", [True, False])
async def test_private_reader_rechecks_each_origin_group_after_the_walk(
    group_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch, surface, withdraw,  # noqa: F811
):
    from types import SimpleNamespace
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.services import content_input_receipts, shared_group_authority
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.explicit_memory_authority import latest_memory_receipt
    from app.services.explicit_memory_tool_sources import record_memory_result
    from app.services.feishu_service import FeishuService
    from app.tools.runtime import ToolExecutionRequest

    g, live_checks = group_memory, Counter()
    n = g.n
    await save_group(g, app_user_sessionmaker)
    result, _ = await group_call(g, app_user_sessionmaker, "save_memory",
        {"content": "Remember the group deployment window SECOND_GROUP_MARKER", "category": "project"})
    assert "Saved memory" in result, result
    async with owner_sessionmaker() as db:
        origin = await db.get(ChatSession, g.session)
        other = await find_or_create_shared_group_session(db, tenant_id=n.tenant, agent_id=n.agent,
            installation_ref=origin.group_installation_ref, chat_id="second-origin-memory-group",
            first_message_title="Another memory origin")
        await db.commit()
    elsewhere = SimpleNamespace(n=n, session=other.id, runs={}, rounds={})
    result, _ = await group_call(elsewhere, app_user_sessionmaker, "save_memory",
        {"content": "Remember the other group's review date OTHER_ORIGIN_MARKER", "category": "project"})
    assert "Saved memory" in result, result
    entries = load_explicit_overlay_entries(n.root.parent, n.agent)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        sources = [(entry, await latest_memory_receipt(db, n.tenant, n.agent, entry.entry_id)) for entry in entries]
    groups = [(e, r) for e, r in sources if r.authority_json["audience_kind"] == "group"]
    groups.sort(key=lambda pair: pair[1].authority_json["source_session_id"] != str(g.session))
    private = [(e, r) for e, r in sources if r.authority_json["audience_kind"] == "private"]
    assert len(groups) == 3 and len(private) == 1
    sources = [*groups, *private]
    principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), purpose="interactive_session")
    arguments = {"query": "release", "scope": "facts"}
    if surface == "result_recording":
        content, invocation = await memory_call(n, app_user_sessionmaker, "search_memory", arguments)
        identity = uuid5(invocation.id, "explicit-memory-tool-result")
        # Reuse the real sealed invocation, removing only this test's first proof
        # so the observed pass must either commit a fresh proof or reject it.
        async with owner_sessionmaker() as db:
            proof = await db.get(InvocationSpan, identity)
            assert proof is not None
            await db.delete(proof)
            await db.commit()
        context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
            workspace=n.root, session_id=str(n.session), runtime_task_id=str(n.run),
            turn_id=f"turn-{n.run.hex}", tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False)
        request = ToolExecutionRequest("search_memory", arguments, context)

    validate = content_input_receipts._validate_input_dependency_batch
    membership = shared_group_authority._require_current_group_member
    injected = False

    async def current_members(_self, _app_id, _app_secret, chat_id, **_kwargs):
        return {"memory-author", "memory-member"} if chat_id == other.group_subject_id else set(g.members)

    async def withdraw_after_first_source(db, tenant_id, dependencies, **kwargs):
        nonlocal injected
        await validate(db, tenant_id, dependencies, **kwargs)
        if dependencies == groups[0][1].dependencies_json and not injected:
            if withdraw:
                g.members.remove("memory-author")
            injected = True

    async def observe_live_membership(db, *, session, **kwargs):
        if content_input_receipts._preflight_inputs(db) is None:
            live_checks[session.id] += 1
        return await membership(db, session=session, **kwargs)

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", withdraw_after_first_source)
    monkeypatch.setattr(shared_group_authority, "_require_current_group_member", observe_live_membership)
    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", current_members)
    if surface == "authorization":
        issues = []
        allowed = await authorized_explicit_entries(tenant_id=n.tenant, agent_id=n.agent, request=principal,
            entries=[e for e, _ in sources], issue_observer=issues)
        expected = [(e, r) for e, r in sources
            if not withdraw or r.authority_json["source_session_id"] != str(g.session)]
        assert [(e.entry_id, r.id) for e, r in allowed] == [(e.entry_id, r.id) for e, r in expected]
        assert issues == ([{"memory_id": e.entry_id, "status": "denied", "code": "group_membership_required"}
            for e, r in groups if r.authority_json["source_session_id"] == str(g.session)] if withdraw else [])
    else:
        if withdraw:
            with pytest.raises(ContentInputReceiptError) as caught:
                await record_memory_result(request, content=content, entries=sources)
            assert (caught.value.code, caught.value.status_code) == ("group_membership_required", 403)
        else:
            await record_memory_result(request, content=content, entries=sources)
        async with owner_sessionmaker() as db:
            proof = await db.get(InvocationSpan, identity)
            if withdraw:
                assert proof is None
            else:
                assert sorted(proof.metadata_json["memory_result"]["sources"], key=lambda ref: ref["source_id"]) == sorted(
                    [source_dependency(r) for _, r in sources], key=lambda ref: ref["source_id"])
    assert injected
    assert live_checks == {g.session: 1, other.id: 1}
