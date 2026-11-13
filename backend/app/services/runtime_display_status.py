"""Five display labels; execution states and replay rules are unchanged."""

import uuid

RUNTIME_LIMIT_MESSAGES = {
    "turn_token_budget_exhausted": "这次用完了用量预算，没有完成。",
    "tool_round_limit": "这次已达到运行轮次上限，没有完成。可以重试继续。",
}
TERMINAL_TASK_READ_PREFIX = "任务结果未变化，已停止本次处理："
DELEGATED_TASK_RUNNING_MESSAGE = "同事正在处理，结果回来后会在这里继续。"
DELEGATED_TASK_CANCELLED_MESSAGE = "已停止，同事没有完成这项工作。"
BUSINESS_DELEGATED_TASK_RUNNING_MESSAGE = "同事还在处理，这次我先停下了。同事完成后，结果会回到这项任务的对话里。"
# R18a: the loop guard's empty-search stop. True for the only input that
# produces it — several searches in one turn all returned nothing.
EMPTY_SEARCH_FINAL_MESSAGE = "多次搜索都没有找到相关资料。"
# Frozen sentences: stored result summaries and stored failure pages are compared
# against these exact bytes, so a wording change desynchronizes old records.
DELEGATION_TIMEOUT_MESSAGE = "没在时限内完成这项任务。"
TASK_FAILED_MESSAGE = "这次执行失败，没有完成。"
IDENTICAL_TERMINAL_READ_MESSAGE = "任务结果未变化，已停止本次处理。"
REFUSED_RESULT_READ_MESSAGE = "这份结果这次没法安全读取，我没能继续。你可以让我重新安排这项工作。"

_FAILURE_MESSAGES = {
    **RUNTIME_LIMIT_MESSAGES,
    "tool_budget": RUNTIME_LIMIT_MESSAGES["turn_token_budget_exhausted"],
    "provider_error": "这次调用模型失败，没有完成。",
    "task_failed": TASK_FAILED_MESSAGE,
    "task_killed": "这次任务已停止。",
    "task_skipped": "这次任务已跳过。",
    "task_needs_reconciliation": "这次任务的结果待确认。",
    "delegation_timeout": DELEGATION_TIMEOUT_MESSAGE,
}
_TERMINAL_READ_MESSAGES = frozenset(TERMINAL_TASK_READ_PREFIX + reason for reason in _FAILURE_MESSAGES.values())
_FAILURE_MESSAGES["identical_terminal_task_read"] = IDENTICAL_TERMINAL_READ_MESSAGE
_FAILURE_MESSAGES["identical_running_task_read"] = DELEGATED_TASK_RUNNING_MESSAGE
_FAILURE_MESSAGES["identical_refused_result_read"] = REFUSED_RESULT_READ_MESSAGE
# R18a round 2/3: readers of a run stopped by the empty-search guard map this
# failure code to the plain sentence — the web member card via the frontend
# failure-code mapping, the parent of a delegated colleague through
# runtime_failure_observation on the child task's top-level metadata, and the
# business member projection through the task-executor outcome summary. A
# parent reading a business RuntimeTask through runtime_failure_observation
# still gets the generic sentence: its error_code sits under
# metadata.outcome, not at the top level (pre-existing, report-only).
_FAILURE_MESSAGES["repeated_empty_search"] = EMPTY_SEARCH_FINAL_MESSAGE
# Every fixed sentence the plain delegation result page may legally carry.
_FIXED_RESULT_SENTENCES = frozenset(_FAILURE_MESSAGES.values()) | _TERMINAL_READ_MESSAGES


FAILED_DELEGATION_MODEL_INSTRUCTION = (
    "Explain the failure reason to the member in their language and offer to retry. "
    "Do not delegate again unless the member asks."
)


def runtime_failure_observation(status: str, metadata: dict, *, result_summary: str | None = None) -> dict | None:
    """Expose terminal execution facts without releasing a failed model's partial body."""
    if not isinstance(status, str):
        return None
    if status in {"killed", "skipped", "needs_reconciliation"}:
        reason = f"task_{status}"
    elif status == "failed":
        reason = metadata.get("error_code")
        if not isinstance(reason, str) or reason not in _FAILURE_MESSAGES:
            reason = metadata.get("terminal_reason")
        if not isinstance(reason, str) or reason not in _FAILURE_MESSAGES:
            # A delegation cut by its time limit has no more specific code; the
            # plain timeout sentence beats the generic task_failed wording.
            reason = "delegation_timeout" if metadata.get("timed_out") else "task_failed"
    else:
        return None
    message = _FAILURE_MESSAGES[reason]
    if reason == "identical_terminal_task_read" and isinstance(result_summary, str) and result_summary in _TERMINAL_READ_MESSAGES:
        message = result_summary  # Only fixed runtime copy, never a failed model's partial body.
    return {"ok": True, "status": status, "reason_code": reason, "reason": message,
        "retryable": reason == "tool_round_limit"}


