"""A2m local timing report (report-only, not an assertion suite).

Builds one store with ~200 receipted explicit entries and ~50 episodic T2
packages, then measures ``build_memory_context`` wall time and SQL statement
count. On BASE the old selection double is delayed by 3 s so the removed
pre-selection cost is visible. Prints one A2M_TIMING JSON line.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event, select as sa_select

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("migrated_pg_url"),
    pytest.mark.skipif(not os.environ.get("HC_A2M_TIMING"),
        reason="seeds 200 entries + 50 packages; run with HC_A2M_TIMING=1 -s to record a timing line"),
]


async def test_a2m_timing_report(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.memory.explicit_overlay import prepare_explicit_memory_entry
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.capability_policy import CapabilityPolicy
    from app.models.organization_identity import ContentInputReceipt
    from app.services import memory_service
    from app.services.explicit_memory_authority import AUTHORITY_SCHEMA, memory_ref, source_dependency
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    async with owner_sessionmaker() as db:
        from app.models.llm import LLMModel
        from app.services.secrets_provider import get_secrets_provider

        db.add(LLMModel(id=uuid.uuid4(), tenant_id=n.tenant, provider="openai", model="fake-4.1",
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-local-provider-only"),
            label="Local fake", enabled=True, max_input_tokens=256_000))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.write",
            allowed=True, requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.read",
            allowed=True, requires_approval=False))
        await db.commit()

    # ~50 episodic T2 packages from other sessions of this agent.
    for index in range(50):
        other = str(uuid.UUID(int=index + 1))
        package = n.root / "memory/t2/sessions" / other / "segments" / "seg-0"
        package.mkdir(parents=True, exist_ok=True)
        (package / "manifest.json").write_text(json.dumps({
            "schema_version": "t2.segment-package.manifest.v1", "package_id": f"t2pkg-{index}",
            "package_status": "reviewed", "session_id": other, "t0_segment_id": "seg-0",
            "created_at": "2026-11-01T00:00:00+00:00",
            "source_refs": [f"t0://session/{other}/segment/seg-0#seq=1..2"]}, ensure_ascii=False))
        (package / "summary.md").write_text(f"Reviewed T2 evidence bundle {index}.\n")

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)

    marker = f"A2M_TIMING_MARK_{uuid.uuid4().hex[:6]}"
    saved, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"category": "project", "content": f"Timing fact 0 {marker}."})
    assert "Saved memory [project]" in saved, saved

    # The remaining 199 entries copy the first receipt's valid dependency set.
    from app.database import tenant_scoped_session

    manifest_extra = []
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        first_row = (await db.execute(sa_select(ContentInputReceipt)
            .where(ContentInputReceipt.carrier_kind == "explicit_memory")
            .order_by(ContentInputReceipt.id.desc()).limit(1))).scalar_one()
        template = first_row.dependencies_json
        authority = first_row.authority_json
        for index in range(1, 200):
            body = f"Timing fact {index} {marker} {index}."
            prepared = prepare_explicit_memory_entry(agent_id=n.agent, data_root=n.root.parent,
                decision=MemoryWriteDecision(original_content=body, content=body, category="project",
                    sensitivity="PL1_public"),
                source_refs=(), target_hint="capabilities", origin="explicit_user_request",
                entry_id=f"a2m-timing-{index}")
            entry = prepared.entry
            entry.path.parent.mkdir(parents=True, exist_ok=True)
            entry.path.write_text(prepared.rendered)
            manifest_extra.append(json.dumps({**entry.metadata, "status": "active"}) + "\n")
            db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
                carrier_ref=memory_ref(n.agent, entry.entry_id), revision=1,
                content_sha256=entry.source_sha256, dependencies_json=list(template),
                authority_json={**authority, "metadata": {**entry.metadata, "status": "active"}}))
        await db.commit()
    with (n.root / "memory/explicit/manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.writelines(manifest_extra)
    manifest_lines = (n.root / "memory/explicit/manifest.jsonl").read_text().splitlines()
    assert len(manifest_lines) == 200, len(manifest_lines)

    from app.memory.retriever import MemoryRetriever

    selection_delayed = hasattr(MemoryRetriever, "_select_with_model")
    if selection_delayed:
        async def delayed_selection(_self, *, items, **_kwargs):
            await asyncio.sleep(3.0)
            return [item.metadata["selection_candidate_id"] for item in items[:5]], "timing double"

        monkeypatch.setattr(MemoryRetriever, "_select_with_model", delayed_selection)

    statements: list[str] = []

    def executed(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.split()))

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        source_engine = db.get_bind()
    event.listen(source_engine, "after_cursor_execute", executed)
    try:
        started = time.perf_counter()
        result = await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(n.session),
            runtime_task_id=str(n.run), current_user_id=n.user,
            return_result=True)
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        event.remove(source_engine, "after_cursor_execute", executed)
    payload = {"elapsed_ms": round(elapsed_ms, 1), "sql_statements": len(statements),
        "status": result.status, "code": result.code,
        "degraded_components": list(result.degraded_components),
        "selection_double_delay_s": 3.0 if selection_delayed else None,
        "selection_present_on_path": selection_delayed,
        "resident_chars": len(result.content)}
    print("A2M_TIMING", json.dumps(payload), flush=True)
