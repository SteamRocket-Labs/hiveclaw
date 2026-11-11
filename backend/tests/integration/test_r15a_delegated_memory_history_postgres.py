"""A delegated instruction's memory reader also covers its own derived history."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, shared_memory  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def delegated_rounds(s, app_user_sessionmaker, owner_sessionmaker, monkeypatch, *, change="active"):
    from app.agents import orchestrator
    from app.config import get_settings
    from app.core.execution_context import (
        clear_execution_identity,
        get_execution_identity,
        set_delegated_user_identity,
        set_execution_identity,
    )
    from app.kernel.engine import _context_message
    from app.services import agent_context, agent_manager, session_model_round
    from app.services.agent_manager import AgentManager
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider

    n = s.n
    child_id, model_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            LLMModel(
                id=model_id,
                tenant_id=n.tenant,
                provider="openai",
                model="fake-4.1",
                api_key_encrypted=get_secrets_provider().encrypt("synthetic-local-provider-only"),
                base_url="http://127.0.0.1:1",
                max_input_tokens=256_000,
                label="Local reference",
                enabled=True,
            )
        )
        db.add(
            Agent(
                id=child_id,
                tenant_id=n.tenant,
                name="Memory colleague",
                creator_id=n.user,
                owner_user_id=n.user,
                primary_model_id=model_id,
            )
        )
        db.add(
            CapabilityPolicy(tenant_id=n.tenant, capability="agent.message.send", allowed=True, requires_approval=False)
        )
        await db.flush()
        parent = await db.get(Agent, n.agent)
        parent.owner_user_id, parent.primary_model_id = n.user, model_id
        for agent in (parent, await db.get(Agent, child_id)):
            await AgentManager().initialize_agent_files(
                db, agent, personality="Local memory reference", original_author_id=n.user
            )
        await db.commit()
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", n.root.parent)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", n.root.parent)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        receipt = await latest_memory_receipt(db, n.tenant, n.agent, s.entry_id)
        assert receipt.authority_json["audience_kind"] == "private"
        assert receipt.authority_json["state"] == "active"
        source = source_dependency(receipt)
    if change == "unrelated_source":
        saved, _ = await memory_call(
            n,
            app_user_sessionmaker,
            "save_memory",
            {
                "category": "project",
                "content": "Remember UNSHARED_PRIVATE_MARKER separately.",
            },
        )
        assert "Saved memory" in saved, saved
        entry = json.loads((n.root / "memory/explicit/manifest.jsonl").read_text().splitlines()[-1])
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            unrelated = source_dependency(await latest_memory_receipt(db, n.tenant, n.agent, entry["id"]))

    prepare = session_model_round.prepare_model_request
    observations = []

    async def observe_prepare(db, **kwargs):
        if kwargs["agent_id"] == n.agent:
            kwargs["messages"].append(
                _context_message(
                    role="system", content="ORCHID_PRIVATE_MARKER", kind="memory_context", sources=[source], unbound=()
                )
            )
        elif kwargs["agent_id"] == child_id and kwargs["round_index"] == 2:
            if change == "no_instruction":
                keep = [
                    index
                    for index, message in enumerate(kwargs["messages"])
                    if getattr(getattr(message, "context_origin", None), "kind", None) != "delegated_instruction"
                ]
                kwargs["messages"][:] = [kwargs["messages"][index] for index in keep]
                wire = kwargs["wire_request"]["messages"]
                wire[:] = [wire[index] for index in keep]
            elif change == "revoked":
                from app.models.organization_identity import ContentInputReceipt

                async with owner_sessionmaker() as owner:
                    current = await owner.get(ContentInputReceipt, receipt.id)
                    current.authority_json = {**current.authority_json, "state": "revoked"}
                    await owner.commit()
            elif change == "bytes":
                path = n.root / f"memory/explicit/entries/{s.entry_id}.md"
                path.write_text(path.read_text() + "\nChanged after the first colleague round.\n")
            elif change == "unrelated_source":
                from app.services.session_model_inputs import message_snapshot

                message = _context_message(
                    role="system",
                    content="UNSHARED_PRIVATE_MARKER",
                    kind="memory_context",
                    sources=[unrelated],
                    unbound=(),
                )
                kwargs["messages"].append(message)
                kwargs["wire_request"]["messages"].append(message_snapshot(message))
        instruction_indexes = [
            index
            for index, message in enumerate(kwargs["messages"])
            if getattr(getattr(message, "context_origin", None), "kind", None) == "delegated_instruction"
        ]
        request_id = await prepare(db, **kwargs)
        if kwargs["agent_id"] == child_id:
            model = await db.scalar(
                select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id)
            )
            observations.append({**model.model_request_snapshot_json, "instruction_indexes": instruction_indexes})
        return request_id

    monkeypatch.setattr(session_model_round, "prepare_model_request", observe_prepare)
    previous_identity = get_execution_identity()
    set_delegated_user_identity(n.user, "Memory owner", "web")
    try:
        result, _ = await memory_call(
            n,
            app_user_sessionmaker,
            "delegate_to_agent",
            {
                "agent_name": "Memory colleague",
                "target_agent_id": str(child_id),
                "message": "List the workspace once, then summarize the files you found.",
            },
        )
    finally:
        if previous_identity is None:
            clear_execution_identity()
        else:
            set_execution_identity(previous_identity)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        child = await db.scalar(
            select(RuntimeTask).where(
                RuntimeTask.tenant_id == n.tenant,
                RuntimeTask.child_agent_id == child_id,
                RuntimeTask.task_type == "delegation",
            )
        )
        assert child is not None, result
        child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "memory-worker", 1, 1
        child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
        run_id, session_id = child.id, child.child_session_id
    client = ScriptedLLMClient(
        [
            {
                "content": "I will list this workspace once.",
                "tool_calls": [
                    {
                        "id": "list-once",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": json.dumps({"path": "workspace"})},
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "I have the earlier listing and can continue.", "finish_reason": "stop"},
        ]
    )
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=run_id, worker_id="memory-worker", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(run_id.hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(token)
    assert len(client.requests) == len(observations) == 2, (client.requests, observations)
    assert len(observations[0]["instruction_indexes"]) == 1
    instruction = observations[0]["input_bindings"]["messages"][observations[0]["instruction_indexes"][0]]
    assert source in instruction["dependencies"] and instruction["status"] == "bound", instruction
    return child_id, session_id, run_id, source, client, observations


async def test_round_two_sees_its_own_assistant_and_tool_history(
    shared_memory,  # noqa: F811
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
):
    _, _, run, source, client, snapshots = await delegated_rounds(
        shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch
    )
    snapshot = snapshots[1]
    entries = snapshot["input_bindings"]["messages"]
    messages = snapshot["wire_request"]["messages"]
    own = [
        (entry, messages[entry["message_index"]])
        for entry in entries
        if entry.get("source_ref", "").startswith(("session-model-result:", "session-tool-result:"))
    ]
    assert {message["role"] for _, message in own} == {"assistant", "tool"}, own
    assert all(
        entry["status"] == "bound" and entry["code"] == "canonical_origin_matched" and source in entry["dependencies"]
        for entry, _ in own
    ), own
    assert any(m.get("content") == "I will list this workspace once." for m in client.requests[1]["messages"])
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_semantic_history import _event_payload

    async with tenant_scoped_session(shared_memory.n.tenant, session_factory=app_user_sessionmaker) as db:
        invocation = await db.scalar(
            select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == run, SessionToolInvocation.provider_tool_use_id == "list-once"
            )
        )
        assert invocation.effect_state == "effect_committed" and invocation.result_event_id is not None
        result = _event_payload(await db.get(ChatTranscriptEvent, invocation.result_event_id))["content"]
    assert result.startswith("📂 workspace:") and "<tool_error>" not in result, result
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "list-once" and m["content"] == result
        for m in client.requests[1]["messages"]
    )


@pytest.mark.parametrize("change", ["no_instruction", "revoked", "bytes"])
async def test_unavailable_delegated_memory_still_withholds_derived_history(
    shared_memory,  # noqa: F811
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
    change,
):
    *_, client, snapshots = await delegated_rounds(
        shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch, change=change
    )
    entries = snapshots[1]["input_bindings"]["messages"]
    own = [
        entry
        for entry in entries
        if entry.get("source_ref", "").startswith(("session-model-result:", "session-tool-result:"))
    ]
    assert len(own) == 2 and all(entry["status"] == "withheld" for entry in own), entries
    assert all(entry["code"] == "explicit_memory_source_unverified" for entry in own), own
    assert not any(m.get("role") in {"assistant", "tool"} for m in client.requests[1]["messages"])
    if change != "no_instruction":
        assert len(snapshots[1]["instruction_indexes"]) == 1
        instruction = entries[snapshots[1]["instruction_indexes"][0]]
        assert instruction["status"] == "withheld", instruction
        assert (
            instruction["code"]
            == {"revoked": "explicit_memory_history_denied", "bytes": "content_source_changed"}[change]
        ), instruction
    assert "ORCHID_PRIVATE_MARKER" not in json.dumps(client.requests[1]["messages"])


async def test_an_attested_instruction_does_not_cover_another_parent_memory_source(
    shared_memory,  # noqa: F811
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
):
    *_, source, client, snapshots = await delegated_rounds(
        shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch, change="unrelated_source"
    )
    entries = snapshots[1]["input_bindings"]["messages"]
    assert any(
        entry["status"] == "bound"
        and source in entry.get("dependencies", [])
        and entry.get("source_ref", "").startswith("session-kernel-context:")
        for entry in entries
    ), entries
    assert entries[-1]["status"] == "withheld" and entries[-1]["code"] == "explicit_memory_source_unverified"
    assert "UNSHARED_PRIVATE_MARKER" not in json.dumps(client.requests[1]["messages"])


async def test_delegated_history_reader_never_grants_direct_parent_memory_reads(
    shared_memory,  # noqa: F811
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
):
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    child, session, run, source, _, _ = await delegated_rounds(
        shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch
    )
    n = shared_memory.n
    principal = AgentRuntimePrincipal(
        agent_id=child,
        requester_user_id=n.user,
        session_id=str(session),
        runtime_task_id=str(run),
        purpose="interactive_session",
    )
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with pytest.raises(ContentInputReceiptError) as denied:
            await _validate_input_dependencies(db, n.tenant, [source], runtime_request=principal)
        assert (denied.value.code, denied.value.status_code) == ("explicit_memory_source_unverified", 403)
    for tool_name, arguments in [
        ("load_memory", {"ids": [shared_memory.entry_id]}),
        ("read_file", {"path": f"../{n.agent}/memory/explicit/entries/{shared_memory.entry_id}.md"}),
    ]:
        content = str(
            await n.service.execute(
                tool_name,
                arguments,
                agent_id=child,
                user_id=n.user,
                session_id=str(session),
                runtime_task_id=str(run),
                emit_runtime_hooks=False,
            )
        )
        assert "ORCHID_PRIVATE_MARKER" not in content, content
