"""Exercise a production-shaped provenance graph on the private memory path."""

import cProfile
import json
import pstats
import time
from collections import Counter
from itertools import cycle, islice
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.memory.explicit_overlay import prepare_explicit_memory_entry
from app.memory.write_gate import MemoryWriteDecision
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.organization_identity import ContentInputReceipt
from app.models.session_v2 import SessionCommand, SessionToolInvocation
from app.packs.catalog_reader import PackCatalogReader
from app.services import agent_context, content_input_receipts, memory_service, skill_source_receipts
from app.services.agent_directory_sources import observe_directory
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.explicit_memory_authority import AUTHORITY_SCHEMA, memory_ref, source_dependency
from app.services.owner_action_policy import render_owner_action_policy_prompt_section, save_owner_action_policy
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.session_model_inputs import tool_result_source_hash
from app.services.session_semantic_history import _event_payload
from app.services.skill_installation import install_active_skill_package
from app.services.skill_seeder import DEFAULT_PACK_SKILL_FOLDERS, builtin_skill_definitions
from tests.integration.test_a2fp1_private_turn_budget_postgres import Probe, app_user_engine  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
from tests.integration.test_memory_source_read_work_postgres import dependency, digest

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _base_sources(n, owner_sessionmaker, app_user_sessionmaker):
    backend = Path(__file__).resolve().parents[2]
    for skill in builtin_skill_definitions(pack_dirs=(backend / "packs",)):
        if skill.get("is_default"):
            install_active_skill_package(workspace=n.root, folder_name=skill["folder_name"],
                files=skill["files"], source="default_registry_skill:fixture", overwrite=True)
    files = skill_source_receipts.original_platform_skill_hashes(n.root)
    soul = "# Soul\n\nHelp the owner complete their work.\n"
    self_path = "memory/self/self.md"
    self_content = "# Self model\n\nOwner-authored initial state.\n"
    (n.root / "soul.md").write_text(soul, encoding="utf-8")
    (n.root / self_path).parent.mkdir(parents=True)
    (n.root / self_path).write_text(self_content, encoding="utf-8")
    resources = {}
    for ref in ("soul", "channels"):
        resource = await agent_context.load_agent_context_resource(agent_id=n.agent, tenant_id=n.tenant, resource_ref=ref)
        assert resource.status == "ok", resource
        resources[ref] = dependency("agent_context_resource", f"{n.agent}\n{ref}", resource.sha256)

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, n.agent)
        identity = agent_context.agent_identity_content(agent.name, agent.role_description)
        bootstrap = await save_revision(db, tenant_id=n.tenant, entity_type="agent_bootstrap", entity_id=n.agent,
            content={"schema": "hive.agent_bootstrap.v1", "identity": identity,
                "soul": {"content_sha256": digest(soul), "resource_sha256": resources["soul"]["source_version"]},
                "resident_sources": {self_path: digest(self_content)}, "platform_skill_sources": files},
            change_source="user", changed_by_user_id=n.user)
        policy = await save_owner_action_policy(db, agent_id=n.agent, tenant_id=n.tenant,
            actions={"tool.local_read": "full_authority", "tool.local_write": "full_authority",
                "tool.external_effect": "confirm_first"}, changed_by_user_id=n.user)
        for capability in ("agent.memory.read", "workspace.file.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()

    principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), purpose="interactive_session")
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        _, directory = await observe_directory(db, tenant_id=n.tenant, agent_id=n.agent, runtime_request=principal)
        actor, _ = await content_input_receipts.record_session_actor_identity(db, tenant_id=n.tenant,
            agent_id=n.agent, session_id=n.session, user_id=n.user, display_name=None, runtime_task_id=n.run)
        await db.commit()

    shared = [dependency("agent_identity", f"{n.agent}\nidentity", canonical_content_hash(identity)),
        dependency("owner_action_policy", f"{n.agent}\n{policy.revision_id}",
                   digest(render_owner_action_policy_prompt_section(policy))), actor, directory, resources["soul"]]
    resident = dependency("agent_resident_memory", f"{n.agent}\n{self_path}", digest(self_content))
    pack, builtin = [], []
    for path, version in sorted(files.items()):
        group = pack if path.split("/")[1] in DEFAULT_PACK_SKILL_FOLDERS else builtin
        group.append(dependency("agent_platform_skill_file", f"{n.agent}\n{path}\n{bootstrap.id}", version))
    return agent, shared, resources["channels"], resident, pack, builtin


