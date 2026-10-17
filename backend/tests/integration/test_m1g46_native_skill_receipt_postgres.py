"""A normal kernel task must issue an artifact receipt from its actual inputs."""

import asyncio
import json
import platform
import sys

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS process gate; Linux delivery is deferred.")]


async def _run_native_skill_task(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, *, load_skill_name="source-probe",
):
    import app.database as database
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools, web_chat_runtime
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider
    from tests.integration.test_m1g46_skill_installation_postgres import _import_original_skill

    seed = await _seed_run(owner_sessionmaker, tmp_path,
        prompt="Load source-probe, run scripts/probe.py and deliver the generated report to this work.")
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    # The seed creates only an empty fixture directory; initialize it normally.
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.base_url = "http://127.0.0.1:1"
        model.max_input_tokens = 256_000
        db.add(ChannelConfig(agent_id=agent.id, tenant_id=seed["tenant_id"], channel_type="feishu",
            is_configured=True, is_connected=False))
        await AgentManager().initialize_agent_files(db, agent, personality="NATIVE_SOUL_CONTEXT_PRESENT",
            original_author_id=seed["user_id"])
        await db.commit()
    factory = database.async_session
    bindings = []
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("app."):
            continue
        for name, value in list(vars(module).items()):
            if value is factory:
                monkeypatch.setattr(module, name, app_user_sessionmaker)
                bindings.append(module.__name__ + "." + name)
    _skill_id, _publisher, imported = await _import_original_skill(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    assert imported["source_receipt"]["source_status"] == "verified"
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    calls = [("load_skill", {"name": load_skill_name}),
        ("run_skill_tool", {"skill": "source-probe", "script": "scripts/probe.py"})]
    client = ScriptedLLMClient([{"tool_calls": [{"id": f"native-{index}", "type": "function",
        "function": {"name": tool, "arguments": json.dumps(args)}}], "finish_reason": "tool_calls"}
        for index, (tool, args) in enumerate(calls)] + [{"content": "Generated native-report.md", "finish_reason": "stop"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(seed["run_id"]), timeout=90)
    return seed, client, bindings, imported


async def test_normal_kernel_skill_output_has_a_usable_source_receipt(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult

    seed, client, bindings, imported = await _run_native_skill_task(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        results = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])
            .order_by(SessionModelResult.round_id))).all()
        snapshots = [row.model_request_snapshot_json for row in results]
        (tmp_path / "native-model-inputs.json").write_text(json.dumps(snapshots, indent=2, default=str))
        (tmp_path / "native-environment-bindings.json").write_text(json.dumps(bindings, indent=2))
        (tmp_path / "native-run-state.json").write_text(json.dumps({"status": task.status,
            "run_id": str(task.id), "metadata": task.metadata_json}, indent=2, default=str))
        assert len(client.requests) == 3, (task.status, task.metadata_json, snapshots)
        assert (seed["agent_root"] / "workspace/native-report.md").read_text() == "NATIVE_SKILL_REPORT"
        assert any("NATIVE_SKILL_EXECUTED" in str(message) for message in client.requests[-1]["messages"])
        assert snapshots and any(len(snapshot["wire_request"]["tools"] or []) > 2 for snapshot in snapshots)
        actual_inputs = json.dumps([snapshot["wire_request"]["messages"] for snapshot in snapshots])
        assert "NATIVE_SOUL_CONTEXT_PRESENT" in actual_inputs
        assert "Controlled source probe." in actual_inputs
        assert "self/self.md" in actual_inputs
        assert all(entry["status"] == "bound" for snapshot in snapshots for entry in snapshot["input_bindings"]["messages"])
        for index, expected in [(0, {"skills/source-probe/SKILL.md"}), (1, {"skills/source-probe/SKILL.md"}),
            (2, {"skills/source-probe/SKILL.md", "skills/source-probe/scripts/probe.py"})]:
            selected = [ref for ref in snapshots[index]["input_bindings"]["dependencies"] if ref["source_kind"] == "agent_skill_file"]
            assert {ref["source_id"].split("\n")[1] for ref in selected} == expected
            assert all(ref["source_id"].split("\n")[2] == imported["source_receipt"]["ref"].split(":", 1)[1] for ref in selected)
        for snapshot in snapshots:
            bundled = [ref for ref in snapshot["input_bindings"]["dependencies"] if ref["source_kind"] == "agent_platform_skill_file"]
            assert len(bundled) == 1 and bundled[0]["source_id"].split("\n")[1] == "skills/mcp-installer/SKILL.md"
            assert any(ref["source_kind"] == "agent_resident_memory" and ref["source_id"] ==
                f"{seed['agent_id']}\nmemory/self/self.md" for ref in snapshot["input_bindings"]["dependencies"])
            context_sources = {ref["source_id"] for ref in snapshot["input_bindings"]["dependencies"]
                if ref["source_kind"] == "agent_context_resource"}
            assert f"{seed['agent_id']}\nsoul" in context_sources
            assert f"{seed['agent_id']}\nchannels" in context_sources
        from app.services.content_input_receipts import validate_runtime_input_dependencies
        from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

        source_reader = ToolExecutionRequest(tool_name="run_skill_tool", arguments={}, context=ToolExecutionContext(
            agent_id=seed["agent_id"], user_id=seed["user_id"], tenant_id=str(seed["tenant_id"]),
            workspace=seed["agent_root"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"])))
        for snapshot in snapshots:
            await validate_runtime_input_dependencies(db, source_reader, [ref for ref in snapshot["input_bindings"]["dependencies"]
                if ref["source_kind"] in {"agent_resident_memory", "agent_skill_file", "agent_platform_skill_file"} or
                (ref["source_kind"] == "agent_context_resource" and ref["source_id"].endswith("\nsoul"))])
        for snapshot in snapshots:
            for index, message in enumerate(snapshot["wire_request"]["messages"]):
                if message["role"] == "assistant":
                    assert snapshot["input_bindings"]["messages"][index]["status"] == "bound", snapshot["input_bindings"]
        receipt = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == seed["tenant_id"],
            ContentInputReceipt.carrier_kind == "workspace_artifact",
            ContentInputReceipt.carrier_ref == f"{seed['agent_id']}\nworkspace/native-report.md"))
        from app.models.invocation_span import InvocationSpan

        output_spans = (await db.scalars(select(InvocationSpan).where(InvocationSpan.agent_id == seed["agent_id"],
            InvocationSpan.span_type == "workspace_output"))).all()
        (tmp_path / "native-execution-receipts.json").write_text(json.dumps([
            span.metadata_json for span in output_spans], indent=2, default=str))
        assert receipt is not None, [snapshot.get("input_bindings") for snapshot in snapshots]
        from app.models.chat_artifact import ChatArtifact

        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == seed["agent_id"],
            ChatArtifact.runtime_task_id == seed["run_id"], ChatArtifact.path == "workspace/native-report.md"))
        assert artifact is not None and artifact.session_id == seed["session_id"]
        assert artifact.snapshot_json["content_input_receipt_id"] == str(receipt.id)
        assert task.metadata_json["artifact_ids"] == [str(artifact.id)]
    from app.database import pin_rls_tenant_context
    from app.models.user import User
    from app.core.resource_authority import authorize_resource_action
    from app.services import agent_tools
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal

    service = agent_tools._get_tool_runtime_service()
    context = await service.runtime_resolver.resolve(agent_id=seed["agent_id"], user_id=seed["user_id"],
        session_id=str(seed["session_id"]), runtime_task_id=seed["run_id"].hex)
    principal = _personal_kb_runtime_principal(ToolExecutionRequest(tool_name="read_file", arguments={}, context=context))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        await authorize_resource_action(db, await db.get(User, seed["user_id"]), agent_id=seed["agent_id"],
            resource_kind="chat_artifact", resource_id=artifact.id, action="read", owner_user_id=artifact.owner_user_id,
            root_session_id=artifact.root_session_id, authority_state=artifact.authority_state, runtime_principal=principal)
    read = await service.execute("read_file", {"path": "workspace/native-report.md"}, agent_id=seed["agent_id"],
        user_id=seed["user_id"], session_id=str(seed["session_id"]), runtime_task_id=seed["run_id"].hex,
        emit_runtime_hooks=False)
    assert "NATIVE_SKILL_REPORT" in str(read), str(read)


