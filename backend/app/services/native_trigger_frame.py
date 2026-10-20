"""Recover the scheduled execution frame from its canonical input command."""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.session_v2 import SessionCommand
from app.services.trigger_input_authority import load_trigger_input_origin


@dataclass(frozen=True, slots=True)
class NativeTriggerFrame:
    model_id: uuid.UUID | None
    allowed_tools: tuple[str, ...]
    excluded_tools: tuple[str, ...]

    def allowed_with(self, session_tools):
        if not session_tools:
            return self.allowed_tools
        if not self.allowed_tools:
            return tuple(session_tools)
        return tuple(name for name in session_tools if name in self.allowed_tools)

    def permits(self, tool_name):
        return (not self.allowed_tools or tool_name in self.allowed_tools) and tool_name not in self.excluded_tools


async def load_native_trigger_frame(db, *, task, session, for_effect=True, effect="runtime_start"):
    try:
        return await _load_native_trigger_frame(db, task=task, session=session, for_effect=for_effect)
    except HTTPException as exc:
        if task is None:
            raise
        code = exc.detail.get("code", "native_trigger_origin_unavailable") if isinstance(exc.detail, dict) else "native_trigger_origin_unavailable"
        raise ExecutionAuthorityUnavailable(code=code, task=task, effect=effect,
            status="denied" if exc.status_code == 403 else "unavailable") from exc


async def _load_native_trigger_frame(db, *, task, session, for_effect):
    if task is None:
        raise HTTPException(409, detail={"code": "native_trigger_origin_unavailable"})
    metadata = task.metadata_json or {}
    origin = metadata.get("trigger_origin")
    if origin is None and metadata.get("source") not in {"loop_same_session", "trigger_reflection"}:
        return None
    if not isinstance(origin, dict) or session is None:
        raise HTTPException(409, detail={"code": "native_trigger_origin_unavailable"})
    actual = await load_trigger_input_origin(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
        session=session, metadata={"source": origin.get("source", "loop_same_session"),
            "trigger_runtime_task_id": origin.get("wrapper_id"), "session_v2_input_id": origin.get("input_id"),
            "session_v2_command_id": origin.get("command_id")}, for_effect=for_effect)
    if (actual != origin or str(task.root_user_id) != actual["requester_user_id"]
        or str(task.parent_session_id) != actual["session_id"] or str(task.budget_run_id) != actual["budget_run_id"]
        or str(task.root_runtime_task_id) != actual["root_runtime_task_id"]
        or task.root_external_principal_id is not None or task.machine_credential_id is not None):
        raise HTTPException(409, detail={"code": "native_trigger_origin_unavailable"})
    command = await db.get(SessionCommand, uuid.UUID(actual["command_id"]))
    options = command.target_json["runtime_metadata"].get("native_trigger_options") or {}
    if options.get("context_from"):
        # Cross-session evidence needs the semantic source adapter, not copied
        # plaintext promoted to a user-authored command.
        raise HTTPException(409, detail={"code": "native_trigger_context_unavailable"})
    model_id = options.get("model_id")
    return NativeTriggerFrame(model_id=uuid.UUID(model_id) if model_id else None,
        allowed_tools=tuple(options.get("allowed_tool_names") or ()),
        excluded_tools=tuple(options.get("excluded_tool_names") or ()))
