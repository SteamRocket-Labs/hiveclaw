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


async def test_command_created_scheduled_input_is_complete():
    from tests.integration.test_a4c_r2 import test_schedule_producer_distinguishes_no_pointer_command

    await test_schedule_producer_distinguishes_no_pointer_command("command")


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
    # Compiler-only origins are dataclasses; include their full fields in the
    # JSON leak check without sending that metadata to the provider.
    from dataclasses import asdict
    messages = json.loads(json.dumps(messages, default=asdict))
    assert all("有一项上下文暂时无法核实" in m["content"]
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
