from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.scripts.migrate_decision_trace_jsonl import migrate_legacy_decision_trace_jsonl


@pytest.mark.asyncio
async def test_legacy_decision_trace_migration_dry_runs_then_archives_after_verified_import(tmp_path) -> None:
    source = tmp_path / "decision_traces.jsonl"
    archive_dir = tmp_path / "legacy-imports"
    decision_id = f"decision-{uuid4().hex}"
    feedback_id = str(uuid4())
    created_at = datetime(2026, 6, 12, 10, 0, tzinfo=UTC).isoformat()
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema": "decision_trace_event.v1",
                        "event": "decision",
                        "decision": {
                            "id": decision_id,
                            "action": "send_feishu_message",
                            "chosen": "ask",
                            "reasoning": "External-visible action.",
                            "alternatives_considered": ["send", "ask"],
                            "situational_factors": ["charter_confirm_first"],
                            "charter_zone": "confirm_first",
                            "preflight": {"decision": "ask"},
                            "sensitivity": "PL1_public",
                            "tenant_id": str(uuid4()),
                            "agent_id": str(uuid4()),
                            "user_id": str(uuid4()),
                            "session_id": str(uuid4()),
                            "created_at": created_at,
                        },
                    }
                ),
                json.dumps(
                    {
                        "schema": "decision_trace_event.v1",
                        "event": "feedback",
                        "feedback": {
                            "id": feedback_id,
                            "refs": f"decision/{decision_id}",
                            "reaction": "useful",
                            "polarity": "positive",
                            "source": "session_feedback",
                            "rationale_from_owner": "Correct boundary.",
                            "created_at": created_at,
                        },
                    }
                ),
                "{malformed-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    imported_decisions = []
    imported_feedback = []

    class ImportingStore:
        async def import_decision(self, decision):
            imported_decisions.append(decision)
            return decision

        async def import_feedback(self, feedback):
            imported_feedback.append(feedback)
            return feedback

    dry_run = await migrate_legacy_decision_trace_jsonl(
        source,
        ImportingStore(),
        apply=False,
        archive_dir=archive_dir,
    )

    assert dry_run["dry_run"] is True
    assert dry_run["decisions_seen"] == 1
    assert dry_run["feedback_seen"] == 1
    assert dry_run["skipped_lines"] == 1
    assert dry_run["source_sha256"]
    assert dry_run["can_apply"] is False
    assert source.exists()
    assert not archive_dir.exists()
    assert imported_decisions == []
    assert imported_feedback == []

    source.write_text(source.read_text(encoding="utf-8").replace("{malformed-json\n", ""), encoding="utf-8")
    clean_dry_run = await migrate_legacy_decision_trace_jsonl(
        source,
        ImportingStore(),
        apply=False,
        archive_dir=archive_dir,
    )
    assert clean_dry_run["can_apply"] is True

    applied = await migrate_legacy_decision_trace_jsonl(
        source,
        ImportingStore(),
        apply=True,
        archive_dir=archive_dir,
        expected_sha256=clean_dry_run["source_sha256"],
    )

    assert applied["verified"] is True
    assert applied["decisions_processed"] == 1
    assert applied["feedback_processed"] == 1
    assert imported_decisions[0].id == decision_id
    assert imported_feedback[0].id == feedback_id
    assert not source.exists()
    archived = archive_dir / applied["archive_name"]
    assert archived.exists()
    assert archived.read_bytes()

    source.write_bytes(archived.read_bytes())
    repeated = await migrate_legacy_decision_trace_jsonl(
        source,
        ImportingStore(),
        apply=True,
        archive_dir=archive_dir,
        expected_sha256=clean_dry_run["source_sha256"],
    )
    assert repeated["verified"] is True
    assert repeated["archive_reused"] is True
    assert not source.exists()
    assert archived.exists()


@pytest.mark.asyncio
async def test_unscoped_legacy_rows_require_hash_bound_explicit_tenant_assignment(tmp_path) -> None:
    source = tmp_path / "decision_traces.jsonl"
    decision_id = f"decision-{uuid4().hex}"
    created_at = datetime(2026, 6, 12, 10, 0, tzinfo=UTC).isoformat()
    source.write_text(
        json.dumps(
            {
                "schema": "decision_trace_event.v1",
                "event": "decision",
                "decision": {
                    "id": decision_id,
                    "action": "send_feishu_message",
                    "chosen": "ask",
                    "reasoning": "External-visible action.",
                    "alternatives_considered": ["send", "ask"],
                    "situational_factors": ["charter_confirm_first"],
                    "charter_zone": "confirm_first",
                    "preflight": {"decision": "ask"},
                    "sensitivity": "PL1_public",
                    "created_at": created_at,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    imported_decisions = []

    class ImportingStore:
        async def import_decision(self, decision):
            imported_decisions.append(decision)
            return decision

        async def import_feedback(self, feedback):
            return feedback

    dry_run = await migrate_legacy_decision_trace_jsonl(source, ImportingStore(), apply=False)

    assert dry_run["missing_tenant_decisions"] == 1
    assert dry_run["unresolved_tenant_decisions"] == 1
    assert dry_run["can_apply"] is False

    with pytest.raises(ValueError, match="expected_sha256"):
        await migrate_legacy_decision_trace_jsonl(
            source,
            ImportingStore(),
            apply=True,
            unscoped_tenant_id=str(uuid4()),
        )

    with pytest.raises(ValueError, match="missing tenant authority"):
        await migrate_legacy_decision_trace_jsonl(
            source,
            ImportingStore(),
            apply=True,
            expected_sha256=dry_run["source_sha256"],
        )

    assigned_tenant_id = str(uuid4())
    assigned_dry_run = await migrate_legacy_decision_trace_jsonl(
        source,
        ImportingStore(),
        apply=False,
        unscoped_tenant_id=assigned_tenant_id,
    )
    assert assigned_dry_run["tenant_assignments_planned"] == 1
    assert assigned_dry_run["unresolved_tenant_decisions"] == 0
    assert assigned_dry_run["can_apply"] is True

    applied = await migrate_legacy_decision_trace_jsonl(
        source,
        ImportingStore(),
        apply=True,
        unscoped_tenant_id=assigned_tenant_id,
        expected_sha256=assigned_dry_run["source_sha256"],
    )

    assert applied["verified"] is True
    assert imported_decisions[0].tenant_id == assigned_tenant_id
