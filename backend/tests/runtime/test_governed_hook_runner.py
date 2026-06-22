from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.hooks import HookContext, HookEvent
from app.services.code_execution.contracts import CodeExecutionResult


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
            HookSpec(key="stop-shell", event=HookEvent.STOP, type="command", command="echo stop"),
            HookSpec(key="notify-shell", event=HookEvent.NOTIFICATION, type="command", command="echo notify"),
        ],
    )

    assert count == 2
    assert registry.describe_registrations()[0]["handler_name"] == "governed_hook_runner"
    result = await registry.emit(HookContext(event=HookEvent.STOP, session_id="s1"))
    assert result is not None
    assert result.block is True
    assert "blocked by registered hook" in result.reason