async def test_native_frozen_identity_and_policy_sources_revalidate_current_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import uuid
    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.session_v2 import SessionModelResult
    from app.services import agent_tools
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
    from app.services.session_model_inputs import load_context_message_origin
    from app.tools.runtime import ToolExecutionRequest

    seed, _client, _bindings, _imported = await _run_native_skill_task(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    context = await agent_tools._get_tool_runtime_service().runtime_resolver.resolve(agent_id=seed["agent_id"],
        user_id=seed["user_id"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]))
    request = ToolExecutionRequest(tool_name="read_file", arguments={}, context=context)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        rows = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).all()
        assert len(rows) == 3
        inventory = []
        for row in rows:
            bindings = row.model_request_snapshot_json["input_bindings"]
            sources = [ref for ref in bindings["dependencies"] if ref["source_kind"] in {"agent_identity", "owner_action_policy"}]
            assert len(sources) == 2 and bindings["dependency_coverage"]["complete"] is True
            await validate_runtime_input_dependencies(db, request, sources)
            frozen_sources = [ref for ref in bindings["dependencies"] if ref["source_kind"] in {
                "agent_identity", "owner_action_policy", "agent_context_resource"}]
            await validate_runtime_input_dependencies(db, request, frozen_sources)
            origin = await load_context_message_origin(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                source_id=uuid.UUID(bindings["messages"][0]["source_ref"].split(":", 1)[1]))
            assert origin["dependency_coverage"] == {"complete": True, "unbound": []}
            dynamic = await load_context_message_origin(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                source_id=uuid.UUID(bindings["messages"][-1]["source_ref"].split(":", 1)[1]))
            unknowns = set(dynamic["dependency_coverage"]["unbound"])
            assert not unknowns.intersection({"dynamic_section_sources_unbound:plan_mode_guidance",
                "dynamic_section_sources_unbound:task_profile", "dynamic_section_inventory_incomplete"})
            assert unknowns == set()
            assert not unknowns.intersection({"dynamic_section_sources_unbound:runtime_metadata_context",
                "dynamic_section_sources_unbound:environment", "runtime_actor_identity_unavailable"})
            actors = [ref for ref in dynamic["dependencies"] if ref["source_kind"] == "session_actor_identity"]
            assert len(actors) == 1
            await validate_runtime_input_dependencies(db, request, actors)
            inventory.append({"round_id": str(row.round_id), "frozen": origin["dependency_coverage"],
                "dynamic": dynamic["dependency_coverage"], "sections": [
                    {key: section.get(key) for key in ("name", "selected", "kind")} for section in dynamic["sections"]]})
        (tmp_path / "native-context-source-ledger.json").write_text(json.dumps(inventory, indent=2))
        identity = next(ref for ref in sources if ref["source_kind"] == "agent_identity")
        policy = next(ref for ref in sources if ref["source_kind"] == "owner_action_policy")
        roster = next(ref for ref in frozen_sources if ref["source_id"].endswith("\na2a-collaborators"))
        with pytest.raises(ContentInputReceiptError) as nonempty:
            await validate_runtime_input_dependencies(db, request, [{**roster, "source_version": "f" * 64}])
        assert nonempty.value.status_code == 503
        async with owner_sessionmaker() as writer:
            agent = await writer.get(Agent, seed["agent_id"])
            original_name = agent.name
            agent.name = "Changed after the observed turn"
            await writer.commit()
        with pytest.raises(ContentInputReceiptError) as denial:
            await validate_runtime_input_dependencies(db, request, [identity])
        assert denial.value.status_code == 403
        async with owner_sessionmaker() as writer:
            (await writer.get(Agent, seed["agent_id"])).name = original_name
            await writer.commit()
        for source in (identity, policy):
            with pytest.raises(ContentInputReceiptError):
                await validate_runtime_input_dependencies(db, request, [{**source, "source_version": "f" * 64}])
        await validate_runtime_input_dependencies(db, request, sources)
    legacy = await _seed_run(owner_sessionmaker, tmp_path / "legacy")
    legacy_context = await agent_tools._get_tool_runtime_service().runtime_resolver.resolve(agent_id=legacy["agent_id"],
        user_id=legacy["user_id"], session_id=str(legacy["session_id"]), runtime_task_id=str(legacy["run_id"]))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, legacy["tenant_id"])
        agent = await db.get(Agent, legacy["agent_id"])
        source = {"source_kind": "agent_identity", "source_id": f"{agent.id}\nidentity",
            "source_version": canonical_content_hash({"name": agent.name, "role_description": agent.role_description or ""})}
        with pytest.raises(ContentInputReceiptError) as missing:
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                context=legacy_context), [source])
        assert missing.value.status_code == 503