async def _tool_and_command_sources(n, owner_sessionmaker, app_user_sessionmaker):
    tools, commands = [], []
    for index in range(9):
        prompt = f"Read the current context for fixture memory {index}."
        raw, invocation = await memory_call(n, app_user_sessionmaker, "read_context_resource", {"ref": "soul"},
                                           command=prompt)
        assert "Help the owner" in raw, raw
        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, invocation.id)
            event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
            command = await db.scalar(select(SessionCommand).where(SessionCommand.tenant_id == n.tenant,
                SessionCommand.session_id == n.session, SessionCommand.namespace == "human_input")
                .order_by(SessionCommand.created_at.desc()).limit(1))
        tools.append(dependency("session_tool_invocation", invocation.id, tool_result_source_hash(_event_payload(event))))
        commands.append(dependency("session_command", command.id, command.request_hash))
    assert len({row["source_id"] for row in commands}) == 9
    return tools, commands


@pytest.fixture
async def mf1_memory_shape(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    n = native_workspace
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", n.root.parent)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", n.root.parent)
    agent, shared, channels, resident, pack, builtin = await _base_sources(n, owner_sessionmaker, app_user_sessionmaker)
    assert len(pack) >= 3
    assert len(builtin) >= 10
    tools, commands = await _tool_and_command_sources(n, owner_sessionmaker, app_user_sessionmaker)
    states = ("retired", "retired", "retired", "retired", "prepared", "reviewing", "active", "active", "active")
    skill_counts = (5, 6, 7, 8, 6, 6, 13, 10, 11)
    parents = ((), (0,), (1,), (), (), (), (0,), (1, 2), (3, 7))
    builtin_paths = cycle(builtin)
    latest, manifest = [], []
    async with owner_sessionmaker() as db:
        for index, state in enumerate(states):
            body = f"MF1 memory {index}: retained notes for the owner."
            prepared = prepare_explicit_memory_entry(agent_id=n.agent, data_root=n.root.parent,
                decision=MemoryWriteDecision(original_content=body, content=body, category="project",
                                             sensitivity="PL1_public"),
                source_refs=(), target_hint="capabilities", origin="explicit_user_request", entry_id=f"mf1-{index}")
            entry = prepared.entry
            entry.path.parent.mkdir(parents=True, exist_ok=True)
            entry.path.write_text(prepared.rendered, encoding="utf-8")
            manifest.append({**entry.metadata, "status": state})
            dependencies = [*pack[:3], *islice(builtin_paths, skill_counts[index] - 3), *shared,
                commands[index], tools[index], tools[(index + 1) % len(tools)],
                *(source_dependency(latest[parent]) for parent in parents[index])]
            if index in {2, 5, 6, 8}:
                dependencies.append(channels)
            if index in {6, 7}:
                dependencies.append(resident)
            authority = {"schema": AUTHORITY_SCHEMA, "tenant_id": str(n.tenant), "agent_id": str(n.agent),
                "owner_user_id": str(n.user), "audience_kind": "private", "source_session_id": str(n.session),
                "metadata": entry.metadata}
            revisions = ("active" if state == "retired" else "prepared", state) if index < 5 else (state,)
            for revision, revision_state in enumerate(revisions, 1):
                receipt = ContentInputReceipt(id=uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
                    carrier_ref=memory_ref(n.agent, entry.entry_id), revision=revision, content_sha256=entry.source_sha256,
                    dependencies_json=dependencies, authority_json={**authority, "state": revision_state})
                db.add(receipt)
            latest.append(receipt)
        for index in range(185):
            path = f"workspace/unrelated-{index}.md"
            body = f"Unrelated tenant artifact {index}."
            (n.root / path).write_text(body, encoding="utf-8")
            content_input_receipts.record_workspace_receipt(db, n.tenant, n.agent, path, digest(body), None)
        await db.commit()
        receipt_kinds = Counter(await db.scalars(select(ContentInputReceipt.carrier_kind)
                                                .where(ContentInputReceipt.tenant_id == n.tenant)))
    (n.root / "memory/explicit/manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in manifest),
                                                         encoding="utf-8")

    assert receipt_kinds == {"workspace_artifact": 185, "explicit_memory": 14}
    assert Counter(receipt.authority_json["state"] for receipt in latest) == {
        "active": 3, "prepared": 1, "reviewing": 1, "retired": 4,
    }
    dependencies = Counter(row["source_kind"] for receipt in latest for row in receipt.dependencies_json)
    assert dependencies == {"agent_platform_skill_file": 72, "session_tool_invocation": 18,
        "agent_context_resource": 13, "agent_identity": 9, "session_command": 9, "owner_action_policy": 9,
        "session_actor_identity": 9, "agent_directory_observation": 9, "explicit_memory": 7,
        "agent_resident_memory": 2}
    assert max(len(receipt.dependencies_json) for receipt in latest) == 24
    expected = {str(receipt.id) for receipt in latest if receipt.authority_json["state"] == "active"}
    return n, agent, expected


