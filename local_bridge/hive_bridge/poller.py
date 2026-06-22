from __future__ import annotations

from hive_bridge.client import HiveBridgeClient
from hive_bridge.runtime import CommandAdapter, NoopAdapter, WorkRequestAdapter, coerce_work_result


class HiveBridgeRunner:
    def __init__(self, *, client: HiveBridgeClient, adapter: WorkRequestAdapter | None = None) -> None:
        self.client = client
        self.adapter = adapter or NoopAdapter()

    def run_once(self) -> int:
        inbox = self.client.poll_inbox()
        processed = 0
        for message in inbox.get("messages", []):
            metadata = message.get("metadata") or {}
            if metadata.get("kind") != "work_request":
                continue
            result = coerce_work_result(self.adapter.handle(message))
            self.client.report_result(
                message_id=message["id"],
                result=result.result,
                attachments=result.attachments,
                metadata=result.metadata,
            )
            processed += 1
        return processed
