"""Unselected same-tenant history and G0 plan evidence for A2w."""

import json
import time
import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID

from app.database import tenant_scoped_session


LAB_ROWS = {"invocation_spans": 98_840, "chat_transcript_events": 176_658,
    "session_tool_invocations": 4_314, "session_model_results": 3_095,
    "runtime_result_objects": 657, "runtime_result_integration_pages": 619,
    "session_commands": 677, "content_input_receipts": 146}


async def add_unrelated_history(n, owner, multiplier):
    """Clone real row shapes into an unreachable session/run, never a new source."""
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.invocation_span import InvocationSpan
    from app.models.organization_identity import ContentInputReceipt
    from app.models.runtime_result import RuntimeResultObject, RuntimeResultIntegrationPage
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation

    seed = f"a2w-history-{n.tenant}-{multiplier}"
    session, run = uuid.uuid4(), uuid.uuid4()
    counts = {table: count * multiplier for table, count in LAB_ROWS.items()}
    turn = f"turn-{run.hex}"
    params = {"tenant": n.tenant, "agent": n.agent, "session": session, "run": run, "seed": seed, "turn": turn}
    identity = lambda table, index="g.i": f"md5(CAST(:seed AS text)||':{table}:'||({index})::text)::uuid"
    request = lambda index="g.i": f"CAST(:seed AS text)||':request:'||({index})::text"
    model_index = f"((g.i-1)%{counts['session_model_results']}+1)"
    started = time.perf_counter()
    async with tenant_scoped_session(n.tenant, session_factory=owner) as db:
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            title="Unrelated operations audit history"))
        db.add(RuntimeTask(id=run, tenant_id=n.tenant, task_type="web_chat_turn", status="completed",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(session),
            child_session_id=str(session), root_session_id=str(session), root_user_id=n.user,
            root_runtime_task_id=run, prompt="Unrelated archived operations", metadata_json={"turn_id": turn}))
        await db.flush()

        async def clone(model, changes, *, condition="true", median=None, first=1, last=None):
            table = model.__tablename__
            last = counts[table] if last is None else last
            columns = [column.name for column in model.__table__.columns]
            assert set(changes).issubset(columns), (table, set(changes) - set(columns))
            before = await db.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"), params)
            order = ""
            if median:
                # A middle-sized real payload avoids both empty metadata and a
                # largest-payload fixture multiplied into hundreds of gigabytes.
                order = f"ORDER BY octet_length({median}::text),id OFFSET (SELECT count(*)/2 FROM {table} " \
                    f"WHERE tenant_id=:tenant AND {condition})"
            selection = f"SELECT * FROM {table} WHERE tenant_id=:tenant AND {condition} {order} LIMIT 1"
            fields = [changes.get(column, "t." + column) for column in columns]
            statement = text(f"WITH template AS MATERIALIZED ({selection}) INSERT INTO {table} "
                f"({','.join(columns)}) SELECT {','.join(fields)} FROM template t "
                f"CROSS JOIN generate_series({first},{last}) AS g(i)")
            # The same UUID appears in UUID columns and text/JSON projections.
            # Keep its driver type explicit rather than asking PG to infer both.
            statement = statement.bindparams(*(bindparam(key, type_=UUID(as_uuid=True))
                for key in ("tenant", "agent", "session", "run") if key in statement._bindparams))
            await db.execute(statement, params)
            after = await db.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"), params)
            assert after - before == last-first+1, (table, before, after, first, last)

        await clone(SessionModelResult, {"id": identity("session_model_results"), "session_id": ":session",
            "run_id": ":run", "round_id": "'history-round-'||g.i::text", "turn_id": ":turn",
            "provider_request_id": request(), "bound_input_ids_json": "'[]'::jsonb", "round_committed_event_id": "NULL"},
            condition="state IN ('sealed','round_committed')")
        await clone(SessionToolInvocation, {"id": identity("session_tool_invocations"), "session_id": ":session",
            "run_id": ":run", "round_id": f"'history-round-'||{model_index}::text",
            "provider_request_id": request(model_index), "provider_tool_use_id": "'history-tool-'||g.i::text",
            "invocation_item_id": identity("tool_items"), "effect_idempotency_key": "CAST(:seed AS text)||':effect:'||g.i::text",
            "result_event_id": "NULL"}, condition="result_event_id IS NOT NULL")
        event = {"id": identity("chat_transcript_events"), "agent_id": ":agent", "session_id": ":session",
            "run_id": ":run", "root_session_id": ":session", "parent_session_id": "NULL",
            "parent_event_id": "NULL", "message_id": "NULL", "command_id": "NULL", "input_id": "NULL",
            "result_id": "NULL", "sequence": "g.i", "item_id": identity("event_items"), "turn_id": ":turn",
            "scope_json": "jsonb_build_object('level','round','session_id',CAST(:session AS text),"
                "'thread_id',CAST(:session AS text),'turn_id',CAST(:turn AS text),'run_id',CAST(:run AS text),"
                f"'round_id','history-round-'||{model_index}::text)"}
        await clone(ChatTranscriptEvent, {**event, "invocation_id": identity("session_tool_invocations"),
            "provider_tool_use_id": "'history-tool-'||g.i::text"},
            condition="schema_version=2 AND item_kind='tool_result' AND lifecycle='completed'",
            median="metadata_json", last=counts["session_tool_invocations"])
        await clone(ChatTranscriptEvent, {**event, "invocation_id": "NULL", "provider_tool_use_id": "NULL"},
            condition="schema_version=2 AND invocation_id IS NULL AND scope_json->>'level'='round' "
                "AND item_kind IN ('assistant_text','assistant_final','assistant_commentary')",
            median="metadata_json", first=counts["session_tool_invocations"]+1)
        # Both IDs come from the same series. Match their existing PKs instead
        # of joining every unrelated event before the post-seed ANALYZE.
        await db.execute(text("UPDATE session_tool_invocations t SET result_event_id="
            f"{identity('chat_transcript_events')} FROM generate_series(1,{counts['session_tool_invocations']}) g(i) "
            f"WHERE t.id={identity('session_tool_invocations')} AND t.tenant_id=:tenant AND t.run_id=:run"), params)
        await clone(InvocationSpan, {"id": identity("invocation_spans"), "runtime_task_id": ":run",
            "session_id": "CAST(:session AS text)", "trace_id": "CAST(:seed AS text)",
            "span_id": "'history-'||g.i::text"},
            condition="span_type NOT IN ('workspace_output','remote_action')", median="metadata_json")
        await clone(SessionCommand, {"id": identity("session_commands"), "session_id": ":session",
            "idempotency_key": "CAST(:seed AS text)||':command:'||g.i::text", "causation_command_id": "NULL"})
        await clone(RuntimeResultObject, {"id": identity("runtime_result_objects"),
            "source_run_id": f"({identity('result_runs')})::text"})
        await clone(RuntimeResultIntegrationPage, {"id": identity("runtime_result_integration_pages"),
            "parent_session_id": ":session", "root_runtime_task_id": ":run", "integration_epoch": "g.i",
            "mailbox_sequence_start": "g.i", "mailbox_sequence_end": "g.i+t.mailbox_sequence_end-t.mailbox_sequence_start"})
        # These receipts have no explicit-entry files or manifest references.
        # Workspace remains the same 57 receipted files, and their latest
        # versions must not be shadowed by unrelated history.
        await clone(ContentInputReceipt, {"id": identity("content_input_receipts"), "revision": "1",
            "carrier_ref": "CAST(:agent AS text)||chr(10)||CAST(:seed AS text)||':memory:'||g.i::text"},
            condition="carrier_kind='explicit_memory'")
        await db.commit()
        for table in LAB_ROWS:
            await db.execute(text(f"ANALYZE {table}"))
        await db.commit()
        sizes = {}
        for table in ("invocation_spans", "chat_transcript_events"):
            sizes[table] = dict((await db.execute(text(f"SELECT count(*) AS rows, "
                f"round(avg(pg_column_size(metadata_json))) AS stored_metadata_bytes, "
                f"round(avg(octet_length(metadata_json::text))) AS metadata_json_bytes "
                f"FROM {table} WHERE tenant_id=:tenant"), params)).one()._mapping)
    row = {"multiplier": multiplier, "added": counts, "sizes": sizes,
        "seconds": round(time.perf_counter() - started, 4)}
    print("A2W_HISTORY", json.dumps(row, default=str), flush=True)
    return row