def is_runtime_failure_observation(payload: object) -> bool:
    """Recognize only the native reader's fixed terminal-status projection."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    expected = runtime_failure_observation(payload.get("status"), {"error_code": payload.get("reason_code")},
        result_summary=payload.get("reason"))
    return expected is not None and all(payload.get(key) == value for key, value in expected.items())


DELEGATION_RESULT_READ_NOTE = (
    "The colleague's task ended without a result body; this notice is all there is to read. "
    + FAILED_DELEGATION_MODEL_INSTRUCTION
)
DELEGATION_RUNNING_RESULT_READ_NOTE = (
    "Delegated work is still running. Its result will arrive in a completion notification. "
    "Wait for that notification; do not retry or delegate again."
)


def delegation_failure_result_page(
    *,
    result_ref: str | None = None,
    task_id: str | None,
    status: str,
    metadata: dict,
    result_summary: str | None = None,
) -> dict | None:
    """Model-facing page for reading a finished delegation that has no body.

    One builder serves all four readers: ``read_runtime_result`` passes the
    caller's ``result_ref``; the task readers (``check_async_task``,
    ``task_output``, ``list_async_tasks``) pass only the ``task_id``. The parent
    model gets plain sentences derived from the same terminal fields the
    observation uses — never the internal codes (reason codes, retryable flags,
    status enums) that leak into member-facing prose when quoted.
    """
    failure = runtime_failure_observation(status, metadata, result_summary=result_summary)
    if failure is None:
        return None
    page = {
        "schema": "hive.runtime_result_page.v1",
        "result": failure["reason"],
        "note": (DELEGATION_RUNNING_RESULT_READ_NOTE if failure["reason_code"] == "identical_running_task_read"
            else DELEGATION_RESULT_READ_NOTE),
    }
    if result_ref:
        page["result_ref"] = result_ref
    if task_id:
        page["task_id"] = task_id
    return page


def is_delegation_failure_result_page(page: object) -> bool:
    """Recognize only the native readers' fixed no-body failure page.

    Like ``is_runtime_failure_observation``, this binds the exact fixed copy:
    the sentence must come from the fixed failure-message set, the note must be
    the platform constant, and a ``task_id`` must be a canonical UUID, so no
    other bytes can certify as this projection.
    """
    if not isinstance(page, dict):
        return False
    if page.get("schema") != "hive.runtime_result_page.v1":
        return False
    # Preserve recognition of stored pages while binding the new running note
    # only to its fixed running sentence.
    if page.get("note") != DELEGATION_RESULT_READ_NOTE and not (
        page.get("note") == DELEGATION_RUNNING_RESULT_READ_NOTE and page.get("result") == DELEGATED_TASK_RUNNING_MESSAGE
    ):
        return False
    if not isinstance(page.get("result"), str) or page["result"] not in _FIXED_RESULT_SENTENCES:
        return False
    if "result_ref" in page and (not isinstance(page["result_ref"], str) or not page["result_ref"]):
        return False
    if "task_id" in page:
        if not isinstance(page["task_id"], str):
            return False
        try:
            if str(uuid.UUID(page["task_id"])) != page["task_id"]:
                return False
        except ValueError:
            return False
    return set(page) in ({"schema", "result", "note"},
                         {"schema", "result_ref", "result", "note"},
                         {"schema", "result", "note", "task_id"},
                         {"schema", "result_ref", "result", "note", "task_id"})


def runtime_display_status(status: str) -> str:
    if status in {"pending", "queued", "running", "active", "resumable", "resuming", "summary_only", "waiting_for_child", "waiting_for_workflow"}:
        return "运行中"
    if status in {"suspended", "waiting_budget_approval", "awaiting_approval", "waiting_user", "blocked", "waiting_for_permission"}:
        return "等你决定"
    if status in {"completed", "succeeded", "done", "complete"}:
        return "完成"
    if status in {"killed", "cancelled", "hard_stopped", "expired", "stopped", "exhausted", "paused", "budget_limited", "usage_limited"}:
        return "已停止"
    return "失败"
