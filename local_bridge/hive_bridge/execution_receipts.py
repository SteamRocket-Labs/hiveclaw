"""Durable replay cache for Local Agent execution results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LocalExecutionReceiptStore:
    """Persist completed result envelopes so reconnect replay never re-executes work."""

    def __init__(self, path: str | Path, *, max_records: int = 1000) -> None:
        self.path = Path(path).expanduser()
        self.max_records = max(1, int(max_records))

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": "hive.local_execution_receipts.v1", "records": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Local execution receipt store is unreadable: {self.path}"
            ) from exc
        if payload.get(
            "schema"
        ) != "hive.local_execution_receipts.v1" or not isinstance(
            payload.get("records"), dict
        ):
            raise RuntimeError(
                f"Local execution receipt store has an invalid schema: {self.path}"
            )
        return payload

    def get(self, replay_key: str) -> dict[str, Any] | None:
        record = self._read()["records"].get(str(replay_key))
        if not isinstance(record, dict) or not isinstance(record.get("result"), dict):
            return None
        return dict(record["result"])

    def put(self, replay_key: str, result: dict[str, Any]) -> None:
        clean_key = str(replay_key or "").strip()
        if not clean_key:
            raise ValueError("replay_key is required")
        payload = self._read()
        records = dict(payload["records"])
        records[clean_key] = {
            "stored_at": datetime.now(UTC).isoformat(),
            "result": dict(result),
        }
        if len(records) > self.max_records:
            ordered = sorted(
                records.items(), key=lambda item: str(item[1].get("stored_at") or "")
            )
            records = dict(ordered[-self.max_records :])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(
                {"schema": "hive.local_execution_receipts.v1", "records": records},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)
