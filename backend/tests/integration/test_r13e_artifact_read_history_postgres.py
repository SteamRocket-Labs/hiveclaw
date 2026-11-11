"""Reading a delivered snapshot does not repeat work for later conversation turns."""

import json
import time
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.chat_artifact import ChatArtifact
from app.models.runtime_task import RuntimeTask
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_delivered_snapshot_queries_do_not_grow_with_later_runs(
    native_workspace,  # noqa: F811
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.api import files
    from app.kernel.engine import _context_message
    from app.services import session_model_round
    from app.services.llm_client import LLMMessage
    from app.services.prompt_cache import apply_cache_hints
    from app.services.session_model_inputs import capture_context_message_origins
    from tests.integration.source_withholding_helpers import legacy_model_requests

    n = native_workspace
    monkeypatch.setattr(files.settings, "AGENT_DATA_DIR", str(n.root.parent))
    prepare, clock = session_model_round.prepare_model_request, None
    first_request = True

    async def with_compiler_context(db, **kwargs):
        nonlocal first_request
        prefix = "Use authorized inputs to prepare the report."
        context = _context_message(role="system", kind="frozen_prompt", content=prefix, unbound=())
        messages = kwargs.pop("messages")
        if first_request:
            first_request = False
            # A retained pre-fix cache clone lost its context origin. The
            # independent compiler witness recovers those exact prefix bytes.
            old = apply_cache_hints([LLMMessage(role="system", content=prefix)], "minimax")[0]
            with legacy_model_requests(monkeypatch):
                request = await prepare(db, **kwargs, messages=[old, *messages])
            await capture_context_message_origins(
                db,
                tenant_id=n.tenant,
                agent_id=n.agent,
                session_id=n.session,
                run_id=n.run,
                result_id=uuid.uuid4(),
                messages=[context],
            )
            return request
        prior = (
            [
                LLMMessage(
                    role="tool",
                    content=clock[0],
                    tool_call_id=clock[1].provider_tool_use_id,
                    source_ref=f"session-tool-result:{clock[1].id}",
                )
            ]
            if clock is not None and kwargs["run_id"] == n.run
            else []
        )
        return await prepare(db, **kwargs, messages=[context, *prior, *messages])

    monkeypatch.setattr(session_model_round, "prepare_model_request", with_compiler_context)
    clock = await memory_call(n, app_user_sessionmaker, "get_current_time", {}, command="Check the report time")
    path = "workspace/delivered-report.md"
    body = "# Delivered report\n" + "Sales total: 42.\n" * 190
    output, _ = await memory_call(
        n, app_user_sessionmaker, "write_file", {"path": path, "content": body}, command="Write and deliver the report"
    )
    async with owner_sessionmaker() as db:
        artifact = (
            await db.scalars(
                select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run, ChatArtifact.path == path)
            )
        ).one()
        assert artifact.snapshot_json["content_input_receipt_id"], output
        assert artifact.snapshot_json["snapshot_storage_path"], output
        artifact_id = artifact.id

    measurements = {}
    previous_run = n.run
    for index in range(1, 31):
        run = uuid.uuid4()
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTask, previous_run)).status = "completed"
            db.add(
                RuntimeTask(
                    id=run,
                    tenant_id=n.tenant,
                    task_type="web_chat_turn",
                    status="running",
                    parent_agent_id=n.agent,
                    child_agent_id=n.agent,
                    parent_session_id=str(n.session),
                    child_session_id=str(n.session),
                    root_user_id=n.user,
                    root_session_id=str(n.session),
                    root_runtime_task_id=run,
                    prompt=f"Revise the report for step {index}",
                    metadata_json={"turn_id": f"turn-{run.hex}"},
                )
            )
            await db.commit()
        current_body = f"# Revision {index}\nSales total: {42 + index}\n"
        output, _ = await memory_call(
            n,
            app_user_sessionmaker,
            "write_file",
            {"path": path, "content": current_body},
            run=run,
            command=f"Revise the report for step {index}",
        )
        assert n.root.joinpath(path).read_text() == current_body, output
        previous_run = run
        if index not in {3, 30}:
            continue
        measurements[index] = {}
        for endpoint in ("download", "content"):
            statements = []

            def executed(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            started = time.perf_counter()
            event.listen(Engine, "after_cursor_execute", executed)
            try:
                response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact_id}/{endpoint}")
            finally:
                event.remove(Engine, "after_cursor_execute", executed)
            row = {
                "N": index,
                "endpoint": endpoint,
                "statements": len(statements),
                "seconds": round(time.perf_counter() - started, 4),
            }
            measurements[index][endpoint] = row
            print("R13E_ARTIFACT_READ", json.dumps(row), flush=True)
            assert response.status_code == 200, response.text
            assert (
                response.content if endpoint == "download" else response.json()["content"].encode()
            ) == body.encode()
    for key in measurements[3]:
        assert measurements[30][key]["statements"] <= measurements[3][key]["statements"] + 5, measurements
