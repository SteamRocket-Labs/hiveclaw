"""Plans for the actual runtime-result live checkpoint statements."""

import json
import re
import time
import uuid

from sqlalchemy import text

from app.database import tenant_scoped_session
from app.models.runtime_result import RuntimeResultObject
from app.models.user import User
from app.services import content_input_receipts as sources
from app.services import runtime_result_sources as runtime


POINTS = {"canonical_delegation", "return_session", "return_publication", "result_work"}


async def runtime_live_statements(db, tenant, dependencies, user, monkeypatch):
    captured = {}
    original = sources.checkpoint_rows

    async def capture(db, point, statement, *args, **kwargs):
        if point in POINTS:
            captured.setdefault(point, statement)
        return await original(db, point, statement, *args, **kwargs)

    with sources.group_source_read(batch_inputs=True, source_catalog=True):
        await sources._preload_execution_sources(db, tenant, dependencies)
        retained = sources._preflight_inputs(db)
        runs = {uuid.UUID(row.source_run_id) for key, row in retained.items()
            if isinstance(key, tuple) and key[0] is RuntimeResultObject and row is not None
            and row.source_kind == "a2a_delegation"}
        from app.models.session_v2 import SessionModelResult

        known_models = {key[1] for key in retained if isinstance(key, tuple) and key[0] is SessionModelResult}
        with monkeypatch.context() as patch:
            patch.setattr(sources, "checkpoint_rows", capture)
            with sources.source_read_checkpoint(kind="runtime_result", phase="live_plan"):
                canonical = await runtime._canonical_checkpoint(db, tenant)
                assert canonical and runs
                await runtime._return_session_checkpoint(db, tenant, "runtime_result", user, None)
                for raw in sources.checkpoint_source_ids("runtime_result"):
                    agent, session, identity = (uuid.UUID(part) for part in raw.split("\n"))
                    result = retained[RuntimeResultObject, identity]
                    await runtime._publication_checkpoint(db, result, agent, session, user.id)
                await runtime._result_work_checkpoint(db, next(iter(canonical.values()))[0], user)
    assert set(captured) == POINTS, captured.keys()
    return captured, runs, known_models


async def measure_runtime_live(n, app_factory, dependencies, monkeypatch, directory, *, scale, volume):
    from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile

    directory.mkdir(parents=True, exist_ok=True)
    async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
        user = await db.get(User, n.user)
        statements, runs, models = await runtime_live_statements(db, n.tenant, dependencies, user, monkeypatch)
        from sqlalchemy import select, union_all, func

        old = old_canonical_statement(n.tenant, runs, models)
        new = statements["canonical_delegation"]
        difference = union_all(old.except_all(new), new.except_all(old)).subquery()
        differing = (await db.execute(select(func.count()).select_from(difference))).scalar_one()
        (directory / f"oracle-scale{scale}-volume{volume}.json").write_text(json.dumps({
            "old": "193e08b4", "differing_rows": differing, "points": sorted(statements)}))
        assert differing == 0, "The canonical live checkpoint must return the complete old row set."
        (directory / f"bindings-scale{scale}-volume{volume}.json").write_text(json.dumps({
            "tenant": str(n.tenant), "runs": [str(value) for value in runs],
            "known_models": [str(value) for value in models]}, indent=2))
        for point, statement in statements.items():
            sql = str(statement.compile(dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True}))
            # SQLAlchemy's literal renderer omits UUID casts inside VALUES;
            # the real bound statement includes them. Preserve its PG types.
            sql = re.sub(r"'([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})'", r"'\1'::uuid", sql)
            stem = f"scale{scale}-volume{volume}-{point}"
            (directory / (stem + ".sql")).write_text(sql + "\n")
            samples = []
            for delay in (0, .006):
                started = time.perf_counter()
                with _profile(monkeypatch, delay=delay) as totals:
                    rows = (await db.execute(statement)).all()
                row = {"point": point, "scale": scale, "volume": volume,
                    "rtt": delay, "rows": len(rows), "seconds": round(time.perf_counter() - started, 4),
                    "statements": sum(r["statements"] for r in totals.values()),
                    "injected_seconds": sum(r["injected_seconds"] for r in totals.values())}
                samples.append(row)
                (directory / (stem + "-timings.json")).write_text(json.dumps(samples, indent=2))
                print("A2W_RUNTIME_LIVE", json.dumps(row), flush=True)
            plan = (await db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql))).scalar_one()
            if isinstance(plan, str):
                plan = json.loads(plan)
            path = directory / (stem + ".json")
            path.write_text(json.dumps(plan, indent=2))
            print("A2W_RUNTIME_PLAN", json.dumps({"point": point, "scale": scale, "volume": volume,
                "path": str(path), "server_ms": plan[0]["Execution Time"]}), flush=True)


