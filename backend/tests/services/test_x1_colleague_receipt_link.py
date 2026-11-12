"""X1-r2 ruling 6: no new colleague id reaches member thread items.

The constructor never writes target_agent_id (the round-1 hunk is reverted),
and the user-audience projection of a stored delegation receipt carries no
colleague agent id or session id. The colleague conversation link gates on
the parent session being the viewer's own private session, never on an
agents-list lookup.
"""

from app.services.thread_items import _item_data, _project_for_audience


def _event_data() -> dict:
    return {
        "runtime_task_id": "run-1",
        "child_session_id": "session-child",
        "parent_session_id": "session-parent",
        "to_agent": "agent-colleague",
        "to_agent_name": "周报助手",
        "reason": "delegation_completed",
        "status": "completed",
    }


def test_item_data_never_writes_the_colleague_agent_id():
    data = _item_data("subagent_activity", event_type="child_session", data=_event_data())
    assert "target_agent_id" not in data
    assert data.get("child_session_id") == "session-child"


def _raw_item() -> dict:
    return {
        "schema": "hive.thread_item.v1",
        "schema_version": 1,
        "id": "item-1",
        "sequence": 7,
        "session_id": "session-parent",
        "run_id": "run-1",
        "item_type": "subagent_activity",
        "item_status": "done",
        "event_type": "child_session",
        "actor_type": "system",
        "content": "周报助手 已完成委派任务。",
        "metadata": dict(_event_data()),
        "parts": [],
        "evidence_refs": [],
        "item_data": {},
    }


def test_user_audience_projection_carries_no_colleague_agent_id():
    projected = _project_for_audience(_raw_item(), audience="user", preserve_user_content=False)
    data = projected["item_data"]
    assert not data.get("child_session_id")
    assert not data.get("target_agent_id")
    assert not data.get("to_agent")
    # The operator view keeps the machinery the member projection strips.
    operator = _project_for_audience(_raw_item(), audience="operator", preserve_user_content=False)
    assert operator["operator_details"]["metadata"]["to_agent"] == "agent-colleague"
