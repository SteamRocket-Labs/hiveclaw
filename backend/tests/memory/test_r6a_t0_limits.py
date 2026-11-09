import hashlib
import json
from uuid import uuid4

import pytest


@pytest.mark.parametrize("reader", ["replay", "tail", "find", "legacy"])
def test_oversized_t0_record_fails_before_materializing_without_rewriting(tmp_path, reader):
    from app.memory.t0 import ledger

    agent, session = uuid4(), uuid4()
    written = ledger.append_t0_session_event(agent_id=agent, session_id=session,
        event_type="tool_result", role="tool", content="ordinary", data_root=tmp_path)
    record = json.loads(written.jsonl_path.read_text())
    record["content"] = "x" * (17 * 1024 * 1024)
    record["event_hash"] = ledger._record_hash(record)
    written.jsonl_path.write_text(json.dumps(record, ensure_ascii=False) + "\n")
    index_path = written.jsonl_path.parents[2] / "index.json"
    index = json.loads(index_path.read_text())
    index["last_event_hash"] = record["event_hash"]
    if reader == "legacy":
        written.path.write_text(written.path.read_text().replace("<content>ordinary</content>",
            "<content>" + record["content"] + "</content>"))
        written.jsonl_path.unlink()
        index["segments"][0].pop("events_path", None)
    index_path.write_text(json.dumps(index))
    source = written.path if reader == "legacy" else written.jsonl_path
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    kwargs = dict(agent_id=agent, session_id=session, data_root=tmp_path)
    with pytest.raises(ledger.T0LedgerIntegrityError, match="t0_record_too_large"):
        if reader in {"replay", "legacy"}:
            ledger.replay_t0_session_events(**kwargs)
        elif reader == "tail":
            ledger.replay_t0_session_events_tail(**kwargs, limit=1)
        else:
            ledger.find_t0_transcript_event(**kwargs, transcript_event_id="missing", transcript_sequence=1)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("event_type", ["tool_result", "tool_result.completed", "user_message"])
def test_direct_t0_tool_writes_bound_text_and_the_metadata_copy(tmp_path, structured, event_type):
    from app.memory.t0 import ledger

    agent, session = uuid4(), uuid4()
    raw = "file.txt:1:match\n" * 100_000
    content = {"name": "grep_search", "result": raw} if structured else raw
    ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type=event_type, role="tool",
        content=content, metadata={"v2_payload": {"content": raw, "model_visible_content": "unchanged"}}, data_root=tmp_path)
    event = ledger.replay_t0_session_events(agent_id=agent, session_id=session, data_root=tmp_path)[0]
    assert len(event.content.encode()) <= 1024 * 1024
    assert len(event.metadata["v2_payload"]["content"].encode()) <= 1024 * 1024
    assert event.metadata["v2_payload"]["model_visible_content"] == "unchanged"
    assert "结果过大，只保留了前面部分。" in event.content


def test_ordinary_t0_replay_keeps_content_metadata_and_order(tmp_path):
    from app.memory.t0 import ledger

    agent, session = uuid4(), uuid4()
    contents = ["ordinary first", '"\\\n' * 60_000, "最后生日记录"]
    for content in contents:
        ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type="tool_result", role="tool",
            content=content, metadata={"source_ref": "ordinary"}, data_root=tmp_path)
    events = ledger.replay_t0_session_events(agent_id=agent, session_id=session, data_root=tmp_path)
    assert [event.content for event in events] == contents
    assert [event.sequence for event in events] == [1, 2, 3]
    assert all(event.metadata["source_ref"] == "ordinary" for event in events)


@pytest.mark.parametrize("surface", ["source.md", "events.jsonl"])
def test_t3_marks_oversized_t0_evidence_unavailable_and_keeps_other_sessions(tmp_path, surface):
    from app.memory.t3_consolidation import _load_t0_evidence

    agent = uuid4()
    refs = []
    for session in ("large", "ordinary"):
        root = tmp_path / str(agent) / "memory/t0/sessions" / session / "segments/segment"
        root.mkdir(parents=True)
        (root / "source.md").write_text("<t0_event><content>ordinary</content></t0_event>")
        (root / "events.jsonl").write_text('{"content":"ordinary"}\n')
        if session == "large":
            text = "x" * (17 * 1024 * 1024)
            (root / surface).write_text("<t0_event><content>" + text + "</content></t0_event>" if surface == "source.md"
                else json.dumps({"content": text}) + "\n")
        refs.append(f"t0://session/{session}/segment/segment")
    evidence = _load_t0_evidence(root=tmp_path, agent_id=agent, source_refs=refs)
    assert evidence[0]["available"] is False
    assert evidence[0]["unavailable_reason"] == "t0_record_too_large"
    assert evidence[0]["source_md"] == evidence[0]["events_jsonl"] == ""
    assert evidence[1]["available"] is True
    assert evidence[1]["source_md"] == "<t0_event><content>ordinary</content></t0_event>"
    assert evidence[1]["events_jsonl"] == '{"content":"ordinary"}\n'