def old_canonical_statement(tenant_id, runs, known_models):
    """Frozen 193e08b4 statement: preserve the complete live row-set oracle."""
    from sqlalchemy import select, case, func, or_
    from sqlalchemy.orm import aliased
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.source_read_queries import fact_columns, source_uuid

    event, commit = aliased(ChatTranscriptEvent), aliased(ChatTranscriptEvent)
    candidate = aliased(SessionModelResult)
    latest = select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.tenant_id == tenant_id,
        ChatTranscriptEvent.agent_id == RuntimeTask.child_agent_id, ChatTranscriptEvent.session_id == ChatSession.id,
        ChatTranscriptEvent.run_id == RuntimeTask.id,
        or_(ChatTranscriptEvent.item_kind.is_(None), ChatTranscriptEvent.item_kind != "run")
    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1).correlate(RuntimeTask, ChatSession).scalar_subquery()
    native_id = event.metadata_json["native_final_model_result_id"].astext
    chosen = select(candidate.id).where(candidate.tenant_id == tenant_id, candidate.run_id == RuntimeTask.id,
        candidate.session_id == event.session_id,
        or_((native_id.is_(None) & (candidate.round_committed_event_id == event.id)),
            (native_id.is_not(None) & (candidate.id == source_uuid(native_id))))
    ).limit(1).correlate(RuntimeTask, event).scalar_subquery()
    native = select(SessionModelResult.id).where(SessionModelResult.tenant_id == tenant_id,
        SessionModelResult.run_id == RuntimeTask.id).correlate(RuntimeTask).exists()
    event_fields = "id sequence tenant_id agent_id session_id run_id parent_event_id root_session_id parent_session_id " \
        "message_id schema_version item_id item_kind lifecycle payload_schema ordinal command_id input_id result_id " \
        "invocation_id provider_tool_use_id content_hash parent_item_id item_type item_status turn_id causation_id " \
        "correlation_id actor_type event_type visibility_scope listed_surface scope_json content parts_json metadata_json"
    model_fields = fact_columns(SessionModelResult, "id tenant_id session_id run_id round_id turn_id provider_request_id "
        "model_request_hash state seal_json round_committed_event_id")
    model_fields = model_fields.op("||")(func.jsonb_build_object("model_request_snapshot_json", case((
        SessionModelResult.id.not_in(known_models) & SessionModelResult.state.in_(("sealed", "round_committed")),
        SessionModelResult.model_request_snapshot_json))))
    statement = select(fact_columns(RuntimeTask, "id tenant_id task_type status parent_agent_id child_agent_id "
        "child_agent_name parent_session_id child_session_id root_user_id root_session_id root_runtime_task_id "
        "root_external_principal_id machine_credential_id prompt result_summary metadata_json trace_id depth "
        "writer_generation config_snapshot_hash policy_snapshot_hash"),
        fact_columns(ChatSession, "id tenant_id agent_id"), fact_columns(event, event_fields),
        model_fields, fact_columns(commit, event_fields),
        native, RuntimeTask.id).select_from(RuntimeTask).outerjoin(ChatSession,
            (ChatSession.id == source_uuid(RuntimeTask.child_session_id)) & (ChatSession.tenant_id == tenant_id)
            & (ChatSession.agent_id == RuntimeTask.child_agent_id)).outerjoin(event, event.id == latest).outerjoin(
                SessionModelResult, SessionModelResult.id == chosen).outerjoin(commit,
                    commit.id == SessionModelResult.round_committed_event_id).where(
                        RuntimeTask.tenant_id == tenant_id, RuntimeTask.task_type == "delegation", RuntimeTask.id.in_(runs))
    return statement
