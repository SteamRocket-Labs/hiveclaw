"""X1: the delegation receipt carries what the colleague link needs.

The receipt event already persists `to_agent`; the thread-item projection now
keeps it as `target_agent_id` so the member's conversation can link to the
colleague's conversation through the existing route.
"""

from app.services.thread_items import _item_data


def test_delegation_receipt_keeps_the_colleague_agent_id():
    data = _item_data(
        "subagent_activity",
        event_type="child_session",
        data={
            "runtime_task_id": "task-1",
            "child_session_id": "11111111-1111-1111-1111-111111111111",
            "parent_session_id": "22222222-2222-2222-2222-222222222222",
            "to_agent": "33333333-3333-3333-3333-333333333333",
            "to_agent_name": "[E2E] 第十五轮助手",
            "reason": "delegation_completed",
            "status": "completed",
        },
    )
    assert data["target_agent_id"] == "33333333-3333-3333-3333-333333333333"
    assert data["target_agent_name"] == "[E2E] 第十五轮助手"
    assert data["child_session_id"] == "11111111-1111-1111-1111-111111111111"


def test_receipts_without_an_agent_id_stay_linkless():
    data = _item_data(
        "subagent_activity",
        event_type="child_session",
        data={"runtime_task_id": "task-1", "child_session_id": "11111111-1111-1111-1111-111111111111"},
    )
    assert data.get("target_agent_id") in (None, "")