async def test_native_actor_observation_rejects_rehash_and_current_owner_loss(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import uuid
    from app.database import pin_rls_tenant_context
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult
    from app.models.user import User
    from app.services import agent_tools
    from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
    from app.services.session_model_inputs import _hash
    from app.tools.runtime import ToolExecutionRequest

    seed, _client, _bindings, _imported = await _run_native_skill_task(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    context = await agent_tools._get_tool_runtime_service().runtime_resolver.resolve(agent_id=seed["agent_id"],
        user_id=seed["user_id"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]))
    request = ToolExecutionRequest(tool_name="read_file", arguments={}, context=context)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
        refs = [ref for ref in model.model_request_snapshot_json["input_bindings"]["dependencies"]
            if ref["source_kind"] == "session_actor_identity"]
        assert len(refs) == 1
        ref = refs[0]
        span_id = uuid.UUID(ref["source_id"])
        span = await db.get(InvocationSpan, span_id)
        original = dict(span.metadata_json)
        from app.services.timezone_utils import get_agent_timezone

        assert original["session_actor_identity"]["timezone"] == await get_agent_timezone(seed["agent_id"])
        async with owner_sessionmaker() as writer:
            actor = await writer.get(User, seed["user_id"])
            old_name = actor.display_name
            actor.display_name = "Renamed after the observed turn"
            await writer.commit()
        await validate_runtime_input_dependencies(db, request, refs)
        forged = {**original["session_actor_identity"], "display_name": "REHASHED_ACTOR_CLAIM"}
        async with owner_sessionmaker() as writer:
            (await writer.get(InvocationSpan, span_id)).metadata_json = {"session_actor_identity": forged}
            await writer.commit()
        with pytest.raises(ContentInputReceiptError):
            await validate_runtime_input_dependencies(db, request, [{**ref, "source_version": _hash(forged)}])
        async with owner_sessionmaker() as writer:
            (await writer.get(InvocationSpan, span_id)).metadata_json = original
            (await writer.get(User, seed["user_id"])).display_name = old_name
            peer = await writer.scalar(select(User.id).where(User.tenant_id == seed["tenant_id"], User.id != seed["user_id"]))
            assert peer is not None
            (await writer.get(ChatSession, seed["session_id"])).user_id = peer
            await writer.commit()
        with pytest.raises(ContentInputReceiptError) as denied:
            await validate_runtime_input_dependencies(db, request, refs)
        assert denied.value.status_code == 403
        async with owner_sessionmaker() as writer:
            (await writer.get(ChatSession, seed["session_id"])).user_id = seed["user_id"]
            await writer.commit()
        await validate_runtime_input_dependencies(db, request, refs)


async def test_native_context_binds_actual_explicit_memory_index(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import hashlib
    from pathlib import Path
    from app.config import get_settings
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult
    from app.services.agent_manager import AgentManager
    from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from tests.memory.test_read_side_two_planes import _write_overlay_entry

    initialize = AgentManager.initialize_agent_files

    async def seed_overlay(manager, db, agent, **kwargs):
        await initialize(manager, db, agent, **kwargs)
        root = Path(get_settings().AGENT_DATA_DIR)
        _write_overlay_entry(root, agent.id, entry_id="native-index")

    monkeypatch.setattr(AgentManager, "initialize_agent_files", seed_overlay)
    seed, client, _bindings, _imported = await _run_native_skill_task(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    paths = ["memory/explicit/manifest.jsonl", "memory/explicit/entries/native-index.md"]
    expected = {f"{seed['agent_id']}\n{path}": hashlib.sha256((seed["agent_root"] / path).read_bytes()).hexdigest() for path in paths}
    assert "周报永远用中文" in json.dumps(client.requests, ensure_ascii=False)
    request = ToolExecutionRequest(tool_name="run_skill_tool", arguments={}, context=ToolExecutionContext(
        agent_id=seed["agent_id"], user_id=seed["user_id"], tenant_id=str(seed["tenant_id"]),
        workspace=seed["agent_root"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"])))
    async with app_user_sessionmaker() as db:
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, seed["tenant_id"])
        rows = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).all()
        assert len(rows) == 3
        output = await db.scalar(select(InvocationSpan).where(InvocationSpan.runtime_task_id == seed["run_id"],
            InvocationSpan.span_type == "workspace_output"))
        assert output.metadata_json["native_content_receipts"] == {"status": "unavailable",
            "reason_code": "model_input_inventory_incomplete", "receipts": {}}
        from app.models.chat_artifact import ChatArtifact

        assert await db.scalar(select(ChatArtifact.id).where(ChatArtifact.agent_id == seed["agent_id"])) is None
        for row in rows:
            bindings = row.model_request_snapshot_json["input_bindings"]
            refs = [ref for ref in bindings["dependencies"] if ref["source_kind"] == "agent_memory_file"]
            assert {ref["source_id"]: ref["source_version"] for ref in refs} == expected
            assert bindings["dependency_coverage"]["complete"] is False
            with pytest.raises(ContentInputReceiptError, match="cannot yet be revalidated"):
                await validate_runtime_input_dependencies(db, request, refs)


@pytest.mark.parametrize("source_case", ["metadata_recovery", "source_revocation"])
async def test_scoped_native_writer_binds_complete_control_input_once(native_workspace, app_user_sessionmaker, owner_sessionmaker, monkeypatch, source_case):  # noqa: F811
    import uuid
    from dataclasses import replace
    from app.database import pin_rls_tenant_context
    from app.kernel.engine import _context_message
    from app.models.invocation_span import InvocationSpan
    from app.models.chat_artifact import ChatArtifact
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionCommand, SessionModelResult
    from app.services.content_input_receipts import record_native_workspace_receipts
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write

    n = native_workspace
    n.path = "workspace/native-control.txt"

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation, arguments, turn = await _prepare_write(db, n, messages=[_context_message(role="system",
            content="Use the authorized tools.", kind="platform_guidance", unbound=()), *await _admitted_write_messages(db, n)])
        trace = {}
        raw = await n.service.execute("write_file", arguments, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
        assert (n.root / n.path).read_text() == arguments["content"], str(raw)
        written = (n.root / n.path).stat()
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            data={"name": "write_file", "args": arguments, "status": "done", "result": str(raw),
                "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
                "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
        output = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "workspace-output-evidence"))
        status = output.metadata_json["native_content_receipts"]
        assert status["status"] == "captured", status
        receipt_id = uuid.UUID(status["receipts"][n.path])
        receipt = await db.get(ContentInputReceipt, receipt_id)
        assert receipt.dependencies_json and {ref["source_kind"] for ref in receipt.dependencies_json} == {"session_command"}
        response = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": n.path})
        assert response.status_code == 200 and response.json()["content"] == arguments["content"], response.text
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))
        assert artifact is not None
        response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/content")
        assert response.status_code == 200 and arguments["content"] in response.text, response.text
        assert artifact.snapshot_json["content_input_receipt_id"] == str(receipt_id)
        async with owner_sessionmaker() as writer:
            (await writer.get(ChatArtifact, artifact.id)).snapshot_json = {
                key: value for key, value in artifact.snapshot_json.items() if key != "content_input_receipt_id"}
            await writer.commit()
        unavailable = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/content")
        assert unavailable.status_code == 409 and arguments["content"] not in unavailable.text
        async with owner_sessionmaker() as writer:
            (await writer.get(ChatArtifact, artifact.id)).snapshot_json = dict(artifact.snapshot_json)
            await writer.commit()
        if source_case == "source_revocation":
            import hashlib
            from datetime import datetime, timezone
            from fastapi import HTTPException
            from app.core.resource_authority import authorize_resource_action
            from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
            from app.models.user import User
            from app.tools.handlers.knowledge import _personal_kb_runtime_principal
            from app.tools.runtime import ToolExecutionRequest

            source = n.root.parent / "revocation-source.md"
            source.write_text("PRIVATE_REVOCABLE_CONTROL_SOURCE")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=n.tenant, scope_type="person", scope_id=n.foreign,
                owner_user_id=n.foreign, source_kind="text", source_sha256=digest, title="Revocable source", status="ready",
                canonical_md_path=source.name, canonical_md_sha256=digest)
            grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",
                resource_id=document.id, document_id=document.id, grantee_type="user", grantee_id=n.user, permission="read")
            # Known-source consumer fixture; the normal Agent path never receives prefilled inputs.
            async with owner_sessionmaker() as writer:
                writer.add(document)
                await writer.flush()
                writer.add(grant)
                retained = await writer.get(ContentInputReceipt, receipt_id)
                retained.dependencies_json = [*retained.dependencies_json, {"source_kind": "knowledge_document",
                    "source_id": str(document.id), "source_version": digest}]
                await writer.commit()
            context = await n.service.runtime_resolver.resolve(agent_id=n.agent, user_id=n.user,
                session_id=str(n.session), runtime_task_id=str(n.run))
            principal = _personal_kb_runtime_principal(ToolExecutionRequest(tool_name="read_file", arguments={}, context=context))
            reader = await db.get(User, n.user)
            from sqlalchemy import text
            from app.database import enter_rls_bypass
            from app.services import content_input_receipts as inputs

            observed_tenants = []
            validate = inputs._validate_input_dependencies

            async def observe_source_scope(source_db, tenant_id, dependencies, **kwargs):
                observed_tenants.append(await source_db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")))
                await validate(source_db, tenant_id, dependencies, **kwargs)

            monkeypatch.setattr(inputs, "_validate_input_dependencies", observe_source_scope)
            async with enter_rls_bypass(db, reason="fixture artifact consumer must attenuate ambient bypass"):
                with pytest.raises(HTTPException) as denial:
                    await authorize_resource_action(db, reader, agent_id=n.agent, resource_kind="chat_artifact",
                        resource_id=artifact.id, action="read", owner_user_id=artifact.owner_user_id,
                        root_session_id=artifact.root_session_id, authority_state=artifact.authority_state,
                        runtime_principal=principal)
            assert denial.value.status_code == 403
            assert observed_tenants and set(observed_tenants) == {str(n.tenant)}
            url = f"/agents/{n.agent}/files/artifacts/{artifact.id}"
            for endpoint in ("/content", "/download"):
                assert (await n.client.get(url + endpoint)).status_code == 200
            async with owner_sessionmaker() as writer:
                (await writer.get(KnowledgeGrant, grant.id)).revoked_at = datetime.now(timezone.utc)
                await writer.commit()
            assert (await n.client.get(f"/agents/{n.agent}/files/content", params={"path": n.path})).status_code == 403
            for endpoint in ("/content", "/download"):
                response = await n.client.get(url + endpoint)
                assert response.status_code == 403 and arguments["content"] not in response.text, response.text
            from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

            for rebind in (False, True):
                errors = []
                parts = await create_chat_artifacts_for_message(db=db, agent_id=n.agent, tenant_id=n.tenant,
                    session_id=n.session, message_id=artifact.message_id, runtime_task_id=n.run,
                    paths=[n.path], workspace_root=n.root, tool_invocation_id=invocation.id,
                    rebind_existing_to_message=rebind, publication_errors=errors)
                assert not parts and errors[0]["status"] == "denied", (parts, errors)
            return
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == invocation.provider_request_id))
        context = await n.service.runtime_resolver.resolve(agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run))
        crossed = await record_native_workspace_receipts(db, context=replace(context, session_id=str(uuid.uuid4())),
            invocation=invocation, model=model, arguments=arguments,
            content_hash=output.metadata_json["workspace_execution_evidence"]["tool_result_hash"])
        assert crossed == {"status": "unavailable", "reason_code": "workspace_execution_scope_mismatch", "receipts": {}}
        assert await record_native_workspace_receipts(db, context=context, invocation=invocation, model=model,
            arguments=arguments, content_hash=output.metadata_json["workspace_execution_evidence"]["tool_result_hash"]) == status
        assert (await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.carrier_ref == f"{n.agent}\n{n.path}"))).all() == [receipt]
        command_id = uuid.UUID(receipt.dependencies_json[0]["source_id"])
        db.expunge(receipt)
        async with owner_sessionmaker() as writer:
            command = await writer.get(SessionCommand, command_id)
            original_target_hash = command.target_hash
            command.target_hash = "0" * 64
            await writer.delete(await writer.get(ContentInputReceipt, receipt_id))
            await writer.commit()
        denied = await record_native_workspace_receipts(db, context=context, invocation=invocation, model=model,
            arguments=arguments, content_hash=output.metadata_json["workspace_execution_evidence"]["tool_result_hash"])
        assert denied["receipts"] == {} and denied["failures"][n.path]["reason_code"] == "content_source_changed"
        assert await db.get(ContentInputReceipt, receipt_id, populate_existing=True) is None
        assert (n.root / n.path).read_text() == arguments["content"]
        assert (await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/content")).status_code == 409
        async with owner_sessionmaker() as writer:
            (await writer.get(SessionCommand, command_id)).target_hash = original_target_hash
            await writer.commit()
        recovered = await record_native_workspace_receipts(db, context=context, invocation=invocation, model=model,
            arguments=arguments, content_hash=output.metadata_json["workspace_execution_evidence"]["tool_result_hash"])
        assert recovered == status
        await db.commit()
        response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/content")
        assert response.status_code == 200 and arguments["content"] in response.text, response.text
        current = (n.root / n.path).stat()
        assert (current.st_ino, current.st_mtime_ns, current.st_size) == (written.st_ino, written.st_mtime_ns, written.st_size)


@pytest.mark.parametrize("load_skill_name", ["source-probe", "skills/mcp-installer/SKILL.md"])
async def test_actual_skill_input_capture_rejects_pointer_replay_and_rehashed_records(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, load_skill_name,
):
    from copy import deepcopy
    import uuid
    from app.database import pin_rls_tenant_context
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.content_input_receipts import ContentInputReceiptError, load_skill_input_evidence
    from app.services.session_semantic_history import _event_payload
    from app.services.session_tool_runtime import _sha256

    seed, _client, _bindings, _imported = await _run_native_skill_task(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, load_skill_name=load_skill_name)
    assert (seed["agent_root"] / "workspace/native-report.md").read_text() == "NATIVE_SKILL_REPORT"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        invocations = (await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])
            .order_by(SessionToolInvocation.tool_name))).all()
        assert [row.tool_name for row in invocations] == ["load_skill", "run_skill_tool"]
        selected = invocations[0]
        event = await db.get(ChatTranscriptEvent, selected.result_event_id)
        payload = _event_payload(event)
        pointer = payload["metadata"]["skill_input_receipt"]
        assert pointer["status"] == "verified" and pointer["unbound"] == [] and len(pointer["sources"]) == 1
        assert pointer["sources"][0]["source_kind"] == ("agent_platform_skill_file" if load_skill_name.startswith("skills/") else "agent_skill_file")
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == selected.provider_request_id))
        args = selected.effective_arguments_json or selected.provider_arguments_json
        capture = {"status": "captured", "receipt_ref": pointer["evidence_ref"]}

        async def load(**changes):
            kwargs = dict(agent_id=seed["agent_id"], invocation=selected, model_result=model, arguments=args,
                content_hash=payload["content_hash"], capture=capture)
            kwargs.update(changes)
            return await load_skill_input_evidence(db, **kwargs)

        assert await load() == pointer
        with pytest.raises(ContentInputReceiptError):
            await load(invocation=invocations[1])
        with pytest.raises(ContentInputReceiptError):
            await load(capture={"status": "captured", "receipt_ref": f"skill-input:{uuid.uuid4()}"})
        evidence_id = uuid.UUID(pointer["evidence_ref"].split(":", 1)[1])
        retained = await db.get(InvocationSpan, evidence_id)
        proof = deepcopy(retained.metadata_json["skill_input_evidence"])
        proof["sources"][0]["source_version"] = "0" * 64
        proof["source_hash"] = _sha256({key: value for key, value in proof.items() if key != "source_hash"})
        async with owner_sessionmaker() as writer:
            (await writer.get(InvocationSpan, evidence_id)).metadata_json = {"skill_input_evidence": proof}
            await writer.commit()
        with pytest.raises(ContentInputReceiptError):
            await load()
        from app.api import files, skills
        from app.models.user import User
        from app.services.content_input_receipts import validate_browser_input_dependencies
        from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot

        path = "skills/mcp-installer/SKILL.md"
        raw = (seed["agent_root"] / path).read_bytes()
        import hashlib

        capture = SkillInputCapture(seed["agent_id"])
        await load_skill_input_snapshot(capture, seed["tenant_id"])
        capture.observe_file(path, hashlib.sha256(raw).hexdigest())
        refs = capture.snapshot()["sources"]
        owner = await db.get(User, seed["user_id"])
        await validate_browser_input_dependencies(db, seed["tenant_id"], owner, refs)
        publisher = await db.scalar(select(User).where(User.tenant_id == seed["tenant_id"], User.role == "platform_admin"))
        created = await skills.create_skill(skills.SkillCreateIn(name="Reinstalled bundle", folder_name="mcp-installer",
            files=[skills.SkillFileIn(path="SKILL.md", content=raw.decode())]), current_user=publisher)
        await files.import_skill_to_agent(seed["agent_id"], files.ImportSkillBody(skill_id=created["id"]), current_user=owner, db=db)
        await db.commit()
        with pytest.raises(ContentInputReceiptError):
            await validate_browser_input_dependencies(db, seed["tenant_id"], owner, refs)
        await skills.delete_skill(created["id"], current_user=publisher)
        capture = SkillInputCapture(seed["agent_id"])
        await load_skill_input_snapshot(capture, seed["tenant_id"])
        capture.observe_file(path, hashlib.sha256(raw).hexdigest())
        assert capture.snapshot()["unbound"] and all(ref["source_kind"] != "agent_platform_skill_file" for ref in capture.snapshot()["sources"])
