"""B2: the session title comes from what the member typed, never from the
runtime's JSON encoding of the input.

`dispatch_admitted_input` used to hand `start_web_chat_run` an empty
`display_content`, so the first turn of a conversation titled the session from
`content` — which `_runtime_content` renders as a JSON array whenever the input
has attachments. Production showed `chat_sessions.title` starting with
`[{"display_content":` (2026-11-04 run, screenshot 32).

The wiring test below drives the real path — `_start_input_runtime` →
`start_web_chat_run` → the orchestrator's `maybe_enter_plan_mode` call — and
captures the created task's `prompt`, the session title, and the classification
argument, so a detached constant or a re-classification onto the model prompt
cannot pass (B2 r3).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[2]

# The attachment input the production run sent (2026-11-04): the model prompt
# embeds the file content (including the words 计划模式 inside the attachment
# body), while the member's own words are the display text.
PARTS = [
    {
        "type": "text",
        "text": "[File: sales.csv]\nFile location: workspace/uploads/sales.csv\n\n| 计划模式 说明 |\n| --- |\n\nQuestion: 总结这个附件",
        "display_content": "[📎 sales.csv]\n总结这个附件",
        "file_name": "sales.csv",
    },
    {"type": "attachment_ref", "attachment": {"name": "sales.csv", "path": "workspace/uploads/sales.csv"}},
]
DISPLAY = "[📎 sales.csv]\n总结这个附件"
CANONICAL_PROMPT = json.dumps(PARTS, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_runtime_display_content_extracts_the_member_text_from_content_parts():
    from app.services.session_input_dispatch import _runtime_display_content

    # A message with attachments: the model gets the internal prompt, the
    # member gets their own words back.
    assert _runtime_display_content(PARTS) == DISPLAY

    # A plain message has no separate display text: the text is the message.
    assert _runtime_display_content([{"type": "text", "text": "hello"}]) == "hello"

    # Nothing to show: empty, never the JSON encoding.
    assert _runtime_display_content([]) == ""


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])


class _WiringDB:
    """The fake the web-chat runtime tests already use, plus the Session V2
    admission lookups `_start_input_runtime`'s metadata triggers."""

    def __init__(self, input_row=None, admission=None, command=None, session_row=None):
        self.added = []
        self.commits = 0
        self.input_row = input_row
        self.admission = admission
        self.command = command
        self.session_row = session_row

    async def execute(self, _stmt):
        text = str(_stmt)
        if "session_writer_epochs" in text:
            return _ScalarResult(SimpleNamespace(
                state="legacy_open",
                new_run_generation=1,
                allowed_existing_generations_json=[1],
                enforcement_mode="observe",
                version=1,
                release_id="test-epoch",
            ))
        if "session_event_cursors" in text:
            return _ScalarResult((self.session_row, None, 0))
        return _ScalarResult(None)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        return None

    async def scalar(self, stmt):
        text = str(stmt)
        if "session_turn_inputs" in text:
            return self.input_row
        if "session_input_admissions" in text:
            return self.admission
        return None

    async def get(self, model, _identifier):
        from app.models.session_v2 import SessionCommand

        if model is SessionCommand:
            return self.command
        return None


async def _run_input_through_runtime(monkeypatch, tmp_path):
    """Drive `_start_input_runtime` with the real `start_web_chat_run`."""
    import app.services.web_chat_runtime as runtime
    from app.services.session_input_dispatch import _start_input_runtime

    agent = SimpleNamespace(id=uuid.uuid4(), name="Agent", tenant_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), username="example-owner", display_name="Example Owner")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        tenant_id=agent.tenant_id,
        title="Session 05-21",
        last_message_at=None,
        audience_kind="private",
        listed_surface="chat",
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        content_parts_json=PARTS,
        target_turn_id=None,
        intent="queue_next_turn",
        status="queued",
        revision=1,
        version=1,
        command_id=uuid.uuid4(),
        rolled_over_to_turn_id=None,
        request_item_id=None,
    )
    command = SimpleNamespace(id=row.command_id, target_json={}, principal_type="user", principal_id=user.id)
    admission = SimpleNamespace(input_id=row.id, input_revision=1, state="admitted", command_id=row.command_id)
    db = _WiringDB(input_row=row, admission=admission, command=command, session_row=session)
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_lock_session_runtime_mutation", async_none, raising=False)
    monkeypatch.setattr(runtime, "_find_active_run", async_none)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await _start_input_runtime(
        db,
        row=row,
        command=command,
        agent=agent,
        user=user,
        session=session,
        successor=False,
    )
    return db, session, agent, user


async def async_none(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_input_runtime_wiring_keeps_prompt_model_faced_and_titles_member_faced(monkeypatch, tmp_path):
    """The real dispatch → runtime → orchestrator path (B2 r3).

    - `RuntimeTask.prompt` is the canonical runtime JSON, byte-for-byte the
      model-facing content (an attachment's 计划模式 wording reaches the model).
    - The session title is the member's display text, never the JSON encoding.
    - Plan Mode classification reads the member's sentence, so an attachment
      body that says 计划模式 with a plain request does not enter plan mode.
    """
    from app.models.runtime_task import RuntimeTask
    from app.services import plan_mode_core
    from app.services.web_chat_run_orchestrator import _WebChatRunState, _handle_pre_invocation_terminal

    db, session, agent, user = await _run_input_through_runtime(monkeypatch, tmp_path)

    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    assert task.prompt == CANONICAL_PROMPT
    assert "计划模式" in task.prompt
    assert task.metadata_json["display_content"] == DISPLAY
    assert "计划模式" not in task.metadata_json["display_content"]
    assert session.title == DISPLAY
    assert not session.title.startswith("[{")

    # The orchestrator's pre-invocation call classifies on the member's
    # words, with the model prompt passed as content.
    captured: dict[str, object] = {}

    async def capture_plan_mode(**kwargs):
        captured.update(kwargs)
        return None

    state = _WebChatRunState(
        run_uuid=task.id,
        ports=SimpleNamespace(context=SimpleNamespace(maybe_enter_plan_mode=capture_plan_mode)),
        cancel_event=asyncio.Event(),
        run_key=task.id.hex,
        broadcast_token=None,
        agent=agent,
        user=user,
        llm_model=object(),
        session_id=str(session.id),
        prompt=task.prompt,
        metadata=task.metadata_json,
    )
    assert await _handle_pre_invocation_terminal(state) is False
    assert captured["content"] == CANONICAL_PROMPT
    assert captured["classification_content"] == DISPLAY
    assert plan_mode_core.classify_plan_mode_entry(captured["classification_content"]).mode == "none"
    assert plan_mode_core.classify_plan_mode_entry(captured["content"]).mode == "explicit"
