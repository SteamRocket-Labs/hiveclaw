"""Crash-safe replay receipts for Local Agent execution results."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
import uuid

_LEGACY_SCHEMA = "hive.local_execution_receipts.v1"
_LOCK_WAIT_SECONDS = 30.0
_STALE_LOCK_SECONDS = 60.0


def _canonical_result(result: dict[str, Any]) -> tuple[str, str]:
    payload = json.dumps(
        dict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quarantine_suffix() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"corrupt-{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "file is not a database",
            "database disk image is malformed",
            "database corruption",
            "quick_check failed",
        )
    )


class LocalExecutionReceiptStore:
    """SQLite/WAL replay ledger with first-writer-wins idempotency.

    ``.json`` constructor paths remain supported: the adjacent ``.sqlite3``
    file becomes canonical and the legacy JSON is imported without deletion.
    Keeping the old file makes migration reversible while a metadata hash keeps
    repeated process starts from re-importing unchanged data.
    """

    def __init__(self, path: str | Path, *, max_records: int = 1000) -> None:
        requested_path = Path(path).expanduser()
        if requested_path.suffix.lower() == ".json":
            self.legacy_path = requested_path
            self.path = requested_path.with_suffix(".sqlite3")
        else:
            self.path = requested_path
            self.legacy_path = requested_path.with_suffix(".json")
        self.max_records = max(1, int(max_records))
        self._initialize_and_import()

    @property
    def _recovery_lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.recovery.lock")

    @contextmanager
    def _recovery_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self._recovery_lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(
                    descriptor,
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "created_at": datetime.now(UTC).isoformat(),
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
                os.fsync(descriptor)
            except FileExistsError:
                stale = False
                try:
                    payload = json.loads(
                        self._recovery_lock_path.read_text(encoding="utf-8")
                    )
                    pid = int(payload.get("pid") or 0)
                    age = time.time() - self._recovery_lock_path.stat().st_mtime
                    stale = not _pid_is_alive(pid) or age > _STALE_LOCK_SECONDS
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    stale = True
                if stale:
                    try:
                        self._recovery_lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for receipt recovery lock: {self._recovery_lock_path}"
                    )
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                self._recovery_lock_path.unlink()
            except FileNotFoundError:
                pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_and_import(self) -> None:
        with self._recovery_lock():
            try:
                self._initialize_database()
            except sqlite3.DatabaseError as exc:
                if not _is_corruption_error(exc):
                    raise
                self._quarantine_database()
                self._initialize_database()
            self._import_legacy_json()

    def _initialize_database(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_receipts (
                    replay_key TEXT PRIMARY KEY,
                    stored_at TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_execution_receipts_stored_at
                    ON execution_receipts(stored_at);
                CREATE TABLE IF NOT EXISTS execution_receipt_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_key TEXT,
                    quarantined_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    raw_result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS receipt_store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"receipt database quick_check failed: {quick_check!r}"
                )
        os.chmod(self.path, 0o600)

    def _quarantine_database(self) -> None:
        suffix = _quarantine_suffix()
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                candidate.replace(candidate.with_name(f"{candidate.name}.{suffix}"))

    def _quarantine_legacy_json(self) -> None:
        if not self.legacy_path.exists():
            return
        suffix = _quarantine_suffix()
        self.legacy_path.replace(
            self.legacy_path.with_name(f"{self.legacy_path.name}.{suffix}")
        )

    def _legacy_fingerprint(self, raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def _import_legacy_json(self) -> None:
        if not self.legacy_path.exists() or self.legacy_path == self.path:
            return
        try:
            raw = self.legacy_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            records = payload.get("records")
            if payload.get("schema") != _LEGACY_SCHEMA or not isinstance(records, dict):
                raise ValueError("invalid legacy receipt schema")
            normalized: list[tuple[str, str, str, str]] = []
            for replay_key, record in records.items():
                if not isinstance(record, dict) or not isinstance(
                    record.get("result"), dict
                ):
                    raise ValueError(f"invalid legacy receipt record: {replay_key}")
                result_json, result_sha256 = _canonical_result(record["result"])
                normalized.append(
                    (
                        str(replay_key),
                        str(record.get("stored_at") or datetime.now(UTC).isoformat()),
                        result_json,
                        result_sha256,
                    )
                )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            self._quarantine_legacy_json()
            return

        fingerprint = self._legacy_fingerprint(raw)
        metadata_key = f"legacy_json_sha256:{self.legacy_path.resolve()}"
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT value FROM receipt_store_metadata WHERE key = ?",
                (metadata_key,),
            ).fetchone()
            if existing and existing[0] == fingerprint:
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO execution_receipts(
                        replay_key, stored_at, result_json, result_sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    normalized,
                )
                self._prune(connection)
                connection.execute(
                    """
                    INSERT INTO receipt_store_metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (metadata_key, fingerprint),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM execution_receipts
            WHERE rowid IN (
                SELECT rowid
                FROM execution_receipts
                ORDER BY stored_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_records,),
        )

    def _recover_after_database_error(self) -> None:
        self._initialize_and_import()

    def _quarantine_record(
        self, replay_key: str, raw_result_json: str, reason: str
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO execution_receipt_quarantine(
                        replay_key, quarantined_at, reason, raw_result_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        replay_key,
                        datetime.now(UTC).isoformat(),
                        reason,
                        raw_result_json,
                    ),
                )
                connection.execute(
                    "DELETE FROM execution_receipts WHERE replay_key = ?", (replay_key,)
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get(self, replay_key: str) -> dict[str, Any] | None:
        clean_key = str(replay_key or "").strip()
        if not clean_key:
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT result_json, result_sha256 FROM execution_receipts WHERE replay_key = ?",
                    (clean_key,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            if not _is_corruption_error(exc):
                raise
            self._recover_after_database_error()
            return None
        if row is None:
            return None
        raw_result_json, expected_sha256 = str(row[0]), str(row[1])
        try:
            result = json.loads(raw_result_json)
            actual_sha256 = hashlib.sha256(raw_result_json.encode("utf-8")).hexdigest()
            if not isinstance(result, dict) or actual_sha256 != expected_sha256:
                raise ValueError("receipt row hash/schema mismatch")
        except (json.JSONDecodeError, ValueError):
            self._quarantine_record(
                clean_key, raw_result_json, "receipt row hash/schema mismatch"
            )
            return None
        return result

    def put(self, replay_key: str, result: dict[str, Any]) -> None:
        clean_key = str(replay_key or "").strip()
        if not clean_key:
            raise ValueError("replay_key is required")
        if not isinstance(result, dict):
            raise TypeError("result must be a dict")
        result_json, result_sha256 = _canonical_result(result)

        for attempt in range(2):
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO execution_receipts(
                                replay_key, stored_at, result_json, result_sha256
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                clean_key,
                                datetime.now(UTC).isoformat(),
                                result_json,
                                result_sha256,
                            ),
                        )
                        self._prune(connection)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                return
            except sqlite3.DatabaseError as exc:
                if attempt:
                    raise
                if not _is_corruption_error(exc):
                    raise
                self._recover_after_database_error()