async def test_memory_context_discovers_bundled_skills_once_per_walk(mf1_memory_shape, monkeypatch):
    n, agent, expected = mf1_memory_shape

    async def build():
        return await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(n.session),
            runtime_task_id=str(n.run), current_user_id=n.user, verified_agent=agent, return_result=True)

    warm = await build()
    assert {ref["source_id"] for ref in warm.source_refs if ref["source_kind"] == "explicit_memory"} == expected, warm
    assert warm.degraded_components == ("explicit_memory_source_authority",)
    assert "有2 条记忆暂时无法核实" in warm.content

    discoveries = 0
    source_reads = 0
    discover = PackCatalogReader.discover
    platform_sources = skill_source_receipts._platform_skill_sources

    def count_discovery(reader):
        nonlocal discoveries
        discoveries += 1
        return discover(reader)

    async def count_platform_sources(*args, **kwargs):
        nonlocal source_reads
        source_reads += 1
        return await platform_sources(*args, **kwargs)

    monkeypatch.setattr(PackCatalogReader, "discover", count_discovery)
    monkeypatch.setattr(skill_source_receipts, "_platform_skill_sources", count_platform_sources)
    probe = Probe()
    with probe.meter():
        probe.switch("memory_context")
        started = time.perf_counter()
        measured = await build()
        wall_ms = (time.perf_counter() - started) * 1000
    measured_discoveries = discoveries
    metrics = probe.phases["memory_context"].metrics()
    print("MF1_MEMORY_CONTEXT", json.dumps({key: metrics[key] for key in ("statements", "transactions", "thread_cpu_ms")}
          | {"wall_ms": round(wall_ms, 3), "pack_discoveries": measured_discoveries}, sort_keys=True))

    source_reads = 0
    with cProfile.Profile(timer=time.thread_time) as profile:
        profiled = await build()
    samples = {entry.code: entry.callcount for entry in profile.getstats()}
    observed = (skill_source_receipts._bundled_platform_skill_hashes, discover,
        skill_source_receipts.original_platform_skill_hashes, content_input_receipts._validate_source_bytes)
    calls = {fn.__qualname__: samples.get(fn.__code__, 0) for fn in observed}
    calls[platform_sources.__qualname__] = source_reads
    print("MF1_PROFILE_CALLS", json.dumps(calls, sort_keys=True))
    stats = pstats.Stats(profile).strip_dirs()
    stats.sort_stats("cumulative").print_stats(25)
    stats.sort_stats("tottime").print_stats(25)
    stats.sort_stats("cumulative").print_stats(
        "_platform_package_checkpoint|original_platform_skill_hashes|_validate_source_bytes|canonical_content_hash"
    )

    assert measured == profiled == warm
    assert (measured_discoveries, discoveries - measured_discoveries) == (1, 1)
