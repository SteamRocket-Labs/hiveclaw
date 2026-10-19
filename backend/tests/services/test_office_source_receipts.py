"""Concrete discovery and connector witnesses cannot be fabricated by result text."""

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.parametrize("failure", ["unattested", "ambiguous_connection", "wrong_reader", "changed_body", "another_platform",
    "wrong_transport", "mixed_transports"])
async def test_feishu_capture_rejects_unwitnessed_or_mismatched_results(monkeypatch, failure):
    from app.services.feishu_read_receipts import capture_feishu_read, attest_feishu_api_read, settle_feishu_read
    from app.services.agent_tool_domains.feishu_acl import with_feishu_read_authority
    # Load transitive runtime consumers before replacing the process-wide DB factory.
    from app.services import session_model_inputs  # noqa: F401
    from app.tools.result_envelope import ToolContentEnvelope  # noqa: F401
    from app.services.feishu_platform import resolve_feishu_platform

    context = SimpleNamespace(agent_id=uuid4(), user_id=uuid4(), tenant_id=uuid4(), session_id=uuid4(),
        runtime_task_id=uuid4(), tool_call_id="read-company-document")
    calls = []
    def unexpected_database(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Rejected captures must not write any source receipt")
    monkeypatch.setattr("app.database.tenant_scoped_session", unexpected_database)
    def result(body):
        return with_feishu_read_authority(body, sources=[("feishu://doc/company", "doc")], agent_id=context.agent_id,
            tenant_id=context.tenant_id, current_user_id=uuid4() if failure == "wrong_reader" else context.user_id)
    with capture_feishu_read(context, "feishu_doc_read", {"document_token": "company"}) as capture:
        capture.connections.append({"id": "observed-connection"})
        if failure == "another_platform":
            capture.connections[0].update(platform_region="lark_global", open_api_domain="https://open.larksuite.com")
        value = result("Company source")
        if failure not in {"unattested", "wrong_transport"}:
            attest_feishu_api_read(value)
        if failure in {"wrong_transport", "mixed_transports"}:
            attest_feishu_api_read(value, platform=resolve_feishu_platform({"platform_region": "lark_global"}))
        if failure == "ambiguous_connection":
            capture.connections.append({"id": "another-observed-connection"})
        if failure == "changed_body":
            value = result("Different source bytes")
        assert await settle_feishu_read(capture, value) is value
    assert calls == []


@pytest.mark.parametrize("subagent", [False, True])
async def test_builtin_discovery_has_an_exact_empty_inventory_but_unknown_metadata_stays_unknown(monkeypatch, tmp_path, subagent):
    from app.runtime import invoker
    from app.skills.registry import SkillRegistry
    from app.services.skill_source_receipts import SkillInputCapture
    from app.services.agent_tool_domains import workspace
    from app.services import agent_tools

    capture = SkillInputCapture(uuid4(), allow_empty=True)
    monkeypatch.setattr(workspace, "_build_skill_registry", lambda *_a, **_kw: SkillRegistry())
    async def names(*_args):
        return ["feishu_doc_read"]
    monkeypatch.setattr(agent_tools, "discoverable_tool_names_for_query", names)
    monkeypatch.setattr(invoker, "discoverable_tool_names_for_query", names)
    monkeypatch.setattr("app.agents.subagent_definition.list_subagent_definitions", lambda **_kw:
        [{"name": "feishu analyst", "description": "A delegated analyst", "type": "agent"}] if subagent else [])
    value = await workspace._tool_search(tmp_path, "feishu", agent_id=capture.agent_id,
        authority_scope=SimpleNamespace(skill_inputs=capture))
    assert "feishu_doc_read" in value
    snapshot = capture.snapshot()
    assert snapshot["sources"] == []
    assert snapshot["unbound"] == (["discovery_subagent_sources_unverified"] if subagent else [])
    assert SkillInputCapture(capture.agent_id).snapshot()["unbound"] == ["skill_source_not_observed"]


async def test_discovery_does_not_read_foreign_workspace_skill_metadata(monkeypatch, tmp_path):
    from app.runtime import invoker
    from app.services.skill_source_receipts import SkillInputCapture
    from app.services.agent_tool_domains import workspace
    from app.services import agent_tools

    source = tmp_path / "workspace/skills/private/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: private\ndescription: FOREIGN_SECRET_METADATA\n---\nPrivate instructions")
    capture = SkillInputCapture(uuid4(), allow_empty=True)
    async def no_tools(*_args):
        return []
    monkeypatch.setattr(agent_tools, "discoverable_tool_names_for_query", no_tools)
    monkeypatch.setattr(invoker, "discoverable_tool_names_for_query", no_tools)
    monkeypatch.setattr("app.agents.subagent_definition.list_subagent_definitions", lambda **_kwargs: [])
    scope = SimpleNamespace(skill_inputs=capture, allowed_paths=frozenset(), operator_view=False, can_read=lambda _path: False)
    value = await workspace._tool_search(tmp_path, "private", agent_id=capture.agent_id, authority_scope=scope)
    assert "FOREIGN_SECRET_METADATA" not in value
    assert capture.snapshot() == {"sources": [], "unbound": []}
