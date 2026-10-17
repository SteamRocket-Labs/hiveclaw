"""A damaged primary ledger cannot become a successful partial replay."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.memory.t0 import ledger


def _ledger(tmp_path):
    agent, session = uuid.uuid4(), uuid.uuid4()
    for content in ("First accepted input", "Second accepted input"):
        result = ledger.append_t0_session_event(agent_id=agent, session_id=session,
            event_type="user_message", role="user", content=content, source="web", data_root=tmp_path)
    return agent, session, result


@pytest.mark.parametrize("tail", [False, True])
@pytest.mark.parametrize("damage", ["body", "truncated", "version", "scope", "chain", "index", "missing_primary"])
def test_replay_refuses_unverified_records_without_using_the_markdown_projection(tmp_path, tail, damage):
    agent, session, result = _ledger(tmp_path)
    index_path = result.jsonl_path.parents[2] / "index.json"
    index = json.loads(index_path.read_text())
    records = [json.loads(line) for line in result.jsonl_path.read_text().splitlines()]
    if damage == "body":
        records[-1]["content"] = "UNVERIFIED_REWRITE"
    elif damage == "version":
        records[-1]["schema_version"] = "unknown.future.format"
    elif damage in {"scope", "chain"}:
        records[-1]["agent_id" if damage == "scope" else "prev_event_hash"] = str(uuid.uuid4()) if damage == "scope" else "0" * 64
        records[-1]["event_hash"] = ledger._record_hash(records[-1])
        index["last_event_hash"] = records[-1]["event_hash"]
    elif damage == "index":
        index["last_event_hash"] = "0" * 64
    result.jsonl_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    index_path.write_text(json.dumps(index))
    if damage == "truncated":
        result.jsonl_path.write_bytes(result.jsonl_path.read_bytes()[:-8])
    elif damage == "missing_primary":
        result.jsonl_path.unlink()
    replay = ledger.replay_t0_session_events_tail if tail else ledger.replay_t0_session_events
    with pytest.raises(ValueError) as error:
        replay(agent_id=agent, session_id=session, data_root=tmp_path, **({"limit": 2} if tail else {}))
    assert error.value.retryable is True
    assert error.value.code.startswith("t0_")
    assert "UNVERIFIED_REWRITE" not in str(error.value)
    assert result.path.is_file()  # The older projection is preserved, never promoted around a damaged primary.


@pytest.mark.parametrize("damage", ["malformed", "missing", "version"])
def test_an_unavailable_existing_index_cannot_be_silently_recreated_by_a_writer(tmp_path, damage):
    agent, session, result = _ledger(tmp_path)
    index_path = result.jsonl_path.parents[2] / "index.json"
    good_index = index_path.read_bytes()
    if damage == "missing":
        index_path.unlink()
    elif damage == "malformed":
        index_path.write_text('{"segments":')
    else:
        index = json.loads(good_index)
        index["schema_version"] = "unknown.future.format"
        index_path.write_text(json.dumps(index))
    before = {path: path.read_bytes() for path in result.jsonl_path.parents[2].rglob("*") if path.is_file()}
    with pytest.raises(ValueError) as error:
        ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type="user_message",
            role="user", content="MUST_NOT_APPEND_ON_UNKNOWN_HISTORY", data_root=tmp_path)
    assert error.value.retryable is True
    assert {path: path.read_bytes() for path in result.jsonl_path.parents[2].rglob("*") if path.is_file()} == before
    index_path.write_bytes(good_index)
    ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type="user_message",
        role="user", content="Recovered after restoring the exact index", data_root=tmp_path)
    assert [event.content for event in ledger.replay_t0_session_events(agent_id=agent, session_id=session,
        data_root=tmp_path)] == ["First accepted input", "Second accepted input", "Recovered after restoring the exact index"]


def test_valid_tail_preserves_the_selected_complete_events_and_hashes(tmp_path):
    agent, session, _result = _ledger(tmp_path)
    full = ledger.replay_t0_session_events(agent_id=agent, session_id=session, data_root=tmp_path)
    tail = ledger.replay_t0_session_events_tail(agent_id=agent, session_id=session, data_root=tmp_path, limit=1)
    assert tail == full[-1:]
    assert tail[0].prev_event_hash == full[0].event_hash


@pytest.mark.parametrize("owned", [False, True])
def test_first_write_after_a_legacy_projection_preserves_the_existing_events(tmp_path, owned):
    agent, session, result = _ledger(tmp_path)
    index_path = result.jsonl_path.parents[2] / "index.json"
    index = json.loads(index_path.read_text())
    index["truth_surface"] = "source.md"
    index.pop("event_record_schema_version", None)
    index.pop("last_event_hash", None)
    for segment in index["segments"]:
        segment.pop("events_path", None)
    if owned:
        index["segments"][-1]["runtime_task_id"] = str(uuid.uuid4())
    index_path.write_text(json.dumps(index))
    result.jsonl_path.unlink()
    original_projection = result.path.read_bytes()
    if owned:
        before = {path: path.read_bytes() for path in result.jsonl_path.parents[2].rglob("*") if path.is_file()}
        with pytest.raises(ValueError) as error:
            ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type="user_message",
                role="user", content="Must not move the legacy owner's active stream", source="web", data_root=tmp_path)
        assert error.value.code == "t0_legacy_owner_reconciliation_required"
        assert {path: path.read_bytes() for path in result.jsonl_path.parents[2].rglob("*") if path.is_file()} == before
        return
    ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type="user_message",
        role="user", content="First modern input after legacy history", source="web", data_root=tmp_path)
    events = ledger.replay_t0_session_events(agent_id=agent, session_id=session, data_root=tmp_path)
    assert [event.content for event in events] == ["First accepted input", "Second accepted input",
        "First modern input after legacy history"]
    assert result.path.read_bytes() == original_projection


@pytest.mark.parametrize("entry", ["claimed", "restart"])
async def test_corrupt_child_history_is_held_instead_of_resuming_from_an_empty_transcript(tmp_path, monkeypatch, entry):
    from app.services import subagent_run_service as svc

    agent, session, result = _ledger(tmp_path)
    records = [json.loads(line) for line in result.jsonl_path.read_text().splitlines()]
    records[-1]["content"] = "UNVERIFIED_CHILD_HISTORY"
    result.jsonl_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    run, requester = str(uuid.uuid4()), str(uuid.uuid4())
    record = {"task_id": run, "task_type": svc.SUBAGENT_RUN_TASK_TYPE, "status": "running",
        "parent_agent_id": str(agent), "child_session_id": str(session), "root_user_id": requester,
        "root_session_id": "parent-session", "parent_session_id": "parent-session",
        "delegation_chain": ["agent:parent", "subagent:child"], "prompt": "Continue the existing work",
        "metadata": {"resume_after_restart": True, "resumable_subagent": True,
            "subagent_type": "explorer", "child_session_id": str(session)}}
    updates, wakes = [], []
    async def get(*_args, **_kwargs):
        return record
    async def active(**_kwargs):
        return [record]
    async def update(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True
    async def projection(**_kwargs):
        return None
    async def wake(**kwargs):
        wakes.append(kwargs)
    async def resolve(*_args, **_kwargs):
        raise AssertionError("Corrupt history must not reach model hydration or dispatch")
    monkeypatch.setattr(svc, "get_runtime_task_record", get)
    monkeypatch.setattr(svc, "list_active_runtime_task_records", active)
    monkeypatch.setattr(svc, "update_runtime_task_record", update)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", projection)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", resolve)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", wake)
    if entry == "claimed":
        assert await svc.dispatch_persisted_subagent_run(run) is True
    else:
        assert await svc.resume_persisted_subagent_runs() == []
    assert len(updates) == 1 and updates[0][1]["status"] == "needs_reconciliation"
    assert "t0_record_hash_mismatch" in str(updates[0][1]["metadata_json"])
    assert wakes == []
