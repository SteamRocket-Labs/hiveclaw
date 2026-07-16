"""Production ingress must adapt to the Session V2 command plane.

These tests intentionally inspect the live endpoint owners, not helper-only
services.  Behavioral database assertions live beside each ingress suite; this
file prevents a later refactor from quietly reconnecting a production caller
to the legacy RuntimeTask metadata mailbox.
"""

from __future__ import annotations

import ast
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]


def _called_names(relative_path: str, function_name: str) -> set[str]:
    source = (BACKEND / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == function_name
    )
    calls: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_legacy_rest_run_ingress_uses_canonical_human_input_adapter() -> None:
    for owner in ("create_session_run", "start_session_run"):
        calls = _called_names("app/api/chat_sessions.py", owner)
        assert "submit_live_human_input" in calls
        assert "start_web_chat_run" not in calls


def test_websocket_message_and_abort_use_canonical_input_adapters() -> None:
    calls = _called_names("app/api/websocket.py", "websocket_chat")
    assert "submit_live_human_input" in calls
    assert "submit_live_cancel_input" in calls
    assert "start_web_chat_run" not in calls
    assert "cancel_web_chat_run" not in calls


def test_session_command_steer_and_interrupt_use_canonical_input_adapters() -> None:
    steer = _called_names("app/services/session_command_runtime.py", "_handle_steer")
    interrupt = _called_names("app/services/session_command_runtime.py", "_handle_interrupt")
    assert "submit_live_human_input" in steer
    assert "steer_active_web_chat_turn" not in steer
    assert "submit_live_cancel_input" in interrupt
    assert "cancel_web_chat_run" not in interrupt

    goal_cancel = _called_names("app/api/session_goals.py", "_cancel_last_goal_run_if_active")
    assert "submit_live_cancel_input" in goal_cancel
    assert "cancel_web_chat_run" not in goal_cancel


def test_all_durable_im_ingress_enters_canonical_human_input_adapter() -> None:
    calls = _called_names("app/services/channel_agent_runtime.py", "call_agent_llm")
    assert "submit_live_human_input" in calls
    assert "start_channel_chat_run_from_saved_turn" not in calls

    resume_calls = _called_names(
        "app/services/channel_ingress_dispatcher.py",
        "_resume_materialized_user_message",
    )
    assert "submit_live_human_input" in resume_calls
    assert "start_channel_chat_run_from_saved_turn" not in resume_calls


def test_agent_team_member_run_enters_canonical_human_input_adapter() -> None:
    calls = _called_names("app/api/agent_teams.py", "start_agent_team_member_run")
    assert "submit_live_human_input" in calls
    assert "start_web_chat_run" not in calls


def test_legacy_runtime_metadata_mailbox_has_no_live_writer_or_drain() -> None:
    services = BACKEND / "app" / "services"
    owners = []
    for path in services.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "pending_user_messages" in source:
            owners.append(path.name)
    assert owners == ["session_control_plane.py"]

    runtime_source = (services / "web_chat_runtime.py").read_text(encoding="utf-8")
    continuation_source = (services / "agent_session_continuation.py").read_text(encoding="utf-8")
    assert "_claim_pending_mid_run_user_messages" not in runtime_source
    assert "_queue_saved_mid_run_user_message" not in runtime_source
    assert "_queue_mid_run_user_message" not in runtime_source
    assert "mid_run_message_drain" not in continuation_source


def test_web_chat_terminal_owner_atomically_settles_session_controls() -> None:
    source = (BACKEND / "app" / "services" / "web_chat_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct_sync_call_owners: list[str] = []
    async_owner_calls: set[str] = set()
    for function in (node for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))):
        called = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if "_apply_terminal_task_update" in called:
            direct_sync_call_owners.append(function.name)
        if function.name == "_apply_terminal_task_update_and_settle":
            async_owner_calls = called

    assert direct_sync_call_owners == ["_apply_terminal_task_update_and_settle"]
    assert "settle_pending_controls_for_run" in async_owner_calls
    assert "_apply_terminal_task_update_and_settle" in _called_names(
        "app/services/web_chat_runtime.py", "_update_runtime_task"
    )
    assert "submit_live_cancel_input" in _called_names("app/services/web_chat_runtime.py", "cancel_web_chat_run")


def test_existing_runtime_worker_periodically_recovers_session_controls() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    sweep_calls = _called_names("app/services/runtime_task_worker.py", "recover_session_control_inputs_once")
    assert "recover_session_control_inputs_once" in tick_calls
    assert "recover_stale_cancel_control_inputs_once" in sweep_calls
    assert "enter_rls_bypass" in sweep_calls


def test_existing_runtime_worker_periodically_expires_session_permissions() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    sweep_calls = _called_names("app/services/runtime_task_worker.py", "expire_session_permission_requests_once")

    assert "expire_session_permission_requests_once" in tick_calls
    assert "expire_stale_session_permission_requests" in sweep_calls
    assert "enter_rls_bypass" in sweep_calls


