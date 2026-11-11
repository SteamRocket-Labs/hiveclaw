"""Five display labels; execution states and replay rules are unchanged."""


RUNTIME_LIMIT_MESSAGES = {
    "turn_token_budget_exhausted": "这次用完了用量预算，没有完成。",
    "tool_round_limit": "这次已达到运行轮次上限，没有完成。可以重试继续。",
}
TERMINAL_TASK_READ_PREFIX = "任务结果未变化，已停止本次处理："
DELEGATED_TASK_RUNNING_MESSAGE = "同事正在处理，结果回来后会在这里继续。"

_FAILURE_MESSAGES = {
    **RUNTIME_LIMIT_MESSAGES,
    "tool_budget": RUNTIME_LIMIT_MESSAGES["turn_token_budget_exhausted"],
    "provider_error": "这次调用模型失败，没有完成。",
    "task_failed": "这次执行失败，没有完成。",
    "task_killed": "这次任务已停止。",
    "task_skipped": "这次任务已跳过。",
    "task_needs_reconciliation": "这次任务的结果待确认。",
    "delegation_timeout": "没在时限内完成这项任务。",
}
_TERMINAL_READ_MESSAGES = frozenset(TERMINAL_TASK_READ_PREFIX + reason for reason in _FAILURE_MESSAGES.values())
_FAILURE_MESSAGES["identical_terminal_task_read"] = "任务结果未变化，已停止本次处理。"
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


DELEGATION_RESULT_READ_NOTE = "The colleague's task ended without a result body; this notice is all there is to read."


def delegation_failure_result_page(
    *,
    result_ref: str,
    task_id: str | None,
    status: str,
    metadata: dict,
    result_summary: str | None = None,
) -> dict | None:
    """Model-facing page for reading a finished delegation that has no body.

    The parent model gets plain sentences derived from the same terminal fields
    the observation uses — never the internal codes (reason codes, retryable
    flags, status enums) that leak into member-facing prose when quoted.
    """
    failure = runtime_failure_observation(status, metadata, result_summary=result_summary)
    if failure is None:
        return None
    page = {
        "schema": "hive.runtime_result_page.v1",
        "result_ref": result_ref,
        "result": failure["reason"],
        "note": DELEGATION_RESULT_READ_NOTE,
    }
    if task_id:
        page["task_id"] = task_id
    return page


def is_delegation_failure_result_page(page: object) -> bool:
    """Recognize only the native reader's fixed no-body failure page.

    Like ``is_runtime_failure_observation``, this binds the exact fixed copy:
    the sentence must come from the fixed failure-message set and the note must
    be the platform constant, so no other bytes can certify as this projection.
    """
    if not isinstance(page, dict):
        return False
    if page.get("note") != DELEGATION_RESULT_READ_NOTE or page.get("schema") != "hive.runtime_result_page.v1":
        return False
    if page.get("result") not in _FIXED_RESULT_SENTENCES:
        return False
    if not isinstance(page.get("result_ref"), str) or not page["result_ref"]:
        return False
    return set(page) in ({"schema", "result_ref", "result", "note"}, {"schema", "result_ref", "result", "note", "task_id"})


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
