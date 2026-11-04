"""Five display labels; execution states and replay rules are unchanged."""


def runtime_display_status(status: str) -> str:
    if status in {"pending", "queued", "running", "active", "resumable", "resuming"}:
        return "运行中"
    if status in {"suspended", "waiting_budget_approval", "awaiting_approval", "waiting_user"}:
        return "等你决定"
    if status in {"completed", "succeeded", "done"}:
        return "完成"
    if status in {"killed", "cancelled", "hard_stopped", "expired", "stopped"}:
        return "已停止"
    return "失败"
