from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_render_relationships_markdown_uses_explicit_relationships_and_current_tool_name():
    from app.services.relationships_file import render_relationships_markdown

    owner = SimpleNamespace(display_name="张三", username="zhangsan", title="产品经理")
    human_relationships = [
        SimpleNamespace(
            relation="collaborator",
            description="负责日常对接",
            member=SimpleNamespace(
                name="李四",
                title="研发经理",
                department_path="研发部/平台组",
                avatar_url=None,
                email="lisi@example.com",
                feishu_open_id="ou_xxx",
            ),
        )
    ]
    agent_relationships = [
        SimpleNamespace(
            relation="assistant",
            description="专门负责代码审查",
            target_agent=SimpleNamespace(
                id=uuid4(),
                name="代码助手",
                role_description="审查代码",
                avatar_url=None,
            ),
        )
    ]

    content = render_relationships_markdown(
        owner=owner,
        human_relationships=human_relationships,
        agent_relationships=agent_relationships,
    )

    assert "## 我的主人" in content
    assert "## 👤 人类同事" in content
    assert "## 🤖 数字员工同事" in content
    assert "send_message_to_agent" in content
    assert "send_agent_message" not in content
    assert "同公司数字员工" not in content

