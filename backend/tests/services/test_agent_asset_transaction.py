from __future__ import annotations

import errno
import json
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest


def _concurrent_asset_writer(agent_root: str, index: int) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction

    with AgentAssetTransaction(
        Path(agent_root),
        operation="concurrent-test",
        idempotency_key=f"writer-{index}",
    ) as transaction:
        transaction.stage_text(f"memory/concurrent/{index}.md", f"writer-{index}\n")
        transaction.commit()


def _prepared_asset_writer_waiting_for_kill(agent_root: str, ready_path: str) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction

    transaction = AgentAssetTransaction(Path(agent_root), operation="real-kill-9")
    transaction.__enter__()
    transaction.stage_text("memory/kill-9-a.md", "a-after-kill\n")
    transaction.stage_text("memory/kill-9-b.md", "b-after-kill\n")
    transaction._prepare()
    Path(ready_path).write_text("ready", encoding="utf-8")
    time.sleep(60)


def test_asset_transaction_commits_all_targets_with_one_revision(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction, read_agent_asset_revision

    agent_root = tmp_path / "agent"
    with AgentAssetTransaction(
        agent_root,
        operation="explicit-memory",
        idempotency_key="candidate-1",
        evidence_refs=("session:s1",),
    ) as transaction:
        transaction.stage_text("memory/explicit/entries/a.md", "entry-a\n")
        transaction.stage_text("memory/explicit/manifest.jsonl", '{"id":"a"}\n')
        receipt = transaction.commit()

    assert (agent_root / "memory/explicit/entries/a.md").read_text() == "entry-a\n"
    assert (agent_root / "memory/explicit/manifest.jsonl").read_text() == '{"id":"a"}\n'
    assert receipt.revision == 1
    assert receipt.changed_paths == (
        "memory/explicit/entries/a.md",
        "memory/explicit/manifest.jsonl",
    )
    assert read_agent_asset_revision(agent_root) == 1
    journal = json.loads(receipt.journal_path.read_text())
    assert journal["status"] == "committed"
    assert journal["evidence_refs"] == ["session:s1"]


def test_asset_transaction_rejects_stale_revision_without_writing(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction, StaleAssetRevisionError

    agent_root = tmp_path / "agent"
    with AgentAssetTransaction(agent_root, operation="first") as transaction:
        transaction.stage_text("soul.md", "v1\n")
        transaction.commit()

    with pytest.raises(StaleAssetRevisionError, match="expected revision 0, current revision 1"):
        with AgentAssetTransaction(agent_root, operation="stale", expected_revision=0) as transaction:
            transaction.stage_text("soul.md", "stale\n")
            transaction.commit()

    assert (agent_root / "soul.md").read_text() == "v1\n"


def test_asset_transaction_idempotency_replays_first_receipt(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction, read_agent_asset_revision

    agent_root = tmp_path / "agent"
    with AgentAssetTransaction(agent_root, operation="skill", idempotency_key="candidate-42") as transaction:
        transaction.stage_text("evolution/skill_registry.json", "first\n")
        first = transaction.commit()

    with AgentAssetTransaction(agent_root, operation="skill", idempotency_key="candidate-42") as transaction:
        assert transaction.is_replay is True
        transaction.stage_text("evolution/skill_registry.json", "second\n")
        replay = transaction.commit()

    assert replay.transaction_id == first.transaction_id
    assert replay.idempotent_replay is True
    assert (agent_root / "evolution/skill_registry.json").read_text() == "first\n"
    assert read_agent_asset_revision(agent_root) == 1


def test_asset_transaction_without_staged_changes_leaves_no_journal(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import AgentAssetTransaction, read_agent_asset_revision

    agent_root = tmp_path / "agent"

    with AgentAssetTransaction(agent_root, operation="read-only-check") as transaction:
        assert transaction.has_changes is False
        assert transaction.read_text("skills/example/SKILL.md") is None

    transactions_root = agent_root / "runtime_artifacts" / "asset_transactions" / "transactions"
    assert list(transactions_root.glob("*/journal.json")) == []
    assert read_agent_asset_revision(agent_root) == 0


def test_asset_transaction_rolls_back_every_target_when_replace_hits_disk_full(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services import agent_asset_transaction as asset_tx

    agent_root = tmp_path / "agent"
    (agent_root / "memory").mkdir(parents=True)
    (agent_root / "memory/a.md").write_text("old-a\n")
    (agent_root / "memory/b.md").write_text("old-b\n")
    real_replace = asset_tx._replace_staged_file
    calls = 0

    def fail_second_replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "disk full")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(asset_tx, "_replace_staged_file", fail_second_replace)

    with pytest.raises(OSError, match="disk full"):
        with asset_tx.AgentAssetTransaction(agent_root, operation="disk-full") as transaction:
            transaction.stage_text("memory/a.md", "new-a\n")
            transaction.stage_text("memory/b.md", "new-b\n")
            transaction.commit()

    assert (agent_root / "memory/a.md").read_text() == "old-a\n"
    assert (agent_root / "memory/b.md").read_text() == "old-b\n"
    assert asset_tx.read_agent_asset_revision(agent_root) == 0
    journals = list((agent_root / "runtime_artifacts/asset_transactions/transactions").glob("*/journal.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text())["status"] == "rolled_back"


def test_asset_transaction_recovers_interrupted_prepared_journal(tmp_path: Path) -> None:
    from app.services import agent_asset_transaction as asset_tx

    agent_root = tmp_path / "agent"
    (agent_root / "memory").mkdir(parents=True)
    (agent_root / "memory/a.md").write_text("old-a\n")

    transaction = asset_tx.AgentAssetTransaction(agent_root, operation="kill-9-simulation")
    transaction.__enter__()
    transaction.stage_text("memory/a.md", "new-a\n")
    transaction.stage_text("memory/b.md", "new-b\n")
    transaction._prepare()  # durable prepared journal: equivalent to process death before apply
    transaction._lock_handle.close()  # emulate the kernel releasing flock on process death
    transaction._lock_handle = None

    receipts = asset_tx.recover_agent_asset_transactions(agent_root)

    assert len(receipts) == 1
    assert receipts[0].recovered is True
    assert (agent_root / "memory/a.md").read_text() == "new-a\n"
    assert (agent_root / "memory/b.md").read_text() == "new-b\n"
    assert asset_tx.read_agent_asset_revision(agent_root) == 1
    assert json.loads(receipts[0].journal_path.read_text())["status"] == "committed"


def test_asset_transaction_rolls_forward_after_revision_commit_point(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services import agent_asset_transaction as asset_tx

    agent_root = tmp_path / "agent"
    real_write_json = asset_tx._atomic_write_json
    failed = False

    def fail_receipt_once(path, payload):
        nonlocal failed
        if not failed and path.parent.name == "receipts":
            failed = True
            raise OSError(errno.ENOSPC, "receipt disk full")
        return real_write_json(path, payload)

    monkeypatch.setattr(asset_tx, "_atomic_write_json", fail_receipt_once)
    with pytest.raises(OSError, match="receipt disk full"):
        with asset_tx.AgentAssetTransaction(
            agent_root,
            operation="commit-point",
            idempotency_key="commit-point-1",
        ) as transaction:
            transaction.stage_text("soul.md", "committed-before-receipt\n")
            transaction.commit()

    assert (agent_root / "soul.md").read_text() == "committed-before-receipt\n"
    assert asset_tx.read_agent_asset_revision(agent_root) == 1

    monkeypatch.setattr(asset_tx, "_atomic_write_json", real_write_json)
    recovered = asset_tx.recover_agent_asset_transactions(agent_root)

    assert len(recovered) == 1
    assert recovered[0].recovered is True
    assert json.loads(recovered[0].journal_path.read_text())["status"] == "committed"


def test_asset_transaction_recovers_after_real_kill_9(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import recover_agent_asset_transactions

    agent_root = tmp_path / "agent"
    ready_path = tmp_path / "prepared.ready"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_prepared_asset_writer_waiting_for_kill,
        args=(str(agent_root), str(ready_path)),
    )
    process.start()
    deadline = time.monotonic() + 5
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready_path.exists()

    os.kill(process.pid, signal.SIGKILL)
    process.join(timeout=5)
    assert process.exitcode == -signal.SIGKILL

    recovered = recover_agent_asset_transactions(agent_root)

    assert len(recovered) == 1
    assert recovered[0].recovered is True
    assert (agent_root / "memory/kill-9-a.md").read_text() == "a-after-kill\n"
    assert (agent_root / "memory/kill-9-b.md").read_text() == "b-after-kill\n"


@pytest.mark.skipif(not hasattr(multiprocessing, "get_context"), reason="multiprocessing unavailable")
def test_asset_transaction_serializes_multiple_processes(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import read_agent_asset_revision

    agent_root = tmp_path / "agent"
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_concurrent_asset_writer, args=(str(agent_root), index)) for index in range(12)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert read_agent_asset_revision(agent_root) == 12
    assert {path.name for path in (agent_root / "memory/concurrent").glob("*.md")} == {
        f"{index}.md" for index in range(12)
    }
