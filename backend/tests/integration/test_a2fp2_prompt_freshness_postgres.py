"""A private member's next turn reads the current resident memory and Skill catalog."""

import hashlib
import uuid

import pytest

from app.core.events import close_redis
from app.models.workspace_resource import WorkspaceResourceManifest
from app.runtime import invoker
from app.services import web_chat_runtime as runtime
from tests.integration.test_a2fp1_private_turn_budget_postgres import (
    ANSWER,
    app_user_engine,  # noqa: F401
    assert_committed_reply,
)
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("source", ["resident_memory", "platform_skill", "scoped_skill"])
async def test_private_turn_uses_the_next_committed_prompt_source(
    member_request, owner_sessionmaker, monkeypatch, tmp_path, source,  # noqa: F811
):
    from app.services import llm_client
    from tests.integration.test_a2fp1_private_turn_budget_postgres import LocalAuxTransport

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    root = tmp_path / str(seed.agent_id)
    before, after = "The reference color is coral.", "The reference color is amber."
    catalogs = []
    if source == "resident_memory":
        relative = "memory/self/self.md"
        template = "# Self\n\n{}\n"
    elif source == "platform_skill":
        relative = "skills/colors/SKILL.md"
        template = "---\nname: colors\ndescription: {}\n---\nFollow the color convention.\n"
    else:
        relative = "workspace/project/skills/colors/SKILL.md"
        template = "---\nname: colors\ndescription: {}\n---\nFollow the color convention.\n"
        async with owner_sessionmaker() as db:
            db.add(WorkspaceResourceManifest(tenant_id=seed.tenant_id, agent_id=seed.agent_id, path=relative,
                owner_user_id=seed.user_id, root_session_id=seed.session_id, authority_state="owned"))
            await db.commit()
        build_catalog = invoker._build_native_skill_catalog

        async def record_catalog(*args, **kwargs):
            catalog = await build_catalog(*args, **kwargs)
            catalogs.append(catalog)
            return catalog

        monkeypatch.setattr(invoker, "_build_native_skill_catalog", record_catalog)

    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    published_skill = tmp_path / "published-SKILL.md"
    if source == "platform_skill":
        from app.services import skill_source_receipts

        bundled_hashes = skill_source_receipts._bundled_platform_skill_hashes

        def hashes(*, source_path=None):
            bundled = bundled_hashes(source_path=source_path)
            if source_path in {None, relative}:
                bundled[relative] = hashlib.sha256(published_skill.read_bytes()).hexdigest()
            return bundled

        monkeypatch.setattr(skill_source_receipts, "_bundled_platform_skill_hashes", hashes)
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport([]))
    try:
        for current, previous in ((before, after), (after, before)):
            written = template.format(current)
            if source == "platform_skill":
                published_skill.write_text(written, encoding="utf-8")
            path.write_text(written, encoding="utf-8")
            provider = ScriptedLLMClient([{"content": ANSWER, "stream_text": ANSWER}])
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/runs",
                json={"content": "hello", "input_id": str(uuid.uuid4())})
            assert response.status_code == 201, response.text
            result = response.json()
            run_id = uuid.UUID(result["run"]["run_id"])
            await claim(seed, run_id, owner_sessionmaker)
            assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="rl0c")
            await runtime._TASKS[run_id.hex]
            await assert_committed_reply(owner_sessionmaker, run_id, uuid.UUID(result["session"]["id"]), seed)
            request, = provider.requests
            text = "\n".join(message["content"] for message in request["messages"] if isinstance(message["content"], str))
            if source == "scoped_skill":
                # The uninstalled workspace skill reaches the catalog but has no versioned source
                # receipt to disclose through the provider boundary.
                catalog, = catalogs
                assert current in catalog and previous not in catalog
                catalogs.clear()
            else:
                assert current in text
            assert previous not in text
    finally:
        await close_redis()
