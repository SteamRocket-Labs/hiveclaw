from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.llm_client import LLMMessage


def test_recovery_manifest_preserves_tool_call_closure_state() -> None:
    from app.runtime.recovery_manifest import build_recovery_manifest
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-tool-closure",
        source="web",
        channel="web",
        active_tool_groups=[{"name": "web_pack", "tools": ["firecrawl_fetch"]}],
        metadata={
            "pending_tool_frame": {
                "tool_call_id": "call_pending",
                "tool_name": "send_email",
                "arguments": {"to": "customer@example.com"},
            },
            "permission_checkpoint": {
                "checkpoint_id": "permission-checkpoint-1",
                "tool_call_id": "call_pending",
                "decision": "allow_once",
            },
            "hook_lifecycle_records": [
                {
                    "hook_run_id": "hook-pre-1",
                    "event": "pre_tool_use",
                    "decision": "rewrite_args",
                }
            ],
            "compaction_lifecycle_records": [
                {
                    "compaction_id": "compact-1",
                    "trigger": "request_preflight",
                    "status": "completed",
                }
            ],
            "permission_profile": {"mode": "request", "allowed_tools": ["send_email"]},
        },
    )
    session.track_discovered_tools(["firecrawl_fetch"])
    session.track_tool_outcome("tool_search", "Discovered firecrawl_fetch for crawled evidence.")

    manifest = build_recovery_manifest(session)

    assert manifest.discovered_tools == ["firecrawl_fetch"]
    assert manifest.pending_tool_frames[0]["tool_call_id"] == "call_pending"
    assert manifest.permission_checkpoints[0]["checkpoint_id"] == "permission-checkpoint-1"
    assert manifest.hook_lifecycle_records[0]["event"] == "pre_tool_use"
    assert manifest.compaction_lifecycle_records[0]["compaction_id"] == "compact-1"
    assert manifest.permission_profile["mode"] == "request"

    restored = manifest.to_restoration_text()
    assert "Discovered Tools" in restored
    assert "Pending Tool Frames" in restored
    assert "Permission Checkpoints" not in restored
    assert "allow_once" not in restored
    assert "Hook Lifecycle Records" in restored
    assert "Compaction Lifecycle Records" in restored


@pytest.mark.asyncio
async def test_request_preflight_compaction_emits_compaction_lifecycle_event() -> None:
    from app.runtime.ccplus_contracts import build_context_policy
    from app.runtime.session_context_controller import prepare_session_context_for_request

    messages = [
        LLMMessage(role="user", content="A" * 120),
        LLMMessage(role="assistant", content="B" * 120),
        LLMMessage(role="user", content="C" * 120),
    ]
    events: list[dict] = []

    async def compress_messages(_messages, **kwargs):
        on_compaction = kwargs.get("on_compaction")
        if on_compaction:
            await on_compaction({"event_type": "compaction_completed", "summary": "summary-ref"})
        return [{"role": "system", "content": "Compacted summary"}]

    result = await prepare_session_context_for_request(
        messages=messages,
        policy=build_context_policy(
            13250,
            overrides={
                "output_reserve": 0,
                "tool_result_inline_limit": 1000,
                "round_tool_result_budget": 1000,
            },
        ),
        estimate_tokens=lambda items: sum(len(item.content or "") for item in items),
        compress_messages=compress_messages,
        cumulative_run_tokens=900,
        session_id="session-compact",
        turn_id="turn-compact",
        runtime_task_id="task-compact",
        on_decision=lambda event: events.append(event),
    )

    lifecycle_events = [event for event in events if event.get("event_type") == "compaction_lifecycle"]

    assert result.compressed is True
    assert lifecycle_events
    lifecycle = lifecycle_events[-1]["compaction_lifecycle"]
    assert lifecycle["session_id"] == "session-compact"
    assert lifecycle["turn_id"] == "turn-compact"
    assert lifecycle["runtime_task_id"] == "task-compact"
    assert lifecycle["trigger"] == "request_preflight"
    assert lifecycle["before_message_count"] == 3
    assert lifecycle["after_message_count"] == 1
    assert lifecycle["before_token_estimate"] >= lifecycle["after_token_estimate"]


