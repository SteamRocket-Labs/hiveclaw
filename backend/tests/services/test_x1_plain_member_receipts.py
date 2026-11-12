"""X1: tool results the member's reply is built from name things plainly.

The creation receipt and the plaza publish receipt are what the model copies
into its member reply. They must lead with the member-facing name and the
existing route, and keep ids only in a clearly internal-marked tail.
"""

import uuid


def _published_post_receipt(content, post_id):
    from app.services.agent_tool_domains.plaza import _published_post_receipt as impl

    return impl(content, post_id)


async def _creation_receipt_notification(db, draft, task, **kwargs):
    from app.services.employee_first_task import creation_receipt_notification as impl

    return await impl(db, draft, task, **kwargs)


class _StubAgent:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubDB:
    def __init__(self, agent) -> None:
        self._agent = agent

    async def get(self, model, key):
        return self._agent if model.__name__ == "Agent" else None


class _StubDraft:
    """Minimal HrCreationDraft shape for the pure summary builder."""

    def __init__(self, *, created_agent_name="陈小满", first_status="dispatched", failure_code=None, first_error=None):
        self.id = uuid.uuid4()
        self.tenant_id = uuid.uuid4()
        self.hr_agent_id = uuid.uuid4()
        self.session_id = uuid.uuid4()
        self.requested_by_user_id = uuid.uuid4()
        self.status = "completed"
        self.blueprint_hash = "hash"
        self.blueprint_json = {"name": "陈小满"}
        self.created_agent_id = uuid.uuid4() if created_agent_name is not None else None
        self.failure_code = failure_code
        first_task = {"status": first_status} if first_status != "not_requested" else {}
        if first_error is not None:
            first_task["error"] = first_error
        self.provisioning_json = {"first_task": first_task} if first_task else {}
        self.preview_json = {
            "creation_authorization": {
                "schema": "hive.employee_creation_authorization.v1",
                "mode": "policy_direct",
                "tenant_id": str(self.tenant_id),
                "requester_user_id": str(self.requested_by_user_id),
                "source_agent_id": str(self.hr_agent_id),
                "source_session_id": str(self.session_id),
                "blueprint_hash": self.blueprint_hash,
                "package": "private_native_employee.v1",
                "source_run_id": str(uuid.uuid4()),
                "input_id": str(uuid.uuid4()),
                "approved_model_id": str(uuid.uuid4()),
                "tool_arguments": {},
                "first_task": "介绍一下你自己",
            },
        }
        self.confirmed_by_user_id = self.requested_by_user_id
        self.confirmed_at = object()


class _StubTask:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status = "completed"


async def test_creation_receipt_names_the_employee_and_marks_ids_internal():
    draft, task = _StubDraft(), _StubTask()
    notification = await _creation_receipt_notification(_StubDB(_StubAgent("陈小满")), draft, task)
    summary = notification.summary
    # The name comes from the hash-pinned blueprint, not the mutable agent row.
    assert '"陈小满"' in summary and f"/agents/{draft.created_agent_id}" in summary
    # Ids appear only inside the internal-marked tail.
    assert "employee_id=" in summary.split("Internal:", 1)[1]
    assert "employee_id=" not in summary.split("Internal:", 1)[0]
    assert "draft_id=" not in summary.split("Internal:", 1)[0]
    # No error-code fields when nothing failed (the member previously read
    # 「无创建错误、无首个任务错误」 copied from them), and the route rule is stated.
    assert "creation_error=none" not in summary and "first_task_error=none" not in summary
    assert "Markdown link target only and is never quoted" in summary


async def test_creation_receipt_keeps_error_codes_when_they_are_real():
    draft, task = _StubDraft(first_status="blocked", failure_code="model_denied", first_error="approval_required"), _StubTask()
    notification = await _creation_receipt_notification(_StubDB(_StubAgent("陈小满")), draft, task)
    tail = notification.summary.split("Internal:", 1)[1]
    assert "creation_error=model_denied" in tail and "first_task_error=approval_required" in tail


async def test_creation_receipt_legacy_summary_still_reproduces_old_bytes():
    draft, task = _StubDraft(), _StubTask()
    notification = await _creation_receipt_notification(_StubDB(_StubAgent("陈小满")), draft, task, legacy_summary=True)
    assert notification.summary.startswith(f"Employee creation: completed; employee_id={draft.created_agent_id};")
    assert "first_task=dispatched; first_task_error=none; creation_error=none" in notification.summary


def test_plaza_publish_receipt_names_the_post_and_hides_the_id():
    receipt = _published_post_receipt("[E2E] 第十五轮测试帖，请忽略", uuid.UUID(int=1))
    # The head names the post by its content and the Plaza page (X1-r2 ruling 7).
    assert 'Post published to the company plaza: "[E2E] 第十五轮测试帖，请忽略"' in receipt
    assert "/plaza" in receipt
    assert "never quote the post id" in receipt
    assert receipt.split("Internal:", 1)[0].count("00000000-0000-0000-0000-000000000001") == 0
    assert "Internal: post_id=00000000-0000-0000-0000-000000000001" in receipt
