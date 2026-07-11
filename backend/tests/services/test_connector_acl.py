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


def test_connector_acl_requires_metadata_for_governed_sources() -> None:
    from app.services.connector_acl import connector_item_visible

    tenant_id = uuid4()
    user_id = uuid4()

    assert (
        connector_item_visible(
            {"source": "feishu://doc/missing-acl", "content": "legacy hidden by default"},
            tenant_id=tenant_id,
            current_user_id=user_id,
            agent_id=uuid4(),
        )
        is False
    )
    assert (
        connector_item_visible(
            {"source": "internal://legacy-note", "content": "legacy internal memory"},
            tenant_id=tenant_id,
            current_user_id=user_id,
            agent_id=uuid4(),
        )
        is True
    )


def test_post_generation_permission_check_reports_forbidden_sources() -> None:
    from app.services.connector_acl import validate_generated_source_permissions

    tenant_id = uuid4()
    user_id = uuid4()
    allowed_source = "feishu://doc/allowed"
    hidden_source = "feishu://doc/hidden"

    result = validate_generated_source_permissions(
        f"Use [{allowed_source}] but do not quote [{hidden_source}].",
        source_items=[
            {
                "source": allowed_source,
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(user_id)]},
            },
            {
                "source": hidden_source,
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
            },
        ],
        tenant_id=tenant_id,
        current_user_id=user_id,
        agent_id=uuid4(),
    )

    assert result.allowed is False
    assert result.forbidden_sources == [hidden_source]
    assert result.allowed_sources == [allowed_source]


def test_authoritative_connector_result_replaces_argument_deny_acl() -> None:
    from app.services.connector_acl import (
        CONNECTOR_SOURCE_ITEMS_METADATA_KEY,
        register_connector_source_items,
        register_connector_source_payload,
        source_items_from_tool_call,
        validate_generated_source_permissions,
    )

    class _Context:
        metadata: dict = {}

    tenant_id = uuid4()
    user_id = uuid4()
    ctx = _Context()

    register_connector_source_items(
        ctx,
        source_items_from_tool_call("feishu_doc_read", {"document_token": "doc-1"}),
        origin="tool_args:feishu_doc_read",
    )
    assert ctx.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["acl"] == {"deny_by_default": True}

    register_connector_source_payload(
        ctx,
        {
            "source": "feishu://doc/doc-1",
            "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(user_id)]},
            "metadata": {"connector": "feishu", "resource_type": "doc"},
        },
        origin="tool_result:feishu_doc_read",
    )

    source_items = ctx.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY]
    assert len(source_items) == 1
    assert source_items[0]["acl"] == {"tenant_ids": [str(tenant_id)], "user_ids": [str(user_id)]}
    assert source_items[0]["origin"] == "tool_result:feishu_doc_read"

    check = validate_generated_source_permissions(
        "Cite feishu://doc/doc-1",
        source_items=source_items,
        tenant_id=tenant_id,
        current_user_id=user_id,
    )
    assert check.allowed is True
    assert check.allowed_sources == ["feishu://doc/doc-1"]


def test_authoritative_connector_item_accepts_document_user_acl_and_blocks_other_user() -> None:
    from app.services.connector_acl import authoritative_connector_source_item, connector_item_visible

    tenant_id = uuid4()
    allowed_user_id = uuid4()
    blocked_user_id = uuid4()
    agent_id = uuid4()

    item = authoritative_connector_source_item(
        source="feishu://doc/doc-1",
        connector="feishu",
        resource_type="doc",
        tenant_id=tenant_id,
        user_ids=[allowed_user_id],
        agent_id=agent_id,
    )

    assert item["acl"] == {"tenant_ids": [str(tenant_id)], "user_ids": [str(allowed_user_id)]}
    assert item["metadata"]["acl_authority"] == "connector_verified"
    assert connector_item_visible(item, tenant_id=tenant_id, current_user_id=allowed_user_id, agent_id=agent_id) is True
    assert (
        connector_item_visible(item, tenant_id=tenant_id, current_user_id=blocked_user_id, agent_id=agent_id) is False
    )


