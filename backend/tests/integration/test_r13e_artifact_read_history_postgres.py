"""Reading a delivered snapshot does not repeat work for later conversation turns."""

import json
import time
import uuid
from contextlib import aclosing
from types import SimpleNamespace

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


@pytest.fixture
async def original_request_report(native_workspace, app_user_sessionmaker, owner_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import files, upload
    from app.database import pin_rls_tenant_context
    from app.kernel.engine import _context_message
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.services import session_model_round
    from app.services.llm_client import LLMMessage
    from app.services.prompt_cache import apply_cache_hints
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_inputs import capture_context_message_origins
    from tests.integration.source_withholding_helpers import legacy_model_requests
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    n = native_workspace
    monkeypatch.setattr(files.settings, "AGENT_DATA_DIR", str(n.root.parent))
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", n.root.parent)
    n.client._transport.app.include_router(upload.router)
    response = await n.client.post(
        "/chat/upload",
        data={"agent_id": str(n.agent), "skip_personal_kb": "true"},
        files={"file": ("original.csv", b"product,sales\nAlpha,42\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    source_path = response.json()["workspace_path"]
    original_text = "Use the original reporting convention."
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.session)
        accepted = await _accepted_input(db, authority=authority, session_id=n.session, content=original_text)
        from app.runtime.hooks import HookResult

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="original-request-admission",
            hook_executor=allow,
        )
        await db.commit()

    prepare, source_read, clock = session_model_round.prepare_model_request, None, None

    async def with_original_request(db, **kwargs):
        prefix = _context_message(
            role="system", kind="frozen_prompt", content="Prepare the original report.", unbound=()
        )
        old_prefix = apply_cache_hints([LLMMessage(role="system", content=prefix.content)], "minimax")[0]
        prior = [
            LLMMessage(
                role="tool",
                content=source[0],
                tool_call_id=source[1].provider_tool_use_id,
                source_ref=f"session-tool-result:{source[1].id}",
            )
            for source in (source_read, clock)
            if source is not None
        ]
        legacy = source_read is not None and clock is None
        messages = [
            old_prefix if legacy else prefix,
            LLMMessage(role="user", content=original_text, source_ref=f"session-input:{accepted.input_id}"),
            *prior,
            *kwargs.pop("messages"),
        ]
        if legacy:
            with legacy_model_requests(monkeypatch):
                request = await prepare(db, **kwargs, messages=messages)
        else:
            request = await prepare(db, **kwargs, messages=messages)
        await capture_context_message_origins(
            db,
            tenant_id=n.tenant,
            agent_id=n.agent,
            session_id=n.session,
            run_id=n.run,
            result_id=uuid.uuid4(),
            messages=[prefix],
        )
        return request

    monkeypatch.setattr(session_model_round, "prepare_model_request", with_original_request)
    source_read = await memory_call(n, app_user_sessionmaker, "read_file", {"path": source_path})
    assert "Alpha,42" in source_read[0], source_read
    # Clock receipts re-expand their model inputs at read time; file-read
    # receipts alone retain a complete flattened inventory and miss this path.
    clock = await memory_call(n, app_user_sessionmaker, "get_current_time", {})
    path, body = "workspace/original-report.md", "# Original report\nSales total: 42.\n"
    output, invocation = await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body})
    async with owner_sessionmaker() as db:
        artifacts = list(
            await db.scalars(
                select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run, ChatArtifact.path == path)
            )
        )
        assert len(artifacts) == 1, output
        artifact = artifacts[0]
        assert artifact.snapshot_json["content_input_receipt_id"], output
        result = (
            await db.scalars(
                select(SessionModelResult).where(
                    SessionModelResult.provider_request_id == invocation.provider_request_id
                )
            )
        ).one()
        assert result.model_request_snapshot_json["input_bindings"]["dependency_coverage"] == {"complete": True}
        original_model = (
            await db.scalars(
                select(SessionModelResult).where(SessionModelResult.provider_request_id == clock[1].provider_request_id)
            )
        ).one()
        snapshot = original_model.model_request_snapshot_json
        assert snapshot["input_bindings"]["dependency_coverage"] == {"complete": False}
        selected = next(
            entry
            for entry in snapshot["input_bindings"]["messages"]
            if entry.get("source_ref") == f"session-input:{accepted.input_id}"
        )
        assert snapshot["wire_request"]["messages"][selected["message_index"]]["content"] == original_text
        row = await db.get(SessionTurnInput, accepted.input_id)
        assert row.status == "accepted" and row.bound_round_id is None
    return SimpleNamespace(
        n=n,
        artifact=artifact,
        body=body,
        input_id=accepted.input_id,
        original_text=original_text,
        source_path=source_path,
    )


async def _assert_original_read_and_live_leaf_revocation(h, owner_sessionmaker):
    from app.models.workspace_resource import WorkspaceResourceManifest

    responses = {
        endpoint: await h.n.client.get(f"/agents/{h.n.agent}/files/artifacts/{h.artifact.id}/{endpoint}")
        for endpoint in ("download", "content")
    }
    for endpoint, response in responses.items():
        print("R13E_ORIGINAL_REQUEST_READ", endpoint, response.status_code, flush=True)
    async with owner_sessionmaker() as db:
        leaf = (
            await db.scalars(
                select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == h.n.agent, WorkspaceResourceManifest.path == h.source_path
                )
            )
        ).one()
        leaf.owner_user_id, leaf.root_session_id = h.n.foreign, None
        await db.commit()
    denied_responses = {}
    for endpoint in responses:
        denied = await h.n.client.get(f"/agents/{h.n.agent}/files/artifacts/{h.artifact.id}/{endpoint}")
        denied_responses[endpoint] = denied
        print("R13E_ORIGINAL_REQUEST_REVOKED", endpoint, denied.status_code, flush=True)
    # Run revocation checks even on the baseline where the earlier read fails closed.
    for endpoint, response in responses.items():
        assert response.status_code == 200, response.text
        assert (response.content if endpoint == "download" else response.json()["content"].encode()) == h.body.encode()
    for denied in denied_responses.values():
        assert denied.status_code == 403 and h.body not in denied.text, denied.text