def test_recovery_manifest_preserves_skill_fork_and_denial_continuation_shape() -> None:
    from app.runtime.recovery_manifest import build_recovery_manifest
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-crash-matrix",
        metadata={
            "pending_skill_handoffs": [
                {
                    "skill": "Research",
                    "skill_slug": "research",
                    "source": "skills/research/SKILL.md",
                    "execution_tool": "spawn_subagent",
                    "tool_arguments": {
                        "skill_source": "skills/research/SKILL.md",
                        "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                    },
                }
            ],
            "executed_skill_handoffs": [
                {
                    "skill": "Review",
                    "skill_slug": "review",
                    "execution_tool": "spawn_subagent",
                    "tool_call_id": "call-load:skill:review",
                    "result": '{"ok": true, "child_session_id": "child-review"}',
                }
            ],
            "source": "session_permission_denied_resume",
            "resumed_from_permission_request_id": "perm-denied",
            "denied_tool_name": "send_email",
            "denied_tool_call_id": "call-email",
            "resumed_turn_id": "turn-denied",
            "resumed_runtime_task_id": "runtime-denied",
            "round_state": {"round": 3},
            "t0_refs": ["t0://sessions/session-crash-matrix/events/7"],
        },
    )

    manifest = build_recovery_manifest(session)

    assert manifest.pending_skill_handoffs[0]["skill_slug"] == "research"
    assert manifest.executed_skill_handoffs[0]["tool_call_id"] == "call-load:skill:review"
    assert manifest.continuation_records == [
        {
            "source": "session_permission_denied_resume",
            "resumed_from_permission_request_id": "perm-denied",
            "denied_tool_name": "send_email",
            "denied_tool_call_id": "call-email",
            "resumed_turn_id": "turn-denied",
            "resumed_runtime_task_id": "runtime-denied",
            "round_state": {"round": 3},
            "t0_refs": ["t0://sessions/session-crash-matrix/events/7"],
        }
    ]
    restored = manifest.to_restoration_text()
    assert "Pending Skill Handoffs" in restored
    assert "Executed Skill Handoffs" in restored
    assert "Continuation Records" in restored


