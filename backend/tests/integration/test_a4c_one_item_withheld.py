"""A4c: a failed source recheck removes only that one item, and the bot is told.

Covers the three A4c regressions that need more than plain unit fixtures:

1. A command-created scheduled input (no configuration snapshot) must reach the
   model: the member's own verified command is the source (S0b contract), so
   ``native_trigger_input_sources`` reports it complete instead of refusing.
2. The per-round withhold: one failing dependency replaces its bound message
   with a typed note, coverage stays complete, and other items continue.
3. An unattributable failure still stops the round (fail closed).
"""

import json
import platform
import uuid
from datetime import datetime, timezone

import pytest

from tests.integration import conftest as integration
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("migrated_pg_url"),
    pytest.mark.skipif(platform.system() != "Darwin", reason="Native macOS context gate; Linux delivery is deferred."),
]


@pytest.fixture
def migration_pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture
def migrated_pg_url(request, migrated_pg_url):
    if request.node.get_closest_marker("isolated_migration") is None:
        return migrated_pg_url
    return integration.migrated_pg_url.__wrapped__(request.getfixturevalue("migration_pg_container"))


async def test_command_created_scheduled_input_is_complete(owner_sessionmaker, monkeypatch):
    """A /loop created from the commands API carries no configuration snapshot.

    Before A4c the immediate run was refused with
    ``native_trigger_input_sources_unavailable`` before the model call; the
    member's own hash-verified command is the source, so the run must proceed.
    The upstream origin loader is covered by the trigger requester batch tests;
    this test pins the changed completeness decision itself.
    """
    from types import SimpleNamespace

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.session_model_inputs import _hash
    from app.services.trigger_instruction_sources import _identity, native_trigger_input_sources

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    wrapper_id, input_id, command_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="A4c Loop Tenant", slug=f"a4c-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(User(id=user_id, username=f"a4c-{user_id.hex[:8]}", email=f"{user_id.hex[:8]}@a4c.test",
            password_hash="x", display_name="A4c", tenant_id=tenant_id))
        await db.flush()
        db.add(Agent(id=agent_id, name="A4c Loop Agent", role_description="probe", tenant_id=tenant_id,
            creator_id=user_id, sponsor_user_id=user_id, owner_user_id=user_id, agent_type="native", status="idle"))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id,
            title="a4c", session_kind="human_chat", runtime_source="web_chat", source_channel="web"))
        await db.flush()
        from app.models.runtime_task import RuntimeTask

        db.add(RuntimeTask(id=wrapper_id, task_type="trigger", tenant_id=tenant_id,
            parent_agent_id=agent_id, root_user_id=user_id, parent_session_id=str(session_id),
            status="completed", metadata_json={"delivery": "same_session", "source_session_id": str(session_id),
                "trigger_ids": []}))
        await db.flush()
        # The exact proof shape the S0b/A4c repro produced for a command-created
        # loop: complete=False, configuration_sources=[] (no model configuration).
        parts = [{"text": "===== Trigger Awakening Context =====", "type": "text"}]
        proof = {"schema": "hive.native_trigger_input.v1", "wrapper_id": str(wrapper_id),
            "input_id": str(input_id), "command_id": str(command_id),
            "request_hash": _hash({"v": 1}), "target_hash": _hash({"v": 2}),
            "input_sha256": _hash(parts), "trigger_ids": [], "configuration_sources": [],
            "complete": False}
        db.add(InvocationSpan(id=_identity(wrapper_id, input_id), tenant_id=tenant_id, agent_id=agent_id,
            user_id=user_id, session_id=str(session_id), runtime_task_id=wrapper_id,
            trace_id=f"trigger:{wrapper_id}", span_id=f"native-trigger-input:{_identity(wrapper_id, input_id)}",
            span_type="native_trigger_input", name="scheduled_input", status="captured", duration_ms=0,
            started_at=datetime.now(timezone.utc), metadata_json={"native_trigger_input": proof}))
        await db.commit()

    async def fake_origin(_db, **_kwargs):
        return {"wrapper_id": str(wrapper_id), "input_id": str(input_id), "command_id": str(command_id),
            "request_hash": proof["request_hash"], "target_hash": proof["target_hash"],
            "input_sha256": proof["input_sha256"],
            "requester_user_id": str(user_id), "trigger_ids": []}

    monkeypatch.setattr("app.services.trigger_input_authority.load_trigger_input_origin", fake_origin)

    row = SimpleNamespace(id=input_id, session_id=session_id, content_parts_json=parts)
    command = SimpleNamespace(id=command_id, request_hash=proof["request_hash"], target_hash=proof["target_hash"],
        target_json={"runtime_metadata": {"source": "loop_same_session"}})
    async with owner_sessionmaker() as db:
        references, complete = await native_trigger_input_sources(
            db, tenant_id=tenant_id, agent_id=agent_id, row=row, command=command)

    assert complete, "a command-created scheduled input must reach the model"
    assert len(references) == 1


