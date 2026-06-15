from __future__ import annotations

from uuid import uuid4

import pytest


def test_connector_acl_mirror_filters_explicit_acl_without_matching_principal() -> None:
    from app.services.connector_acl import connector_item_visible

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()

    visible = connector_item_visible(
        {
            "source": "feishu://doc/allowed",
            "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(user_id)]},
        },
        tenant_id=tenant_id,
        current_user_id=user_id,
        agent_id=agent_id,
    )
    hidden = connector_item_visible(
        {
            "source": "feishu://doc/hidden",
            "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
        },
        tenant_id=tenant_id,
        current_user_id=user_id,
        agent_id=agent_id,
    )

    assert visible is True
    assert hidden is False


@pytest.mark.asyncio
async def test_knowledge_injection_applies_connector_acl_mirror(monkeypatch) -> None:
    from app.services import knowledge_inject

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()

    monkeypatch.setattr(knowledge_inject.viking_client, "is_configured", lambda: True)

    async def fake_find(*_args, **_kwargs):
        return [
            {
                "source": "feishu://doc/allowed",
                "content": "visible content",
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(user_id)]},
            },
            {
                "source": "feishu://doc/hidden",
                "content": "hidden payroll content",
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
            },
        ]

    monkeypatch.setattr(knowledge_inject.viking_client, "find", fake_find)

    block = await knowledge_inject.fetch_relevant_knowledge(
        "payroll",
        tenant_id=tenant_id,
        agent_id=agent_id,
        current_user_id=user_id,
    )

    assert "visible content" in block
    assert "hidden payroll content" not in block


@pytest.mark.asyncio
async def test_memory_retriever_external_layer_applies_connector_acl_mirror(monkeypatch, tmp_path) -> None:
    from app.memory.activation import ActivationContext
    from app.memory.retriever import MemoryRetriever
    from app.memory.types import MemoryKind
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
    from app.services import viking_client

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()

    monkeypatch.setattr(viking_client, "is_configured", lambda: True)

    captured = {}

    async def fake_find(*_args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "source": "feishu://doc/allowed",
                "content": "visible source content",
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(user_id)]},
            },
            {
                "source": "feishu://doc/hidden",
                "content": "hidden payroll content",
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
            },
        ]

    monkeypatch.setattr(viking_client, "find", fake_find)
    context = ActivationContext(
        query="policy",
        principal_stack=PrincipalStack(
            company=Principal(PrincipalRole.COMPANY, str(tenant_id), "Acme"),
            current_user=Principal(PrincipalRole.CURRENT_USER, str(user_id), "User"),
        ),
    )

    items = await MemoryRetriever(data_root=tmp_path).retrieve(
        agent_id,
        "policy",
        session_id=None,
        tenant_id=str(tenant_id),
        activation_context=context,
    )

    external = [item for item in items if item.kind == MemoryKind.EXTERNAL]
    assert [item.content for item in external] == ["visible source content"]
    assert captured["user_id"] == str(user_id)
    assert captured["agent_id"] == str(agent_id)


@pytest.mark.asyncio
async def test_viking_add_resource_sends_acl_metadata(monkeypatch) -> None:
    from app.services import viking_client

    requests = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class _Client:
        async def post(self, url, *, json, headers):
            requests.append({"url": url, "json": json, "headers": headers})
            return _Response()

    monkeypatch.setattr(viking_client, "_get_client", lambda: _Client())

    result = await viking_client.add_resource(
        content="policy",
        to="viking://enterprise/knowledge_base/policy.md",
        tenant_id="tenant-1",
        agent_id="agent-1",
        user_id="user-1",
        acl={"tenant_ids": ["tenant-1"], "user_ids": ["user-1"]},
        metadata={"source_type": "feishu"},
    )

    assert result == {"ok": True}
    assert requests[0]["json"]["acl"] == {"tenant_ids": ["tenant-1"], "user_ids": ["user-1"]}
    assert requests[0]["json"]["metadata"] == {"source_type": "feishu"}
    assert requests[0]["headers"]["X-OpenViking-User"] == "user-1"
