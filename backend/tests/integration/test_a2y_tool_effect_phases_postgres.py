"""Measure native effect-to-result work with the Server-Timing cursor counter."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from time import perf_counter
import uuid

import pytest

from app.core.request_timing import RequestTiming, install_cursor_timing, request_timing
from app.database import pin_rls_tenant_context
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    native_workspace, _admitted_write_messages, _prepare_write,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
_active = ContextVar("a2y_measurement", default=None)


class Phases:
    def __init__(self):
        self.rows = {}
        self.stack = []

    @contextmanager
    def phase(self, name):
        timing = RequestTiming()
        token = request_timing.set(timing)
        frame = [perf_counter(), 0.0]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = perf_counter() - frame[0]
            self.stack.pop()
            if self.stack:
                self.stack[-1][1] += elapsed
            row = self.rows.setdefault(name, dict(seconds=0.0, statements=0, db_seconds=0.0, calls=0))
            row["seconds"] += elapsed - frame[1]
            row["statements"] += timing.statements
            row["db_seconds"] += timing.db_seconds
            row["calls"] += 1
            request_timing.reset(token)


@pytest.fixture
def phases(monkeypatch):
    install_cursor_timing()
    from app.services import content_input_receipts as receipts
    from app.services import workspace_resource_authority as authority
    from app.services import workspace_receipt_versions as versions
    from app.tools import adapters

    def instrument(module, symbol, name):
        original = getattr(module, symbol)
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def wrapped(*args, **kwargs):
                clock = _active.get()
                if clock is None:
                    return await original(*args, **kwargs)
                with clock.phase(name):
                    return await original(*args, **kwargs)
        else:
            @wraps(original)
            def wrapped(*args, **kwargs):
                clock = _active.get()
                if clock is None:
                    return original(*args, **kwargs)
                with clock.phase(name):
                    return original(*args, **kwargs)
        monkeypatch.setattr(module, symbol, wrapped)

    for symbol in ("authorize_workspace_receipt_read", "authorize_workspace_execution_inputs"):
        instrument(receipts, symbol, "source_validation")
    instrument(receipts, "persist_workspace_execution_evidence", "execution_evidence")
    instrument(receipts, "record_native_workspace_receipts", "receipt_write")
    instrument(versions, "capture_receipt_version", "version_fsync")
    instrument(authority, "record_workspace_tool_mutations", "manifest_write")
    instrument(adapters, "adapt_and_call", "adapter")
    return Phases


async def seed_workspace(n, factory, files, versions):
    from app.models.capability_policy import CapabilityPolicy
    from app.services.content_input_receipts import record_workspace_receipt
    from app.services.workspace_receipt_versions import capture_receipt_version
    from app.services.workspace_resource_authority import register_workspace_path

    async with factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.code.execute",
            allowed=True, requires_approval=False))
        n.sources = []
        for index in [*range(1, files), 0]:
            path = "workspace/report.md" if index == 0 else f"workspace/input-{index:02}.md"
            previous = None
            for revision in range(versions if index == 0 else 1):
                body = f"# Weekly report\nRevision {revision}\nRevenue: 42\n"
                (n.root / path).write_text(body)
                digest = hashlib.sha256(body.encode()).hexdigest()
                dependencies = [*n.sources] if index == 0 else []
                if previous is not None:
                    dependencies.append(dict(source_kind="workspace_artifact", source_id=str(previous.id),
                        source_version=previous.content_sha256))
                previous = record_workspace_receipt(db, n.tenant, n.agent, path, digest, previous,
                    additional_dependencies=dependencies)
                await db.flush()
                capture_receipt_version(n.agent, path, previous)
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=digest)
            n.sources.append(dict(source_kind="workspace_artifact", source_id=str(previous.id),
                source_version=previous.content_sha256))
        await db.commit()


async def measured_call(n, factory, clock_type, name, arguments, round_index):
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import mark_tool_effect_started
    from app.services.web_chat_runtime import _persist_tool_call
    from app.kernel.engine import _context_message
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_semantic_history import _event_payload

    async with factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        messages = await _admitted_write_messages(db, n, round_index=round_index)
        messages.append(_context_message(role="system", kind="dynamic_prompt", content="Authorized report inputs",
            sources=n.sources, unbound=()))
        invocation, arguments, turn = await _prepare_write(db, n, messages=messages,
            tool_name=name, arguments=arguments, round_index=round_index)
    clock = clock_type()
    trace = {}
    boundary = []

    async def effect(_payload):
        async with factory() as db:
            await pin_rls_tenant_context(db, n.tenant)
            await mark_tool_effect_started(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=n.session, invocation_id=invocation.id, effective_arguments=arguments)
            await db.commit()
        boundary.append(perf_counter())
        timing = request_timing.get()
        boundary.extend((sum(row["statements"] for row in clock.rows.values()) + timing.statements,
            sum(row["db_seconds"] for row in clock.rows.values()) + timing.db_seconds))

    token = _active.set(clock)
    start = perf_counter()
    try:
        with clock.phase("dispatch_and_hooks"):
            raw = await n.service.execute(name, arguments, agent_id=n.agent, user_id=n.user,
                session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
                tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False,
                trace_metadata_sink=trace, pre_effect_callback=effect)
        with clock.phase("result_transcript_ledger"):
            await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session),
                data={"name": name, "args": arguments, "status": "done", "result": str(raw),
                    "tool_call_id": invocation.provider_tool_use_id,
                    "provider_request_id": invocation.provider_request_id,
                    "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    finally:
        _active.reset(token)
    assert len(boundary) == 3, (name, str(raw), trace)
    before = boundary[0] - start
    clock.rows["dispatch_and_hooks"]["seconds"] -= before
    clock.rows["dispatch_and_hooks"]["statements"] -= boundary[1]
    clock.rows["dispatch_and_hooks"]["db_seconds"] -= boundary[2]
    clock.rows["before_effect"] = dict(seconds=before, statements=boundary[1], db_seconds=boundary[2], calls=1)
    row = {"tool": name, "seconds": perf_counter() - start,
        "before_effect": before, "effect_seconds": perf_counter() - boundary[0],
        "effect_statements": sum(p["statements"] for p in clock.rows.values()) - boundary[1],
        "statements": sum(p["statements"] for p in clock.rows.values()), "phases": clock.rows,
        "result": str(raw), "evidence": _execution_evidence(trace)}
    async with factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        settled = await db.get(SessionToolInvocation, invocation.id)
        event = await db.get(ChatTranscriptEvent, settled.result_event_id)
        assert event.event_type == "tool_result.completed"
        row["outcome"] = _event_payload(event)["outcome"]
        if name != "execute_code":
            assert row["outcome"] == "success", row
        else:
            row["subprocess"] = trace["code_execution_result"]
    row["invocation_id"] = str(invocation.id)
    return row


@pytest.mark.parametrize("files,versions", [(2, 1), (57, 1), (57, 9)])
async def test_native_tool_effect_phases(native_workspace, app_user_sessionmaker, phases, files, versions):  # noqa: F811
    n = native_workspace
    await seed_workspace(n, app_user_sessionmaker, files, versions)
    rows = []
    calls = [("write_file", {"path": "workspace/new.md", "content": "New report\n"}),
        ("edit_file", {"path": "workspace/report.md", "old_text": "Revenue: 42", "new_text": "Revenue: 43"}),
        ("execute_code", {"language": "python", "code": "from pathlib import Path\nprint(Path('report.md').read_text(), Path('input-01.md').read_text())"}),
        ("read_file", {"path": "workspace/report.md"}), ("list_files", {"path": "workspace"})]
    for index, (name, arguments) in enumerate(calls, start=1):
        row = await measured_call(n, app_user_sessionmaker, phases, name, arguments, index)
        row.update(files=files, versions=versions)
        rows.append(row)
        print("A2Y_MEASURE", json.dumps(row), flush=True)
    if os.environ.get("FULL_RUN"):
        directory = Path(os.environ["FULL_RUN"]) / "evidence"
        (directory / f"{sys.argv[1]}-{files}-{versions}.json").write_text(json.dumps(rows, indent=2))
    assert (n.root / "workspace/new.md").read_text() == "New report\n"
    assert "Revenue: 43" in (n.root / "workspace/report.md").read_text()
    # Exact cursor counts are the bounded-cost contract: neither the workspace
    # source count nor the report's immutable ancestry adds round trips.
    for row in rows:
        if row["tool"] in {"write_file", "edit_file"}:
            assert row["effect_seconds"] <= 3, row
    # owner 11-18 decision A: private cards skip seven source-validation statements.
    assert {row["tool"]: row["effect_statements"] for row in rows
        if row["tool"] in {"write_file", "edit_file"}} == {"write_file": 53, "edit_file": 53}
    from sqlalchemy import select
    from app.models.organization_identity import ContentInputReceipt
    from app.models.chat_artifact import ChatArtifact
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        for row, path in zip(rows, ("workspace/new.md", "workspace/report.md")):
            receipt = await db.get(ContentInputReceipt, uuid.uuid5(
                uuid.UUID(row["invocation_id"]), f"workspace-content:{path}"))
            assert receipt.revision == (1 if row["tool"] == "write_file" else versions + 1)
            assert all(source in receipt.dependencies_json for source in n.sources)
            artifact = (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent,
                ChatArtifact.path == path))).one()
            assert artifact.snapshot_json["content_input_receipt_id"] == str(receipt.id)
