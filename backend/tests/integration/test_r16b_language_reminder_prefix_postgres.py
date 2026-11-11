"""R16b: the language reminder reaches the request tail; the frozen prefix is unchanged.

Drives the real kernel (member assistant, Chinese member message, one tool call)
so the run issues two model requests. The system message the provider receives
is the frozen prefix; the per-request tail carries § Environment. The frozen
prefix must be byte-identical across both requests — before and after R16b —
because the reminder lives only in the tail.
"""

import pytest

from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from tests.integration.test_r5e_unavailable_app_postgres import app_work as app_work
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

LANGUAGE_REMINDER = "in the member's language (Chinese if unclear)"


def _system_blocks(request: dict) -> tuple[str, str]:
    system = next(message["content"] for message in request["messages"] if message.get("role") == "system")
    # The per-request tail may be split into several context parts after the
    # "[System Notice]" header; the member's own message never contains the
    # English reminder text, so scanning all user messages is safe.
    notice = "\n".join(
        message["content"]
        for message in request["messages"]
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    )
    return system, notice


@pytest.mark.parametrize("app_work", ["帮我把这份销售数据整理成三条周报要点"], indirect=True)
async def test_language_reminder_rides_the_request_tail_and_prefix_stays_stable(
    app_work, owner_sessionmaker, monkeypatch,
):
    import app.runtime.invoker as invoker
    from app.services.web_chat_runtime import execute_web_chat_run

    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "call-list", "type": "function", "function": {
            "name": "list_files", "arguments": "{}"}}], "finish_reason": "tool_calls"},
        {"content": "好的，三条周报要点已经整理好了。"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=app_work["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(app_work["run_id"])
    finally:
        reset_runtime_task_fence(token)

    assert len(client.requests) == 2
    first_system, first_notice = _system_blocks(client.requests[0])
    second_system, second_notice = _system_blocks(client.requests[1])

    # Cache-stable prefix: byte-identical across the two requests of the run.
    assert first_system == second_system
    # The member-language rule still ships in the frozen prefix itself.
    assert "same language the user uses" in first_system
    # The new one-line reminder rides the per-request tail, never the prefix.
    assert LANGUAGE_REMINDER not in first_system
    assert LANGUAGE_REMINDER in first_notice
    assert LANGUAGE_REMINDER in second_notice
