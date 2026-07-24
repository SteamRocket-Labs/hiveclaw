from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import uuid

import pytest

from app.services.exact_secret_boundary import ExactSecretBoundary


def test_stream_receivers_do_not_log_raw_user_text_before_durable_boundary() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "app/services/dingtalk_stream.py",
        "app/services/wecom_stream.py",
        "app/services/wechat_personal_stream.py",
    ):
        source = (backend_root / relative_path).read_text(encoding="utf-8")
        assert "user_text[:80]" not in source


@pytest.mark.asyncio
async def test_session_v2_redacts_exact_secret_before_building_durable_content_parts(
    monkeypatch,
) -> None:
    import app.services.session_live_input as live_input

    exact_secret = "tenant-live-secret-0123456789"
    captured: dict = {}

    async def fake_redact(_db, *, payload, **_kwargs):
        boundary = ExactSecretBoundary.from_pairs([("llm-model://model-1/api_key", exact_secret)])
        return boundary.redact_payload_with_evidence(payload)

    def capture_content_parts(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop-before-persistence")

    monkeypatch.setattr(
        "app.services.credential_boundary_loader.redact_runtime_ingress_payload",
        fake_redact,
    )
    monkeypatch.setattr(live_input, "content_parts_from_live_ingress", capture_content_parts)

    tenant_id = uuid.uuid4()
    with pytest.raises(RuntimeError, match="stop-before-persistence"):
        await live_input.submit_live_human_input(
            db=SimpleNamespace(),
            agent=SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id),
            user=SimpleNamespace(id=uuid.uuid4()),
            session=SimpleNamespace(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                delivery_target_json={},
            ),
            content=f"Use {exact_secret}; preserve api_key=sk-example-not-authority.",
            display_content=f"Display {exact_secret}",
            file_name=f"{exact_secret}.txt",
            attachments=[{"label": exact_secret}],
            parts=[{"type": "text", "text": exact_secret}],
            source="web",
        )

    assert exact_secret not in str(captured)
    assert "api_key=sk-example-not-authority" in captured["content"]
    assert captured["content"].count("[REDACTED_SECRET]") == 1
    assert captured["display_content"] == "Display [REDACTED_SECRET]"
    assert captured["file_name"] == "[REDACTED_SECRET].txt"
    assert captured["attachments"] == [{"label": "[REDACTED_SECRET]"}]
    assert captured["parts"] == [{"type": "text", "text": "[REDACTED_SECRET]"}]


@pytest.mark.asyncio
async def test_active_turn_redacts_exact_secret_before_session_v2_and_queued_projection(
    monkeypatch,
) -> None:
    import app.services.web_chat_runtime as runtime

    exact_secret = "tenant-queued-secret-0123456789"
    captured: dict = {}

    async def fake_redact(_db, *, payload, **_kwargs):
        boundary = ExactSecretBoundary.from_pairs([("tool-config://tool-1/token", exact_secret)])
        return boundary.redact_payload_with_evidence(payload)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return {
            "input_id": str(uuid.uuid4()),
            "input_status": "queued",
            "dispatch_status": "mailbox_queued",
            "queue_ordinal": 7,
        }

    monkeypatch.setattr(
        "app.services.credential_boundary_loader.redact_runtime_ingress_payload",
        fake_redact,
    )
    monkeypatch.setattr(
        "app.services.session_live_input.submit_live_human_input",
        fake_submit,
    )

    tenant_id = uuid.uuid4()
    active_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        metadata_json={"turn_id": "turn-active"},
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
    )
    result = await runtime._submit_active_session_input(
        db=SimpleNamespace(),
        active_run=active_run,
        agent=SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id),
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            delivery_target_json={},
            last_message_at=None,
        ),
        content=f"Queue {exact_secret}; preserve token=example-placeholder.",
        display_content=f"Visible {exact_secret}",
        file_name=f"{exact_secret}.md",
        attachments=[{"name": exact_secret}],
        parts=[{"type": "text", "text": exact_secret}],
    )

    assert exact_secret not in str(captured)
    assert exact_secret not in str(result)
    assert "token=example-placeholder" in captured["content"]
    assert captured["runtime_metadata"]["exact_secret_ingress_redaction"] == {
        "schema": "hive.exact_secret_redaction_receipt",
        "schema_version": 1,
        "phase": "active_session_input",
        "redacted_count": 5,
        "source_refs": ["tool-config://tool-1/token"],
    }
    assert result["queued"]["llm_content"].startswith("Queue [REDACTED_SECRET]")


@pytest.mark.asyncio
async def test_direct_web_run_redacts_exact_secret_before_runtime_task_and_chat_message(
    monkeypatch,
) -> None:
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime as runtime
    from app.services.runtime_budget_failover import unavailable_runtime_budget_root_binding

    exact_secret = "tenant-direct-secret-0123456789"
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Agent", tenant_id=tenant_id)
    user = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        title="Session",
        last_message_at=None,
        root_session_id=None,
        delivery_target_json={},
    )

    class FakeDB:
        def __init__(self) -> None:
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def fake_redact(_db, *, payload, **_kwargs):
        boundary = ExactSecretBoundary.from_pairs([("llm-model://model-1/api_key", exact_secret)])
        return boundary.redact_payload_with_evidence(payload)

    async def no_active(*_args, **_kwargs):
        return None

    async def unavailable(**_kwargs):
        return unavailable_runtime_budget_root_binding(
            source="web",
            interactive=True,
            error=RuntimeError("budget store offline"),
        )

    async def no_op(*_args, **_kwargs):
        return None

    async def assign_writer_generation(_db, task):
        task.writer_generation = 1
        return 1

    monkeypatch.setattr(
        "app.services.credential_boundary_loader.redact_runtime_ingress_payload",
        fake_redact,
    )
    monkeypatch.setattr(runtime, "_find_active_run", no_active)
    monkeypatch.setattr(runtime, "_create_runtime_budget_root_run_for_chat", unavailable)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", no_op)
    monkeypatch.setattr(runtime, "register_runtime_task_root_item", no_op)
    monkeypatch.setattr(
        "app.services.session_writer_epoch.assign_runtime_task_writer_generation",
        assign_writer_generation,
    )
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        no_op,
    )
    db = FakeDB()

    await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=f"Run {exact_secret}; preserve bearer example-placeholder.",
        display_content=f"Visible {exact_secret}",
        file_name=f"{exact_secret}.txt",
        attachments=[{"name": exact_secret}],
        parts=[{"type": "text", "text": exact_secret}],
    )

    task = next(value for value in db.added if isinstance(value, RuntimeTask))
    message = next(value for value in db.added if isinstance(value, ChatMessage))
    assert exact_secret not in task.prompt
    assert exact_secret not in str(task.metadata_json)
    assert exact_secret not in message.content
    assert "bearer example-placeholder" in task.prompt
    assert task.metadata_json["exact_secret_ingress_redaction"] == {
        "schema": "hive.exact_secret_redaction_receipt",
        "schema_version": 1,
        "phase": "web_chat_run",
        "redacted_count": 5,
        "source_refs": ["llm-model://model-1/api_key"],
    }
