from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_recoverable_context_excerpt_carries_hash_pinned_continuation():
    from app.services.agent_context import AgentContextResource, render_context_resource_excerpt

    resource = AgentContextResource(
        ref="company",
        source_ref="agent-context://company",
        content="A" * 500 + "OMITTED_SUFFIX",
    )

    rendered = render_context_resource_excerpt(resource, budget_chars=320)

    assert "OMITTED_SUFFIX" not in rendered
    assert "context_ref=agent-context://company" in rendered
    assert "read_context_resource" in rendered
    assert '"ref":"company"' in rendered
    assert '"offset":' in rendered
    assert f'"expected_sha256":"{resource.sha256}"' in rendered


def test_personal_kb_is_not_an_agent_context_resource():
    from app.services.agent_context import AGENT_CONTEXT_RESOURCE_REFS

    assert "personal-kb" not in AGENT_CONTEXT_RESOURCE_REFS
    assert "knowledge" not in AGENT_CONTEXT_RESOURCE_REFS


@pytest.mark.asyncio
async def test_build_agent_context_renders_recoverable_previews_for_every_bounded_resource(monkeypatch, tmp_path):
    import app.services.agent_context as agent_context

    agent_id = uuid4()
    tenant_id = uuid4()
    soul = "SOUL_HEAD\n" + ("s" * 900) + "SOUL_SUFFIX"
    company = "COMPANY_HEAD\n" + ("c" * 900) + "COMPANY_SUFFIX"
    organization = "ORG_HEAD\n" + ("o" * 900) + "ORG_SUFFIX"
    a2a = "## A2A Collaborators\nA2A_HEAD\n" + ("a" * 900) + "A2A_SUFFIX"

    tool_agent_root = tmp_path / "tool" / str(agent_id)
    tool_agent_root.mkdir(parents=True)
    (tool_agent_root / "soul.md").write_text(soul, encoding="utf-8")
    org_root = tmp_path / "data" / f"enterprise_info_{tenant_id}"
    org_root.mkdir(parents=True)
    (org_root / "org_structure.md").write_text(organization, encoding="utf-8")

    sessions = [
        _FakeSession([[]]),
        _FakeSession([SimpleNamespace(value={"content": company})]),
    ]

    def fake_tenant_scoped_session(*_args, **_kwargs):
        return sessions.pop(0)

    async def fake_a2a(*_args, **_kwargs):
        return a2a

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path / "tool")
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path / "data")
    monkeypatch.setattr(agent_context, "_build_a2a_collaborators_context", fake_a2a)
    budget = SimpleNamespace(
        soul_budget_chars=360,
        relationships_budget_chars=360,
        company_info_budget_chars=360,
        org_structure_budget_chars=360,
    )

    prompt = await agent_context.build_agent_context(
        agent_id,
        "Ops Agent",
        tenant_id=tenant_id,
        budget_profile=budget,
        include_skill_catalog=False,
    )

    for ref in ("soul", "company", "organization", "a2a-collaborators"):
        assert f'"ref":"{ref}"' in prompt
    for omitted_suffix in ("SOUL_SUFFIX", "COMPANY_SUFFIX", "ORG_SUFFIX", "A2A_SUFFIX"):
        assert omitted_suffix not in prompt


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value or [])


class _FakeSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        if not self._execute_values:
            return _FakeScalarResult(None)
        return _FakeScalarResult(self._execute_values.pop(0))


@pytest.mark.asyncio
async def test_build_agent_context_limits_confirmation_rule_to_conversation_mode(monkeypatch, tmp_path):
    from app.services.agent_context import build_agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")

    conversation_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        invocation_scope="conversation",
    )

    sessions = [_FakeSession([[]]), _FakeSession([None])]
    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))

    task_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        invocation_scope="task",
    )

    # PR-22 quantified the confirmation rule with explicit trigger categories
    # ("Confirm with the user BEFORE any of these actions, every time: …").
    # The safety boundary is preserved; wording evolved.
    assert "Confirm with the user BEFORE" in conversation_prompt
    assert "Confirm with the user BEFORE" not in task_prompt
    assert "executing an assigned task autonomously" in task_prompt


@pytest.mark.asyncio
async def test_build_agent_context_limits_confirmation_rule_to_heartbeat_mode(monkeypatch, tmp_path):
    from app.services.agent_context import build_agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")

    heartbeat_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        invocation_scope="heartbeat",
    )

    assert "Confirm with the user BEFORE" not in heartbeat_prompt
    assert "self-evolution mode" in heartbeat_prompt


@pytest.mark.asyncio
async def test_build_agent_context_keeps_confirmation_rule_for_coordinator_mode(monkeypatch, tmp_path):
    from app.services.agent_context import build_agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")

    coordinator_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        invocation_scope="coordinator",
    )

    assert "Confirm with the user BEFORE" in coordinator_prompt
    assert "operating in coordinator mode" in coordinator_prompt


@pytest.mark.asyncio
async def test_build_agent_context_default_excludes_runtime_time_from_frozen_prefix(monkeypatch, tmp_path):
    import app.services.agent_context as agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    async def fake_runtime_metadata(*_args, **_kwargs):
        return ["\n## Current Time\n2026-06-12 09:30:00 (Asia/Shanghai)"]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(agent_context, "_build_runtime_metadata_sections", fake_runtime_metadata)

    prompt = await agent_context.build_agent_context(
        agent_id,
        "Ops Agent",
        include_focus=False,
        invocation_scope="conversation",
    )

    assert "Current Time" not in prompt
    assert "Asia/Shanghai" not in prompt


@pytest.mark.asyncio
async def test_frozen_context_uses_pinned_tenant_session_for_channel_and_company_reads(monkeypatch, tmp_path):
    import app.services.agent_context as agent_context

    agent_id = uuid4()
    tenant_id = uuid4()
    tenant_calls = []
    sessions = [
        _FakeSession([[SimpleNamespace(channel_type="slack")]]),
        _FakeSession([None, None, None]),
    ]

    def fake_tenant_scoped_session(requested_tenant_id, **_kwargs):
        tenant_calls.append(requested_tenant_id)
        return sessions.pop(0)

    async def no_a2a(*_args, **_kwargs):
        return ""

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(
        "app.database.async_session",
        lambda: (_ for _ in ()).throw(AssertionError("frozen context must not use an unscoped DB session")),
    )
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr(agent_context, "_build_a2a_collaborators_context", no_a2a)

    prompt = await agent_context.build_agent_context(
        agent_id,
        "Ops Agent",
        tenant_id=tenant_id,
        include_skill_catalog=False,
    )

    assert tenant_calls == [tenant_id, tenant_id]
    assert "You have slack channel(s) configured" in prompt


@pytest.mark.asyncio
async def test_unreadable_existing_soul_is_a_required_context_failure(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from app.kernel.contracts import ContextDependencyUnavailable
    from app.services import agent_context

    agent_id = uuid4()
    tool_root = tmp_path / "tool"
    data_root = tmp_path / "data"
    soul_path = tool_root / str(agent_id) / "soul.md"
    soul_path.parent.mkdir(parents=True)
    soul_path.write_text("# Soul", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == soul_path:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tool_root)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", data_root)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    with pytest.raises(ContextDependencyUnavailable) as exc:
        await agent_context.load_agent_context_resource(
            agent_id=agent_id,
            tenant_id=uuid4(),
            resource_ref="soul",
        )

    assert exc.value.dependency == "soul"
    assert exc.value.code == "soul_context_unavailable"
