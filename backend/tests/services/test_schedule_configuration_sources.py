"""Schedule writes retain the real authors of reused configuration fields."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from app.models.invocation_span import InvocationSpan
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.trigger_context_sources import record_trigger_configuration, trigger_field_dependencies

FIELDS = {"name", "type", "config", "reason"}


class MemoryDB:
    def __init__(self):
        self.rows = {}

    async def get(self, model, identity, **_kwargs):
        return self.rows.get((model, identity))

    def add(self, row):
        self.rows[type(row), row.id] = row

    async def flush(self):
        pass


@pytest.fixture
def source():
    tenant, agent = uuid4(), uuid4()
    author = SimpleNamespace(id=uuid4(), tenant_id=tenant)
    runner = SimpleNamespace(id=uuid4(), tenant_id=tenant)
    db = MemoryDB()
    db.rows[User, author.id] = author
    db.rows[User, runner.id] = runner

    def trigger(owner, name):
        return AgentTrigger(
            id=uuid4(),
            tenant_id=tenant,
            agent_id=agent,
            name=name,
            type="cron",
            config={"expr": "0 9 * * *", "created_by": str(owner.id), "authority_state": "owned"},
            reason="Prepare the selected report.",
        )

    return SimpleNamespace(
        db=db, author=author, runner=runner, original=trigger(author, "Original"), copied=trigger(runner, "Manual copy")
    )


async def proof(source, trigger):
    row = await source.db.get(InvocationSpan, uuid5(trigger.id, "trigger-configuration-pointer"))
    return row.metadata_json["configuration"]


async def test_manual_run_preserves_copied_model_sources_without_relabeling_the_runner(source):
    await record_trigger_configuration(source.db, trigger=source.original, actor=source.author, changed_fields=FIELDS)
    original = await proof(source, source.original)
    tool = {"kind": "tool", "invocation_id": str(uuid4())}
    original["sources"]["reason"] = tool
    frozen = deepcopy(original)
    await record_trigger_configuration(
        source.db,
        trigger=source.copied,
        actor=source.runner,
        changed_fields=FIELDS,
        copied_from=source.original,
        copied_fields={"name", "reason"},
    )
    copied = await proof(source, source.copied)
    assert copied["sources"]["reason"] == tool
    assert copied["sources"]["name"] == {"kind": "human", "user_id": str(source.author.id)}
    assert copied["sources"]["config"] == {"kind": "human", "user_id": str(source.runner.id)}
    assert await proof(source, source.original) == frozen


@pytest.mark.parametrize("missing", ["absent", "stale"])
async def test_manual_run_keeps_old_unknown_sources_unknown(source, missing):
    if missing == "stale":
        await record_trigger_configuration(
            source.db, trigger=source.original, actor=source.author, changed_fields=FIELDS
        )
        source.original.reason = "Changed outside the recorded producer."
    await record_trigger_configuration(
        source.db,
        trigger=source.copied,
        actor=source.runner,
        changed_fields=FIELDS,
        copied_from=source.original,
        copied_fields={"name", "reason"},
    )
    copied = await proof(source, source.copied)
    assert copied["sources"]["name"] == copied["sources"]["reason"] == {"kind": "unknown"}
    assert (
        await trigger_field_dependencies(source.db, source.copied.tenant_id, source.copied.agent_id, copied["sources"])
    )[1] is False


@pytest.mark.parametrize("known", [False, True])
async def test_partial_config_write_preserves_the_previous_contributors(source, known):
    if known:
        await record_trigger_configuration(
            source.db, trigger=source.original, actor=source.author, changed_fields=FIELDS
        )
    source.original.config = {**source.original.config, "expr": "0 10 * * *"}
    await record_trigger_configuration(
        source.db,
        trigger=source.original,
        actor=source.runner,
        changed_fields={"config"},
        partial_fields={"config"},
    )
    recorded = await proof(source, source.original)
    prior = {"kind": "human", "user_id": str(source.author.id)} if known else {"kind": "unknown"}
    assert recorded["sources"]["config"] == {
        "kind": "derived",
        "sources": [prior, {"kind": "human", "user_id": str(source.runner.id)}],
    }
    assert (
        await trigger_field_dependencies(
            source.db, source.original.tenant_id, source.original.agent_id, recorded["sources"]
        )
    )[1] is known
