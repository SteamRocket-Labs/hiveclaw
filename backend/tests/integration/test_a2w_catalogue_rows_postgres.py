"""The G0 query shape must preserve every row, including malformed ID cases."""

from pathlib import Path
from datetime import datetime, timezone
import uuid

import pytest

from app.database import tenant_scoped_session
from app.models.invocation_span import InvocationSpan
from tests.integration.test_r12a_source_graph_work_postgres import (
    _graph, _source, native_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
LEGACY_SQL = Path(__file__).with_name("fixtures") / "a2w_catalogue_0c902.sql"


async def assert_catalogue_rows(db, tenant, dependencies, monkeypatch, *, fixture):
    import json
    import time
    from sqlalchemy import text
    from tests.integration._a2w_volume import catalogue_sql

    current, parameters = await catalogue_sql(db, tenant, dependencies, monkeypatch)
    legacy = LEGACY_SQL.read_text()
    # One PostgreSQL snapshot, JSONB equality and EXCEPT ALL preserve both
    # complete payloads and duplicates without hashing large model snapshots.
    statement = f"""WITH old_rows AS MATERIALIZED ({legacy}), new_rows AS MATERIALIZED ({current}),
        difference AS (
            (SELECT * FROM old_rows EXCEPT ALL SELECT * FROM new_rows)
            UNION ALL (SELECT * FROM new_rows EXCEPT ALL SELECT * FROM old_rows)
        ) SELECT (SELECT count(*) FROM old_rows) AS old_count,
            (SELECT count(*) FROM new_rows) AS new_count, (SELECT count(*) FROM difference) AS differences"""
    started = time.perf_counter()
    row = dict((await db.execute(text(statement), parameters)).one()._mapping)
    print("A2W_ROWSET", json.dumps({"fixture": fixture, **row,
        "seconds": round(time.perf_counter() - started, 4)}), flush=True)
    assert row["old_count"] > 0
    assert row["old_count"] == row["new_count"]
    assert row["differences"] == 0


async def test_r12a_catalogue_rows_and_text_uuid_semantics(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    n = native_workspace
    await _graph(n, 10, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    witness = uuid.uuid4()
    async with tenant_scoped_session(n.tenant, session_factory=owner_sessionmaker) as db:
        db.add(InvocationSpan(id=witness, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            runtime_task_id=n.run, session_id=str(n.session), trace_id=f"rowset-{witness}",
            span_id="unselected", span_type="rowset_witness", name="Unselected row-set witness",
            status="captured", started_at=datetime.now(timezone.utc),
            metadata_json={"purpose": "query row-set comparison"}))
        await db.commit()
    root = _source(n.receipts[-1])
    cases = [("r12a", None), ("kind-agnostic-canonical", ("unknown_kind", str(witness))),
        ("uppercase", ("unknown_kind", str(witness).upper())),
        ("compact", ("unknown_kind", witness.hex)), ("braced", ("unknown_kind", "{" + str(witness) + "}")),
        ("invalid", ("unknown_kind", "not-a-uuid")),
        ("team-composite", ("runtime_team_context", f"{n.agent}\n{n.session}\n{witness}")),
        ("team-uppercase", ("runtime_team_context", f"{n.agent}\n{n.session}\n{str(witness).upper()}")),
        ("team-extra-component", ("runtime_team_context", f"{n.agent}\n{n.session}\n{witness}\nextra"))]
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        for name, extra in cases:
            sources = [root] + ([{"source_kind": extra[0], "source_id": extra[1], "source_version": "unused"}]
                if extra else [])
            await assert_catalogue_rows(db, n.tenant, sources, monkeypatch, fixture=name)
