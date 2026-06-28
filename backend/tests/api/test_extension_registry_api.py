"""GET /agents/{agent_id}/extensions serves the ExtensionRegistryV1 projection (U4).

This exercises the live route -> build_extension_registry_projection ->
tool_spec_v1 path. It fails if the route is unwired or if the ToolSpecV1
derivation feeding the tool_pack descriptors is reverted.

Named `extension_registry` so `pytest -k extension_registry` collects it.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.api.agents as agents_mod
from app.api.agents import router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_client():
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


@pytest.mark.asyncio
async def test_get_agent_extensions_returns_extension_registry_projection(monkeypatch):
    expected_agent_id = uuid4()
    client, fake_db, current_user = _build_client()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == expected_agent_id
        return SimpleNamespace(id=expected_agent_id), "manage"

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check_agent_access)

    # Make the route deterministic regardless of global tool-registry / taxonomy
    # state. We pin ONE controlled web_pack taxonomy descriptor and a
    # controlled ToolSpecV1 for exa_search, so this test exercises the real
    # route -> build_extension_registry_projection -> _tool_spec_effects path
    # against a fixed input. The raw ToolSpecV1-from-ToolMeta derivation is
    # covered separately by tests/tools/test_tool_spec_v1.py.
    import app.services.extension_registry as registry_mod
    import app.services.governance_capability_taxonomy as taxonomy_mod
    from app.runtime.ccplus_contracts import GovernanceCapabilityDescriptorV1, ToolSpecV1
    from app.services.command_registry import _command

    fake_descriptor = GovernanceCapabilityDescriptorV1(
        name="web_pack",
        layer="platform_addon",
        source="platform",
        tools=("exa_search",),
        default_enabled=False,
        l2_visible=True,
        enterprise_toggleable=True,
        notes="Controlled web pack",
    )
    monkeypatch.setattr(taxonomy_mod, "iter_runtime_l2_capabilities", lambda: (fake_descriptor,))
    monkeypatch.setattr(
        agents_mod,
        "build_default_command_registry",
        lambda *a, **k: SimpleNamespace(
            values=lambda: [
                _command(
                    "review",
                    "Review current work.",
                    category="coding_pack",
                    source="builtin",
                    execution_mode="metadata",
                    handler_ref="builtin:review",
                )
            ]
        ),
        raising=False,
    )

    class _FakeHookRegistry:
        def describe_event_catalog(self):
            return [
                {
                    "event": "post_tool_use",
                    "runtime_consumer": "kernel_post_tool_rewrite_consumer",
                    "lifecycle_state": "active_observe",
                }
            ]

    monkeypatch.setattr(agents_mod, "hook_registry", _FakeHookRegistry(), raising=False)
    monkeypatch.setattr(
        agents_mod,
        "get_agent_mcp_servers",
        lambda *_args, **_kwargs: [
            {
                "id": "mcp-docs",
                "name": "Docs MCP",
                "tools": ["mcp__docs__search"],
                "resources": ["skill://docs/research"],
            }
        ],
        raising=False,
    )

    def fake_tool_spec_v1(name: str):
        if name == "exa_search":
            return ToolSpecV1(
                name="exa_search",
                capability="web_pack",
                read_only=True,
                concurrency_safe=True,
                defer_loading=True,
                permission_axes=("safe",),
            )
        return None

    monkeypatch.setattr(registry_mod, "tool_spec_v1", fake_tool_spec_v1)

    response = client.get(f"/agents/{expected_agent_id}/extension-registry")

    assert response.status_code == 200
    payload = response.json()
    # ExtensionRegistryV1 projection shape.
    assert "extensions" in payload
    extensions = payload["extensions"]
    assert isinstance(extensions, list)

    by_id = {item["id"]: item for item in extensions}
    # The platform deferred runtime tool groups are projected as tool_pack
    # descriptors served by this route.
    assert "tool_pack:web_pack" in by_id, sorted(by_id)
    web_pack = by_id["tool_pack:web_pack"]
    assert web_pack["type"] == "tool_pack"
    assert "exa_search" in web_pack["exposed_tools"]

    effects = set(web_pack["runtime_effects"])
    # These effects are produced ONLY by the live ToolSpecV1 derivation. If the
    # tool_spec_v1 wiring or the projection consumer is reverted, they vanish.
    assert "capability:web_pack" in effects
    assert "tool:exa_search" in effects
    assert "read_only:exa_search" in effects
    assert "concurrency_safe:exa_search" in effects
    # exa_search permission axis (ToolMeta.governance="safe") flows into the
    # descriptor's permission_requirements via the spec.
    assert "safe" in web_pack["permission_requirements"]

    assert "command:review" in by_id
    assert by_id["command:review"]["type"] == "command"
    assert "hook:post_tool_use" in by_id
    assert by_id["hook:post_tool_use"]["runtime_effects"] == [
        "active_observe",
        "consumer:kernel_post_tool_rewrite_consumer",
    ]
    assert "mcp_server:mcp-docs" in by_id
    assert "mcp_resource:skill://docs/research->skill" in by_id["mcp_server:mcp-docs"]["runtime_effects"]


@pytest.mark.asyncio
async def test_get_agent_extensions_requires_agent_access(monkeypatch):
    expected_agent_id = uuid4()
    client, _fake_db, _current_user = _build_client()

    from fastapi import HTTPException

    async def deny_access(db_session, user, target_agent_id):
        raise HTTPException(status_code=403, detail="no access")

    monkeypatch.setattr(agents_mod, "check_agent_access", deny_access)

    response = client.get(f"/agents/{expected_agent_id}/extension-registry")
    assert response.status_code == 403