async def test_original_request_read_ignores_later_unrelated_history_failure_and_checks_live_leaf(
    original_request_report,
    app_user_sessionmaker,
    owner_sessionmaker,
):
    """Accepted change: only history actually contained in the original request affects its file read."""
    from app.database import pin_rls_tenant_context
    from app.models.session_v2 import SessionModelResult
    from app.services.session_semantic_history import SessionSemanticHistoryUnavailable, load_session_semantic_history

    h = original_request_report
    _, later = await memory_call(h.n, app_user_sessionmaker, "get_current_time", {})
    async with owner_sessionmaker() as db:
        result = (
            await db.scalars(
                select(SessionModelResult).where(SessionModelResult.provider_request_id == later.provider_request_id)
            )
        ).one()
        result.state, result.seal_json = "failed", None
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, h.n.tenant)
        with pytest.raises(SessionSemanticHistoryUnavailable) as unavailable:
            await load_session_semantic_history(db, tenant_id=h.n.tenant, agent_id=h.n.agent, session_id=h.n.session)
        assert unavailable.value.code == "committed_model_seal_unavailable"
    await _assert_original_read_and_live_leaf_revocation(h, owner_sessionmaker)


async def test_original_request_read_keeps_pre_revision_unbound_input_bytes_and_checks_live_leaf(
    original_request_report,
    app_user_sessionmaker,
    owner_sessionmaker,
):
    """Accepted change: reconstruct the pre-revision bytes cited while the input was accepted but unbound."""
    from app.database import pin_rls_tenant_context
    from app.services.session_human_input import revise_unbound_human_input
    from app.services.session_semantic_history import load_session_semantic_history
    from tests.services.test_session_input_control_v2 import _authority

    h = original_request_report
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, h.n.tenant)
        authority = await _authority(db, user_id=h.n.user, agent_id=h.n.agent, session_id=h.n.session)
        revised = await revise_unbound_human_input(
            db,
            authority=authority,
            input_id=h.input_id,
            expected_revision=1,
            content_parts=[{"type": "text", "text": "Use the revised reporting convention."}],
        )
        assert revised.revision == 2
        history = await load_session_semantic_history(
            db, tenant_id=h.n.tenant, agent_id=h.n.agent, session_id=h.n.session
        )
        assert next(m.content for m in history.messages if m.id == f"session-input:{h.input_id}") == (
            "Use the revised reporting convention."
        )
        await db.commit()
    await _assert_original_read_and_live_leaf_revocation(h, owner_sessionmaker)


async def test_external_witness_validates_with_nested_asyncpg_queries_while_stream_is_open(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.database import pin_rls_tenant_context
    from app.kernel.engine import _context_message
    from app.services.historical_model_inputs import _complete_origins
    from app.services.session_model_inputs import capture_context_message_origins
    from tests.integration.test_external_context_origins_postgres import _external_run

    tenant, agent, session, run, principal = await _external_run(owner_sessionmaker)
    context = _context_message(
        role="system", kind="frozen_prompt", content="Original external compiler context", unbound=()
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        await capture_context_message_origins(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            result_id=uuid.uuid4(),
            messages=[context],
        )
        # Leave more than one fetch batch in the server cursor when the first
        # external witness triggers its nested identity queries.
        await capture_context_message_origins(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            result_id=uuid.uuid4(),
            messages=[
                _context_message(
                    role="system", kind="frozen_prompt", content=f"Later external prefix {index}", unbound=()
                )
                for index in range(32)
            ],
        )
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert db.bind.dialect.driver == "asyncpg"
        stream_scalars, streams, nested = db.stream_scalars, [], []

        async def observe_stream(*args, **kwargs):
            rows = await stream_scalars(*args, **kwargs)
            streams.append(rows)
            return rows

        def executed(_connection, _cursor, statement, _parameters, _context, _many):
            if streams and not streams[0].closed:
                nested.append(statement)

        monkeypatch.setattr(db, "stream_scalars", observe_stream)
        event.listen(db.bind.sync_engine, "after_cursor_execute", executed)
        try:
            async with aclosing(
                _complete_origins(db, tenant_id=tenant, agent_id=agent, session_id=session, kind="frozen_prompt")
            ) as origins:
                ref, proof = await anext(origins)
                assert ref == context.source_ref
                assert proof["message"] == {"role": "system", "content": context.content}
                assert proof["requester_external_principal_id"] == str(principal)
                assert streams and not streams[0].closed
        finally:
            event.remove(db.bind.sync_engine, "after_cursor_execute", executed)
        assert streams[0].closed
        for table in ("chat_sessions", "runtime_tasks", "external_principals", "channel_configs"):
            assert any(table in statement for statement in nested), nested
        print("R13E_STREAM_NESTED", json.dumps({"driver": "asyncpg", "nested_statements": len(nested)}), flush=True)