def test_tool_content_envelope_agent_only_acl_is_not_authoritative_for_connector_source() -> None:
    from app.services.connector_acl import (
        CONNECTOR_SOURCE_ITEMS_METADATA_KEY,
        authoritative_connector_source_item,
        register_connector_source_payload,
        source_items_from_tool_call,
        validate_generated_source_permissions,
    )
    from app.tools.result_envelope import ToolContentEnvelope

    class _Context:
        metadata: dict = {}

    agent_id = uuid4()
    ctx = _Context()

    source_item = authoritative_connector_source_item(
        source="feishu://doc/doc-1",
        connector="feishu",
        resource_type="doc",
        agent_id=agent_id,
        protected_text="visible",
    )
    result = ToolContentEnvelope(
        text="📄 Document content (`doc-1`):\n\nvisible", metadata={CONNECTOR_SOURCE_ITEMS_METADATA_KEY: [source_item]}
    )

    assert register_connector_source_payload(ctx, result, origin="tool:feishu_doc_read") == 1
    assert ctx.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["acl"] == {"deny_by_default": True}
    assert ctx.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["metadata"]["acl_authority"] == "connector_unverified"

    check = validate_generated_source_permissions(
        "Cite feishu://doc/doc-1",
        source_items=ctx.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY],
        tenant_id=uuid4(),
        current_user_id=uuid4(),
        agent_id=agent_id,
    )
    assert check.allowed is False
    assert check.forbidden_sources == ["feishu://doc/doc-1"]

    # The same tool argument source must remain deny-by-default until the
    # successful tool result provides an authoritative payload.
    arg_items = source_items_from_tool_call("feishu_doc_read", {"document_token": "doc-2"})
    assert arg_items[0]["acl"] == {"deny_by_default": True}


def test_post_generation_permission_check_blocks_protected_snippet_without_source_uri() -> None:
    from app.services.connector_acl import authoritative_connector_source_item, validate_generated_source_permissions

    tenant_id = uuid4()
    user_id = uuid4()
    other_user_id = uuid4()
    hidden_source = "feishu://doc/hidden"
    item = authoritative_connector_source_item(
        source=hidden_source,
        connector="feishu",
        resource_type="doc",
        tenant_id=tenant_id,
        user_ids=[other_user_id],
        protected_text="FY26 acquisition codename is Northstar and budget is 42M.",
    )

    check = validate_generated_source_permissions(
        "The FY26 acquisition codename is Northstar.",
        source_items=[item],
        tenant_id=tenant_id,
        current_user_id=user_id,
        agent_id=uuid4(),
    )

    assert check.allowed is False
    assert check.forbidden_sources == [hidden_source]


def test_viking_scheme_without_authoritative_acl_fails_closed() -> None:
    from app.services.connector_acl import connector_item_visible, validate_generated_source_permissions

    source = "viking://enterprise/knowledge_base/policy.md"
    item = {"source": source, "content": "legacy policy without ACL"}

    assert (
        connector_item_visible(
            item,
            tenant_id="tenant-1",
            current_user_id="user-1",
            agent_id="agent-1",
        )
        is False
    )

    check = validate_generated_source_permissions(
        f"Citing {source}",
        source_items=[item],
        tenant_id="tenant-1",
        current_user_id="user-1",
        agent_id="agent-1",
    )
    assert check.allowed is False
    assert check.forbidden_sources == [source]
    assert check.authorization_decision_entry["result"] == "blocked"


@pytest.mark.asyncio
async def test_memory_retriever_does_not_inject_connector_sources(tmp_path) -> None:
    from app.memory.activation import ActivationContext
    from app.memory.retriever import MemoryRetriever
    from app.memory.types import MemoryKind
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
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
    assert external == []
