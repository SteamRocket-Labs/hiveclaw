from __future__ import annotations


def test_tool_evidence_ledger_builds_summary_from_parts() -> None:
    from app.runtime.tool_evidence_ledger import ToolEvidenceLedger

    ledger = ToolEvidenceLedger.from_parts(
        [
            {"type": "tool_call", "name": "web_fetch", "status": "done", "result": "ok"},
            {"type": "tool_call", "name": "send_message_to_agent", "status": "failed", "result": "timeout"},
        ]
    )

    summary = ledger.to_summary()

    assert summary["schema"] == "hive.ccplus.tool_evidence_ledger.v1"
    assert summary["has_tool_evidence"] is True
    assert summary["tool_names"] == ["send_message_to_agent", "web_fetch"]
    assert summary["status_counts"]["done"] == 1
    assert summary["status_counts"]["failed"] == 1
    assert ledger.has_evidence_for("web_fetch") is True
    assert ledger.has_evidence_for("read_file") is False


def test_tool_evidence_ledger_records_malformed_replay_event() -> None:
    from app.runtime.tool_evidence_ledger import ToolEvidenceLedger

    ledger = ToolEvidenceLedger()
    ledger.record_replay_repair(tool_name="unknown", reason="malformed tool_call record")

    summary = ledger.to_summary()

    assert summary["has_tool_evidence"] is True
    assert summary["status_counts"]["replay_repair"] == 1
    assert summary["events"][0]["reason"] == "malformed tool_call record"
