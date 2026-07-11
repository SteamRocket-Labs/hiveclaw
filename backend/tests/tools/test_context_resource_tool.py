from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest


def _request(*, arguments: dict, tmp_path: Path):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="read_context_resource",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            workspace=tmp_path,
            session_id=str(uuid4()),
        ),
    )


@pytest.mark.asyncio
async def test_context_resource_tool_pages_hash_pinned_agent_context(monkeypatch, tmp_path):
    from app.services.agent_context import AgentContextResource
    from app.tools.handlers import context_resources

    content = "0123456789" * 80
    calls: list[tuple[object, object, str]] = []

    async def fake_load(*, agent_id, tenant_id, resource_ref):
        calls.append((agent_id, tenant_id, resource_ref))
        return AgentContextResource(
            ref=resource_ref,
            source_ref=f"agent-context://{resource_ref}",
            content=content,
        )

    monkeypatch.setattr(context_resources, "load_agent_context_resource", fake_load)
    request = _request(arguments={"ref": "company", "offset": 11, "limit": 256}, tmp_path=tmp_path)

    first = json.loads(await context_resources.read_context_resource(request))

    assert first["schema"] == "hive.agent_context_resource_page.v1"
    assert first["status"] == "ok"
    assert first["ref"] == "company"
    assert first["content"] == content[11:267]
    assert first["offset"] == 11
    assert first["next_offset"] == 267
    assert first["complete"] is False
    assert first["total_chars"] == len(content)
    assert len(first["sha256"]) == 64
    assert calls == [(request.context.agent_id, request.context.tenant_id, "company")]

    stale_request = _request(
        arguments={
            "ref": "company",
            "offset": first["next_offset"],
            "limit": 256,
            "expected_sha256": "0" * 64,
        },
        tmp_path=tmp_path,
    )
    stale = json.loads(await context_resources.read_context_resource(stale_request))

    assert stale["status"] == "stale_resource"
    assert stale["restart_offset"] == 0
    assert stale["actual_sha256"] == first["sha256"]
    assert "content" not in stale


@pytest.mark.asyncio
async def test_context_resource_tool_rejects_caller_selected_principal(tmp_path):
    from app.tools.handlers.context_resources import read_context_resource

    request = _request(
        arguments={"ref": "soul", "agent_id": str(uuid4())},
        tmp_path=tmp_path,
    )

    result = json.loads(await read_context_resource(request))

    assert result["status"] == "invalid_arguments"
    assert result["unknown_fields"] == ["agent_id"]


def test_context_resource_tool_is_always_available_but_excludes_personal_kb():
    from app.services.capability_gate import _CAPABILITY_GATE_EXEMPT_TOOLS
    from app.services.governance_capability_taxonomy import CORE_TOOL_NAMES
    from app.tools.collector import collect_tools
    from app.tools.governance import _STATIC_SAFE_TOOLS
    from app.tools.plan_mode_policy import PLAN_MODE_READONLY_TOOLS

    collected = collect_tools()
    schema = next(
        tool["function"] for tool in collected.openai_tools if tool["function"]["name"] == "read_context_resource"
    )

    assert "read_context_resource" in CORE_TOOL_NAMES
    assert "read_context_resource" in _CAPABILITY_GATE_EXEMPT_TOOLS
    assert "read_context_resource" in _STATIC_SAFE_TOOLS
    assert "read_context_resource" in PLAN_MODE_READONLY_TOOLS
    assert schema["parameters"]["properties"]["ref"]["enum"] == [
        "index",
        "soul",
        "company",
        "organization",
        "channels",
        "a2a-collaborators",
    ]
    resource_refs = schema["parameters"]["properties"]["ref"]["enum"]
    assert not any("personal" in ref or "knowledge" in ref for ref in resource_refs)
    assert "does not search or read Personal Knowledge Base" in schema["description"]
