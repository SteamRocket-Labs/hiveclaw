"""GovernedHookRunner production wiring tests.

GovernedHookRunner is the single external command/prompt/HTTP/agent hook runner.
It is wired through the existing plugin hook registration path, not a second
hook runtime.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.hooks import HookContext, HookEvent
from app.services.code_execution.contracts import CodeExecutionResult


def test_governed_hook_runner_is_registered_through_plugin_hook_service() -> None:
    """External hooks must enter through plugin_hook_service, not a hidden path."""
    import app.packs.catalog_reader as catalog_reader
    import app.services.plugin_hook_service as plugin_hooks

    assert {"hook.command", "hook.prompt", "hook.http", "hook.agent"} <= set(catalog_reader.HOOK_HANDLER_ALLOWLIST)
    plugin_src = inspect.getsource(plugin_hooks)
    assert "GovernedHookRunner" in plugin_src
    assert "register_governed_hook_specs" not in plugin_src


def test_plugin_hook_row_builds_governed_spec_and_keeps_matcher_separate() -> None:
    """DB rows carry matcher + governed runner spec without mixing the two."""
    from app.services import plugin_hook_service as plugin_hooks

    tenant_id = uuid4()
    row = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        installed_plugin_id=uuid4(),
        event="pre_tool_use",
        handler="hook.command",
        mode="enforce",
        matcher_json={
            "matcher": {"tool_names": ["write_file"]},
            "spec": {
                "type": "command",
                "command": "python guard.py",
                "timeout_seconds": 9,
                "status_message": "checking write",
            },
        },
    )
    plugin = SimpleNamespace(plugin_key="guard_pack")

    spec = plugin_hooks._governed_hook_spec_from_row(row, plugin)
    matcher = plugin_hooks._matcher_for(row, plugin, ["agent-1"])

    assert spec.key.startswith("guard_pack:")
    assert spec.event == HookEvent.PRE_TOOL_USE
    assert spec.type == "command"
    assert spec.command == "python guard.py"
    assert spec.timeout_seconds == 9
    assert spec.status_message == "checking write"
    assert spec.failure_mode == "required"
    assert matcher["tool_names"] == ["write_file"]
    assert matcher["agent_ids"] == ["agent-1"]
    assert "spec" not in matcher


def test_observe_plugin_hook_builds_advisory_spec() -> None:
    from app.services import plugin_hook_service as plugin_hooks

    row = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        installed_plugin_id=uuid4(),
        event="user_prompt_submit",
        handler="hook.prompt",
        mode="observe",
        matcher_json={"spec": {"type": "prompt", "prompt": "observe"}},
    )

    spec = plugin_hooks._governed_hook_spec_from_row(row, SimpleNamespace(plugin_key="observer"))

    assert spec.failure_mode == "advisory"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode,blocks", [("required", True), ("advisory", False)])
async def test_governed_runner_converts_executor_failure_by_typed_mode(
    tmp_path: Path,
    failure_mode: str,
    blocks: bool,
) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    async def broken_prompt(_prompt: str, **_kwargs) -> dict:
        raise TimeoutError("provider timed out")

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"prompt"}),
        prompt_executor=broken_prompt,
    )
    handler = runner.build_handler(
        HookSpec(
            key=f"{failure_mode}-prompt",
            event=HookEvent.USER_PROMPT_SUBMIT,
            type="prompt",
            prompt="check",
            failure_mode=failure_mode,
        )
    )

    result = await handler(HookContext(event=HookEvent.USER_PROMPT_SUBMIT, prompt="hello"))

    if blocks:
        assert result is not None
        assert result.block is True
        assert result.failure is True
        assert result.retryable is True
    else:
        assert result is None


@pytest.mark.asyncio
async def test_governed_hook_runner_is_visible_in_live_hook_catalog(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookContext, HookEvent, HookRegistry
    from app.services.code_execution.contracts import CodeExecutionResult

    async def fake_command_executor(_command: list[str], **_kwargs) -> CodeExecutionResult:
        return CodeExecutionResult(stdout='{"additional_contexts":["registered"]}', exit_code=0)

    registry = HookRegistry()
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
    )
    registry.register(
        HookEvent.USER_PROMPT_SUBMIT,
        runner.build_handler(
            HookSpec(
                key="prompt-context",
                event=HookEvent.USER_PROMPT_SUBMIT,
                type="command",
                command="echo context",
            )
        ),
        key="governed:prompt-context",
        handler_name="governed_hook_runner",
    )

    catalog = registry.describe_event_catalog()
    event = next(item for item in catalog if item["event"] == HookEvent.USER_PROMPT_SUBMIT.value)
    assert "governed_hook_runner" in event["runtime_consumer"]
    result = await registry.emit(HookContext(event=HookEvent.USER_PROMPT_SUBMIT, session_id="s1"))
    assert result is not None
    assert result.additional_contexts == ["registered"]


@pytest.mark.asyncio
async def test_command_hook_requires_explicit_policy_enablement(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    calls: list[list[str]] = []

    async def fake_command_executor(command: list[str], **_kwargs) -> CodeExecutionResult:
        calls.append(command)
        return CodeExecutionResult(stdout="should not run")

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=False, work_dir=tmp_path),
        command_executor=fake_command_executor,
    )

    result = await runner.run(
        HookSpec(key="stop-shell", event=HookEvent.STOP, type="command", command="echo blocked"),
        HookContext(event=HookEvent.STOP, session_id="s1", agent_id="agent-1"),
    )

    assert result.status == "disabled"
    assert result.hook_result is None
    assert calls == []


@pytest.mark.asyncio
async def test_command_hook_uses_governed_code_execution_and_writes_replayable_events(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    executed: list[dict] = []
    transcript_events: list[dict] = []
    spans: list[dict] = []

    async def fake_command_executor(command: list[str], **kwargs) -> CodeExecutionResult:
        executed.append({"command": command, **kwargs})
        return CodeExecutionResult(stdout="ok\n", stderr="needs follow-up\n", exit_code=2)

    async def fake_transcript_writer(event: dict) -> None:
        transcript_events.append(event)

    async def fake_span_recorder(fact: dict) -> None:
        spans.append(fact)

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
        transcript_writer=fake_transcript_writer,
        span_recorder=fake_span_recorder,
    )

    result = await runner.run(
        HookSpec(
            key="stop-shell",
            event=HookEvent.STOP,
            type="command",
            command="echo ok",
            timeout_seconds=7,
            status_message="checking final answer",
        ),
        HookContext(event=HookEvent.STOP, session_id="s1", agent_id="agent-1", metadata={"tenant_id": "tenant-1"}),
    )

    assert executed == [
        {
            "command": ["bash", "-lc", "echo ok"],
            "work_dir": tmp_path,
            "env": {},
            "timeout": 7,
            "runtime": "hook_runner",
            "network_policy": "deny",
        }
    ]
    assert result.status == "blocked"
    assert result.hook_result is not None
    assert result.hook_result.block is True
    assert "needs follow-up" in result.hook_result.reason
    assert [event["event_type"] for event in transcript_events] == [
        "hook_progress",
        "hook_attachment",
        "hook_summary",
    ]
    assert transcript_events[1]["metadata"]["hook_event"] == "stop"
    assert transcript_events[1]["metadata"]["exit_code"] == 2
    assert spans[0]["fact_type"] == "hook_run"
    assert spans[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_governed_hook_span_reuses_boundary_evidence_transaction(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    evidence_db = object()
    captured: list[tuple[dict, object | None]] = []

    async def fake_span_recorder(fact: dict, *, evidence_db=None) -> None:
        captured.append((fact, evidence_db))

    async def fake_command_executor(_command: list[str], **_kwargs) -> CodeExecutionResult:
        return CodeExecutionResult(stdout="ok", exit_code=0)

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
        span_recorder=fake_span_recorder,
    )

    await runner.run(
        HookSpec(key="file-audit", event=HookEvent.FILE_CHANGED, type="command", command="echo ok"),
        HookContext(
            event=HookEvent.FILE_CHANGED,
            session_id="session-1",
            agent_id="agent-1",
            metadata={"tenant_id": "tenant-1", "runtime_task_id": "task-1"},
            _evidence_db=evidence_db,
        ),
    )

    assert len(captured) == 1
    assert captured[0][1] is evidence_db


@pytest.mark.asyncio
async def test_prompt_and_agent_hooks_are_routed_through_injected_governed_adapters(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    calls: list[tuple[str, str]] = []

    async def fake_prompt_executor(prompt: str, **_kwargs) -> dict:
        calls.append(("prompt", prompt))
        return {"text": "prompt says continue", "decision": "allow"}

    async def fake_agent_executor(prompt: str, **_kwargs) -> dict:
        calls.append(("agent", prompt))
        return {"text": "agent verifier says stop", "decision": "block"}

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"prompt", "agent"}),
        prompt_executor=fake_prompt_executor,
        agent_executor=fake_agent_executor,
    )

    prompt_result = await runner.run(
        HookSpec(key="prompt-check", event=HookEvent.STOP, type="prompt", prompt="Check $ARGUMENTS"),
        HookContext(event=HookEvent.STOP, session_id="s1", prompt="user prompt"),
    )
    agent_result = await runner.run(
        HookSpec(key="agent-check", event=HookEvent.STOP, type="agent", prompt="Verify $ARGUMENTS"),
        HookContext(event=HookEvent.STOP, session_id="s1", last_assistant_message="draft"),
    )

    assert prompt_result.status == "success"
    assert agent_result.status == "blocked"
    assert agent_result.hook_result is not None
    assert agent_result.hook_result.block is True
    assert calls[0][0] == "prompt"
    assert '"event": "stop"' in calls[0][1]
    assert calls[1][0] == "agent"


@pytest.mark.asyncio
async def test_prompt_hook_can_rewrite_pre_tool_input(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    async def fake_prompt_executor(_prompt: str, **_kwargs) -> dict:
        return {
            "text": "normalized command timeout",
            "decision": "allow",
            "updated_input": {"command": "pytest -q", "timeout": 30},
        }

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"prompt"}),
        prompt_executor=fake_prompt_executor,
    )

    result = await runner.run(
        HookSpec(key="rewrite-command", event=HookEvent.PRE_TOOL_USE, type="prompt", prompt="Rewrite $ARGUMENTS"),
        HookContext(
            event=HookEvent.PRE_TOOL_USE,
            session_id="s1",
            tool_name="run_command",
            tool_args={"command": "pytest"},
        ),
    )

    assert result.status == "success"
    assert result.hook_result is not None
    assert result.hook_result.modified_args == {"command": "pytest -q", "timeout": 30}


@pytest.mark.asyncio
async def test_prompt_hook_parses_hook_specific_output_for_its_declared_event(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    async def fake_prompt_executor(_prompt: str, **_kwargs) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "Carry this context into the next turn.",
            }
        }

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"prompt"}),
        prompt_executor=fake_prompt_executor,
    )

    result = await runner.run(
        HookSpec(key="stop-context", event=HookEvent.STOP, type="prompt", prompt="Check $ARGUMENTS"),
        HookContext(event=HookEvent.STOP, session_id="s1", last_assistant_message="final draft"),
    )

    assert result.status == "success"
    assert result.hook_result is not None
    assert result.hook_result.additional_contexts == ["Carry this context into the next turn."]


@pytest.mark.asyncio
async def test_command_hook_parses_json_output_for_context_and_rewrite(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    async def fake_command_executor(_command: list[str], **_kwargs) -> CodeExecutionResult:
        return CodeExecutionResult(
            stdout=(
                '{"decision":"allow",'
                '"updated_input":{"file_path":"workspace/report.md"},'
                '"additional_contexts_for_model":["Use the rewritten workspace path."]}'
            ),
            exit_code=0,
        )

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
    )

    result = await runner.run(
        HookSpec(key="rewrite-file", event=HookEvent.PRE_TOOL_USE, type="command", command="rewrite"),
        HookContext(
            event=HookEvent.PRE_TOOL_USE,
            session_id="s1",
            tool_name="read_file",
            tool_args={"file_path": "report.md"},
        ),
    )

    assert result.status == "success"
    assert result.hook_result is not None
    assert result.hook_result.modified_args == {"file_path": "workspace/report.md"}
    assert result.hook_result.additional_contexts == ["Use the rewritten workspace path."]


@pytest.mark.asyncio
async def test_governed_hook_specs_register_into_existing_hook_registry(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec, register_governed_hook_specs
    from app.runtime.hooks import HookRegistry

    async def fake_command_executor(_command: list[str], **_kwargs) -> CodeExecutionResult:
        return CodeExecutionResult(stderr="blocked by registered hook", exit_code=2)

    registry = HookRegistry()
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
    )

    count = register_governed_hook_specs(
        registry=registry,
        runner=runner,
        specs=[
            HookSpec(
                key="stop-shell",
                event=HookEvent.STOP,
                type="command",
                command="echo stop",
                failure_mode="required",
            ),
            HookSpec(key="notify-shell", event=HookEvent.NOTIFICATION, type="command", command="echo notify"),
        ],
    )

    assert count == 2
    assert registry.describe_registrations()[0]["handler_name"] == "governed_hook_runner"
    result = await registry.emit(HookContext(event=HookEvent.STOP, session_id="s1"))
    assert result is not None
    assert result.block is True
    assert "blocked by registered hook" in result.reason
