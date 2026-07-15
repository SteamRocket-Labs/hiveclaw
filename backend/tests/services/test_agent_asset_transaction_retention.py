from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _journal(receipt) -> dict:
    return json.loads(receipt.journal_path.read_text(encoding="utf-8"))


def test_append_text_stages_only_delta_and_auto_finalizes(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction

    agent_root = tmp_path / "agent"
    ledger = agent_root / "evolution" / "skill_review.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"x" * (1024 * 1024))

    with AgentAssetTransaction(agent_root, operation="append-audit") as transaction:
        transaction.append_text("evolution/skill_review.md", "\nnew-event\n")
        receipt = transaction.commit()

    journal = _journal(receipt)
    operation = journal["operations"][0]
    assert operation["action"] == "append"
    assert operation["append_size"] == len(b"\nnew-event\n")
    assert (receipt.journal_path.parent / operation["stage_file"]).stat().st_size == len(b"\nnew-event\n")
    assert not (receipt.journal_path.parent / "backups").exists()
    assert journal["status"] == "committed"
    assert journal["lifecycle_state"] == "finalized"
    assert journal["finalized_at"]
    assert journal["rollback_deadline"]
    assert ledger.read_bytes().endswith(b"\nnew-event\n")


def test_append_recovery_does_not_duplicate_applied_delta(tmp_path: Path) -> None:
    from app.services import agent_asset_transaction as asset_tx

    agent_root = tmp_path / "agent"
    ledger = agent_root / "memory" / "events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"id":1}\n', encoding="utf-8")

    transaction = asset_tx.AgentAssetTransaction(agent_root, operation="append-crash")
    transaction.__enter__()
    transaction.append_text("memory/events.jsonl", '{"id":2}\n')
    transaction._prepare()
    operation = transaction._journal["operations"][0]
    transaction._journal["status"] = "applying"
    asset_tx._atomic_write_json(transaction.journal_path, transaction._journal)
    asset_tx._apply_operation(agent_root, transaction.transaction_dir, operation)
    transaction._release()

    receipts = asset_tx.recover_agent_asset_transactions(agent_root)

    assert len(receipts) == 1
    assert ledger.read_text(encoding="utf-8") == '{"id":1}\n{"id":2}\n'


def test_append_compensation_truncates_only_matching_tail(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import (
        AgentAssetTransaction,
        StaleAssetRevisionError,
        compensate_agent_asset_transaction,
    )

    agent_root = tmp_path / "agent"
    ledger = agent_root / "memory" / "events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("base\n", encoding="utf-8")

    with AgentAssetTransaction(agent_root, operation="saga-append", requires_projection=True) as transaction:
        transaction.append_text("memory/events.jsonl", "candidate\n")
        receipt = transaction.commit()

    compensation = compensate_agent_asset_transaction(agent_root, receipt, reason="projection_failed")
    assert compensation.revision == 2
    assert ledger.read_text(encoding="utf-8") == "base\n"

    with AgentAssetTransaction(agent_root, operation="saga-append-2", requires_projection=True) as transaction:
        transaction.append_text("memory/events.jsonl", "candidate-2\n")
        second = transaction.commit()
    ledger.write_text("base\ncandidate-2\nexternal\n", encoding="utf-8")

    with pytest.raises(StaleAssetRevisionError):
        compensate_agent_asset_transaction(agent_root, second, reason="projection_failed")


def test_projection_transaction_requires_explicit_finalize(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import (
        AgentAssetTransaction,
        finalize_agent_asset_transaction,
    )

    agent_root = tmp_path / "agent"
    with AgentAssetTransaction(agent_root, operation="cross-store", requires_projection=True) as transaction:
        transaction.stage_text("memory/value.md", "value\n")
        receipt = transaction.commit()

    assert _journal(receipt)["lifecycle_state"] == "committed_recoverable"
    pinned_until = datetime.now(UTC) + timedelta(days=7)
    finalized = finalize_agent_asset_transaction(
        agent_root,
        receipt,
        projection_ref="postgres:memory_projection:42",
        pinned_until=pinned_until,
    )

    assert finalized["lifecycle_state"] == "finalized"
    assert finalized["projection_ref"] == "postgres:memory_projection:42"
    assert datetime.fromisoformat(finalized["payload_gc_at"]) >= pinned_until
