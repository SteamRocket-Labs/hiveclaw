"""Recovery-source storage failure must not falsify a committed tool outcome."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from app.runtime.session import SessionContext
from app.services.delegation_session_runtime import DelegationSessionRuntime


async def test_recovery_capture_failure_keeps_the_tool_receipt_and_marks_sources_unknown(monkeypatch):
    from app.services import delegation_context_sources, web_chat_runtime

    persisted = AsyncMock()
    monkeypatch.setattr(web_chat_runtime, "_persist_tool_call", persisted)
    monkeypatch.setattr(delegation_context_sources, "capture_native_recovery_context",
        AsyncMock(side_effect=RuntimeError("source storage unavailable")))
    session = SessionContext(session_id=str(uuid4()))
    session.metadata["_native_recovery_checkpoint"] = {
        "authority_hash": "a" * 64, "manifest_hash": "b" * 64, "session_id": session.session_id}

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))

    runtime = DelegationSessionRuntime(scope={"agent_id": uuid4(), "session_id": session.session_id,
        "run_id": uuid4()}, attempt_owner="owner:1:1", session_context=session)
    monkeypatch.setattr(runtime, "transaction", transaction)
    await runtime.tool_call({"name": "ask_user_question", "status": "done", "tool_call_id": "question"})
    persisted.assert_awaited_once()
    source = session.metadata["_native_recovery_context_source"]
    assert source.sources == () and source.unbound == ("native_recovery_capture_unavailable",)
