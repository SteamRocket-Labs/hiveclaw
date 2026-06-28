from __future__ import annotations

from pathlib import Path

import pytest


def _write_skill(workspace: Path, *, slug: str = "review") -> None:
    skill_dir = workspace / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: Review",
                "description: Review work before final response.",
                "metadata:",
                "  hive:",
                "    hooks:",
                "      - user_prompt_submit",
                "      - post_tool_use",
                "---",
                "# Review",
                "Check evidence before answering.",
            ]
        ),
        encoding="utf-8",
    )


def _write_fork_skill(workspace: Path, *, slug: str = "research") -> None:
    skill_dir = workspace / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: Research",
                "description: Research with a scoped worker.",
                "allowed-tools:",
                "  - web_search",
                "  - read_file",
                "metadata:",
                "  hive:",
                "    context: fork",
                "    agent: researcher",
                "    hooks:",
                "      - user_prompt_submit",
                "---",
                "# Research",
                "Use a worker when the task needs isolated exploration.",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_loaded_skill_frontmatter_hooks_register_session_scoped_handlers(tmp_path: Path) -> None:
    from app.runtime.hooks import HookContext, HookEvent, HookRegistry
    from app.runtime.session import SessionContext
    from app.runtime.skill_hooks import register_loaded_skill_hooks

    workspace = tmp_path / "agent"
    _write_skill(workspace)
    registry = HookRegistry()
    session = SessionContext(session_id="session-1", metadata={"tenant_id": "tenant-1"})

    registered = register_loaded_skill_hooks(
        workspace,
        "review",
        session_context=session,
        registry=registry,
        agent_id="agent-1",
    )

    assert [item["event"] for item in registered] == ["user_prompt_submit", "post_tool_use"]
    registrations = registry.describe_registrations()
    assert {item["key"] for item in registrations} == {
        "skill:session-1:review:user_prompt_submit",
        "skill:session-1:review:post_tool_use",
    }
    assert all(item["handler_name"] == "skill_frontmatter_hook" for item in registrations)
    assert all(item["matcher_spec"]["session_ids"] == ["session-1"] for item in registrations)
    assert session.metadata["skill_hook_registrations"] == registered

    same_session = await registry.emit(
        HookContext(event=HookEvent.USER_PROMPT_SUBMIT, session_id="session-1", prompt="check it")
    )
    assert same_session is not None
    assert same_session.additional_contexts == [
        "Loaded skill `Review` declares a `user_prompt_submit` hook. Use its loaded instructions when this event applies."
    ]

    other_session = await registry.emit(
        HookContext(event=HookEvent.USER_PROMPT_SUBMIT, session_id="session-2", prompt="check it")
    )
    assert other_session is None


def test_loaded_skill_frontmatter_records_execution_plan_and_permission_profile(tmp_path: Path) -> None:
    from app.runtime.session import SessionContext
    from app.runtime.skill_hooks import register_loaded_skill_hooks

    workspace = tmp_path / "agent"
    _write_fork_skill(workspace)
    session = SessionContext(session_id="session-1", metadata={"tenant_id": "tenant-1"})

    registered = register_loaded_skill_hooks(
        workspace,
        "research",
        session_context=session,
        agent_id="agent-1",
    )

    assert registered
    plans = session.metadata["skill_execution_plans"]
    assert len(plans) == 1
    plan = plans[0]
    assert plan["skill"] == "Research"
    assert plan["execution_mode"] == "fork"
    assert plan["execution_tool"] == "spawn_subagent"
    assert plan["agent_type"] == "researcher"
    assert plan["permission_profile"]["allowed_tools"] == ["web_search", "read_file"]
    assert plan["permission_profile"]["mode"] == "auto"
    assert plan["tool_arguments"]["permission_profile"]["allowed_tools"] == ["web_search", "read_file"]
    assert session.metadata["permission_profile"]["allowed_tools"] == ["web_search", "read_file"]
    handoffs = session.metadata["pending_skill_handoffs"]
    assert handoffs == [
        {
            "skill": "Research",
            "skill_slug": "research",
            "source": "skills/research/SKILL.md",
            "execution_tool": "spawn_subagent",
            "tool_arguments": plan["tool_arguments"],
            "permission_profile": plan["permission_profile"],
        }
    ]
