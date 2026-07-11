"""Hive Hook wire standard — DEFERRED external-runner tests (B-2).

``parse_hook_json_output`` decodes the exit-code / JSON-output semantics of
external command hooks. Hive has no external command hook runtime today (only
in-process allowlisted Python handlers run live), so this parser has no
production caller. These tests pin the Hive Hook wire standard against the
FreeCode baseline while keeping the public runtime API Hive-owned rather than
as a separate compatibility module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.hooks import HookContext, HookEvent
from app.services.code_execution.contracts import CodeExecutionResult


def test_hook_wire_parser_has_no_production_caller() -> None:
    """Revert guard: ``parse_hook_json_output`` is deferred — only the
    (also-deferred) hook_runner and tests reference it. A new production caller
    means the external-hook runtime was wired; update the deferred docstrings
    rather than leaving the status surfaces stale."""
    import inspect

    import app.main as main_mod

    assert "parse_hook_json_output" not in inspect.getsource(main_mod)
    legacy_module_name = "cc_" + "hook_contract"
    assert legacy_module_name not in inspect.getsource(main_mod)


def test_hook_wire_standard_has_single_runtime_control_module() -> None:
    """The Hook standard is maintained from ``app.runtime.hooks``.

    Do not reintroduce a second runtime control module for a source baseline
    such as CC/Codex/another CLI. Baselines belong in tests and docs; Hive owns
    the runtime standard.
    """

    backend_root = Path(__file__).resolve().parents[2]
    legacy_contract_module = "cc_" + "hook_contract.py"
    assert not (backend_root / "app/runtime" / legacy_contract_module).exists()


FREECODE_HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
    "CwdChanged",
    "FileChanged",
)


def test_hook_wire_events_match_freecode_baseline_exactly() -> None:
    from app.runtime.hooks import HOOK_WIRE_EVENTS, hook_event_for_wire_name, wire_name_for_hook_event

    assert HOOK_WIRE_EVENTS == FREECODE_HOOK_EVENTS
    assert len(HOOK_WIRE_EVENTS) == 27
    assert len(set(HOOK_WIRE_EVENTS)) == 27

    for wire_name in FREECODE_HOOK_EVENTS:
        event = hook_event_for_wire_name(wire_name)
        assert isinstance(event, HookEvent)
        assert wire_name_for_hook_event(event) == wire_name


def test_hook_output_parser_preserves_hook_specific_semantics() -> None:
    from app.runtime.hooks import parse_hook_json_output

    result = parse_hook_json_output(
        HookEvent.PRE_TOOL_USE,
        {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "trusted workspace read",
                "updatedInput": {"path": "workspace/report.md"},
                "additionalContext": "Use the rewritten workspace path.",
            },
        },
    )

    assert result.hook_result is not None
    assert result.status == "success"
    assert result.hook_result.modified_args == {"path": "workspace/report.md"}
    assert result.hook_result.additional_contexts == ["Use the rewritten workspace path."]
    assert result.hook_result.permission_behavior == "allow"
    assert result.hook_result.hook_permission_decision_reason == "trusted workspace read"


def test_permission_request_hook_output_resolves_in_session_permission() -> None:
    from app.runtime.hooks import parse_hook_json_output

    result = parse_hook_json_output(
        HookEvent.PERMISSION_REQUEST,
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "allow",
                    "updatedInput": {"query": "github trending"},
                    "updatedPermissions": [{"tool": "web_search", "behavior": "allow", "scope": "session"}],
                },
            },
        },
    )

    assert result.hook_result is not None
    assert result.hook_result.permission_request_result == {
        "behavior": "allow",
        "updatedInput": {"query": "github trending"},
        "updatedPermissions": [{"tool": "web_search", "behavior": "allow", "scope": "session"}],
    }


def test_elicitation_result_hook_output_can_override_action_and_content() -> None:
    from app.runtime.hooks import parse_hook_json_output

    result = parse_hook_json_output(
        HookEvent.ELICITATION_RESULT,
        {
            "hookSpecificOutput": {
                "hookEventName": "ElicitationResult",
                "action": "accept",
                "content": {"answer": "approved scope"},
            }
        },
    )

    assert result.hook_result is not None
    assert result.hook_result.elicitation_action == "accept"
    assert result.hook_result.elicitation_content == {"answer": "approved scope"}


def test_worktree_create_hook_output_returns_governed_workspace_path() -> None:
    from app.runtime.hooks import parse_hook_json_output

    result = parse_hook_json_output(
        HookEvent.WORKTREE_CREATE,
        {
            "hookSpecificOutput": {
                "hookEventName": "WorktreeCreate",
                "worktreePath": "session://branch-1/workspace",
            }
        },
    )

    assert result.hook_result is not None
    assert result.hook_result.worktree_path == "session://branch-1/workspace"


@pytest.mark.asyncio
async def test_command_hook_exit_code_one_is_non_blocking_error(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    async def fake_command_executor(_command: list[str], **_kwargs) -> CodeExecutionResult:
        return CodeExecutionResult(stderr="diagnostic warning", exit_code=1)

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
    )

    result = await runner.run(
        HookSpec(key="warn-only", event=HookEvent.POST_TOOL_USE, type="command", command="warn"),
        HookContext(event=HookEvent.POST_TOOL_USE, session_id="s1", tool_name="read_file"),
    )

    assert result.status == "non_blocking_error"
    assert result.hook_result is None


@pytest.mark.asyncio
async def test_command_hook_parses_hook_json_output_shape(tmp_path: Path) -> None:
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec

    async def fake_command_executor(_command: list[str], **_kwargs) -> CodeExecutionResult:
        return CodeExecutionResult(
            stdout=(
                '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
                '"updatedInput":{"path":"workspace/report.md"},'
                '"additionalContext":"Use session artifact path."}}'
            ),
            exit_code=0,
        )

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_hook_types={"command"}),
        command_executor=fake_command_executor,
    )

    result = await runner.run(
        HookSpec(key="rewrite", event=HookEvent.PRE_TOOL_USE, type="command", command="rewrite"),
        HookContext(
            event=HookEvent.PRE_TOOL_USE, session_id="s1", tool_name="read_file", tool_args={"path": "report.md"}
        ),
    )

    assert result.status == "success"
    assert result.hook_result is not None
    assert result.hook_result.modified_args == {"path": "workspace/report.md"}
    assert result.hook_result.additional_contexts == ["Use session artifact path."]