# ---------------------------------------------------------------------------
# The per-round withhold (pure helpers; no database).
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self):
        self.added = []

    async def get(self, *_args, **_kwargs):
        return None

    async def scalar(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(scalars=lambda: [])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


def _bindings(message_count, dependencies_of_message):
    messages = [{"message_index": i, "status": "bound", "code": "canonical_origin_matched",
                 "dependencies": dependencies_of_message.get(i, []),
                 "dependency_status": "resolved"} for i in range(message_count)]
    all_deps = []
    for deps in dependencies_of_message.values():
        all_deps.extend(deps)
    return {"schema": "hive.model_input_bindings.v1", "status": "resolved", "messages": messages,
            "dependencies": list({json.dumps(d, sort_keys=True): d for d in all_deps}.values()),
            "dependency_coverage": {"complete": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,count,private_payload", [("explicit_memory", 1, False), ("team_memory", 1, False),
    ("learned_skill_file", 1, False), ("team_task_input", 1, False), ("explicit_memory", 5, False),
    ("explicit_memory", 1, True)])
async def test_one_failing_source_replaces_its_message_and_continues(monkeypatch, kind, count, private_payload):
    from app.services import session_model_round as rounds

    bad = [{"source_kind": kind, "source_id": f"receipt-{i}", "source_version": "v1"} for i in range(count)]
    good = {"source_kind": "session_input", "source_id": "input-1", "source_version": "v1"}
    input_bindings = _bindings(count + 1, {**{i: [ref] for i, ref in enumerate(bad)}, count: [good]})
    messages = [{"role": "user", "content": f"recalled block {i}"} for i in range(count)]
    messages.append({"role": "user", "content": "member message"})
    if private_payload:
        marker = "WITHDRAWN_PRIVATE_PAYLOAD"
        messages[0].update(role="assistant", reasoning_content=marker, reasoning_signature=marker,
            tool_calls=[{"id": "tainted-call", "type": "function", "function": {
                "name": "run_command", "arguments": json.dumps({"command": marker})}}])
        messages.insert(1, {"role": "tool", "tool_call_id": "tainted-call", "content": marker})
        input_bindings = _bindings(3, {0: [bad[0]], 1: [good], 2: [good]})
    member_index = len(messages) - 1
    wire_request = {"messages": [dict(m) for m in messages]}

    async def refuse(_db, *, dependencies, **_kwargs):
        from app.kernel.contracts import ExecutionAuthorityUnavailable

        if any(ref in bad for ref in dependencies or []):
            raise ExecutionAuthorityUnavailable(code="content_source_changed", task=_Task(),
                effect="model_request")

    async def allow(_db, **_kwargs):
        return None

    monkeypatch.setattr("app.services.shared_group_authority.validate_shared_group_model_inputs", allow)
    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources", allow)
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", allow)
    monkeypatch.setattr("app.services.explicit_memory_authority.validate_memory_model_sources", refuse)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(rounds, "_bound_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(rounds, "append_session_events", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.capture_context_message_origins", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.build_model_input_bindings", AsyncMock(return_value=input_bindings))
    db = _FakeDB()
    await rounds.prepare_model_request(db, tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(), run_id=uuid.uuid4(), turn_id="a4c", round_index=0,
        messages=messages, tools=[], provider="local", model="a4c", wire_request=wire_request)

    request = db.added[0].model_request_snapshot_json["wire_request"]
    assert all("[context item withheld]" in m["content"] and kind in m["content"]
        for m in request["messages"][:count])
    assert "recalled block" not in json.dumps(request)
    assert all("recalled block" not in m["content"] for m in messages)
    assert request["messages"][member_index]["content"] == "member message"
    assert all(entry["status"] == "withheld" for entry in input_bindings["messages"][:count])
    assert "withheld_sources" not in input_bindings
    # A withheld entry is settled: coverage stays complete for downstream consumers.
    assert input_bindings["dependency_coverage"]["complete"] is True
    assert all(ref not in input_bindings["dependencies"] for ref in bad)
    if private_payload:
        assert marker not in json.dumps(request)
        assert marker not in json.dumps(messages)
        assert all(m["role"] == "system" and set(m) == {"role", "content"}
            for m in request["messages"][:2])


@pytest.mark.asyncio
async def test_unattributable_failure_still_stops_the_round(monkeypatch):
    from app.kernel.contracts import ExecutionAuthorityUnavailable

    from app.services import session_model_round as rounds

    input_bindings = _bindings(1, {})
    messages = [{"role": "user", "content": "member message"}]
    wire_request = {"messages": [dict(m) for m in messages]}

    async def refuse(_db, **_kwargs):
        raise ExecutionAuthorityUnavailable(code="reader_unavailable", task=_Task(), effect="model_request")

    async def allow(_db, **_kwargs):
        return None

    monkeypatch.setattr("app.services.shared_group_authority.validate_shared_group_model_inputs", refuse)
    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources", allow)
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", allow)
    monkeypatch.setattr("app.services.explicit_memory_authority.validate_memory_model_sources", allow)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(rounds, "_bound_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(rounds, "append_session_events", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.capture_context_message_origins", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.build_model_input_bindings", AsyncMock(return_value=input_bindings))
    db = _FakeDB()

    with pytest.raises(ExecutionAuthorityUnavailable):
        await rounds.prepare_model_request(db, tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(),
            session_id=uuid.uuid4(), run_id=uuid.uuid4(), turn_id="a4c", round_index=0,
            messages=messages, tools=[], provider="local", model="a4c", wire_request=wire_request)
    assert not db.added


class _Task:
    id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    parent_session_id = str(uuid.uuid4())
    root_user_id = uuid.uuid4()
