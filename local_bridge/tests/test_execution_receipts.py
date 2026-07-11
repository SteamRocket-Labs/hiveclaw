from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from hive_bridge.execution_receipts import LocalExecutionReceiptStore


def test_parallel_receipt_puts_are_lossless(tmp_path) -> None:
    requested_path = tmp_path / "execution_receipts.json"

    def write(index: int) -> None:
        LocalExecutionReceiptStore(requested_path, max_records=200).put(
            f"replay-{index}",
            {"status": "completed", "output": f"result-{index}"},
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(write, range(80)))

    store = LocalExecutionReceiptStore(requested_path, max_records=200)
    assert store.path.suffix == ".sqlite3"
    assert all(
        store.get(f"replay-{index}")
        == {"status": "completed", "output": f"result-{index}"}
        for index in range(80)
    )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_replay_key_is_first_writer_wins(tmp_path) -> None:
    store = LocalExecutionReceiptStore(tmp_path / "receipts.sqlite3")

    store.put("same-key", {"output": "first"})
    store.put("same-key", {"output": "second"})

    assert store.get("same-key") == {"output": "first"}


def test_legacy_json_receipts_are_backfilled_without_reexecution(tmp_path) -> None:
    legacy_path = tmp_path / "receipts.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema": "hive.local_execution_receipts.v1",
                "records": {
                    "legacy-key": {
                        "stored_at": "2026-07-11T10:00:00+00:00",
                        "result": {"status": "completed", "output": "legacy-result"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = LocalExecutionReceiptStore(legacy_path)

    assert store.get("legacy-key") == {"status": "completed", "output": "legacy-result"}
    assert store.path == tmp_path / "receipts.sqlite3"
    assert legacy_path.exists()


def test_corrupt_database_is_quarantined_and_store_recovers(tmp_path) -> None:
    database_path = tmp_path / "receipts.sqlite3"
    database_path.write_bytes(b"not-a-sqlite-database")

    store = LocalExecutionReceiptStore(database_path)
    store.put("new-key", {"output": "safe"})

    assert store.get("new-key") == {"output": "safe"}
    quarantined = list(tmp_path.glob("receipts.sqlite3.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"not-a-sqlite-database"


def test_uncommitted_sqlite_write_does_not_create_ghost_receipt(tmp_path) -> None:
    store = LocalExecutionReceiptStore(tmp_path / "receipts.sqlite3")
    connection = sqlite3.connect(store.path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO execution_receipts(replay_key, stored_at, result_json, result_sha256) VALUES (?, ?, ?, ?)",
        ("ghost", "2026-07-11T10:00:00+00:00", '{"output":"ghost"}', "0" * 64),
    )
    connection.close()

    assert store.get("ghost") is None
    store.put("real", {"output": "committed"})
    assert store.get("real") == {"output": "committed"}