def test_existing_runtime_worker_recovers_sealed_rounds_and_terminal_outcomes() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    round_sweep_calls = _called_names(
        "app/services/runtime_task_worker.py",
        "recover_session_model_rounds_once",
    )
    terminal_sweep_calls = _called_names(
        "app/services/runtime_task_worker.py",
        "recover_session_terminal_outcomes_once",
    )

    assert "recover_session_model_rounds_once" in tick_calls
    assert "recover_session_terminal_outcomes_once" in tick_calls
    assert {"recover_sealed_model_rounds_once", "enter_rls_bypass"} <= round_sweep_calls
    assert {
        "recover_terminal_outcomes_once",
        "recover_terminal_candidates_once",
        "enter_rls_bypass",
    } <= terminal_sweep_calls


def test_existing_runtime_worker_owns_turn_replacement_saga_progress() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    sweep_calls = _called_names("app/services/runtime_task_worker.py", "recover_turn_replacement_sagas_once")
    starter_calls = _called_names("app/services/session_turn_replacement.py", "_start_replacement_runtime")
    ingress_calls = _called_names("app/api/chat_sessions.py", "_submit_session_human_input")

    assert "recover_turn_replacement_sagas_once" in tick_calls
    assert "recover_turn_replacements_once" in sweep_calls
    assert "enter_rls_bypass" in sweep_calls
    assert {"start_web_chat_run", "admit_replacement_run"} <= starter_calls
    assert {
        "_get_run_session_and_agent",
        "_session_permission_metadata",
        "_validate_session_input_shape",
        "dict",
        "submit_live_human_input",
    } <= ingress_calls
    assert (
        not {
            "accept_human_input",
            "run_user_prompt_admission",
            "start_web_chat_run",
            "queue_admitted_human_input",
            "request_turn_replacement",
            "dispatch_fork_side_thread",
        }
        & ingress_calls
    )


def test_existing_runtime_worker_publishes_canonical_session_event_outbox() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    drain_calls = _called_names("app/services/runtime_task_worker.py", "drain_session_event_outbox_once")
    publisher_calls = _called_names("app/services/session_event_outbox.py", "drain_once")
    redis_calls = _called_names("app/services/web_chat_stream_bus.py", "publish_canonical_session_event")
    consumer_calls = _called_names("app/services/web_chat_stream_bus.py", "_listen_web_chat_stream_once")

    assert "drain_session_event_outbox_once" in tick_calls
    assert "drain_once" in drain_calls
    assert "publish_callback" in publisher_calls
    assert "publish_canonical_session_event" in (BACKEND / "app" / "services" / "session_event_outbox.py").read_text(
        encoding="utf-8"
    )
    assert "publish" in redis_calls
    assert {"subscribe", "send_session_message"} <= consumer_calls


def test_existing_runtime_worker_recovers_stale_current_revision_admissions() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    sweep_calls = _called_names("app/services/runtime_task_worker.py", "recover_stale_session_input_admissions_once")
    hook_evidence_calls = _called_names("app/runtime/hooks.py", "_persist_hook_boundary_evidence")

    assert "recover_stale_session_input_admissions_once" in tick_calls
    assert "recover_stale_input_admissions_once" in sweep_calls
    assert "enter_rls_bypass" in sweep_calls
    assert "persist_invocation_span" in hook_evidence_calls
    hooks_source = (BACKEND / "app" / "runtime" / "hooks.py").read_text(encoding="utf-8")
    assert '"hook_run_id": hook_run_id' in hooks_source
    assert '"hook_result_payload": _json_ready(safe_result)' in hooks_source


def test_explicit_session_input_endpoint_has_one_canonical_live_adapter() -> None:
    calls = _called_names("app/api/chat_sessions.py", "_submit_session_human_input")
    assert "submit_live_human_input" in calls
    assert (
        not {
            "accept_human_input",
            "run_user_prompt_admission",
            "start_web_chat_run",
            "queue_admitted_human_input",
            "request_turn_replacement",
            "dispatch_fork_side_thread",
        }
        & calls
    )

    revision_calls = _called_names("app/api/chat_sessions.py", "revise_session_human_input")
    assert {
        "revise_unbound_human_input",
        "run_user_prompt_admission",
        "dispatch_admitted_input_fast_path",
    } <= revision_calls


def test_runtime_worker_owns_post_admission_input_dispatch() -> None:
    tick_calls = _called_names("app/services/runtime_task_worker.py", "start_runtime_task_worker_loop")
    sweep_calls = _called_names("app/services/runtime_task_worker.py", "recover_session_input_dispatches_once")

    assert "recover_session_input_dispatches_once" in tick_calls
    assert "recover_admitted_session_inputs_once" in sweep_calls
    assert "enter_rls_bypass" in sweep_calls


def test_web_and_channel_run_creation_bind_database_writer_epoch_before_flush() -> None:
    for owner in ("start_web_chat_run", "start_channel_chat_run_from_saved_turn"):
        calls = _called_names("app/services/web_chat_runtime.py", owner)
        assert "assign_runtime_task_writer_generation" in calls