async def catalogue_sql(db, tenant, dependencies, monkeypatch):
    """Capture the real loader's SQL without executing or changing its rows."""
    from app.services.content_input_receipts import _preload_execution_sources, group_source_read

    captured = []

    async def capture(statement, parameters=None, **_kwargs):
        captured.append((str(statement), parameters))
        return []

    with group_source_read(batch_inputs=True, source_catalog=True):
        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", capture)
            await _preload_execution_sources(db, tenant, dependencies)
    assert len(captured) == 1, captured
    return captured[0]


async def measure_catalogue(n, app_factory, monkeypatch, directory, *, scale, volume):
    from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile

    directory.mkdir(parents=True, exist_ok=True)
    async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
        # Establish the tenant transaction outside the standalone statement sample.
        await db.execute(text("SELECT 1"))
        statement, params = await catalogue_sql(db, n.tenant, n.search_memory_sources, monkeypatch)
        (directory / f"catalogue-scale{scale}-volume{volume}.sql").write_text(statement)
        samples = []
        for delay in (0, .006):
            started = time.perf_counter()
            with _profile(monkeypatch, delay=delay) as totals:
                result = await db.execute(text(statement), params)
                rows = result.all()
            row = {"scale": scale, "volume": volume, "rtt": delay, "rows": len(rows),
                "seconds": round(time.perf_counter() - started, 4),
                "statements": sum(v["statements"] for v in totals.values()),
                "injected_seconds": sum(v["injected_seconds"] for v in totals.values())}
            samples.append(row)
            print("A2W_G0", json.dumps(row), flush=True)
        plan = (await db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement), params)).scalar_one()
        if isinstance(plan, str):
            plan = json.loads(plan)
        target = directory / f"plan-scale{scale}-volume{volume}.json"
        target.write_text(json.dumps(plan, indent=2))
        print("A2W_PLAN", json.dumps({"scale": scale, "volume": volume, "path": str(target),
            "planning_ms": plan[0]["Planning Time"], "execution_ms": plan[0]["Execution Time"]}), flush=True)
        from tests.integration.test_a2w_catalogue_rows_postgres import assert_catalogue_rows

        await assert_catalogue_rows(db, n.tenant, n.search_memory_sources, monkeypatch,
            fixture=f"run20-scale{scale}-volume{volume}")
    return samples