@pytest.mark.parametrize(
    ("tool_name", "tool_arguments", "allowed_tools"),
    [
        (
            "write_file",
            {"path": "workspace/report.md", "content": "draft"},
            ["write_file"],
        ),
        (
            "spawn_subagent",
            {
                "prompt": "continue research",
                "permission_profile": {"mode": "auto", "allowed_tools": ["web_search"]},
            },
            ["spawn_subagent", "web_search"],
        ),
        (
            "start_workflow",
            {
                "definition": {"name": "wait_review", "steps": []},
                "args": {},
                "preview_id": "preview-wait-review",
            },
            ["start_workflow"],
        ),
    ],
)
def test_killed_process_invoke_agent_persists_recoverable_tool_matrix(
    tmp_path,
    monkeypatch,
    tool_name: str,
    tool_arguments: dict,
    allowed_tools: list[str],
) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    agent_id = uuid4()
    user_id = uuid4()
    session_id = f"session-killed-process-{tool_name}"
    tool_call_id = f"call-before-kill-{tool_name}"
    truth_ref = f"truth://policy/{tool_name.replace('_', '-')}"
    tool_started_path = tmp_path / "tool_started.json"
    child_script = tmp_path / "run_killed_invoke_agent.py"
    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"test tool {tool_name}",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
            },
        }
    ]
    child_script.write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import json
            import os
            from pathlib import Path
            from types import SimpleNamespace
            from uuid import UUID

            import app.runtime.invoker as invoker
            from app.kernel import RuntimeConfig
            from app.runtime.invoker import AgentInvocationRequest, invoke_agent
            from app.runtime.session import SessionContext

            agent_id = UUID({str(agent_id)!r})
            user_id = UUID({str(user_id)!r})
            tool_started_path = Path(os.environ["TOOL_STARTED_PATH"])

            class FakeClient:
                async def stream(self, **kwargs):
                    return SimpleNamespace(
                        content="",
                        tool_calls=[
                            {{
                                "id": {tool_call_id!r},
                                "function": {{
                                    "name": {tool_name!r},
                                    "arguments": json.dumps({tool_arguments!r}),
                                }},
                            }}
                        ],
                        reasoning_content="need to write the report",
                        reasoning_signature=None,
                        usage={{"total_tokens": 7}},
                    )

                async def close(self):
                    return None

            async def fake_runtime_config(_agent_id):
                return RuntimeConfig(
                    tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                    max_tool_rounds=2,
                    turn_token_budget=100000,
                )

            async def empty_async(*_args, **_kwargs):
                return ""

            async def allow_quota(_user_id):
                return None

            async def no_memory(**_kwargs):
                return None

            async def fake_tool(tool_name, args, **kwargs):
                tool_started_path.write_text(
                    json.dumps(
                        {{
                            "tool_name": tool_name,
                            "args": args,
                            "tool_call_id": kwargs.get("tool_call_id"),
                            "runtime_task_id": kwargs.get("runtime_task_id"),
                            "turn_id": kwargs.get("turn_id"),
                            "origin_channel": kwargs.get("origin_channel"),
                        }},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                while True:
                    await asyncio.sleep(1)

            invoker._resolve_runtime_config = fake_runtime_config
            invoker._resolve_current_user_name = empty_async
            invoker.build_agent_context = empty_async
            invoker.build_agent_runtime_context = empty_async
            invoker.build_memory_context = empty_async
            invoker.build_skill_catalog_section_for_agent = lambda *_args, **_kwargs: ""
            invoker.get_agent_tools_for_llm = lambda *_args, **_kwargs: {tool_schema!r}
            invoker.check_user_token_quota = allow_quota
            invoker.create_llm_client = lambda **_kwargs: FakeClient()
            invoker.record_token_usage = lambda *_args, **_kwargs: None
            invoker.persist_runtime_memory = no_memory
            invoker.record_skill_runtime_usage_for_invocation = lambda *_args, **_kwargs: None
            invoker.get_max_tokens = lambda *_args, **_kwargs: 2048

            session = SessionContext(
                session_id={session_id!r},
                source="web",
                channel="feishu",
                metadata={{
                    "runtime_task_id": "runtime-kill",
                    "claim_version": 1,
                    "claim_worker_id": "killed-process-worker",
                    "turn_id": "turn-kill",
                    "origin_channel": "feishu",
                    "round_state": {{"round": 1}},
                    "t0_refs": ["t0://sessions/{session_id}/events/1"],
                    "permission_profile": {{"mode": "default", "allowed_tools": {allowed_tools!r}}},
                    "mcp_assignments": [{{"server": "docs", "tool": "read_mcp_resource"}}],
                    "truth_evidence_refs": [{truth_ref!r}],
                    "truth_evidence": [{{"evidence_id": {truth_ref!r}, "provider": "test"}}],
                    "pending_skill_handoffs": [
                        {{
                            "skill": "Research",
                            "skill_slug": "research",
                            "execution_tool": "spawn_subagent",
                            "tool_arguments": {{
                                "task": "continue research",
                                "permission_profile": {{"mode": "auto", "allowed_tools": ["web_search"]}},
                            }},
                        }}
                    ],
                    "continuation_record": {{
                        "source": "session_permission_denied_resume",
                        "resumed_from_permission_request_id": "perm-kill",
                        "denied_tool_name": {tool_name!r},
                        "resumed_runtime_task_id": "runtime-denial",
                        "t0_refs": ["t0://sessions/{session_id}/events/0"],
                    }},
                }},
            )

            asyncio.run(
                invoke_agent(
                    AgentInvocationRequest(
                        model=SimpleNamespace(
                            provider="openai",
                            model="gpt-4.1",
                            api_key="test-key",
                            base_url=None,
                            max_output_tokens=None,
                            max_input_tokens=128000,
                        ),
                        messages=[{{"role": "user", "content": "write the report"}}],
                        agent_name="Crash Probe",
                        role_description="Probe recovery",
                        agent_id=agent_id,
                        user_id=user_id,
                        memory_session_id={session_id!r},
                        session_context=session,
                        tool_executor=fake_tool,
                        max_tool_rounds=2,
                    )
                )
            )
            """
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AGENT_DATA_DIR"] = str(tmp_path)
    env["TOOL_STARTED_PATH"] = str(tool_started_path)
    env["PYTHONPATH"] = str(backend_root) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen([sys.executable, str(child_script)], cwd=backend_root, env=env)
    from app.runtime.recovery_manifest import recovery_manifest_path

    manifest_path = recovery_manifest_path(
        agent_id,
        session_id=session_id,
        runtime_task_id="runtime-kill",
        data_root=tmp_path,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if manifest_path.exists() and tool_started_path.exists():
                break
            if process.poll() is not None:
                raise AssertionError(
                    f"child process exited before tool crash point: rc={process.returncode}, "
                    f"manifest_exists={manifest_path.exists()}"
                )
            time.sleep(0.05)
        else:
            raise AssertionError("invoke_agent child did not persist recovery manifest before the running tool hung")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=5)

    tool_started = json.loads(tool_started_path.read_text(encoding="utf-8"))
    assert tool_started["tool_name"] == tool_name
    assert tool_started["args"] == tool_arguments
    assert tool_started["tool_call_id"] == tool_call_id

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["session_id"] == session_id
    assert payload["pending_tool_frames"][0]["tool_call_id"] == tool_call_id
    assert payload["pending_tool_frames"][0]["tool_name"] == tool_name
    assert payload["pending_tool_frames"][0]["runtime_task_id"] == "runtime-kill"
    assert payload["pending_tool_frames"][0]["origin_channel"] == "feishu"
    assert payload["pending_skill_handoffs"][0]["execution_tool"] == "spawn_subagent"
    assert payload["mcp_assignments"] == [{"server": "docs", "tool": "read_mcp_resource"}]
    assert payload["truth_evidence_refs"] == [truth_ref]
    assert payload["continuation_records"][0]["source"] == "session_permission_denied_resume"

    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    restored = _build_restoration_context(
        agent_id,
        session_context=SessionContext(
            session_id=session_id,
            metadata={
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "runtime_task_id": "runtime-kill",
                "claim_version": 2,
                "claim_worker_id": "recovery-reader",
            },
        ),
    )
    assert tool_call_id in restored
    assert tool_name in restored
    assert "Pending Skill Handoffs" in restored
    assert "MCP Assignments" in restored
    assert "Truth Evidence" in restored
    assert "Continuation Records" in restored
    assert "session_permission_denied_resume" in restored


def test_persisted_recovery_manifest_restores_full_crash_matrix(tmp_path, monkeypatch) -> None:
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    tenant_id = uuid4()
    runtime_task_id = "runtime-crash-matrix"
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-crash-matrix",
                "agent_id": str(agent_id),
                "tenant_id": str(tenant_id),
                "runtime_task_id": runtime_task_id,
                "claim_version": 1,
                "claim_worker_id": "worker-1",
                "discovered_tools": ["firecrawl_fetch"],
                "pending_tool_frames": [
                    {
                        "permission_request_id": "perm-1",
                        "tool_call_id": "call-send",
                        "tool_name": "send_email",
                        "runtime_task_id": "runtime-1",
                        "turn_id": "turn-1",
                        "round_state": {"round": 2},
                        "t0_refs": ["t0://sessions/session-crash-matrix/events/5"],
                    }
                ],
                "permission_checkpoints": [
                    {
                        "permission_request_id": "perm-1",
                        "decision": "deny",
                        "continuation_runtime_task_id": "runtime-denial-continuation",
                    }
                ],
                "compaction_lifecycle_records": [
                    {"compaction_id": "compact-1", "trigger": "mid_loop_auto", "status": "completed"}
                ],
                "permission_profile": {"mode": "request", "allowed_tools": ["send_email"]},
                "mcp_assignments": [{"server": "docs", "tool": "read_mcp_resource"}],
                "truth_evidence_refs": ["truth://provider-error/web-search"],
                "truth_evidence": [{"evidence_id": "truth://provider-error/web-search", "provider": "searxng"}],
                "pending_skill_handoffs": [
                    {
                        "skill": "Research",
                        "skill_slug": "research",
                        "execution_tool": "spawn_subagent",
                        "tool_arguments": {
                            "skill_source": "skills/research/SKILL.md",
                            "permission_profile": {"mode": "auto", "allowed_tools": ["web_search"]},
                        },
                    }
                ],
                "executed_skill_handoffs": [
                    {
                        "skill": "Review",
                        "skill_slug": "review",
                        "tool_call_id": "call-load:skill:review",
                        "result": '{"child_session_id": "child-review"}',
                    }
                ],
                "continuation_records": [
                    {
                        "source": "session_permission_denied_resume",
                        "resumed_from_permission_request_id": "perm-1",
                        "denied_tool_name": "send_email",
                        "resumed_runtime_task_id": "runtime-denial-continuation",
                        "t0_refs": ["t0://sessions/session-crash-matrix/events/5"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(
        agent_id,
        session_context=SessionContext(
            session_id="session-crash-matrix",
            metadata={
                "agent_id": str(agent_id),
                "tenant_id": str(tenant_id),
                "runtime_task_id": runtime_task_id,
                "claim_version": 1,
                "claim_worker_id": "worker-1",
            },
        ),
    )

    assert "### Recovery Manifest" in restored
    assert "Pending Tool Frames" in restored
    assert "runtime-1" in restored
    assert "Permission Checkpoints" not in restored
    assert "allowed_tools" not in restored
    assert "runtime-denial-continuation" in restored
    assert "Compaction Lifecycle Records" in restored
    assert "MCP Assignments" in restored
    assert "Truth Evidence" in restored
    assert "Pending Skill Handoffs" in restored
    assert "Executed Skill Handoffs" in restored
    assert "Continuation Records" in restored


@pytest.mark.asyncio
async def test_mixed_recovered_batch_is_tombstoned_before_any_replay_after_restart(tmp_path, monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_recovered_pending_tool_frames
    from app.runtime.recovery_manifest import (
        load_and_hydrate_recovery_manifest,
        persist_recovery_manifest,
        recovery_manifest_path,
    )
    from app.runtime.session import SessionContext

    agent_id, tenant_id, runtime_task_id = uuid4(), uuid4(), uuid4()
    session_id = "session-mixed-recovery"
    identity = {
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id),
        "runtime_task_id": str(runtime_task_id),
        "claim_version": 3,
        "claim_worker_id": "worker-3",
    }
    safe_frame = {
        "tool_call_id": "call-read",
        "tool_name": "read_file",
        "arguments": {"path": "workspace/report.md"},
        "status": "running",
    }
    unsafe_frame = {
        "tool_call_id": "call-write",
        "tool_name": "write_file",
        "arguments": {"path": "workspace/report.md", "content": "changed"},
        "status": "running",
    }
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    crashed = SessionContext(
        session_id=session_id,
        metadata={**identity, "pending_tool_frames": [safe_frame, unsafe_frame]},
    )
    assert persist_recovery_manifest(agent_id, crashed, data_root=tmp_path)
    path = recovery_manifest_path(
        agent_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )
    restarted = SessionContext(session_id=session_id, metadata=dict(identity))
    assert load_and_hydrate_recovery_manifest(agent_id, restarted, data_root=tmp_path) is not None
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        session_context=restarted,
        memory_session_id=session_id,
    )
    executed: list[str] = []
    ordering: list[str] = []
    events: list[dict] = []

    async def execute(tool_name, *_args, **_kwargs):
        executed.append(tool_name)
        return "EXECUTED"

    async def renew():
        assert executed == []
        ordering.append("renew")

    async def emit(event):
        assert executed == []
        assert restarted.metadata.get("recovery_manifest_checkpoint_receipt")
        ordering.append("event")
        events.append(dict(event))

    text = await _execute_recovered_pending_tool_frames(
        execute_tool=execute,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
        emit_event=emit,
        renew_runtime_lease=renew,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    receipt = restarted.metadata["recovery_manifest_checkpoint_receipt"]
    assert executed == []
    assert ordering[0] == "renew"
    assert ordering[1:] == ["event", "event"]
    assert restarted.metadata["recovery_reconciliation_blocked"] is True
    assert [frame["tool_call_id"] for frame in payload["pending_tool_frames"]] == ["call-read", "call-write"]
    assert {frame["status"] for frame in payload["pending_tool_frames"]} == {"needs_reconciliation"}
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert all(event["recovery_tombstone_persisted"] is True for event in events)
    assert all(event["recovery_manifest_checkpoint_receipt"]["sha256"] == receipt["sha256"] for event in events)
    assert "entire recovered tool batch" in text


@pytest.mark.asyncio
async def test_incomplete_authority_manifest_blocks_new_tool_without_hydrating_evidence(tmp_path, monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.recovery_manifest import load_and_hydrate_recovery_manifest, recovery_manifest_path
    from app.runtime.session import SessionContext

    agent_id, tenant_id, runtime_task_id = uuid4(), uuid4(), uuid4()
    session_id = "session-incomplete-authority"
    path = recovery_manifest_path(
        agent_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    payload = {
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id),
        "session_id": session_id,
        "runtime_task_id": runtime_task_id.hex,
        "claim_version": 4,
        "pending_tool_frames": [{"tool_call_id": "call-old", "tool_name": "read_file", "status": "running"}],
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    restarted = SessionContext(
        session_id=session_id,
        metadata={
            "agent_id": str(agent_id),
            "tenant_id": str(tenant_id),
            "runtime_task_id": runtime_task_id.hex,
            "claim_version": 5,
            "claim_worker_id": "worker-5",
        },
    )

    assert load_and_hydrate_recovery_manifest(agent_id, restarted, data_root=tmp_path) is None
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        session_context=restarted,
        memory_session_id=session_id,
    )
    executed: list[str] = []
    events: list[dict] = []

    async def execute(tool_name, *_args, **_kwargs):
        executed.append(tool_name)
        return "EXECUTED"

    async def emit(event):
        events.append(dict(event))

    result, _args, did_execute = await _execute_tool_with_hooks(
        execute_tool=execute,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
        tool_name="read_file",
        tool_args={"path": "workspace/new.md"},
        tool_call_id="call-new",
        emit_event=emit,
    )

    assert did_execute is False
    assert executed == []
    assert "reconciliation" in result.lower()
    assert restarted.metadata.get("recovered_pending_tool_frames") in (None, [])
    assert restarted.metadata["recovery_reconciliation_reason"] == "incomplete_recovery_manifest_authority"
    assert events[-1]["event_type"] == "recovery_reconciliation_blocked"
    assert path.read_bytes() == raw


@pytest.mark.asyncio
async def test_unknown_mutating_frame_survives_two_restarts_and_blocks_new_tools(tmp_path, monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_recovered_pending_tool_frames, _execute_tool_with_hooks
    from app.runtime.recovery_manifest import (
        load_and_hydrate_recovery_manifest,
        persist_recovery_manifest,
        recovery_manifest_path,
    )
    from app.runtime.session import SessionContext

    agent_id, tenant_id, runtime_task_id = uuid4(), uuid4(), uuid4()
    session_id = "session-reconciliation"
    identity = {
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id),
        "runtime_task_id": str(runtime_task_id),
        "claim_version": 1,
        "claim_worker_id": "worker-1",
    }
    frame = {
        "tool_call_id": "call-write",
        "tool_name": "write_file",
        "arguments": {"path": "workspace/a.md", "content": "x"},
        "status": "running",
    }
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    crashed = SessionContext(
        session_id=session_id,
        metadata={**identity, "pending_tool_frame": dict(frame), "pending_tool_frames": [dict(frame)]},
    )
    assert persist_recovery_manifest(agent_id, crashed, data_root=tmp_path)
    path = recovery_manifest_path(
        agent_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )
    assert len(json.loads(path.read_text(encoding="utf-8"))["pending_tool_frames"]) == 1

    calls: list[str] = []
    events: list[dict] = []

    async def execute(tool_name, *_args, **_kwargs):
        calls.append(tool_name)
        return "EXECUTED"

    async def emit(event):
        events.append(event)

    runtime_config = RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3)

    def make_request(session):
        return InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1"),
            messages=[{"role": "user", "content": "continue"}],
            agent_name="Agent",
            role_description="role",
            agent_id=agent_id,
            session_context=session,
            memory_session_id=session_id,
        )

    restart_1 = SessionContext(session_id=session_id, metadata=dict(identity))
    assert load_and_hydrate_recovery_manifest(agent_id, restart_1, data_root=tmp_path) is not None
    assert len(restart_1.metadata["recovered_pending_tool_frames"]) == 1
    await _execute_recovered_pending_tool_frames(
        execute_tool=execute,
        request=make_request(restart_1),
        runtime_config=runtime_config,
        emit_event=emit,
    )
    payload_1 = json.loads(path.read_text(encoding="utf-8"))
    assert calls == []
    assert len(payload_1["pending_tool_frames"]) == 1
    assert payload_1["pending_tool_frames"][0]["tool_call_id"] == "call-write"
    assert payload_1["pending_tool_frames"][0]["status"] == "needs_reconciliation"

    restart_2 = SessionContext(session_id=session_id, metadata=dict(identity))
    assert load_and_hydrate_recovery_manifest(agent_id, restart_2, data_root=tmp_path) is not None
    assert len(restart_2.metadata["recovered_pending_tool_frames"]) == 1
    await _execute_recovered_pending_tool_frames(
        execute_tool=execute,
        request=make_request(restart_2),
        runtime_config=runtime_config,
        emit_event=emit,
    )
    payload_2 = json.loads(path.read_text(encoding="utf-8"))
    assert calls == []
    assert len(payload_2["pending_tool_frames"]) == 1
    assert payload_2["pending_tool_frames"][0]["status"] == "needs_reconciliation"

    result, _args, executed = await _execute_tool_with_hooks(
        execute_tool=execute,
        request=make_request(restart_2),
        runtime_config=runtime_config,
        tool_name="send_email",
        tool_args={"to": "user@example.com"},
        tool_call_id="call-new",
        emit_event=emit,
    )
    assert executed is False
    assert calls == []
    assert "requires operator reconciliation" in result
    assert events[-1]["event_type"] == "recovery_reconciliation_blocked"
    assert all(
        item["tool_call_id"] != "call-new"
        for item in json.loads(path.read_text(encoding="utf-8"))["pending_tool_frames"]
    )
